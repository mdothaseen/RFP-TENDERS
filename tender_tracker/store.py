"""SQLite storage for items, runs and per-site state. Used from the main thread only."""

from __future__ import annotations

import json
import sqlite3
from uuid import uuid4
from contextlib import closing
from datetime import datetime
from pathlib import Path

from .classify import Classification
from .discover.crawler import Found, SiteScan

UNLINKED_VIA = {"sitemap", "wp-media", "wp-post", "wp-search"}
# How exact each source of the upload date is.
UPLOAD_PRECISION = {
    "website media library": 4, "file name time stamp": 4, "server file date": 3, "upload folder (month only)": 1,
    "": 0,
}
# How exact each kind of date is; a more exact date replaces a rougher one.
DATE_PRECISION = {
    "upload date": 4, "post date": 4, "listing text": 3, "file name": 3, "server date": 2, "sitemap lastmod": 2,
    "upload folder": 1, "": 0,
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
    id INTEGER PRIMARY KEY,
    key TEXT UNIQUE NOT NULL,
    site_code TEXT NOT NULL,
    url TEXT,
    title TEXT,
    link_text TEXT,
    context TEXT,
    source_page TEXT,
    found_via TEXT,
    linked_seen INTEGER DEFAULT 0,
    hidden INTEGER DEFAULT 0,
    is_document INTEGER DEFAULT 0,
    file_ext TEXT,
    category TEXT,
    keywords TEXT,
    relevance TEXT,
    published_date TEXT,
    date_source TEXT,
    deadline_hint TEXT,
    wp_id INTEGER,
    first_seen TEXT NOT NULL,
    last_seen TEXT NOT NULL,
    first_run_id INTEGER,
    baseline INTEGER DEFAULT 0,
    removed INTEGER DEFAULT 0,
    notified INTEGER DEFAULT 0,
    doc_status TEXT,
    doc_attempts INTEGER DEFAULT 0,
    doc_error TEXT,
    local_path TEXT,
    text_path TEXT,
    sha256 TEXT,
    doc_size INTEGER,
    doc_pages INTEGER,
    text_chars INTEGER,
    scanned INTEGER,
    content_category TEXT,
    ai_status TEXT,
    ai_attempts INTEGER DEFAULT 0,
    ai_json TEXT,
    ai_model TEXT,
    ai_at TEXT,
    ai_error TEXT,
    ai_partial INTEGER DEFAULT 0,
    xl_master_hash TEXT,
    xl_site_hash TEXT,
    uploaded_date TEXT,
    uploaded_source TEXT
);
CREATE INDEX IF NOT EXISTS idx_items_site ON items(site_code);
CREATE INDEX IF NOT EXISTS idx_items_run ON items(first_run_id);
CREATE INDEX IF NOT EXISTS idx_items_sha ON items(sha256);

CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY,
    started TEXT, finished TEXT, mode TEXT, baseline INTEGER, deep INTEGER,
    status TEXT, new_items INTEGER, notes TEXT
);
CREATE TABLE IF NOT EXISTS site_runs (
    id INTEGER PRIMARY KEY,
    run_id INTEGER, site_code TEXT, started TEXT, finished TEXT, status TEXT, method TEXT,
    pages INTEGER, items INTEGER, new_items INTEGER, documents INTEGER, wp INTEGER, wp_media INTEGER,
    sitemap_urls INTEGER, robots_skipped INTEGER, errors TEXT, error_count INTEGER, ssl_fallback INTEGER
);
CREATE INDEX IF NOT EXISTS idx_site_runs_site ON site_runs(site_code, id);
CREATE TABLE IF NOT EXISTS page_texts (
    key TEXT PRIMARY KEY, site_code TEXT, text TEXT, updated TEXT
);
CREATE TABLE IF NOT EXISTS ai_usage (
    id INTEGER PRIMARY KEY, run_id INTEGER, item_id INTEGER, model TEXT,
    input_tokens INTEGER, output_tokens INTEGER, cost_usd REAL, at TEXT
);
CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE IF NOT EXISTS change_events (
    id INTEGER PRIMARY KEY, event_key TEXT UNIQUE NOT NULL, item_id INTEGER,
    kind TEXT NOT NULL, created TEXT NOT NULL, payload TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_item ON change_events(item_id, id);
CREATE TABLE IF NOT EXISTS notification_outbox (
    id INTEGER PRIMARY KEY, event_id INTEGER NOT NULL, channel TEXT NOT NULL, recipient TEXT NOT NULL,
    payload TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0,
    next_attempt TEXT NOT NULL, lease_until TEXT, accepted_at TEXT, last_error TEXT,
    UNIQUE(event_id, channel, recipient)
);
CREATE INDEX IF NOT EXISTS idx_outbox_due ON notification_outbox(status, next_attempt);
CREATE TABLE IF NOT EXISTS document_versions (
    id INTEGER PRIMARY KEY, item_id INTEGER NOT NULL, revision INTEGER NOT NULL,
    sha256 TEXT NOT NULL, local_path TEXT, observed_at TEXT NOT NULL,
    UNIQUE(item_id, revision)
);
"""


def _iso(d) -> str | None:
    return d.isoformat() if d else None


class Store:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(path), timeout=300)  # another job (Excel rebuild, enrich) may hold the DB for minutes
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        old_columns = {r["name"] for r in self.db.execute("PRAGMA table_info(items)")}
        if old_columns and "revision" not in old_columns and self.db.execute("SELECT 1 FROM items LIMIT 1").fetchone():
            # Preserve a consistent copy before migrating an existing populated tracker.
            snapshot = path.with_name(f"{path.stem}_pre_monitor_{datetime.now():%Y%m%d_%H%M%S}_{uuid4().hex[:8]}.db")
            with closing(sqlite3.connect(str(snapshot))) as target:
                self.db.backup(target)
        self.db.executescript(SCHEMA)
        self._migrate()
        self.db.commit()

    def _migrate(self) -> None:
        """Add columns introduced after a database was created."""
        self.db.execute("BEGIN IMMEDIATE")
        have = {r["name"] for r in self.db.execute("PRAGMA table_info(items)")}
        for column in ("xl_master_hash", "xl_site_hash", "uploaded_date", "uploaded_source"):
            if column not in have:
                self.db.execute(f"ALTER TABLE items ADD COLUMN {column} TEXT")
        for column, kind in {
            "etag": "TEXT", "last_modified": "TEXT", "last_checked": "TEXT", "last_full_check": "TEXT",
            "revalidate_error": "TEXT", "revision": "INTEGER NOT NULL DEFAULT 0",
            "missing_checks": "INTEGER NOT NULL DEFAULT 0", "user_status": "TEXT NOT NULL DEFAULT 'new'",
            "related_item_id": "INTEGER",
            "deadline_override": "TEXT",
            "duplicate_of": "INTEGER",  # a later copy of the same file (see duplicates.py)
        }.items():
            if column not in have:
                self.db.execute(f"ALTER TABLE items ADD COLUMN {column} {kind}")
        if "uploaded_date" not in have:
            # Media-library dates are upload times.
            self.db.execute("UPDATE items SET uploaded_date=published_date, uploaded_source='website media library' "
                            "WHERE date_source='upload date'")
        page_columns = {r["name"] for r in self.db.execute("PRAGMA table_info(page_texts)")}
        if "revision" not in page_columns:
            self.db.execute("ALTER TABLE page_texts ADD COLUMN revision INTEGER NOT NULL DEFAULT 0")

    def close(self) -> None:
        self.db.commit()
        self.db.close()

    # ------------------------------------------------------------------ kv / runs
    def kv_get(self, key: str, default: str | None = None) -> str | None:
        row = self.db.execute("SELECT v FROM kv WHERE k=?", (key,)).fetchone()
        return row["v"] if row else default

    def kv_set(self, key: str, value: str) -> None:
        self.db.execute("INSERT INTO kv(k, v) VALUES(?, ?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", (key, value))
        self.db.commit()

    def start_run(self, mode: str, baseline: bool, deep: bool) -> int:
        cur = self.db.execute(
            "INSERT INTO runs(started, mode, baseline, deep, status) VALUES(?, ?, ?, ?, 'running')",
            (datetime.now().isoformat(timespec="seconds"), mode, int(baseline), int(deep)),
        )
        self.db.commit()
        return cur.lastrowid

    def finish_run(self, run_id: int, status: str, new_items: int, notes: str = "") -> None:
        self.db.execute(
            "UPDATE runs SET finished=?, status=?, new_items=?, notes=? WHERE id=?",
            (datetime.now().isoformat(timespec="seconds"), status, new_items, notes, run_id),
        )
        self.db.commit()

    def run(self, run_id: int) -> sqlite3.Row:
        return self.db.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()

    def has_items(self) -> bool:
        return self.db.execute("SELECT 1 FROM items LIMIT 1").fetchone() is not None

    def claimed_files(self) -> set[str]:
        """Downloaded files that belong to an item (output/... paths)."""
        return {r["local_path"] for r in self.db.execute("SELECT local_path FROM items WHERE local_path IS NOT NULL")}

    def site_codes_with_items(self) -> set[str]:
        return {r["site_code"] for r in self.db.execute("SELECT DISTINCT site_code FROM items")}

    # ------------------------------------------------------------------ scan state
    def known_wp_ids(self, site_code: str) -> set[int]:
        rows = self.db.execute("SELECT wp_id FROM items WHERE site_code=? AND wp_id IS NOT NULL", (site_code,))
        return {r["wp_id"] for r in rows}

    def page_texts(self, site_code: str) -> dict[str, str]:
        rows = self.db.execute("SELECT key, text FROM page_texts WHERE site_code=?", (site_code,))
        return {r["key"]: r["text"] for r in rows}

    def save_page_text(self, key: str, site_code: str, text: str, observed: str | None = None) -> None:
        self.db.execute(
            "INSERT INTO page_texts(key, site_code, text, updated) VALUES(?, ?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET text=excluded.text, updated=excluded.updated, "
            "revision=page_texts.revision + (page_texts.text<>excluded.text)",
            (key, site_code, text[:200_000], observed or datetime.now().isoformat(timespec="microseconds")),
        )

    # ------------------------------------------------------------------ items
    def upsert(self, found: Found, cls: Classification, run_id: int, baseline: bool, now: str,
               ai_categories: set[str]) -> tuple[int, bool]:
        row = self.db.execute("SELECT * FROM items WHERE key=?", (found.key,)).fetchone()
        via = set(found.via)
        if row is None:
            hidden = int(not found.linked and via <= UNLINKED_VIA)
            cur = self.db.execute(
                """INSERT INTO items(key, site_code, url, title, link_text, context, source_page, found_via,
                   linked_seen, hidden, is_document, file_ext, category, keywords, relevance, published_date,
                   date_source, deadline_hint, wp_id, first_seen, last_seen, first_run_id, baseline, ai_status,
                   uploaded_date, uploaded_source, local_path, sha256, doc_size)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (found.key, found.site_code, found.url, found.title, found.link_text, found.context,
                 found.source_page, ",".join(sorted(via)), int(found.linked), hidden, int(found.is_document),
                 found.file_ext, cls.category, ", ".join(cls.matched[:5]), cls.relevance, _iso(found.published),
                 found.date_source, _iso(found.deadline), found.wp_id, now, now, run_id, int(baseline),
                 "pending" if cls.category in ai_categories else "skipped",
                 _iso(found.uploaded), found.uploaded_source or None,
                 found.local_path, found.sha256, found.doc_size),
            )
            return cur.lastrowid, True

        old_via = set((row["found_via"] or "").split(",")) - {""}
        merged_via = old_via | via
        linked = int(bool(row["linked_seen"]) or found.linked)
        hidden = int(not linked and merged_via <= UNLINKED_VIA)
        title = row["title"]
        if found.linked and found.title:
            title = found.title
        category = row["category"]
        if cls.category in ai_categories and category not in ai_categories:
            category = cls.category
        ai_status = row["ai_status"]
        if category in ai_categories and ai_status == "skipped":
            ai_status = "pending"
        source_page = row["source_page"]
        if found.linked and not row["linked_seen"]:
            source_page = found.source_page
        published, date_source = row["published_date"], row["date_source"] or ""
        if found.published and DATE_PRECISION.get(found.date_source, 0) > DATE_PRECISION.get(date_source, 0):
            published, date_source = _iso(found.published), found.date_source
        uploaded, uploaded_source = row["uploaded_date"], row["uploaded_source"] or ""
        if found.uploaded and UPLOAD_PRECISION.get(found.uploaded_source, 0) > UPLOAD_PRECISION.get(uploaded_source, 0):
            uploaded, uploaded_source = _iso(found.uploaded), found.uploaded_source
        url = row["url"]
        if "wp-media" in via and not row["wp_id"] and found.url:
            url = found.url  # the media library holds the real file address
        self.db.execute(
            """UPDATE items SET last_seen=?, found_via=?, linked_seen=?, hidden=?, title=?, category=?,
               relevance=CASE WHEN ?='High' THEN 'High' ELSE relevance END, removed=0, source_page=?,
               published_date=?, date_source=?, deadline_hint=COALESCE(?, deadline_hint),
               wp_id=COALESCE(wp_id, ?),
               context=CASE WHEN ? THEN ? ELSE context END,
               ai_status=?, url=?, uploaded_date=?, uploaded_source=?
               WHERE id=?""",
            (now, ",".join(sorted(merged_via)), linked, hidden, title, category, cls.relevance, source_page,
             published, date_source, _iso(found.deadline), found.wp_id, int(found.linked or not row["context"]), found.context,
             ai_status, url, uploaded, uploaded_source or None, row["id"]),
        )
        if found.local_path and row["local_path"] != found.local_path:
            # A portal document downloaded during this scan: hand it to the documents step for reading.
            self.db.execute(
                "UPDATE items SET local_path=?, sha256=?, doc_size=?, file_ext=?, is_document=1, "
                "doc_status=NULL, doc_attempts=0, doc_error=NULL WHERE id=?",
                (found.local_path, found.sha256, found.doc_size, found.file_ext or "zip", row["id"]),
            )
        previous_deadline = row["deadline_hint"]
        if not previous_deadline:
            try:
                previous_deadline = json.loads(row["ai_json"] or "{}").get("last_date_submission")
            except (ValueError, AttributeError):
                pass
        if found.deadline and previous_deadline and _iso(found.deadline) != previous_deadline:
            from .events import record_item_event
            self.update_item(row["id"], deadline_override=_iso(found.deadline))
            record_item_event(self, row["id"], "deadline_changed", {
                "previous_deadline": previous_deadline, "deadline": _iso(found.deadline),
            }, event_key=f"deadline:{row['id']}:{previous_deadline}:{_iso(found.deadline)}")
        return row["id"], False

    def mark_removed(self, site_codes: list[str], cutoff_iso: str) -> None:
        if not site_codes:
            return
        marks = ",".join("?" * len(site_codes))
        self.db.execute(
            f"UPDATE items SET removed=1 WHERE site_code IN ({marks}) AND last_seen < ? AND removed=0",
            (*site_codes, cutoff_iso),
        )

    def record_site_run(self, run_id: int, scan: SiteScan, new_items: int) -> None:
        self.db.execute(
            """INSERT INTO site_runs(run_id, site_code, started, finished, status, method, pages, items, new_items,
               documents, wp, wp_media, sitemap_urls, robots_skipped, errors, error_count, ssl_fallback)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (run_id, scan.site.code, scan.started.isoformat(timespec="seconds"),
             scan.finished.isoformat(timespec="seconds") if scan.finished else None, scan.status, scan.method,
             scan.pages, len(scan.items), new_items, sum(1 for f in scan.items.values() if f.is_document),
             int(scan.wp), scan.wp_media, scan.sitemap_urls, scan.robots_skipped,
             json.dumps(scan.errors, ensure_ascii=False), scan.error_count, int(scan.ssl_fallback)),
        )

    def commit(self) -> None:
        self.db.commit()

    def update_item(self, item_id: int, **fields) -> None:
        if not fields:
            return
        cols = ", ".join(f"{k}=?" for k in fields)
        self.db.execute(f"UPDATE items SET {cols} WHERE id=?", (*fields.values(), item_id))

    def update_item_revision(self, item_id: int, expected_revision: int, **fields) -> bool:
        cols = ", ".join(f"{k}=?" for k in fields)
        cursor = self.db.execute(f"UPDATE items SET {cols} WHERE id=? AND revision=?",
                                 (*fields.values(), item_id, expected_revision))
        return cursor.rowcount == 1

    def item(self, item_id: int) -> sqlite3.Row:
        return self.db.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()

    def items(self, where: str = "1=1", params: tuple = ()) -> list[sqlite3.Row]:
        return list(self.db.execute(f"SELECT * FROM items WHERE {where}", params))

    def find_by_sha(self, sha256: str, exclude_id: int) -> sqlite3.Row | None:
        return self.db.execute(
            "SELECT * FROM items WHERE sha256=? AND id<>? AND ai_status='done' LIMIT 1", (sha256, exclude_id),
        ).fetchone()

    def mark_notified(self, ids: list[int]) -> None:
        self.db.executemany("UPDATE items SET notified=1 WHERE id=?", [(i,) for i in ids])
        self.db.commit()

    # ------------------------------------------------------------------ AI usage
    def log_ai_usage(self, run_id: int, item_id: int, model: str, input_tokens: int, output_tokens: int,
                     cost: float | None) -> None:
        self.db.execute(
            "INSERT INTO ai_usage(run_id, item_id, model, input_tokens, output_tokens, cost_usd, at) "
            "VALUES(?,?,?,?,?,?,?)",
            (run_id, item_id, model, input_tokens, output_tokens, cost, datetime.now().isoformat(timespec="seconds")),
        )

    def ai_cost(self, since_iso: str | None = None, run_id: int | None = None) -> tuple[int, float]:
        if run_id is not None:
            row = self.db.execute(
                "SELECT COUNT(*) n, COALESCE(SUM(cost_usd),0) c FROM ai_usage WHERE run_id=?", (run_id,)).fetchone()
        else:
            row = self.db.execute(
                "SELECT COUNT(*) n, COALESCE(SUM(cost_usd),0) c FROM ai_usage WHERE at >= ?",
                (since_iso or "0000",)).fetchone()
        return row["n"], row["c"]

    # ------------------------------------------------------------------ site status
    def site_history(self, site_code: str, limit: int = 10) -> list[sqlite3.Row]:
        return list(self.db.execute(
            "SELECT * FROM site_runs WHERE site_code=? ORDER BY id DESC LIMIT ?", (site_code, limit)))

    def last_success(self, site_code: str) -> str | None:
        row = self.db.execute(
            "SELECT finished FROM site_runs WHERE site_code=? AND status='ok' ORDER BY id DESC LIMIT 1",
            (site_code,)).fetchone()
        return row["finished"] if row else None

    def consecutive_failures(self, site_code: str) -> int:
        count = 0
        for row in self.site_history(site_code, 30):
            if row["status"] == "failed":
                count += 1
            else:
                break
        return count
