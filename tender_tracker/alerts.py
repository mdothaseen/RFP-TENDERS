"""What the user is told after a scan: the summary email, reminders, health, and test alerts."""

from __future__ import annotations

import logging
from datetime import date

from . import events
from .config import DB_PATH
from .excel_report import summary_report, write_reports
from .notify import build_digest, send_all
from .store import Store

log = logging.getLogger("tender_tracker")


def problems_from_history(store: Store, sites: dict) -> list[str]:
    """Lines for the email's Notes: a Claude logout and websites whose latest check failed."""
    problems = []
    claude_problem = store.kv_get("claude_problem")
    if claude_problem:
        problems.append(claude_problem)
    for code in sites:
        hist = store.site_history(code, 1)
        if hist and hist[0]["status"] == "failed":
            problems.append(f"{code}: website could not be read (failed {store.consecutive_failures(code)} run(s) in a row)")
    return problems


def send_digest(store: Store, settings, keywords, sites: dict, run_row, today: date, report: dict) -> None:
    events.queue_daily_digest(store, report, run_row, settings, keywords, sites, today,
                              problems_from_history(store, sites))
    log.info("Outbox: %s", events.drain_outbox(store))
    store.kv_set("email_pending", "")


def publish(store, settings, keywords, sites, run_row, today, report=None, deliver=True) -> None:
    from .health import queue_health
    report = report or summary_report(store, keywords, settings, sites, run_row, today)
    events.queue_discoveries(store, report["records"], settings)
    events.queue_changes(store, settings)
    events.queue_reminders(store, report["records"], today, settings)
    # One summary email after every scan (the old per-day 18:00 digest made scans without an email).
    events.queue_run_digest(store, report, run_row, settings, sites, today, problems_from_history(store, sites))
    queue_health(store, sites, settings)
    if deliver:
        log.info("Outbox: %s", events.drain_outbox(store))


def pending_email(store: Store, settings, keywords, sites: dict, today: date) -> int:
    """Send the email that waited for the Claude app (after --ai-import, or the 12:30 fallback)."""
    run_id = store.kv_get("email_pending") or ""
    if not run_id:
        log.info("No email is waiting.")
        return 0
    run_row = store.run(int(run_id))
    report = write_reports(store, keywords, settings, sites, run_row, today)
    send_digest(store, settings, keywords, sites, run_row, today, report)
    return 0


def test_notify(settings, keywords, sites: dict) -> int:
    """Sends the latest real summary on every channel, marked [TEST]."""
    store = Store(DB_PATH)
    try:
        run_row = store.db.execute("SELECT * FROM runs WHERE status='done' ORDER BY id DESC LIMIT 1").fetchone()
        if run_row is None:
            print("Pehle ek run chalayein, phir test alert bhejein.")
            return 1
        today = date.today()
        report = summary_report(store, keywords, settings, sites, run_row, today)
        digest = build_digest(report, run_row, problems_from_history(store, sites), None, today,
                              int(settings.excel.get("closing_soon_days", 7)), len(sites))
        digest.subject = "[TEST] " + digest.subject
        for channel, result in send_all(digest, report, settings, test=True).items():
            print(f"{channel:9s}: {result}")
    finally:
        store.close()
    return 0
