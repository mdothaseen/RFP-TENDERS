"""Observable health and incident/recovery notifications without unchanged-state spam."""

from __future__ import annotations

import hashlib
import json
import shutil
from datetime import datetime, timedelta

from .config import ROOT, env
from .events import enqueue, message, record_event, stamp


def snapshot(store, sites: dict, settings, now: datetime | None = None) -> dict:
    now = now or datetime.now()
    stale_minutes = int(settings.monitoring.get("stale_after_minutes", 60))
    issues, site_states = {}, []
    for code, site in sites.items():
        history = store.site_history(code, 3)
        last = history[0] if history else None
        success = store.last_success(code)
        stale = not success or datetime.fromisoformat(success) < now - timedelta(minutes=stale_minutes)
        failures = store.consecutive_failures(code)
        partial = len(history) == 3 and all(r["status"] == "partial" for r in history)
        if failures >= int(settings.monitoring.get("failure_threshold", 3)):
            issues[f"site:{code}:failed"] = f"{code}: failed {failures} consecutive checks"
        elif partial:
            issues[f"site:{code}:partial"] = f"{code}: last 3 checks only partially successful"
        if stale:
            issues[f"site:{code}:stale"] = f"{code}: no fully successful check within {stale_minutes} minutes"
        site_states.append({"code": code, "name": site.name, "status": last["status"] if last else "not checked",
                            "last_success": success, "last_attempt": last["finished"] if last else None,
                            "failures": failures, "stale": stale})
    outbox = dict(store.db.execute("""SELECT COUNT(*) total,
        COALESCE(SUM(o.status IN ('pending','sending')),0) pending,
        COALESCE(SUM(o.status='accepted'),0) accepted,
        MIN(CASE WHEN o.status IN ('pending','sending') THEN e.created END) oldest
        FROM notification_outbox o JOIN change_events e ON e.id=o.event_id""").fetchone())
    if outbox["oldest"] and outbox["oldest"] < stamp(now - timedelta(minutes=30)):
        issues["outbox:overdue"] = f"Email/Telegram delivery backlog: {outbox['pending']} waiting"
    email_ready = bool(env("SMTP_USER") and env("SMTP_PASSWORD") and env("EMAIL_TO"))
    if not email_ready:
        issues["email:configuration"] = "Email configuration incomplete"
    free = shutil.disk_usage(ROOT).free
    if free < int(settings.monitoring.get("min_free_disk_mb", 2048)) * 1024**2:
        issues["disk:low"] = f"Disk space low: {free / 1024**3:.2f} GiB free"
    backup_at = store.kv_get("last_backup_at")
    if settings.backup.get("enabled", True) and (not backup_at or backup_at < stamp(now - timedelta(hours=36))):
        issues["backup:stale"] = "No verified backup within 36 hours"
    last_run = store.db.execute("SELECT id,mode,started,finished,status,notes FROM runs ORDER BY id DESC LIMIT 1").fetchone()
    return {"status": "degraded" if issues else "ok", "checked_at": stamp(now), "issues": issues,
            "sites": site_states, "outbox": outbox, "email_configured": email_ready,
            "disk_free_gib": round(free / 1024**3, 2), "last_backup": backup_at,
            "ai_pending": store.db.execute("SELECT COUNT(*) FROM items WHERE ai_status IN ('pending','failed')").fetchone()[0],
            "last_run": dict(last_run) if last_run else None}


def queue_health(store, sites: dict, settings, now: datetime | None = None) -> dict:
    health = snapshot(store, sites, settings, now)
    signature = hashlib.sha256(json.dumps(sorted(health["issues"])).encode()).hexdigest()
    previous = store.kv_get("health_signature")
    if previous != signature:
        revision = int(store.kv_get("health_revision", "0")) + 1
        # Off on the laptop: scans are not hourly there, so every site would look "stale".
        emails_on = bool(settings.monitoring.get("health_emails", False))
        if emails_on and (health["issues"] or previous is not None):
            text = "\n".join(health["issues"].values()) if health["issues"] else "All monitored health conditions recovered."
            event_id = record_event(store, f"health:{revision}", "health", {"issues": health["issues"]}, now=now)
            enqueue(store, event_id, message(f"Tracker health: {health['status']}", text), now)
        store.kv_set("health_revision", str(revision))
        store.kv_set("health_signature", signature)
    store.commit()
    return health


def heartbeat() -> None:
    """Optional external dead-man monitor; unlike local email it can detect a stopped VPS."""
    if env("HEARTBEAT_URL"):
        import requests
        requests.get(env("HEARTBEAT_URL"), timeout=10).raise_for_status()
