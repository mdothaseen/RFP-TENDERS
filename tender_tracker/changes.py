"""Meaningful listing diffs and same-URL file revalidation; existing versions stay immutable."""

from __future__ import annotations

import difflib
import hashlib
import logging
import re
from datetime import datetime, timedelta
from pathlib import Path
from uuid import uuid4

from .classify import classify
from .config import ROOT, site_dir
from .documents import _download_url, sniff_kind
from .events import record_item_event, stamp
from .fetcher import Fetcher

log = logging.getLogger(__name__)
_NOISE = re.compile(r"^(?:visitors?|visitor count|page views?|copyright|all rights reserved|last updated|current time)\b", re.I)
_DEADLINE = re.compile(r"last\s*date|deadline|submission|closing|extended|withdrawn|cancelled|corrigendum|निविदा|अंतिम", re.I)


def meaningful_diff(old: str, new: str, keywords) -> str:
    def lines(text):
        return [line for raw in text.splitlines() if (line := re.sub(r"\s+", " ", raw).strip())
                and not _NOISE.search(line)]
    before, after = lines(old), lines(new)
    changes = []
    for tag, i, j, a, b in difflib.SequenceMatcher(a=before, b=after, autojunk=False).get_opcodes():
        if tag == "equal":
            continue
        previous, current = before[i:j], after[a:b]
        context = " ".join(before[max(0, i-2):j+2] + after[max(0, a-2):b+2])
        relevant = classify(keywords, " ".join(previous + current), context).category
        if relevant not in set(keywords.tender_categories) | {"Notice", "Recruitment"} and not _DEADLINE.search(context):
            continue
        changes.extend("- " + line for line in previous[:4])
        changes.extend("+ " + line for line in current[:4])
    return "\n".join(changes)[:1800]


def revalidate_documents(store, settings, keywords, sites: dict, now: datetime | None = None,
                         fetcher_factory=Fetcher) -> dict:
    now = now or datetime.now()
    interval = int(settings.monitoring.get("document_check_minutes", 15))
    cutoff = stamp(now - timedelta(minutes=interval))
    active_since = (now.date() - timedelta(days=int(settings.monitoring.get("active_document_days", 60)))).isoformat()
    candidates = store.items("is_document=1 AND sha256 IS NOT NULL AND removed=0 AND user_status<>'ignore' "
                            "AND (last_checked IS NULL OR last_checked<=?)", (cutoff,))
    rows = [r for r in candidates if (r["content_category"] or r["category"]) in keywords.tender_categories
            and ((r["deadline_hint"] or "") >= now.date().isoformat()
                 or (r["published_date"] or r["uploaded_date"] or r["first_seen"][:10]) >= active_since)]
    rows.sort(key=lambda r: (r["last_checked"] or "", r["id"]))
    rows = rows[:max(0, int(settings.monitoring.get("document_checks_per_cycle", 20)))]
    counts = {"checked": 0, "changed": 0, "failed": 0}
    fetchers = {}
    try:
        for old in rows:
            if old["site_code"] not in sites:
                continue
            code = old["site_code"]
            if code not in fetchers:
                fetchers[code] = fetcher_factory(sites[code], settings)
            fetcher = fetchers[code]
            folder = site_dir(code) / "Documents"
            folder.mkdir(parents=True, exist_ok=True)
            temporary = folder / f".revalidate_{old['id']}_{uuid4().hex}"
            full_due = not old["last_full_check"] or old["last_full_check"] <= stamp(now - timedelta(hours=24))
            missing_file = not old["local_path"] or not (ROOT / old["local_path"]).is_file()
            headers = {}
            if not full_due and not missing_file:
                if old["etag"]:
                    headers["If-None-Match"] = old["etag"]
                if old["last_modified"]:
                    headers["If-Modified-Since"] = old["last_modified"]
            log.info("[%s] checking document version #%d", code, old["id"])
            response, saved = fetcher.download(_download_url(old["url"]), temporary,
                int(settings.documents.get("max_download_mb", 50)) * 1_000_000, headers=headers)
            counts["checked"] += 1
            store.commit()
            store.db.execute("BEGIN IMMEDIATE")
            row = store.item(old["id"])  # Another process may have updated the item while HTTP was in flight.
            if row["last_checked"] and row["last_checked"] > stamp(now):
                if saved is not None:
                    saved.unlink(missing_ok=True)
                store.commit()
                continue
            if response.status == 304:
                store.update_item(row["id"], last_checked=stamp(now), revalidate_error=None, missing_checks=0)
            elif saved is None:
                counts["failed"] += 1
                misses = row["missing_checks"] + 1 if response.status in (404, 410) else 0
                fields = {"last_checked": stamp(now), "revalidate_error": response.error or f"HTTP {response.status}",
                          "missing_checks": misses}
                if misses >= 2:
                    fields["removed"] = 1
                    record_item_event(store, row["id"], "withdrawn", event_key=f"withdrawn:{row['id']}:{row['revision']}")
                store.update_item(row["id"], **fields)
            elif sniff_kind(saved, row["file_ext"] or "") in ("html", "unknown"):
                counts["failed"] += 1
                saved.unlink(missing_ok=True)
                store.update_item(row["id"], last_checked=stamp(now), revalidate_error="Server returned a page instead of the document")
            else:
                digest = hashlib.sha256(saved.read_bytes()).hexdigest()
                store.db.execute("INSERT OR IGNORE INTO document_versions(item_id,revision,sha256,local_path,observed_at) VALUES(?,?,?,?,?)",
                    (row["id"], row["revision"], row["sha256"], row["local_path"], stamp(now)))
                fields = {"last_checked": stamp(now), "last_full_check": stamp(now), "missing_checks": 0,
                          "revalidate_error": None, "etag": response.headers.get("etag"),
                          "last_modified": response.headers.get("last-modified")}
                if digest != row["sha256"] or missing_file:
                    suffix = "." + (row["file_ext"] or "pdf")
                    destination = folder / f"revision_{row['id']}_{digest[:12]}_{uuid4().hex[:8]}{suffix}"
                    saved.replace(destination)
                    revision = row["revision"] + int(digest != row["sha256"])
                    local_path = destination.resolve().relative_to(ROOT.resolve()).as_posix()
                    fields.update(local_path=local_path, sha256=digest, doc_status=None, doc_attempts=0,
                                  doc_error=None, text_path=None, text_chars=None, scanned=None, doc_pages=None,
                                  doc_size=destination.stat().st_size, revision=revision)
                    if digest != row["sha256"]:
                        fields.update(ai_status="pending", ai_attempts=0, ai_json=None, ai_error=None,
                                      ai_at=None, ai_partial=0)
                        # A deadline extracted from the old revision must not survive a new document.
                        fields["deadline_hint"] = None
                        fields["deadline_override"] = None
                    store.update_item(row["id"], **fields)
                    store.db.execute("INSERT OR REPLACE INTO document_versions(item_id,revision,sha256,local_path,observed_at) VALUES(?,?,?,?,?)",
                        (row["id"], revision, digest, local_path, stamp(now)))
                    if digest != row["sha256"]:
                        record_item_event(store, row["id"], "document_changed", {"previous_sha256": row["sha256"]})
                        counts["changed"] += 1
                else:
                    saved.unlink(missing_ok=True)
                    store.update_item(row["id"], **fields)
            store.commit()
    finally:
        if store.db.in_transaction:
            store.db.rollback()
        for fetcher in fetchers.values():
            fetcher.session.close()
    return counts
