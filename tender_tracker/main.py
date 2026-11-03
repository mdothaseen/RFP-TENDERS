"""Command line entry point:  python -m tender_tracker [options]"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from datetime import date, datetime
from pathlib import Path

from . import ai_extract, alerts, enrich, events, locking, scanning
from .changes import revalidate_documents
from .config import DB_PATH, DATA_DIR, LOG_DIR, OUTPUT_DIR, ROOT, ensure_dirs, load_keywords, load_settings, load_sites
from .excel_report import write_reports, summary_report
from .store import Store
from .system import internet_ok, keep_awake

log = logging.getLogger("tender_tracker")
MIN_OK_SHARE = 0.85  # a full scan counts as done when at least this share of websites was read fine


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
    modes.add_argument("--reclassify", action="store_true",
                       help="apply the current keywords.yaml to every saved item (category, relevance, title)")
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
    for noisy in ("urllib3", "httpx", "httpx2", "charset_normalizer", "PIL", "fontTools", "asyncio"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    # "Retrying ... SSLError / connection reset" lines: the tracker retries and falls back on its own.
    logging.getLogger("urllib3.connectionpool").setLevel(logging.ERROR)
    cutoff = time.time() - 60 * 86400
    for old in LOG_DIR.glob("run_*.log"):
        try:
            if old.stat().st_mtime < cutoff:
                old.unlink()
        except OSError as exc:
            log.debug("Could not remove old log %s: %s", old, exc)


def _ask(settings) -> int:
    """Hourly: ask on the desktop whether to scan (skipped while a scan runs or right after one)."""
    from .notify.windows_notify import ask_to_scan

    if locking.run_in_progress():
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


def _maintenance(args, settings, keywords, sites) -> int:
    from . import backup, health
    if args.check_config:
        from .config import env
        checks = {"email_user": bool(env("SMTP_USER")), "email_password": bool(env("SMTP_PASSWORD")),
                  "email_recipients": bool(env("EMAIL_TO")), "dashboard_password": len(env("DASHBOARD_PASSWORD")) >= 12,
                  "sites": bool(sites)}
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
    needs_lock = args.enrich or args.backup or args.reclassify
    lock_path = locking.LOCK if args.reclassify else DATA_DIR / ("enrich.lock" if args.enrich else "backup.lock")
    if needs_lock and not locking.acquire(lock_path):
        return 3
    try:
        store = Store(DB_PATH)
        try:
            today = date.today()
            if args.reclassify:
                from .reclassify import reclassify
                for change, count in reclassify(store, keywords, settings).most_common():
                    print(f"{count:7d}  {change}")
            elif args.drain_outbox:
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
                    ai_summary = enrich.enrich(store, settings, keywords, sites, args, today, run_id)
                    report = write_reports(store, keywords, settings, sites, store.run(run_id), today, ai_summary)
                    if not args.no_notify:
                        alerts.publish(store, settings, keywords, sites, store.run(run_id), today, report)
                    store.finish_run(run_id, "done", 0, f"Enrichment complete; AI {ai_summary}")
                except BaseException:
                    store.finish_run(run_id, "failed", 0, "Enrichment interrupted or failed; saved work retained")
                    raise
        finally:
            store.close()
    finally:
        if needs_lock:
            locking.release()
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
    if any((args.enrich, args.drain_outbox, args.health, args.backup, args.serve, args.verify_backup, args.restore_backup, args.check_config,
            args.reclassify)):
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
        return alerts.test_notify(settings, keywords, sites)
    if args.ask:
        return _ask(settings)
    if args.from_popup:
        from .notify.windows_notify import say
        if locking.run_in_progress():
            say("SSC Tender Tracker", "Scan pehle se chal raha hai. Khatam hone par email aur popup aayega.")
            return 0
        say("SSC scan shuru ho gaya", "Lagbhag 25-30 minute. Nayi PDFs Claude padhega, phir Excel update hogi "
                                        "aur email aayega.")
        args.force = True
    if args.ai_export or args.ai_import or args.send_pending_email:
        queue_lock = bool(args.ai_export or args.ai_import)
        if queue_lock and not locking.acquire(DATA_DIR / "enrich.lock"):
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
                saved, problems = ai_extract.import_results(store, Path(args.ai_import))
                print(f"Saved AI details for {saved} documents." + (f" Problems: {problems}" if problems else ""))
                log.info("Claude app results imported: %d saved, %d problems", saved, len(problems))
                if store.kv_get("email_pending"):
                    return alerts.pending_email(store, settings, keywords, sites, today)
                run_row = store.db.execute("SELECT * FROM runs WHERE status='done' ORDER BY id DESC LIMIT 1").fetchone()
                if run_row is not None:
                    write_reports(store, keywords, settings, sites, run_row, today)
                return 0
            return alerts.pending_email(store, settings, keywords, sites, today)
        finally:
            store.close()
            if queue_lock:
                locking.release()
    if not (ROOT / ".env").exists():
        log.info("Tip: .env file nahi mili. .env.example ko copy karke .env banayein (email, Telegram).")

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
        if not internet_ok():
            log.error("Internet connection nahi hai - scan nahi chalaya. Internet aane par dobara chalayein.")
            from .notify.windows_notify import say
            say("SSC scan nahi chala", "Internet connection nahi mila. Internet aane par popup se dobara chalayein.")
            return 4
        if not locking.acquire(DATA_DIR / "light.lock" if args.light else locking.LOCK):
            log.warning("Ek aur run abhi chal raha hai (data/run.lock). Baad mein try karein.")
            return 3
        keep_awake(True)
        try:
            return _run(store, settings, keywords, sites, selected, full_run, args, today)
        finally:
            keep_awake(False)
            locking.release()
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
        new_count, scans = scanning.scan_sites(store, settings, keywords, selected, run_id, baseline, deep, today, args,
                                 notify_enabled=notify_enabled)
        ai_summary = None
        if not args.no_download:
            log.info("Document version checks: %s", revalidate_documents(store, settings, keywords, sites))
        if not args.light and not args.defer_enrichment:
            ai_summary = enrich.enrich(store, settings, keywords, sites, args, today, run_id)
        run_row = store.run(run_id)
        report = (summary_report(store, keywords, settings, sites, run_row, today) if args.light else
                  write_reports(store, keywords, settings, sites, run_row, today, ai_summary))
        if notify_enabled:
            alerts.publish(store, settings, keywords, sites, run_row, today, report, deliver=not args.defer_enrichment)

        minutes = (time.monotonic() - started) / 60
        failed = [s.site.code for s in scans if s.status == "failed"]
        notes = f"{minutes:.1f} min; failed: {', '.join(failed) or 'none'}"
        if ai_summary:
            notes += f"; AI read {ai_summary['done']}"
        # One closed website must not mark the whole scan unfinished; the failed ones are listed in the email.
        ok_share = sum(s.status == "ok" for s in scans) / max(1, len(scans))
        status = "done" if ok_share >= MIN_OK_SHARE else "partial"
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
