"""One-off / scheduled drain of the OLD document backlog, separate from the regular new-only scan.

The regular scan (documents.baseline_days) only queues recent documents. This worker clears what is
left over from before that window:
  * retire pass  - old closed / low-relevance / non-tender items are marked skipped so they leave the
                   queue for good (no download, no OCR, no AI: they are not worth reading);
  * drain pass   - the remaining old-but-live, relevant tenders are downloaded, OCR'd and read by the
                   configured AI (Claude / OpenRouter / local Ollama) in bounded batches.
Run it on a schedule (`python -m tender_tracker --drain-backlog`) until the backlog is empty; after
that it is a no-op and the regular scan keeps working on new documents only.
"""

from __future__ import annotations

import logging
from datetime import date, timedelta

from . import enrich
from .ai_extract import STALE
from .store import Store

log = logging.getLogger("tender_tracker")


def retire_old_junk(store: Store, settings, keywords, today: date) -> int:
    """Mark old closed / low-relevance / non-tender documents skipped so they stop filling the queue."""
    days = int(settings.documents.get("baseline_days", 30))
    window = (today - timedelta(days=days)).isoformat()
    worth = set(keywords.tender_categories) | {"Notice", "Recruitment"} | set(settings.ai.get("categories") or [])
    rows = store.items("removed=0 AND url IS NOT NULL AND "
                       "(doc_status IS NULL OR (doc_status='error' AND doc_attempts < 3))")
    retired = 0
    for row in rows:
        published = row["published_date"]
        category = row["content_category"] or row["category"]
        old_dated = bool(published and published < window)
        closed = bool(row["deadline_hint"] and row["deadline_hint"] < today.isoformat())
        junk = category not in worth
        undated_junk = not published and junk
        if not (closed or undated_junk or (old_dated and (junk or row["relevance"] == "Low"))):
            continue  # recent, or old-but-live and relevant: the drain pass reads those
        reason = ("closed" if closed else "undated non-tender" if undated_junk
                  else f"older than {days}d and not relevant")
        store.update_item(row["id"], doc_status="skipped", doc_error=f"backlog retired: {reason}",
                          ai_status="skipped", ai_error=STALE + f"backlog retired: {reason}")
        retired += 1
    store.commit()
    if retired:
        log.info("Backlog: %d old closed/low/non-tender documents retired (skipped)", retired)
    return retired


def drain(store: Store, settings, keywords, sites: dict, args, today: date, run_id: int) -> dict:
    """Retire the junk, then download + OCR + AI-read the old-but-live relevant documents."""
    retired = retire_old_junk(store, settings, keywords, today)
    window = today - timedelta(days=int(settings.documents.get("baseline_days", 30)))
    workers = args.workers or int(settings.monitoring.get("document_workers", 2))
    processed = enrich.process_documents(store, settings, keywords, sites, today, workers,
                                         window_days=365, before=window)
    ai_summary = None if args.no_ai else enrich.read_with_claude_plan(store, settings, sites, today)
    return {"retired": retired, "documents_ready": processed, "ai": ai_summary}
