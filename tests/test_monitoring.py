"""Monitoring failure/restart/revision regressions. No external services are contacted."""

import hashlib
import json
import smtplib
import sqlite3
import subprocess
import sys
import threading
from datetime import date, datetime, timedelta
from http.server import ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import pytest
import requests

from tender_tracker import backup, changes, dashboard, events, health, main
from tender_tracker.ai_extract import import_results
from tender_tracker.classify import classify
from tender_tracker.config import Site, load_keywords, load_settings
from tender_tracker.discover.crawler import Found, SiteScan, SiteScanner
from tender_tracker.excel_report import build_records, summary_report
from tender_tracker.fetcher import Response
from tender_tracker.locking import RunLock
from tender_tracker.notify import Digest, email_notify
from tender_tracker.renderer import RenderResult
from tender_tracker.relations import link_corrigenda
from tender_tracker.store import SCHEMA, Store

NOW = datetime.now().replace(microsecond=0) + timedelta(hours=1)
TODAY = NOW.date()


@pytest.fixture
def ctx(tmp_path, monkeypatch):
    for key in ("SMTP_USER", "SMTP_PASSWORD", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "HEARTBEAT_URL"):
        monkeypatch.setenv(key, "")
    monkeypatch.setenv("EMAIL_TO", "one@example.invalid")
    store = Store(tmp_path / "data" / "tracker.db")
    settings, kw = load_settings(), load_keywords()
    settings.notify["separate_emails"] = True  # these tests cover the per-item alert mode
    site = Site(code="TST", name="Test Council", url="https://t.example.org/")
    yield SimpleNamespace(store=store, settings=settings, kw=kw, site=site, sites={"TST": site}, root=tmp_path)
    store.close()


def item(ctx, title="EOI for Assessment Agencies", key=None):
    key = key or str(len(ctx.store.items()) + 1)
    f = Found(key=key, site_code="TST", url=f"https://t.example.org/{key}.pdf", title=title, link_text=title,
              context="", source_page="https://t.example.org/tenders", via={"page"}, linked=True,
              is_document=True, file_ext="pdf", published=TODAY)
    item_id, _ = ctx.store.upsert(f, classify(ctx.kw, title), 1, False, NOW.isoformat(), set(ctx.kw.tender_categories))
    ctx.store.commit()
    return item_id


def records(ctx):
    return build_records(ctx.store, ctx.kw, ctx.settings, ctx.sites, {"id": 1, "baseline": 0}, TODAY)


def test_outbox_fail_retry_and_recipient_acceptance(ctx, monkeypatch):
    monkeypatch.setenv("EMAIL_TO", "one@example.invalid,two@example.invalid")
    item_id = item(ctx)
    events.queue_discoveries(ctx.store, records(ctx), ctx.settings)
    deliveries = []

    def deliver(row, payload, message_id):
        deliveries.append((row["recipient"], message_id))
        if row["recipient"].startswith("two") and row["attempts"] == 0:
            raise TimeoutError("temporary SMTP outage")

    result = events.drain_outbox(ctx.store, now=NOW, deliver=deliver)
    assert result == {"accepted": 1, "failed": 1}
    assert ctx.store.item(item_id)["notified"] == 0
    # Requeueing discovery cannot duplicate a recipient's already accepted alert.
    events.queue_discoveries(ctx.store, records(ctx), ctx.settings)
    assert ctx.store.db.execute("SELECT COUNT(*) FROM notification_outbox").fetchone()[0] == 2
    assert events.drain_outbox(ctx.store, now=NOW + timedelta(seconds=30), deliver=deliver)["accepted"] == 0
    assert events.drain_outbox(ctx.store, now=NOW + timedelta(minutes=2), deliver=deliver)["accepted"] == 1
    assert ctx.store.item(item_id)["notified"] == 1
    assert deliveries[1][1] == deliveries[2][1]  # Stable Message-ID for an ambiguous retry.


def test_missing_credentials_do_not_consume_alert(ctx):
    item_id = item(ctx)
    events.queue_discoveries(ctx.store, records(ctx), ctx.settings)
    assert events.drain_outbox(ctx.store, now=NOW)["failed"] == 1
    assert ctx.store.item(item_id)["notified"] == 0
    assert ctx.store.db.execute("SELECT status FROM notification_outbox").fetchone()[0] == "pending"


def test_no_recipients_keeps_discovery_available(ctx, monkeypatch):
    monkeypatch.setenv("EMAIL_TO", "")
    item_id = item(ctx)
    events.queue_discoveries(ctx.store, records(ctx), ctx.settings)
    assert ctx.store.db.execute("SELECT COUNT(*) FROM change_events").fetchone()[0] == 1
    assert ctx.store.db.execute("SELECT COUNT(*) FROM notification_outbox").fetchone()[0] == 0
    monkeypatch.setenv("EMAIL_TO", "one@example.invalid")
    events.queue_discoveries(ctx.store, records(ctx), ctx.settings)
    events.drain_outbox(ctx.store, now=NOW, deliver=lambda *args: None)
    assert ctx.store.item(item_id)["notified"] == 1


def test_delivery_lease_prevents_duplicate_worker_and_recovers_crash(ctx):
    item(ctx)
    events.queue_discoveries(ctx.store, records(ctx), ctx.settings)
    ctx.store.db.execute("UPDATE notification_outbox SET status='sending',lease_until=?", (events.stamp(NOW + timedelta(minutes=15)),))
    ctx.store.commit()
    calls = []
    assert events.drain_outbox(ctx.store, now=NOW, deliver=lambda *args: calls.append(1))["accepted"] == 0
    assert events.drain_outbox(ctx.store, now=NOW + timedelta(minutes=16), deliver=lambda *args: calls.append(1))["accepted"] == 1
    assert calls == [1]


def test_ignored_item_cancels_queued_alert(ctx):
    item_id = item(ctx)
    events.queue_discoveries(ctx.store, records(ctx), ctx.settings)
    ctx.store.update_item(item_id, user_status="ignore")
    calls = []
    events.drain_outbox(ctx.store, now=NOW, deliver=lambda *args: calls.append(1))
    assert not calls
    assert ctx.store.db.execute("SELECT status FROM notification_outbox").fetchone()[0] == "cancelled"


def test_partial_smtp_refusal_is_not_success(ctx, monkeypatch):
    monkeypatch.setenv("SMTP_USER", "test@example.invalid")
    monkeypatch.setenv("SMTP_PASSWORD", "test-only-password")
    monkeypatch.setenv("SMTP_PORT", "587")
    class SMTP:
        def __init__(self, *args, **kwargs): pass
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def starttls(self, **kwargs): pass
        def login(self, *args): pass
        def send_message(self, *args, **kwargs): return {"one@example.invalid": (550, b"refused")}
    monkeypatch.setattr(email_notify.smtplib, "SMTP", SMTP)
    with pytest.raises(smtplib.SMTPRecipientsRefused):
        email_notify.send(Digest("subject", "headline", "text", "<p>text</p>"), [])


def test_low_relevance_waits_for_daily_digest_once(ctx):
    item_id = item(ctx, "Tender for office furniture")
    ctx.store.update_item(item_id, relevance="Low")
    events.queue_discoveries(ctx.store, records(ctx), ctx.settings)
    assert ctx.store.db.execute("SELECT COUNT(*) FROM notification_outbox").fetchone()[0] == 0
    report = summary_report(ctx.store, ctx.kw, ctx.settings, ctx.sites, {"id": 1, "baseline": 0}, TODAY)
    ctx.settings.notify["attach_master_daily"] = False
    for _ in range(2):
        events.queue_daily_digest(ctx.store, report, {"id": 1, "baseline": 0}, ctx.settings, ctx.kw, ctx.sites, TODAY, [])
    assert ctx.store.db.execute("SELECT COUNT(*) FROM notification_outbox").fetchone()[0] == 1
    events.drain_outbox(ctx.store, now=NOW + timedelta(days=1), deliver=lambda *args: None)
    assert ctx.store.item(item_id)["notified"] == 1


def test_reminders_deduplicate_and_follow_revised_deadline(ctx):
    item_id = item(ctx)
    ctx.store.update_item(item_id, deadline_hint=(TODAY + timedelta(days=7)).isoformat())
    for _ in range(2):
        events.queue_reminders(ctx.store, records(ctx), TODAY, ctx.settings)
    assert ctx.store.db.execute("SELECT COUNT(*) FROM notification_outbox").fetchone()[0] == 1
    ctx.store.update_item(item_id, deadline_hint=(TODAY + timedelta(days=3)).isoformat())
    events.queue_reminders(ctx.store, records(ctx), TODAY, ctx.settings)
    assert ctx.store.db.execute("SELECT COUNT(*) FROM notification_outbox").fetchone()[0] == 2
    ctx.store.update_item(item_id, user_status="applied", deadline_hint=(TODAY + timedelta(days=1)).isoformat())
    events.queue_reminders(ctx.store, records(ctx), TODAY, ctx.settings)
    assert ctx.store.db.execute("SELECT COUNT(*) FROM notification_outbox").fetchone()[0] == 2


def document(ctx, monkeypatch):
    monkeypatch.setattr(changes, "ROOT", ctx.root)
    monkeypatch.setattr(changes, "site_dir", lambda code: ctx.root / "output" / code)
    item_id = item(ctx)
    path = ctx.root / "output" / "TST" / "Documents" / "original.pdf"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"%PDF-1.7\noriginal bytes")
    ctx.store.update_item(item_id, local_path=path.relative_to(ctx.root).as_posix(), sha256=changes.hashlib.sha256(path.read_bytes()).hexdigest(),
                          doc_status="ok", etag='"v1"', last_modified="old", last_checked=None,
                          last_full_check=events.stamp(NOW), ai_status="done", ai_json='{"last_date_submission":"2026-10-30"}')
    ctx.store.commit()
    return item_id, path


class VersionFetcher:
    status = 200
    body = b"%PDF-1.7\nrevised bytes"
    calls = []
    def __init__(self, *args): self.session = SimpleNamespace(close=lambda: None)
    def download(self, url, destination, max_bytes, headers=None):
        self.calls.append(headers)
        response = Response(url, url, self.status, {"etag": '"v2"'})
        if self.status != 200: return response, None
        destination.write_bytes(self.body)
        return response, destination


def test_same_url_revision_saves_history_clears_ai_and_emits_once(ctx, monkeypatch):
    item_id, original = document(ctx, monkeypatch)
    assert changes.revalidate_documents(ctx.store, ctx.settings, ctx.kw, ctx.sites, NOW, VersionFetcher)["changed"] == 1
    revised = ctx.store.item(item_id)
    assert original.exists() and revised["revision"] == 1
    assert revised["ai_json"] is None and revised["doc_status"] is None and revised["ai_status"] == "pending"
    assert (ctx.root / revised["local_path"]).is_file()
    assert ctx.store.db.execute("SELECT COUNT(*) FROM document_versions").fetchone()[0] == 2
    assert changes.revalidate_documents(ctx.store, ctx.settings, ctx.kw, ctx.sites, NOW + timedelta(minutes=16), VersionFetcher)["changed"] == 0
    assert ctx.store.db.execute("SELECT COUNT(*) FROM change_events WHERE kind='document_changed'").fetchone()[0] == 1


def test_304_preserves_completed_document_and_ai(ctx, monkeypatch):
    item_id, original = document(ctx, monkeypatch)
    monkeypatch.setattr(VersionFetcher, "status", 304)
    monkeypatch.setattr(VersionFetcher, "calls", [])
    assert changes.revalidate_documents(ctx.store, ctx.settings, ctx.kw, ctx.sites, NOW, VersionFetcher)["changed"] == 0
    assert VersionFetcher.calls == [{"If-None-Match": '"v1"', "If-Modified-Since": "old"}]
    assert ctx.store.item(item_id)["ai_status"] == "done" and original.exists()


def test_periodic_full_hash_check_ignores_stale_validators(ctx, monkeypatch):
    item_id, _ = document(ctx, monkeypatch)
    ctx.store.update_item(item_id, last_full_check=events.stamp(NOW - timedelta(days=2)))
    monkeypatch.setattr(VersionFetcher, "calls", [])
    changes.revalidate_documents(ctx.store, ctx.settings, ctx.kw, ctx.sites, NOW, VersionFetcher)
    assert VersionFetcher.calls == [{}]


def test_missing_download_is_repaired_without_false_revision(ctx, monkeypatch):
    item_id, path = document(ctx, monkeypatch)
    body = path.read_bytes()
    path.unlink()
    monkeypatch.setattr(VersionFetcher, "body", body)
    changes.revalidate_documents(ctx.store, ctx.settings, ctx.kw, ctx.sites, NOW, VersionFetcher)
    row = ctx.store.item(item_id)
    assert row["revision"] == 0 and (ctx.root / row["local_path"]).is_file()
    assert ctx.store.db.execute("SELECT COUNT(*) FROM change_events").fetchone()[0] == 0


def test_error_page_does_not_replace_good_pdf_and_withdrawal_needs_two_checks(ctx, monkeypatch):
    item_id, path = document(ctx, monkeypatch)
    monkeypatch.setattr(VersionFetcher, "body", b"<html>Login required</html>")
    changes.revalidate_documents(ctx.store, ctx.settings, ctx.kw, ctx.sites, NOW, VersionFetcher)
    assert path.exists() and ctx.store.item(item_id)["revision"] == 0
    monkeypatch.setattr(VersionFetcher, "status", 404)
    changes.revalidate_documents(ctx.store, ctx.settings, ctx.kw, ctx.sites, NOW + timedelta(minutes=16), VersionFetcher)
    assert ctx.store.item(item_id)["removed"] == 0
    changes.revalidate_documents(ctx.store, ctx.settings, ctx.kw, ctx.sites, NOW + timedelta(minutes=32), VersionFetcher)
    assert ctx.store.item(item_id)["removed"] == 1


def test_short_date_edits_and_deletions_detected_but_visitors_ignored(ctx):
    before = "EOI for Training Partners\nLast date\n23-10-2026\nVisitors: 100"
    assert "24-10-2026" in changes.meaningful_diff(before, before.replace("23-10", "24-10"), ctx.kw)
    assert changes.meaningful_diff(before, before.replace("Visitors: 100", "Visitors: 101"), ctx.kw) == ""
    assert "- EOI" in changes.meaningful_diff(before, "No current tenders", ctx.kw)


def test_browser_challenge_never_advances_page_snapshot(ctx):
    scanner = SiteScanner(ctx.site, ctx.settings, ctx.kw, set(), light=True)
    scanner.renderer = SimpleNamespace(render=lambda url: RenderResult(url, url, "<title>Just a moment</title>" + "x"*2000, 200))
    assert scanner._render(ctx.site.url) is None
    assert scanner.scan.pages == 0 and scanner.scan.error_count == 1 and not scanner.scan.page_texts


def test_light_mode_skips_bulk_discovery(ctx, monkeypatch):
    scanner = SiteScanner(ctx.site, ctx.settings, ctx.kw, set(), light=True)
    scanner.settings.crawl["respect_robots"] = False
    def crawl(): scanner.scan.pages = 1; return True
    monkeypatch.setattr(scanner, "_crawl", crawl)
    monkeypatch.setattr(scanner, "_sitemaps", lambda: pytest.fail("Light check must skip sitemaps"))
    monkeypatch.setattr(scanner, "_wordpress", lambda: pytest.fail("Light check must skip WordPress media"))
    assert scanner.run().status == "ok" and scanner.max_pages == 6 and scanner.max_depth == 1


def test_light_scan_ignores_daily_guard_and_never_calls_ai(ctx, monkeypatch):
    item(ctx)
    args = main.parse_args(["--light", "--no-download", "--no-notify"])
    monkeypatch.setattr(main, "_scan", lambda *args, **kwargs: (0, [SiteScan(ctx.site, pages=1, finished=NOW)]))
    monkeypatch.setattr(main, "_enrich", lambda *args: pytest.fail("AI/OCR cannot run in light scan"))
    ctx.store.kv_set("last_full_run_date", TODAY.isoformat())
    assert main._run(ctx.store, ctx.settings, ctx.kw, ctx.sites, ctx.sites, True, args, TODAY) == 0
    assert ctx.store.db.execute("SELECT mode FROM runs ORDER BY id DESC").fetchone()[0] == "light"


def test_discovery_alert_is_saved_before_ai_failure(ctx, monkeypatch):
    item(ctx, key="old")
    args = main.parse_args(["--force", "--no-download"])
    found = Found(key="new", site_code="TST", url="https://t.example.org/new.pdf", title="EOI for Assessment Agencies",
                  link_text="EOI for Assessment Agencies", context="", source_page=ctx.site.url,
                  via={"page"}, linked=True, is_document=True, file_ext="pdf", published=TODAY)
    monkeypatch.setattr(main, "scan_site", lambda *args, **kwargs: SiteScan(ctx.site, items={"new": found}, pages=1, finished=NOW))
    monkeypatch.setattr(events, "drain_outbox", lambda *args, **kwargs: {})
    def failure(*args):
        assert ctx.store.db.execute("SELECT COUNT(*) FROM notification_outbox").fetchone()[0] > 0
        raise RuntimeError("AI unavailable")
    monkeypatch.setattr(main, "_enrich", failure)
    assert main._run(ctx.store, ctx.settings, ctx.kw, ctx.sites, ctx.sites, True, args, TODAY) == 1
    assert ctx.store.db.execute("SELECT COUNT(*) FROM notification_outbox WHERE status='pending'").fetchone()[0] > 0


def test_stale_ai_answer_cannot_overwrite_new_revision(ctx, tmp_path):
    item_id = item(ctx)
    ctx.store.update_item(item_id, revision=1)
    path = tmp_path / "answer.json"
    path.write_text(json.dumps([{"id": item_id, "doc_type": "EOI", "last_date_submission": "2026-10-30"}]))
    saved, problems = import_results(ctx.store, path, expected={item_id: 0})
    assert saved == 0 and problems and ctx.store.item(item_id)["ai_json"] is None


def test_corrigendum_exact_reference_links_and_changes_effective_deadline(ctx):
    parent = item(ctx, "RFP for Assessment Project")
    child = item(ctx, "Corrigendum for Assessment Project")
    reference = "RFP/AA/2026/123"
    ctx.store.update_item(parent, ai_status="done", ai_json=json.dumps({"doc_type":"RFP", "reference_no":reference,"last_date_submission":"2026-10-23"}))
    ctx.store.update_item(child, ai_status="done", ai_json=json.dumps({"doc_type":"Corrigendum", "reference_no":reference,"last_date_submission":"2026-11-01"}))
    assert link_corrigenda(ctx.store) == 1
    assert ctx.store.item(child)["related_item_id"] == parent
    result = {r["id"]: r for r in records(ctx)}
    assert result[parent]["last_date"] == date(2026, 11, 1)
    assert result[parent]["last_source"] == f"Corrigendum #{child}"


def test_health_sends_only_incident_changes_and_recovery(ctx, monkeypatch):
    monkeypatch.setenv("SMTP_USER", "configured")
    monkeypatch.setenv("SMTP_PASSWORD", "configured")
    ctx.settings.backup["enabled"] = False
    ctx.settings.monitoring["health_emails"] = True
    ctx.settings.monitoring["min_free_disk_mb"] = 0
    first = health.queue_health(ctx.store, ctx.sites, ctx.settings, NOW)
    assert first["status"] == "degraded"
    health.queue_health(ctx.store, ctx.sites, ctx.settings, NOW + timedelta(minutes=1))
    assert ctx.store.db.execute("SELECT COUNT(*) FROM change_events WHERE kind='health'").fetchone()[0] == 1
    ctx.store.record_site_run(1, SiteScan(ctx.site, pages=1, finished=NOW), 0)
    ctx.store.commit()
    assert health.queue_health(ctx.store, ctx.sites, ctx.settings, NOW + timedelta(minutes=2))["status"] == "ok"
    assert ctx.store.db.execute("SELECT COUNT(*) FROM change_events WHERE kind='health'").fetchone()[0] == 2


def test_backup_verify_restore_deduplicates_and_excludes_secrets(ctx, monkeypatch):
    document(ctx, monkeypatch)
    (ctx.root / ".env").write_text("DO_NOT_BACK_UP=secret")
    target = ctx.root / "backups"
    first = backup.create_backup(ctx.store, ctx.root, target)
    second = backup.create_backup(ctx.store, ctx.root, target)
    manifest = backup.verify_backup(second)
    assert all(".env" not in p["path"] for p in manifest["files"])
    assert len(list((target / "assets").iterdir())) == 1
    restore = ctx.root / "restored"
    backup.restore_backup(first, restore)
    assert (restore / "output/TST/Documents/original.pdf").read_bytes().startswith(b"%PDF")
    restored = Store(restore / "data/tracker.db")
    try: assert len(restored.items()) == 1
    finally: restored.close()
    with pytest.raises(ValueError): backup.restore_backup(first, restore)
    blob = next((target / "assets").iterdir())
    blob.write_bytes(b"corrupted")
    with pytest.raises(ValueError): backup.verify_backup(first)


def test_populated_legacy_schema_gets_snapshot_before_migration(tmp_path):
    path = tmp_path / "legacy.db"
    connection = sqlite3.connect(path)
    connection.executescript(SCHEMA)
    connection.execute("INSERT INTO items(key,site_code,first_seen,last_seen) VALUES('old','TST','2026-10-01','2026-10-01')")
    connection.commit(); connection.close()
    store = Store(path)
    try:
        assert store.item(1)["revision"] == 0 and store.item(1)["user_status"] == "new"
        assert len(list(tmp_path.glob("legacy_pre_monitor_*.db"))) == 1
    finally: store.close()


def test_os_lock_is_exclusive_between_processes(tmp_path):
    path = tmp_path / "scan.guard"
    lock = RunLock(path)
    assert lock.acquire()
    code = "import sys; from pathlib import Path; from tender_tracker.locking import RunLock; l=RunLock(Path(sys.argv[1])); print(l.acquire()); l.release()"
    first = subprocess.run([sys.executable, "-c", code, str(path)], capture_output=True, text=True, timeout=10)
    assert first.returncode == 0 and first.stdout.strip() == "False"
    lock.release()
    second = subprocess.run([sys.executable, "-c", code, str(path)], capture_output=True, text=True, timeout=10)
    assert second.returncode == 0 and second.stdout.strip() == "True"


def test_authenticated_dashboard_api_status_and_csrf(ctx):
    item_id = item(ctx)
    server = ThreadingHTTPServer(("127.0.0.1", 0), dashboard.handler(ctx.store.path, ctx.settings, ctx.kw, ctx.sites, "test", "test-password-only"))
    worker = threading.Thread(target=server.serve_forever, daemon=True); worker.start()
    url = f"http://127.0.0.1:{server.server_port}"
    client = requests.Session(); client.trust_env = False
    try:
        assert client.get(url, timeout=5).status_code == 401
        client.auth = ("test", "test-password-only")
        assert client.get(url, timeout=5).status_code == 200
        state = client.get(url + "/api/dashboard", timeout=5).json()
        assert state["items"][0]["id"] == item_id
        endpoint = url + f"/api/items/{item_id}/status"
        assert client.post(endpoint, json={"status":"applied"}, timeout=5).status_code == 403
        assert client.post(endpoint, json={"status":"applied"}, headers={"X-Tracker-Request":"dashboard", "Origin":"https://evil.invalid"}, timeout=5).status_code == 403
        response = client.post(endpoint, json={"status":"applied"}, headers={"X-Tracker-Request":"dashboard"}, timeout=5)
        assert response.status_code == 200 and ctx.store.item(item_id)["user_status"] == "applied"
        assert client.get(url + "/.env", timeout=5).status_code == 404
    finally:
        client.close(); server.shutdown(); server.server_close(); worker.join(timeout=5)


@pytest.mark.parametrize("change", ["applied", "extended"])
def test_stale_queued_reminder_is_cancelled_before_delivery(ctx, change):
    item_id = item(ctx)
    ctx.store.update_item(item_id, deadline_hint=(TODAY + timedelta(days=7)).isoformat())
    events.queue_reminders(ctx.store, records(ctx), TODAY, ctx.settings)
    if change == "applied":
        ctx.store.update_item(item_id, user_status="applied")
    else:
        ctx.store.update_item(item_id, deadline_hint=(TODAY + timedelta(days=10)).isoformat())
    calls = []
    events.drain_outbox(ctx.store, now=NOW, deliver=lambda *args: calls.append(1))
    assert not calls
    assert ctx.store.db.execute("SELECT status FROM notification_outbox").fetchone()[0] == "cancelled"


def test_listing_deadline_override_beats_old_ai(ctx):
    item_id = item(ctx)
    ctx.store.update_item(item_id, ai_status="done", ai_json=json.dumps({"last_date_submission": "2026-10-23"}))
    found = Found(key="1", site_code="TST", url="https://t.example.org/1.pdf", title="EOI for Assessment Agencies",
                  link_text="EOI", context="Last date extended", source_page=ctx.site.url, via={"page"},
                  linked=True, is_document=True, file_ext="pdf", deadline=date(2026, 11, 3))
    ctx.store.upsert(found, classify(ctx.kw, found.title), 2, False, NOW.isoformat(), set(ctx.kw.tender_categories))
    assert records(ctx)[0]["last_date"] == date(2026, 11, 3)
    assert ctx.store.db.execute("SELECT COUNT(*) FROM change_events WHERE kind='deadline_changed'").fetchone()[0] == 1


def test_pending_revised_corrigendum_makes_original_deadline_unconfirmed(ctx):
    parent = item(ctx)
    child = item(ctx, "Corrigendum for Assessment Agencies")
    ctx.store.update_item(parent, deadline_hint=(TODAY + timedelta(days=3)).isoformat())
    ctx.store.update_item(child, related_item_id=parent, ai_status="pending", ai_json=None)
    original = next(r for r in records(ctx) if r["id"] == parent)
    assert original["last_date"] is None and "verification pending" in original["last_source"]


def test_report_writer_lock_excludes_concurrent_excel_updates(ctx, monkeypatch):
    from tender_tracker import excel_report
    monkeypatch.setattr(excel_report, "OUTPUT_DIR", ctx.root / "output")
    guard = RunLock(ctx.root / "output/.reports.guard")
    assert guard.acquire()
    try:
        monkeypatch.setattr(excel_report, "_write_reports", lambda *args: pytest.fail("Concurrent Excel write"))
        result = excel_report.write_reports(ctx.store, ctx.kw, ctx.settings, ctx.sites, {"id": 1, "baseline": 0}, TODAY)
        assert result["locked"]
    finally:
        guard.release()


def test_outbox_health_uses_event_age_even_when_next_retry_is_in_future(ctx):
    event_id = events.record_event(ctx.store, "old", "health", {}, now=NOW - timedelta(hours=2))
    events.enqueue(ctx.store, event_id, events.message("test", "test"), NOW + timedelta(hours=1))
    state = health.snapshot(ctx.store, {}, ctx.settings, NOW)
    assert "outbox:overdue" in state["issues"]


def test_delayed_crawl_cannot_overwrite_newer_page_snapshot(ctx):
    key = "test-page"
    ctx.store.save_page_text(key, "TST", "EOI deadline 30 October", NOW.isoformat())
    scan = SiteScan(ctx.site)
    scan.page_texts[key] = (ctx.site.url, "EOI deadline 23 October")
    scan.page_observed[key] = (NOW - timedelta(minutes=5)).isoformat()
    main._save_scan(ctx.store, scan, ctx.kw, 2, False, NOW.isoformat(), set(ctx.kw.tender_categories), TODAY)
    saved = ctx.store.db.execute("SELECT text FROM page_texts WHERE key=?", (key,)).fetchone()[0]
    assert saved == "EOI deadline 30 October"
    assert ctx.store.db.execute("SELECT COUNT(*) FROM change_events").fetchone()[0] == 0


def test_missing_text_recovery_keeps_existing_pdf(ctx, monkeypatch):
    from tender_tracker import documents
    item_id, path = document(ctx, monkeypatch)
    monkeypatch.setattr(documents, "ROOT", ctx.root)
    ctx.store.update_item(item_id, text_path="data/text/lost.txt")
    assert documents.reconcile_files(ctx.store) == 1
    row = ctx.store.item(item_id)
    assert row["doc_status"] is None and row["ai_status"] == "done" and path.is_file()
    assert documents.plan_documents(ctx.store, ctx.settings, ctx.kw, TODAY).rows[0]["id"] == item_id


def test_valid_reminder_is_accepted_on_its_scheduled_day(ctx):
    item_id = item(ctx)
    ctx.store.update_item(item_id, deadline_hint=(TODAY + timedelta(days=3)).isoformat())
    events.queue_reminders(ctx.store, records(ctx), TODAY, ctx.settings)
    assert events.drain_outbox(ctx.store, now=NOW, deliver=lambda *args: None) == {"accepted": 1, "failed": 0}


def test_changed_bytes_during_missing_file_recovery_invalidate_old_ai(ctx, monkeypatch):
    from tender_tracker import documents
    item_id, path = document(ctx, monkeypatch)
    path.unlink()
    ctx.store.update_item(item_id, doc_status=None)
    def recover(rows, sites, settings, keywords, workers, on_result, claimed):
        fields = {"sha256": "new-hash", "local_path": "output/TST/Documents/new.pdf", "doc_status": "ok"}
        on_result(item_id, fields)
        return [(item_id, fields)]
    monkeypatch.setattr(documents, "run_documents", recover)
    assert main._documents(ctx.store, ctx.settings, ctx.kw, ctx.sites, TODAY, 1) == 1
    revised = ctx.store.item(item_id)
    assert revised["revision"] == 1 and revised["ai_json"] is None and revised["ai_status"] == "pending"
    assert ctx.store.db.execute("SELECT COUNT(*) FROM change_events WHERE kind='document_changed'").fetchone()[0] == 1


def test_default_sends_only_the_summary_email(ctx):
    ctx.settings.notify["separate_emails"] = False
    found = Found(key="k1", site_code="TST", url="https://t.example.org/eoi.pdf", title="EOI for Assessment Agencies",
                  link_text="", context="", source_page="", via={"page"}, linked=True, is_document=True)
    item_id, _ = ctx.store.upsert(found, classify(ctx.kw, found.title), 1, False, datetime.now().isoformat(),
                                  set(ctx.kw.tender_categories))
    record = {"id": item_id, "is_new": True, "category": "EOI", "relevance": "High", "org": "TST",
              "title": found.title, "last_date": None, "url": found.url}
    events.queue_discoveries(ctx.store, [record], ctx.settings)
    assert ctx.store.db.execute("SELECT COUNT(*) FROM notification_outbox").fetchone()[0] == 0
