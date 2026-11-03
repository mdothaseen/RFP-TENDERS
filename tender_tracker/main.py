"""Command line entry point:  python -m tender_tracker [options]"""

from __future__ import annotations

import argparse
import hashlib
import logging
import os
import sys
import time
import json
from dataclasses import replace
from pathlib import Path
from urllib.parse import urlsplit
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta

from . import ai_extract, documents, events
from .changes import meaningful_diff, revalidate_documents
from .classify import classify
from .config import DB_PATH, DATA_DIR, LOG_DIR, NEW_DIR, OUTPUT_DIR, ROOT, ensure_dirs, load_keywords, load_settings, load_sites
from .discover.crawler import Found, SiteScan, scan_site
from .excel_report import MASTER_NAME, write_reports, build_records, summary_report
from .notify import build_digest, send_all
from .store import Store
from .locking import RunLock

log = logging.getLogger("tender_tracker")
LOCK = DATA_DIR / "run.lock"
_RUN_GUARD = None
_LOCK_PATH = None


def parse_args(argv=None):
    p = argparse.ArgumentParser(
        prog="python -m tender_tracker",
        description="Checks Sector Skill Council, NSDC and NCVET websites for tenders, RFPs, EOIs and notices.",
    )
    p.add_argument("--site", nargs="+", metavar="CODE", help="only these sites (codes from config/sites.yaml)")
    p.add_argument("--baseline", action="store_true", help="treat this run as the first run")
    p.add_argument("--deep", action="store_true", help="deeper crawl (normally only on Sundays)")
    p.add_argument("--no-ai", action="store_true", help="do not send documents to Claude")
    p.add_argument("--no-download", action="store_true", help="do not download documents")
    p.add_argument("--no-notify", action="store_true", help="do not send email / Telegram / popup")
    p.add_argument("--notify", action="store_true", help="send alerts even for a --site test run")
    p.add_argument("--force", action="store_true", help="run even if today's run has already finished")
    modes = p.add_mutually_exclusive_group()
    modes.add_argument("--light", action="store_true", help="one lightweight listing/version check; no sitemap/OCR/AI")
    modes.add_argument("--enrich", action="store_true", help="process saved document/AI backlog without a website crawl")
    modes.add_argument("--drain-outbox", action="store_true", help="retry due email/Telegram deliveries")
    modes.add_argument("--health", action="store_true", help="print health JSON; add --notify for incident alerts")
    modes.add_argument("--backup", action="store_true", help="create and verify a database/files backup")
    modes.add_argument("--serve", action="store_true", help="serve authenticated dashboard on localhost")
    modes.add_argument("--check-config", action="store_true", help="check VPS email/dashboard prerequisites without sending")
    modes.add_argument("--verify-backup", metavar="PATH", help="verify a snapshot directory")
    modes.add_argument("--restore-backup", metavar="PATH", help="restore a verified snapshot to --restore-to (new directory)")
    p.add_argument("--restore-to", metavar="DIRECTORY")
    p.add_argument("--defer-enrichment", action="store_true", help="let the separate --enrich worker do OCR/AI")
    p.add_argument("--port", type=int, default=8765, help="dashboard port (default 8765)")
    p.add_argument("--max-pages", type=int, help="page limit per site (testing)")
    p.add_argument("--workers", type=int, help="sites checked in parallel")
    p.add_argument("--excel-only", action="store_true", help="only rebuild the Excel files from saved data")
    p.add_argument("--test-notify", action="store_true", help="send a sample alert on every channel")
    p.add_argument("--telegram-setup", action="store_true", help="show your Telegram chat id")
    p.add_argument("--list-sites", action="store_true", help="list configured websites")
    p.add_argument("--ask", action="store_true", help="show the hourly 'scan now?' popup")
    p.add_argument("--from-popup", action="store_true", help=argparse.SUPPRESS)
    p.add_argument("--ai-export", action="store_true", help="write data/ai_queue.json for the Claude app")
    p.add_argument("--ai-import", metavar="FILE", help="save the Claude app's answers, update Excel, send email")
    p.add_argument("--send-pending-email", action="store_true",
                   help="send today's email if it is still waiting for the Claude app")
    p.add_argument("--add-site", nargs="+", metavar=("URL", "NAME"),
                   help='add a website: --add-site https://example.org "Organisation Name" [CODE]')
    p.add_argument("-v", "--verbose", action="store_true")
    return p.parse_args(argv)


def setup_logging(verbose: bool) -> None:
    ensure_dirs()
    log_file = LOG_DIR / f"run_{date.today().isoformat()}.log"
    handlers = [logging.FileHandler(log_file, encoding="utf-8"), logging.StreamHandler(sys.stdout)]
    logging.basicConfig(level=logging.DEBUG if verbose else logging.INFO,
                        format="%(asctime)s %(levelname)-7s %(message)s", handlers=handlers, force=True)
    for noisy in ("urllib3", "httpx", "httpx2", "anthropic", "charset_normalizer", "PIL", "fontTools", "asyncio"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    # "Retrying ... SSLError / connection reset" lines: the tracker retries and falls back on its own.
    logging.getLogger("urllib3.connectionpool").setLevel(logging.ERROR)
    cutoff = time.time() - 60 * 86400
    for old in LOG_DIR.glob("run_*.log"):
        try:
            if old.stat().st_mtime < cutoff:
                old.unlink()
        except OSError:
            pass


def _keep_awake(on: bool) -> None:
    """Stop Windows from going to sleep while a scan runs (closing the lid can still sleep the laptop)."""
    if sys.platform != "win32":
        return
    import ctypes

    ES_CONTINUOUS, ES_SYSTEM_REQUIRED = 0x80000000, 0x00000001
    try:
        ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS | (ES_SYSTEM_REQUIRED if on else 0))
    except (AttributeError, OSError):
        pass


def _internet_ok() -> bool:
    import socket

    for host in ("www.google.com", "ncvet.gov.in", "1.1.1.1"):
        try:
            socket.create_connection((host, 443), timeout=8).close()
            return True
        except OSError:
            continue
    return False


def _pid_alive(pid: int) -> bool:
    if os.name != "nt":
        try:
            os.kill(pid, 0)
        except OSError:
            return False
        return True
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.windll.kernel32
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        return False
    try:
        code = wintypes.DWORD()
        if not (kernel32.GetExitCodeProcess(handle, ctypes.byref(code)) and code.value == 259):  # STILL_ACTIVE
            return False
        # On Windows, PIDs get reused by unrelated system/user processes after Python exits.
        # Verify that the active process is actually python.
        buf = ctypes.create_unicode_buffer(1024)
        size = wintypes.DWORD(1024)
        if kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
            exe_name = Path(buf.value).name.lower()
            return exe_name.startswith("python")
        return True
    finally:
        kernel32.CloseHandle(handle)


def _run_in_progress(path: Path | None = None) -> bool:
    """True while another run holds the lock. A lock left behind by a closed window or a crash is ignored."""
    path = path or LOCK
    if _RUN_GUARD is not None and _LOCK_PATH == path:
        return True
    try:
        pid = int(path.read_text(encoding="utf-8").split()[0])
        if pid != os.getpid() and _pid_alive(pid):
            return True
    except (OSError, ValueError, IndexError):
        pass
    probe = RunLock(path.with_suffix(".guard"))
    if not probe.acquire():
        return True
    probe.release()
    if path.exists():
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass
    return False


def _acquire_lock(path: Path | None = None) -> bool:
    global _RUN_GUARD, _LOCK_PATH
    path = path or LOCK
    if _RUN_GUARD is not None or _run_in_progress(path):
        return False
    guard = RunLock(path.with_suffix(".guard"))
    if not guard.acquire():
        return False
    path.write_text(f"{os.getpid()} {datetime.now().isoformat()}", encoding="utf-8")
    _RUN_GUARD, _LOCK_PATH = guard, path
    return True


def _release_lock() -> None:
    global _RUN_GUARD, _LOCK_PATH
    try:
        if _RUN_GUARD is not None:
            _LOCK_PATH.unlink(missing_ok=True)
    except OSError:
        pass
    finally:
        if _RUN_GUARD is not None:
            _RUN_GUARD.release()
        _RUN_GUARD, _LOCK_PATH = None, None


def _save_scan(store: Store, scan: SiteScan, keywords, run_id: int, baseline: bool, now: str,
               ai_categories: set[str], today: date) -> int:
    new = 0
    for found in scan.items.values():
        cls = classify(keywords, found.title, found.context, found.url or "")
        _, is_new = store.upsert(found, cls, run_id, baseline, now, ai_categories)
        new += int(is_new)

    # Seed pages whose text changed without a new link (e.g. "last date extended").
    for key, (page_url, text) in scan.page_texts.items():
        text = text[:200_000]
        saved = store.db.execute("SELECT text,updated,revision FROM page_texts WHERE key=?", (key,)).fetchone()
        observed = scan.page_observed.get(key) or datetime.now().isoformat(timespec="microseconds")
        if saved and saved["updated"] > observed:
            continue
        old = saved["text"] if saved else None
        if old is not None and old != text and text:
            snippet = meaningful_diff(old, text, keywords)
            if snippet:
                version = saved["revision"] + 1
                title = f"Page updated: {page_url}"
                change = Found(key=f"change:{key}:{version}", site_code=scan.site.code, url=page_url,
                    title=title, link_text="", context=snippet, source_page=page_url, via={"change"}, linked=True,
                    is_document=False, published=today, date_source="page change")
                item_id, is_new = store.upsert(change, classify(keywords, snippet, text[:2000]), run_id, baseline, now, ai_categories)
                events.record_item_event(store, item_id, "page_changed", {"diff": snippet},
                                         event_key=f"page:{key}:{version}")
                new += int(is_new)
        if text:
            store.save_page_text(key, scan.site.code, text, observed)
    return new


def _scan(store: Store, settings, keywords, selected: dict, run_id: int, baseline: bool, deep: bool,
          today: date, args, notify_enabled: bool = False) -> tuple[int, list[SiteScan]]:
    light = bool(args.light)
    workers = args.workers or int(settings.monitoring.get("light_workers", 3) if light else settings.crawl.get("workers", 6))
    if light:
        watched = {}
        for code, site in selected.items():
            pages = [r[0] for r in store.db.execute("""SELECT DISTINCT source_page FROM items WHERE site_code=?
                AND linked_seen=1 AND category IN ('Tender','RFP','EOI','Empanelment','Corrigendum')
                AND source_page IS NOT NULL ORDER BY last_seen DESC LIMIT 5""", (code,))]
            watched[code] = replace(site, seeds=list(dict.fromkeys(site.seeds + [p for p in pages if site.owns(urlsplit(p).hostname or '')])))
        selected = watched
    known = {code: store.known_wp_ids(code) for code in selected}
    # A newly added site's first scan is its baseline: its old items are not reported as new.
    scanned_before = store.site_codes_with_items()
    ai_categories = set(settings.ai.get("categories") or [])
    now = datetime.now().isoformat(timespec="seconds")
    scans, new_total = [], 0
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        futures = {
            pool.submit(scan_site, site, settings, keywords, known[code], deep, today, args.max_pages, light=light): code
            for code, site in selected.items()
        }
        for fut in as_completed(futures):
            code = futures[fut]
            try:
                scan = fut.result()
            except Exception as exc:  # noqa: BLE001
                log.exception("[%s] scan failed", code)
                scan = SiteScan(selected[code], status="failed", errors=[str(exc)[:200]], error_count=1,
                                finished=datetime.now())
            site_baseline = baseline or code not in scanned_before
            if site_baseline and not baseline:
                log.info("[%s] first scan of this site: saved as its baseline", code)
            store.commit()
            store.db.execute("BEGIN IMMEDIATE")
            new_items = _save_scan(store, scan, keywords, run_id, site_baseline, now, ai_categories, today)
            store.record_site_run(run_id, scan, new_items)
            store.commit()
            new_total += new_items
            scans.append(scan)
            log.info("[%s] saved: %d items, %d new (%d/%d sites done)", code, len(scan.items), new_items,
                     len(scans), len(selected))
            if notify_enabled:
                records = build_records(store, keywords, settings, selected, store.run(run_id), today,
                                        "site_code=? AND notified=0", (code,))
                events.queue_discoveries(store, records, settings)
                events.queue_changes(store, settings)
                if not args.defer_enrichment:
                    events.drain_outbox(store, limit=2, time_budget_seconds=5)
    # Absence in a budget-limited crawl is not evidence that a document was removed.
    store.commit()
    return new_total, scans


def _documents(store: Store, settings, keywords, sites: dict, today: date, workers: int, deep: bool = False) -> int:
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


def _site_problems(store: Store, scans: list[SiteScan]) -> list[str]:
    problems = []
    for scan in sorted(scans, key=lambda s: s.site.code):
        if scan.status == "failed":
            days = store.consecutive_failures(scan.site.code)
            reason = f" - {scan.errors[0]}" if scan.errors else ""
            problems.append(f"{scan.site.code}: website nahi khuli (lagatar {days} run){reason}"[:250])
    return problems


def _read_with_claude_plan(store: Store, settings, sites: dict, today: date) -> dict:
    """Queue the new documents, let Claude Code read them with the Claude plan, and save the answers."""
    from .claude_reader import read_queue

    summary = {"done": 0, "failed": 0, "copied": 0, "cost": 0.0, "skipped_reason": ""}
    queued = ai_extract.export_queue(store, settings, sites, today)
    if not queued:
        log.info("No new documents to read")
        return summary
    log.info("%d documents queued; starting Claude Code (Claude plan, no API cost)", queued)
    queue = json.loads(ai_extract.QUEUE_FILE.read_text(encoding="utf-8"))
    expected = {int(r["id"]): int(r["revision"]) for r in queue["items"]}
    ok, message = read_queue(settings, timeout_minutes=int(settings.ai.get("timeout_minutes", 20)))
    log.info("Claude Code: %s", message)
    # Kept until a later read works, so every summary email shows it.
    store.kv_set("claude_problem", "Claude Code is not logged in: documents are waiting. Open a terminal, run "
                 "`claude`, type /login and sign in." if "not logged in" in message.lower() else "")
    store.commit()
    if ok:
        saved, problems = ai_extract.import_results(store, ai_extract.RESULTS_FILE, expected=expected)
        summary["done"] = saved
        if problems:
            log.warning("Claude answers with problems: %s", problems[:5])
    if not ok or summary["done"] < queued:
        summary["skipped_reason"] = (f"{queued - summary['done']} of {queued} documents could not be read this time "
                                     f"({message}); they will be tried again in the next scan.")
    return summary


def _ask(settings) -> int:
    """Hourly: ask on the desktop whether to scan (skipped while a scan runs or right after one)."""
    from .notify.windows_notify import ask_to_scan

    if _run_in_progress():
        log.info("Scan is running - not asking")
        return 0
    store = Store(DB_PATH)
    try:
        last = store.db.execute("SELECT finished, new_items FROM runs WHERE status='done' AND mode='full' "
                                "ORDER BY id DESC LIMIT 1").fetchone()
    finally:
        store.close()
    last_text = "abhi tak nahi"
    if last and last["finished"]:
        finished = datetime.fromisoformat(last["finished"])
        if (datetime.now() - finished).total_seconds() < 50 * 60:
            log.info("Last scan finished less than 50 minutes ago - not asking")
            return 0
        last_text = f"{finished:%d %b, %H:%M} ({last['new_items']} naye items)"
    log.info("Popup: %s", ask_to_scan(last_text, len(load_sites())))
    return 0


def _problems_from_history(store: Store, sites: dict) -> list[str]:
    problems = []
    claude_problem = store.kv_get("claude_problem")
    if claude_problem:
        problems.append(claude_problem)
    for code in sites:
        hist = store.site_history(code, 1)
        if hist and hist[0]["status"] == "failed":
            problems.append(f"{code}: website could not be read (failed {store.consecutive_failures(code)} run(s) in a row)")
    return problems


def _send_digest(store: Store, settings, keywords, sites: dict, run_row, today: date, report: dict,
                 sites_checked: int) -> None:
    events.queue_daily_digest(store, report, run_row, settings, keywords, sites, today,
                              _problems_from_history(store, sites))
    log.info("Outbox: %s", events.drain_outbox(store))
    store.kv_set("email_pending", "")


def _publish_alerts(store, settings, keywords, sites, run_row, today, report=None, deliver=True) -> None:
    from .health import queue_health
    report = report or summary_report(store, keywords, settings, sites, run_row, today)
    events.queue_discoveries(store, report["records"], settings)
    events.queue_changes(store, settings)
    events.queue_reminders(store, report["records"], today, settings)
    # One summary email after every scan (the old per-day 18:00 digest made scans without an email).
    events.queue_run_digest(store, report, run_row, settings, sites, today, _problems_from_history(store, sites))
    queue_health(store, sites, settings)
    if deliver:
        log.info("Outbox: %s", events.drain_outbox(store))


def _enrich(store, settings, keywords, sites, args, today, run_id):
    path = DATA_DIR / "enrich.lock"
    guard = None
    if _LOCK_PATH != path:
        guard = RunLock(path.with_suffix(".guard"))
        if not guard.acquire():
            log.info("Enrichment worker already active; saved backlog will be processed there")
            return None
    try:
        return _enrich_work(store, settings, keywords, sites, args, today, run_id)
    finally:
        if guard is not None:
            guard.release()


def _enrich_work(store, settings, keywords, sites, args, today, run_id):
    from .relations import link_corrigenda
    workers = args.workers or int(settings.monitoring.get("document_workers", 2))
    if not args.no_download:
        recovered = documents.reconcile_files(store)
        if recovered:
            log.info("Missing saved assets: %d documents queued for recovery", recovered)
        _documents(store, settings, keywords, sites, today, workers, deep=bool(store.run(run_id)["deep"]))
        documents.fill_upload_dates(store, sites, settings, keywords, today, workers=workers)
    summary = None
    if not args.no_ai:
        if str(settings.ai.get("reader", "claude_app")) == "claude_app":
            summary = _read_with_claude_plan(store, settings, sites, today)
        else:
            rows = ai_extract.select_items(store, settings, False, today)
            store.commit()
            summary = ai_extract.run_ai(store, settings, rows, sites, run_id)
    link_corrigenda(store)
    return summary


def _maintenance(args, settings, keywords, sites) -> int:
    from . import backup, health
    if args.check_config:
        from .config import env
        checks = {"email_user": bool(env("SMTP_USER")), "email_password": bool(env("SMTP_PASSWORD")),
                  "email_recipients": bool(env("EMAIL_TO")), "dashboard_password": len(env("DASHBOARD_PASSWORD")) >= 12,
                  "sites": bool(sites), "ai_reader": settings.ai.get("reader", "claude_app")}
        print(json.dumps(checks))
        return 0 if all(checks[k] for k in ("email_user", "email_password", "email_recipients", "dashboard_password", "sites")) else 2
    if args.serve:
        from .dashboard import serve
        serve(DB_PATH, settings, keywords, sites, port=args.port)
        return 0
    if args.verify_backup:
        manifest = backup.verify_backup(Path(args.verify_backup))
        print(f"Backup verified: {len(manifest['files'])} assets")
        return 0
    if args.restore_backup:
        if not args.restore_to:
            raise ValueError("--restore-backup requires --restore-to NEW_DIRECTORY")
        backup.restore_backup(Path(args.restore_backup), Path(args.restore_to))
        print(f"Restored to new directory: {args.restore_to}")
        return 0
    needs_lock = args.enrich or args.backup
    lock_path = DATA_DIR / ("enrich.lock" if args.enrich else "backup.lock")
    if needs_lock and not _acquire_lock(lock_path):
        return 3
    try:
        store = Store(DB_PATH)
        try:
            today = date.today()
            if args.drain_outbox:
                print(json.dumps(events.drain_outbox(store)))
            elif args.health:
                state = health.queue_health(store, sites, settings) if args.notify else health.snapshot(store, sites, settings)
                if args.notify:
                    events.drain_outbox(store)
                print(json.dumps(state, ensure_ascii=True))
                return 0 if state["status"] == "ok" else 2
            elif args.backup:
                target = ROOT / str(settings.backup.get("directory", "backups"))
                result = backup.create_backup(store, ROOT, target, int(settings.backup.get("keep_snapshots", 7)))
                print(f"Verified backup: {result}")
            elif args.enrich:
                run_id = store.start_run("enrich", False, args.deep)
                try:
                    ai_summary = _enrich(store, settings, keywords, sites, args, today, run_id)
                    report = write_reports(store, keywords, settings, sites, store.run(run_id), today, ai_summary)
                    if not args.no_notify:
                        _publish_alerts(store, settings, keywords, sites, store.run(run_id), today, report)
                    store.finish_run(run_id, "done", 0, f"Enrichment complete; AI {ai_summary}")
                except BaseException:
                    store.finish_run(run_id, "failed", 0, "Enrichment interrupted or failed; saved work retained")
                    raise
        finally:
            store.close()
    finally:
        if needs_lock:
            _release_lock()
    return 0


def _pending_email(store: Store, settings, keywords, sites: dict, today: date) -> int:
    """Send the email that waited for the Claude app (after --ai-import, or the 12:30 fallback)."""
    run_id = store.kv_get("email_pending") or ""
    if not run_id:
        log.info("No email is waiting.")
        return 0
    run_row = store.run(int(run_id))
    report = write_reports(store, keywords, settings, sites, run_row, today)
    _send_digest(store, settings, keywords, sites, run_row, today, report, len(sites))
    return 0


def _test_notify(settings, keywords, sites: dict) -> int:
    """Sends the latest real summary on every channel, marked [TEST]."""
    from .excel_report import summary_report

    store = Store(DB_PATH)
    try:
        run_row = store.db.execute("SELECT * FROM runs WHERE status='done' ORDER BY id DESC LIMIT 1").fetchone()
        if run_row is None:
            print("Pehle ek run chalayein, phir test alert bhejein.")
            return 1
        today = date.today()
        report = summary_report(store, keywords, settings, sites, run_row, today)
        problems = []
        for code in sites:
            hist = store.site_history(code, 1)
            if hist and hist[0]["status"] == "failed":
                problems.append(f"{code}: website could not be read")
        digest = build_digest(report, run_row, problems, None, today,
                              int(settings.excel.get("closing_soon_days", 7)), len(sites))
        digest.subject = "[TEST] " + digest.subject
        for channel, result in send_all(digest, report, settings, test=True).items():
            print(f"{channel:9s}: {result}")
    finally:
        store.close()
    return 0


def main(argv=None) -> int:
    args = parse_args(argv)
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass
    setup_logging(args.verbose)
    settings, keywords, site_list = load_settings(), load_keywords(), load_sites()
    sites = {s.code: s for s in site_list}
    if any((args.enrich, args.drain_outbox, args.health, args.backup, args.serve, args.verify_backup, args.restore_backup, args.check_config)):
        return _maintenance(args, settings, keywords, sites)

    if args.telegram_setup:
        from .notify.telegram_notify import print_chat_ids
        return print_chat_ids()
    if args.add_site:
        from .add_site import add_site
        if len(args.add_site) < 2:
            print('Use: --add-site https://example.org "Organisation Name"')
            return 2
        return add_site(args.add_site[0], args.add_site[1], args.add_site[2] if len(args.add_site) > 2 else None)
    if args.list_sites:
        for s in site_list:
            print(f"{s.code:10s} {s.url:45s} {'(JS)' if s.js else ''} {s.name}")
        return 0
    if args.test_notify:
        return _test_notify(settings, keywords, sites)
    if args.ask:
        return _ask(settings)
    if args.from_popup:
        from .notify.windows_notify import say
        if _run_in_progress():
            say("SSC Tender Tracker", "Scan pehle se chal raha hai. Khatam hone par email aur popup aayega.")
            return 0
        say("SSC scan shuru ho gaya", "Lagbhag 25-30 minute. Nayi PDFs Claude padhega, phir Excel update hogi "
                                        "aur email aayega.")
        args.force = True
    if args.ai_export or args.ai_import or args.send_pending_email:
        queue_lock = bool(args.ai_export or args.ai_import)
        if queue_lock and not _acquire_lock(DATA_DIR / "enrich.lock"):
            log.info("AI queue is in use by the enrichment worker; try again after it finishes")
            return 3
        store = Store(DB_PATH)
        try:
            today = date.today()
            if args.ai_export:
                count = ai_extract.export_queue(store, settings, sites, today)
                print(f"{count} documents to read -> {ai_extract.QUEUE_FILE}")
                return 0
            if args.ai_import:
                from pathlib import Path
                saved, problems = ai_extract.import_results(store, Path(args.ai_import))
                print(f"Saved AI details for {saved} documents." + (f" Problems: {problems}" if problems else ""))
                log.info("Claude app results imported: %d saved, %d problems", saved, len(problems))
                if store.kv_get("email_pending"):
                    return _pending_email(store, settings, keywords, sites, today)
                run_row = store.db.execute("SELECT * FROM runs WHERE status='done' ORDER BY id DESC LIMIT 1").fetchone()
                if run_row is not None:
                    write_reports(store, keywords, settings, sites, run_row, today)
                return 0
            return _pending_email(store, settings, keywords, sites, today)
        finally:
            store.close()
            if queue_lock:
                _release_lock()
    if not (ROOT / ".env").exists():
        log.info("Tip: .env file nahi mili. .env.example ko copy karke .env banayein (API key, email, Telegram).")

    today = date.today()
    store = Store(DB_PATH)
    try:
        if args.excel_only:
            run_row = store.db.execute("SELECT * FROM runs ORDER BY id DESC LIMIT 1").fetchone()
            if run_row is None:
                print("Abhi tak koi run nahi hua.")
                return 1
            report = write_reports(store, keywords, settings, sites, run_row, today)
            print(f"Excel: {report['master']}")
            return 0

        selected = sites
        if args.site:
            lookup = {c.upper(): c for c in sites}
            missing = [c for c in args.site if c.upper() not in lookup]
            if missing:
                print(f"Unknown site code(s): {', '.join(missing)}. Use --list-sites.")
                return 2
            selected = {lookup[c.upper()]: sites[lookup[c.upper()]] for c in args.site}
        full_run = not args.site
        if full_run and not args.light and not args.force and store.kv_get("last_full_run_date") == today.isoformat():
            log.info("Aaj ka run pehle hi ho chuka hai. Dobara chalane ke liye --force lagayein.")
            return 0
        if not _internet_ok():
            log.error("Internet connection nahi hai - scan nahi chalaya. Internet aane par dobara chalayein.")
            from .notify.windows_notify import say
            say("SSC scan nahi chala", "Internet connection nahi mila. Internet aane par popup se dobara chalayein.")
            return 4
        if not _acquire_lock(DATA_DIR / "light.lock" if args.light else LOCK):
            log.warning("Ek aur run abhi chal raha hai (data/run.lock). Baad mein try karein.")
            return 3
        _keep_awake(True)
        try:
            return _run(store, settings, keywords, sites, selected, full_run, args, today)
        finally:
            _keep_awake(False)
            _release_lock()
    finally:
        store.close()


def _run(store: Store, settings, keywords, sites: dict, selected: dict, full_run: bool, args, today: date) -> int:
    baseline = args.baseline or not store.has_items()
    deep = not args.light and (args.deep or (full_run and today.weekday() == int(settings.crawl.get("deep_weekday", 6))))
    mode = ("light" if args.light else "full") if full_run else ("light:" if args.light else "sites:") + ",".join(selected)
    stale = store.db.execute("UPDATE runs SET status='interrupted', notes='stopped before finishing "
                             "(laptop sleep, shutdown or crash)' WHERE status='running' AND mode=?", (mode,)).rowcount
    if stale:
        log.info("%d earlier run(s) had stopped before finishing; their new items are included in this email", stale)
    run_id = store.start_run(mode, baseline, deep)
    started = time.monotonic()
    log.info("Run %d started: %d sites, baseline=%s, deep=%s", run_id, len(selected), baseline, deep)
    try:
        notify_enabled = (full_run or args.notify) and not args.no_notify
        new_count, scans = _scan(store, settings, keywords, selected, run_id, baseline, deep, today, args,
                                 notify_enabled=notify_enabled)
        ai_summary = None
        if not args.no_download:
            log.info("Document version checks: %s", revalidate_documents(store, settings, keywords, sites))
        if not args.light and not args.defer_enrichment:
            ai_summary = _enrich(store, settings, keywords, sites, args, today, run_id)
        run_row = store.run(run_id)
        report = (summary_report(store, keywords, settings, sites, run_row, today) if args.light else
                  write_reports(store, keywords, settings, sites, run_row, today, ai_summary))
        if notify_enabled:
            _publish_alerts(store, settings, keywords, sites, run_row, today, report, deliver=not args.defer_enrichment)

        minutes = (time.monotonic() - started) / 60
        failed = [s.site.code for s in scans if s.status == "failed"]
        notes = f"{minutes:.1f} min; failed: {', '.join(failed) or 'none'}"
        if ai_summary:
            notes += f"; AI read {ai_summary['done']}"
        status = "partial" if any(s.status != "ok" for s in scans) else "done"
        store.finish_run(run_id, status, new_count, notes)
        if full_run and not args.light and status == "done":
            store.kv_set("last_full_run_date", today.isoformat())
        if args.light:
            from .health import heartbeat
            try:
                if status == "done":
                    heartbeat()
            except Exception:
                log.warning("External heartbeat failed; check monitor configuration/connectivity")
        log.info("Run %d finished in %.1f min: %d new items, %d new tender-type, sites failed: %s", run_id, minutes,
                 new_count, len(report["new_tenders"]), ", ".join(failed) or "none")
        print(f"\nMaster Excel : {report['master']}")
        print(f"Har SSC      : {OUTPUT_DIR}\\<SSC code>\\  (Excel + Documents)")
        if report["daily"]:
            print(f"Aaj ke naye  : {report['daily']}")
        return 0 if status == "done" else 2
    except KeyboardInterrupt:
        store.finish_run(run_id, "cancelled", 0, "stopped by user")
        raise
    except Exception as exc:  # noqa: BLE001
        log.exception("Run failed")
        store.finish_run(run_id, "failed", 0, str(exc)[:300])
        return 1
