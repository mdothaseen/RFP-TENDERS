"""The document details Claude fills in, which documents it reads, and the queue/answer files it works on.

Claude Code reads the queue with the user's Claude plan (see claude_reader.py); the tracker never calls the
Claude API itself.
"""

from __future__ import annotations

import json
import logging
from datetime import date, datetime, timedelta
from pathlib import Path

from pydantic import BaseModel, ValidationError

from .config import ROOT, Settings
from .dates import parse_iso

log = logging.getLogger(__name__)

DOC_TYPES = ["Tender", "RFP", "RFQ", "EOI", "Empanelment", "Corrigendum", "Notice", "Circular",
             "Recruitment", "Result", "Other"]
QUEUE_FILE = ROOT / "data" / "ai_queue.json"
RESULTS_FILE = ROOT / "data" / "ai_results.json"
STALE = "stale: "  # ai_error prefix of documents that are too old to be worth reading

_FIELDS = {
    "doc_type": "What kind of document this is.",
    "title": "Clean, complete title of the document.",
    "issuing_organization": "Organisation that issued it.",
    "reference_no": "Reference / tender / file number, if printed.",
    "issue_date": "Date of issue, YYYY-MM-DD.",
    "last_date_submission": "Final date to submit the bid / proposal / application, YYYY-MM-DD. For a corrigendum that extends it, the new date.",
    "last_time_submission": "Time on the last date, HH:MM 24-hour.",
    "prebid_date": "Pre-bid meeting or last date for queries, YYYY-MM-DD.",
    "bid_opening_date": "Technical bid opening date, YYYY-MM-DD.",
    "other_key_dates": "Any other important dates, short.",
    "emd": "Earnest money deposit / bid security, as written (with currency), or 'Not required'.",
    "tender_fee": "Tender / processing / application fee, as written.",
    "estimated_value": "Estimated contract value or budget, as written.",
    "contract_period": "Duration of the assignment / empanelment.",
    "who_can_apply": "Type of organisation or person that can apply.",
    "eligibility_summary": "Key eligibility criteria as short points separated by '; ' (turnover, experience, registrations, certifications).",
    "scope_summary": "2-3 plain sentences: what work is asked for, where, how much / how many.",
    "location": "States / districts / places covered.",
    "submission_mode": "How and where to submit (email address, portal, GeM, physical address).",
    "contact": "Contact person, email and phone.",
    "corrigendum_of": "If this is a corrigendum/addendum, the title or reference of the original document.",
    "relevance": "High / Medium / Low relevance for the company profile.",
    "relevance_reason": "One short sentence explaining the relevance.",
    "action_needed": "One line: what the company should do and by when.",
}


class TenderInfo(BaseModel):
    doc_type: str
    title: str
    issuing_organization: str
    reference_no: str
    issue_date: str
    last_date_submission: str
    last_time_submission: str
    prebid_date: str
    bid_opening_date: str
    other_key_dates: str
    emd: str
    tender_fee: str
    estimated_value: str
    contract_period: str
    who_can_apply: str
    eligibility_summary: str
    scope_summary: str
    location: str
    submission_mode: str
    contact: str
    corrigendum_of: str
    relevance: str
    relevance_reason: str
    action_needed: str


def retire_stale(store, settings: Settings, today: date) -> int:
    """Stop waiting on tenders that can no longer be applied for, so the reader works on live ones.

    A document is retired when its last date has passed, or (without a future last date) when it is older than
    ai.recent_days, or when it came from a website's first scan without any date. Returns how many were retired.
    """
    categories = tuple(settings.ai.get("categories") or [])
    if not categories:
        return 0
    days = int(settings.ai.get("recent_days", 120))
    since = (today - timedelta(days=days)).isoformat()
    now = today.isoformat()
    pending = ("removed=0 AND ai_status IN ('pending','failed') "
               f"AND COALESCE(content_category, category) IN ({','.join('?' * len(categories))})")
    no_future_deadline = ("(deadline_hint IS NULL OR deadline_hint < ?) "
                          "AND (deadline_override IS NULL OR deadline_override < ?)")
    rules = [
        ("last date has passed", "deadline_hint < ? AND (deadline_override IS NULL OR deadline_override < ?)",
         (now, now)),
        (f"published more than {days} days ago",
         f"{no_future_deadline} AND COALESCE(uploaded_date, published_date) < ?", (now, now, since)),
        ("from the website's first scan, without any date",
         f"{no_future_deadline} AND baseline=1 AND published_date IS NULL AND uploaded_date IS NULL", (now, now)),
    ]
    retired = 0
    for reason, condition, args in rules:
        retired += store.db.execute(
            f"UPDATE items SET ai_status='skipped', ai_error=? WHERE {pending} AND {condition}",
            (STALE + reason, *categories, *args)).rowcount
    store.commit()
    if retired:
        log.info("AI backlog: %d old documents will not be read (last date passed or too old)", retired)
    return retired


def _mentions_our_work(row, chars: int, patterns: list) -> bool:
    """True when the title, link text or the first `chars` of the document (about 2 pages) mention
    training or assessment work (keywords.yaml relevance_high + relevance_medium)."""
    text = " ".join(str(row[k] or "") for k in ("title", "link_text", "context"))
    if row["text_path"]:
        try:
            with open(ROOT / row["text_path"], encoding="utf-8", errors="replace") as fh:
                text += " " + fh.read(chars)
        except OSError:
            return True  # cannot check: let the model read it
    elif row["scanned"]:
        return True      # scanned PDF without text: nothing to check here
    return any(p.search(text) for p in patterns)


def select_items(store, settings: Settings, baseline: bool, today: date) -> list:
    categories = set(settings.ai.get("categories") or [])
    max_attempts = int(settings.ai.get("max_attempts", 3))
    since_baseline = (today - timedelta(days=int(settings.ai.get("baseline_days", 45)))).isoformat()
    since_recent = (today - timedelta(days=int(settings.ai.get("recent_days", 120)))).isoformat()
    rows = store.items("removed=0 AND ai_status IN ('pending','failed') AND ai_attempts < ?", (max_attempts,))
    read_from = str(settings.ai.get("read_from") or "")
    # Slow local models (Ollama): only documents whose title or first pages mention our work are read.
    prefilter = int(settings.ai.get("prefilter_chars") or 0)
    patterns = []
    if prefilter:
        from .config import load_keywords
        kw = load_keywords()
        patterns = list(kw.relevance_high) + list(kw.relevance_medium)
    skipped = 0
    chosen = []
    for row in rows:
        if read_from and (row["first_seen"] or "")[:10] < read_from:
            store.update_item(row["id"], ai_status="skipped",
                              ai_error=f"found before {read_from} - not read by AI")
            continue
        category = row["content_category"] or row["category"]
        if category not in categories:
            continue
        if row["doc_status"] not in ("ok", "html"):
            continue
        published = row["published_date"]
        deadline = row["deadline_hint"]
        # Items from a site's first scan (baseline) are read only when clearly recent.
        row_baseline = baseline or bool(row["baseline"])
        since = since_baseline if row_baseline else since_recent
        fresh = (published and published >= since) or (deadline and deadline >= today.isoformat()) \
            or (not published and row["linked_seen"] and not row["baseline"])
        if not fresh:
            continue
        if not row["text_path"] and not row["scanned"]:
            store.update_item(row["id"], ai_status="skipped", ai_error="document text could not be read")
            continue
        if prefilter and not _mentions_our_work(row, prefilter, patterns):
            store.update_item(row["id"], ai_status="skipped", relevance="Low",
                              ai_error=f"prefilter: no training/assessment words in the first ~2 pages")
            skipped += 1
            continue
        chosen.append(row)
    if skipped:
        log.info("AI pre-filter: %d documents skipped (no training/assessment words in the first ~2 pages)", skipped)
    rank = {"High": 0, "Medium": 1, "Low": 2}
    chosen.sort(key=lambda r: (rank.get(r["relevance"], 3), -(parse_iso(r["published_date"]) or date.min).toordinal()))
    return chosen[: int(settings.ai.get("max_docs_per_run", 40))]


def ai_fields(row) -> dict:
    """The AI answer of an item as a dict (empty when not read yet)."""
    if not row["ai_json"]:
        return {}
    try:
        return json.loads(row["ai_json"])
    except ValueError:
        return {}


def past_decisions(store, sites: dict, per_kind: int = 15) -> dict[str, list[str]]:
    """The user's latest dashboard choices, so the reader learns what the company wants."""
    def titles(statuses: str) -> list[str]:
        rows = store.db.execute(f"SELECT site_code, title FROM items WHERE user_status IN ({statuses}) "
                                "AND title IS NOT NULL ORDER BY id DESC LIMIT ?", (per_kind,)).fetchall()
        return [f"{sites[r['site_code']].name if r['site_code'] in sites else r['site_code']}: {r['title'][:200]}"
                for r in rows]
    return {"wanted": titles("'interested','applied'"), "not_wanted": titles("'ignore'")}


def export_queue(store, settings: Settings, sites: dict, today: date) -> int:
    """Write data/ai_queue.json: the documents the Claude app should read, with instructions and fields."""
    retire_stale(store, settings, today)
    rows = select_items(store, settings, False, today)
    store.commit()
    items = []
    for row in rows:
        site = sites.get(row["site_code"])
        has_text = bool(row["text_path"]) and (row["text_chars"] or 0) >= 500
        scanned_pdf = row["scanned"] and not has_text and row["local_path"] and row["local_path"].lower().endswith(".pdf")
        items.append({
            "id": row["id"],
            "revision": row["revision"],
            "organisation": site.name if site else row["site_code"],
            "title_on_website": row["title"] or "",
            "text_next_to_link": (row["context"] or "")[:500],
            "link": row["url"] or "",
            "date_on_website": row["published_date"] or "",
            "read_this_file": str(ROOT / (row["local_path"] if scanned_pdf else row["text_path"])),
            "file_is_scanned_pdf": bool(scanned_pdf),
        })
    QUEUE_FILE.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "created": datetime.now().isoformat(timespec="seconds"),
        "company_profile": settings.company_profile,
        "past_decisions": past_decisions(store, sites),
        "instructions": [
            "Read each file in read_this_file and fill every field listed in 'fields' for that item.",
            "Use only what the document says; unknown fields = empty string.",
            "Dates YYYY-MM-DD, times HH:MM (24h). For a corrigendum that changes a date, give the new date.",
            "last_date_submission = final date to submit the bid/proposal/application (not pre-bid/opening).",
            "doc_type must be one of: " + ", ".join(DOC_TYPES) + ". relevance must be High, Medium or Low "
            "(judged against company_profile).",
            f"Write a JSON list of objects {{\"id\": <id>, \"revision\": <revision from queue>, <all fields>}} to {RESULTS_FILE}.",
        ],
        "fields": _FIELDS,
        "items": items,
    }
    QUEUE_FILE.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return len(items)


def import_results(store, path: Path, expected: dict[int, int] | None = None,
                   model: str = "claude-app") -> tuple[int, list[str]]:
    """Store answers written by the Claude app. Returns (saved, problems)."""
    data = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(data, dict):
        data = data.get("items") or data.get("results") or []
    saved, problems = 0, []
    now = datetime.now().isoformat(timespec="seconds")
    for entry in data:
        try:
            item_id = int(entry.get("id"))
            values = {k: str(entry.get(k) or "") for k in _FIELDS}
            if values["doc_type"] not in DOC_TYPES:
                values["doc_type"] = "Other"
            if values["relevance"] not in ("High", "Medium", "Low"):
                values["relevance"] = "Medium"
            info = TenderInfo.model_validate(values)
        except (TypeError, ValueError, ValidationError) as exc:
            problems.append(f"{entry.get('id')}: {str(exc)[:100]}")
            continue
        if store.item(item_id) is None:
            problems.append(f"{item_id}: unknown id")
            continue
        revision = expected.get(item_id) if expected is not None else entry.get("revision", 0)
        try:
            revision = int(revision) if revision is not None else None
        except (TypeError, ValueError):
            revision = None
        if revision is None or not store.update_item_revision(item_id, revision, ai_status="done", ai_json=info.model_dump_json(),
                                                             ai_model=model, ai_at=now, ai_error=None):
            problems.append(f"{item_id}: document changed while being read; stale AI answer discarded")
            continue
        saved += 1
    store.commit()
    return saved, problems
