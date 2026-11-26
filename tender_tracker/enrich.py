"""After a scan: download and OCR the documents, then let Claude Code read the live tenders (Claude plan)."""

from __future__ import annotations

import json
import logging
from datetime import date

from . import ai_extract, documents, events, locking
from .config import DATA_DIR
from .duplicates import mark_duplicates
from .store import Store

log = logging.getLogger("tender_tracker")


def process_documents(store: Store, settings, keywords, sites: dict, today: date, workers: int,
                      deep: bool = False) -> int:
    batch = documents.plan_documents(store, settings, keywords, today, deep=deep)
    rows = batch.rows
    pending = batch.priority_pending + batch.unknown_pending
    log.info("Document queue: %d pending (%d priority, %d unknown); processing %d, deferred %d",
             pending, batch.priority_pending, batch.unknown_pending, len(rows), pending - len(rows))
    if not rows:
        return 0
    ai_categories = set(settings.ai.get("categories") or [])
    selected_revisions = {r["id"]: r["revision"] for r in rows}

    def save(item_id: int, fields: dict) -> None:
        # Saved one by one: if the run stops, finished files are not downloaded or OCR'd again.
        row = store.item(item_id)
        changed = (row["revision"] == selected_revisions[item_id] and row["sha256"] and fields.get("sha256")
                   and row["sha256"] != fields["sha256"])
        if changed:
            fields.update(revision=row["revision"] + 1, ai_json=None, ai_status="pending", ai_error=None,
                          deadline_hint=None, deadline_override=None)
        suggested = fields.get("content_category")
        if suggested in ai_categories:
            if row["ai_status"] == "skipped":
                fields["ai_status"] = "pending"
        if not store.update_item_revision(item_id, selected_revisions[item_id], **fields):
            log.info("Document #%d changed during reading; stale result discarded", item_id)
        elif changed:
            for revision, digest, path in ((row["revision"], row["sha256"], row["local_path"]),
                                            (fields["revision"], fields["sha256"], fields.get("local_path"))):
                store.db.execute("INSERT OR IGNORE INTO document_versions(item_id,revision,sha256,local_path,observed_at) VALUES(?,?,?,?,?)",
                                 (item_id, revision, digest, path, events.stamp()))
            events.record_item_event(store, item_id, "document_changed", {"revision": fields["revision"]})
        store.commit()

    results = documents.run_documents(rows, sites, settings, keywords, workers, on_result=save,
                                      claimed=store.claimed_files())
    ok = sum(1 for _, f in results if f.get("doc_status") in ("ok", "html"))
    log.info("Documents: %d/%d processed, %d ready, %d errors; %d still pending",
             len(results), len(rows), ok, len(results) - ok, pending - len(results))
    return ok


def read_with_claude_plan(store: Store, settings, sites: dict, today: date) -> dict:
    """Queue the new documents, let Claude Code (or OpenRouter, see openrouter_reader.py) read them, save the answers."""
    from . import openrouter_reader

    summary = {"done": 0, "failed": 0, "copied": 0, "cost": 0.0, "skipped_reason": ""}
    use_openrouter = openrouter_reader.enabled()
    if use_openrouter:
        # Free OpenRouter models allow few requests a day; the rest are read in later scans.
        settings.ai["max_docs_per_run"] = openrouter_reader.docs_per_run(int(settings.ai.get("max_docs_per_run", 25)))
    queued = ai_extract.export_queue(store, settings, sites, today)
    if not queued:
        log.info("No new documents to read")
        return summary
    queue = json.loads(ai_extract.QUEUE_FILE.read_text(encoding="utf-8"))
    expected = {int(r["id"]): int(r["revision"]) for r in queue["items"]}
    timeout = int(settings.ai.get("timeout_minutes", 20))
    if use_openrouter:
        reader, model = "OpenRouter", "openrouter:" + openrouter_reader.env("OPENROUTER_MODEL")
        log.info("%d documents queued; starting OpenRouter", queued)
        ok, message = openrouter_reader.read_queue(settings, timeout_minutes=timeout)
    else:
        from .claude_reader import read_queue
        reader, model = "Claude Code", "claude-app"
        log.info("%d documents queued; starting Claude Code (Claude plan, no API cost)", queued)
        ok, message = read_queue(settings, timeout_minutes=timeout)
    log.info("%s: %s", reader, message)
    # Kept until a later read works, so every summary email shows it.
    store.kv_set("claude_problem", "Claude Code is not logged in: documents are waiting. Open a terminal, run "
                 "`claude`, type /login and sign in." if "not logged in" in message.lower() else
                 f"AI reading: {message}" if use_openrouter and "not set up" in message else "")
    store.commit()
    if ok:
        saved, problems = ai_extract.import_results(store, ai_extract.RESULTS_FILE, expected=expected, model=model)
        summary["done"] = saved
        if problems:
            log.warning("Claude answers with problems: %s", problems[:5])
    if not ok or summary["done"] < queued:
        summary["skipped_reason"] = (f"{queued - summary['done']} of {queued} documents could not be read this time "
                                     f"({message}); they will be tried again in the next scan.")
    return summary


def enrich(store, settings, keywords, sites, args, today, run_id):
    """Documents + AI reading, once at a time (a separate --enrich worker may already be busy)."""
    path = DATA_DIR / "enrich.lock"
    guard = None
    if locking.held_path() != path:
        guard = locking.RunLock(path.with_suffix(".guard"))
        if not guard.acquire():
            log.info("Enrichment worker already active; saved backlog will be processed there")
            return None
    try:
        return _work(store, settings, keywords, sites, args, today, run_id)
    finally:
        if guard is not None:
            guard.release()


def _work(store, settings, keywords, sites, args, today, run_id):
    from .relations import link_corrigenda
    workers = args.workers or int(settings.monitoring.get("document_workers", 2))
    # Closed and old tenders first leave the queue, so downloads and reading go to live ones.
    ai_extract.retire_stale(store, settings, today)
    if not args.no_download:
        recovered = documents.reconcile_files(store)
        if recovered:
            log.info("Missing saved assets: %d documents queued for recovery", recovered)
        process_documents(store, settings, keywords, sites, today, workers, deep=bool(store.run(run_id)["deep"]))
        mark_duplicates(store)  # downloads give each file its sha256: same bytes under two addresses
        documents.fill_upload_dates(store, sites, settings, keywords, today, workers=workers)
    summary = None
    if not args.no_ai:
        summary = read_with_claude_plan(store, settings, sites, today)
    link_corrigenda(store)
    return summary
