"""Durable change events and per-recipient notification delivery. No network on import."""

from __future__ import annotations

import hashlib
import html
import json
import logging
import time
from datetime import datetime, timedelta
from pathlib import Path

from .config import env, env_bool
from .notify import Digest

log = logging.getLogger(__name__)


def stamp(now: datetime | None = None) -> str:
    return (now or datetime.now()).isoformat(timespec="seconds")


def record_event(store, key: str, kind: str, payload: dict, item_id: int | None = None,
                 now: datetime | None = None) -> int:
    store.db.execute("INSERT OR IGNORE INTO change_events(event_key,item_id,kind,created,payload) VALUES(?,?,?,?,?)",
                     (key, item_id, kind, stamp(now), json.dumps(payload, ensure_ascii=False)))
    return store.db.execute("SELECT id FROM change_events WHERE event_key=?", (key,)).fetchone()["id"]


def record_item_event(store, item_id: int, kind: str, extra: dict | None = None,
                      event_key: str | None = None) -> int:
    row = store.item(item_id)
    payload = {"title": row["title"], "org": row["site_code"], "url": row["url"],
               "category": row["content_category"] or row["category"], "relevance": row["relevance"] or "",
               "deadline": row["deadline_hint"], "revision": row["revision"], **(extra or {})}
    return record_event(store, event_key or f"{kind}:{item_id}:{row['revision']}", kind, payload, item_id)


def recipients() -> list[tuple[str, str]]:
    result = [("email", a.strip()) for a in env("EMAIL_TO").split(",") if a.strip()]
    if env("TELEGRAM_BOT_TOKEN") and env("TELEGRAM_CHAT_ID"):
        result.append(("telegram", env("TELEGRAM_CHAT_ID")))
    return list(dict.fromkeys(result))


def enqueue(store, event_id: int, payload: dict, now: datetime | None = None) -> None:
    for channel, recipient in recipients():
        store.db.execute("""INSERT INTO notification_outbox
            (event_id,channel,recipient,payload,next_attempt) VALUES(?,?,?,?,?)
            ON CONFLICT(event_id,channel,recipient) DO UPDATE SET status='pending',payload=excluded.payload,
            next_attempt=excluded.next_attempt,lease_until=NULL,last_error=NULL
            WHERE notification_outbox.status='cancelled'""",
            (event_id, channel, recipient, json.dumps(payload, ensure_ascii=False), stamp(now)))


def message(subject: str, text: str, attachments: list | None = None) -> dict:
    return {"subject": subject, "text": text, "html": "<pre style='white-space:pre-wrap'>" + html.escape(text) + "</pre>",
            "attachments": [str(p) for p in (attachments or []) if p]}


def separate_emails(settings) -> bool:
    """False (default): new items, page changes and reminders only go into the one summary email."""
    return bool(settings.notify.get("separate_emails", False))


def queue_discoveries(store, records: list[dict], settings) -> int:
    count = 0
    allowed = set(settings.notify.get("instant_relevance", ["High", "Medium", ""]))
    categories = set(settings.notify.get("instant_categories", ["Tender", "RFP", "EOI", "Empanelment", "Corrigendum", "Notice"]))
    for r in records:
        item = store.item(r["id"])
        if not r["is_new"] or item["user_status"] == "ignore" or "change" in (item["found_via"] or "").split(","):
            continue
        event_id = record_item_event(store, r["id"], "new", event_key=f"new:{r['id']}")
        if r["category"] in categories and (r["relevance"] or "") in allowed:
            text = (f"[{r['org']}] {r['category']}: {r['title']}\n"
                    f"Relevance: {r['relevance'] or 'Not assessed'}\n"
                    f"Website deadline: {r['last_date'] or 'Not confirmed'}\n{r['url'] or ''}\n\n"
                    "Discovery alert. Document verification and AI details follow in the daily digest.")
            if separate_emails(settings):
                enqueue(store, event_id, message(f"New {r['category']} | {r['org']} | {r['title'][:100]}", text))
                count += 1
    store.commit()
    return count


def queue_changes(store, settings) -> None:
    rows = store.db.execute("""SELECT e.* FROM change_events e JOIN items i ON i.id=e.item_id
        WHERE e.kind IN ('document_changed','deadline_changed','withdrawn','page_changed')
        AND i.user_status<>'ignore' ORDER BY e.id""").fetchall()
    for row in rows:
        p = json.loads(row["payload"])
        lines = [f"[{p['org']}] {p['title']}", row["kind"].replace("_", " ").title()]
        if p.get("previous_deadline"):
            lines.append(f"Deadline: {p['previous_deadline']} -> {p.get('deadline') or 'Not confirmed'}")
        elif p.get("deadline"):
            lines.append(f"Updated deadline: {p['deadline']}")
        if p.get("corrigendum_url"):
            lines.append(f"Corrigendum: {p['corrigendum_url']}")
        if p.get("diff"):
            lines.append(p["diff"][:1500])
        lines.append(p.get("url") or "")
        if not separate_emails(settings):
            continue
        enqueue(store, row["id"], message(f"Update |{p['org']} | {p['title'][:100]}", "\n".join(lines)))
    store.commit()


def queue_reminders(store, records: list[dict], today, settings) -> None:
    offsets = set(settings.notify.get("reminder_days", [7, 3, 1]))
    for r in records:
        row = store.item(r["id"])
        if not r["tender"] or not r["last_date"] or row["removed"] or row["user_status"] in ("ignore", "applied"):
            continue
        days = (r["last_date"] - today).days
        if days not in offsets:
            continue
        key = f"reminder:{r['id']}:{r['last_date']}:{days}"
        event_id = record_event(store, key, "reminder", {"deadline": str(r["last_date"]), "days": days}, r["id"])
        if not separate_emails(settings):
            continue
        enqueue(store, event_id, message(f"{days} day(s) left | {r['org']} | {r['title'][:100]}",
                f"{r['title']}\nSubmission deadline: {r['last_date']} ({r.get('last_source', 'verify source')})\n"
                f"{r['url'] or ''}\nVerify the original notice/corrigendum before applying."))
    store.commit()


def queue_daily_digest(store, report: dict, run, settings, keywords, sites, today, problems: list[str]) -> None:
    from .notify import build_digest
    key = f"digest:{today.isoformat()}"
    existing = store.db.execute("SELECT * FROM change_events WHERE event_key=?", (key,)).fetchone()
    if existing:
        payload = json.loads(existing["payload"])
        if payload.get("message"):
            enqueue(store, existing["id"], payload["message"])
            store.commit()
        return
    # Include today's detected/revised items even if an instant alert was already accepted.
    changed = {r[0] for r in store.db.execute("SELECT item_id FROM change_events WHERE created>=? AND item_id IS NOT NULL",
                                           (today.isoformat(),))}
    daily = dict(report)
    daily["new"] = [r for r in report["records"] if (r["id"] in changed or r["first_seen"] == today)
                    and store.item(r["id"])["user_status"] != "ignore"]
    daily["new_tenders"] = [r for r in daily["new"] if r["tender"]]
    digest = build_digest(daily, run, problems, None, today, int(settings.excel.get("closing_soon_days", 7)), len(sites))
    payload = {"subject": "Daily digest | " + digest.subject, "text": digest.text, "html": digest.html,
               "attachments": [str(report["master"])] if settings.notify.get("attach_master_daily", True) else []}
    event_id = record_event(store, key, "digest", {"items": [r["id"] for r in daily["new"]], "message": payload})
    enqueue(store, event_id, payload)
    store.commit()


def queue_run_digest(store, report: dict, run, settings, sites, today, problems: list[str]) -> bool:
    """One summary email per scan: new items not emailed before, closing-soon tenders, today's Excel attached."""
    from .notify import build_digest
    digest = build_digest(report, run, problems, None, today, int(settings.excel.get("closing_soon_days", 7)),
                          len(sites))
    if not (report["new"] or report["closing"] or settings.notify.get("when_empty", False)):
        return False
    # Non-stop scanning (setup_scheduler.ps1 -Auto): a scan that found nothing new sends no email,
    # otherwise the same closing-soon list would arrive every two hours.
    if (settings.notify.get("only_when_new", False) or env_bool("EMAIL_ONLY_WHEN_NEW")) and not report["new"]:
        return False
    attach = report.get("daily")
    payload = {"subject": digest.subject, "text": digest.text, "html": digest.html,
               "attachments": [str(attach)] if attach and Path(str(attach)).exists() else []}
    event_id = record_event(store, f"digest:run:{run['id']}", "digest",
                            {"items": [r["id"] for r in report["new"]], "message": payload})
    enqueue(store, event_id, payload)
    store.mark_notified([r["id"] for r in report["new"]])
    store.commit()
    return True


def _redact(error: Exception) -> str:
    text = str(error)
    for key in ("SMTP_PASSWORD", "TELEGRAM_BOT_TOKEN", "ANTHROPIC_API_KEY", "DASHBOARD_PASSWORD"):
        value = env(key)
        if value:
            text = text.replace(value, "[redacted]")
    return text[:500]


def _reminder_valid(store, row, item, now) -> bool:
    from .ai_extract import ai_fields
    from .dates import parse_any
    if item is None or item["removed"] or item["user_status"] in ("ignore", "applied"):
        return False
    current = parse_any(item["deadline_override"] or ai_fields(item).get("last_date_submission") or item["deadline_hint"])
    for child in store.db.execute("SELECT * FROM items WHERE related_item_id=? AND removed=0 ORDER BY first_seen,id", (item["id"],)):
        if child["ai_status"] != "done":
            current = None
        elif newer := parse_any(ai_fields(child).get("last_date_submission") or child["deadline_hint"]):
            current = newer
    expected = json.loads(row["event_payload"])
    return bool(current and current.isoformat() == expected["deadline"]
                and (current - now.date()).days == expected["days"])


def drain_outbox(store, limit: int = 50, now: datetime | None = None, deliver=None,
                  time_budget_seconds: float = 45) -> dict:
    """Lease one delivery atomically; failed/missing configuration is retried, never consumed."""
    from .notify import email_notify
    import requests
    now = now or datetime.now()
    counts = {"accepted": 0, "failed": 0}
    started = time.monotonic()
    store.commit()
    for _ in range(limit):
        if time.monotonic() - started >= time_budget_seconds:
            break
        store.db.execute("BEGIN IMMEDIATE")
        row = store.db.execute("""SELECT o.*,e.event_key,e.item_id,e.kind,e.payload AS event_payload FROM notification_outbox o
            JOIN change_events e ON e.id=o.event_id WHERE
            (o.status='pending' AND o.next_attempt<=?) OR (o.status='sending' AND o.lease_until<=?)
            ORDER BY o.id LIMIT 1""", (stamp(now), stamp(now))).fetchone()
        if row is None:
            store.commit()
            break
        item = store.item(row["item_id"]) if row["item_id"] else None
        if (item is not None and item["user_status"] == "ignore") or (row["kind"] == "reminder" and not _reminder_valid(store, row, item, now)):
            store.db.execute("UPDATE notification_outbox SET status='cancelled' WHERE id=?", (row["id"],))
            store.commit()
            continue
        store.db.execute("UPDATE notification_outbox SET status='sending',attempts=attempts+1,lease_until=? WHERE id=?",
                         (stamp(now + timedelta(minutes=15)), row["id"]))
        store.commit()
        payload = json.loads(row["payload"])
        msg_id = "<" + hashlib.sha256(f"{row['event_key']}:{row['recipient']}".encode()).hexdigest() + "@tender-tracker>"
        try:
            if deliver is not None:
                deliver(row, payload, msg_id)
            elif row["channel"] == "email":
                digest = Digest(payload["subject"], payload["subject"], payload["text"], payload["html"])
                result = email_notify.send(digest, [Path(p) for p in payload.get("attachments", [])],
                                           recipients=[row["recipient"]], message_id=msg_id)
                if not result.startswith("sent "):
                    raise RuntimeError(result)
            else:
                token = env("TELEGRAM_BOT_TOKEN")
                if not token:
                    raise RuntimeError("TELEGRAM_BOT_TOKEN is not configured")
                response = requests.post(f"https://api.telegram.org/bot{token}/sendMessage", timeout=30,
                    data={"chat_id": row["recipient"], "text": (payload["subject"] + "\n\n" + payload["text"])[:4000],
                          "disable_web_page_preview": "true"})
                if not response.ok or not response.json().get("ok"):
                    raise RuntimeError(f"Telegram refused message (HTTP {response.status_code})")
            store.db.execute("UPDATE notification_outbox SET status='accepted',accepted_at=?,last_error=NULL,lease_until=NULL WHERE id=?",
                             (stamp(now), row["id"]))
            if row["kind"] == "new" and row["channel"] == "email":
                waiting = store.db.execute("SELECT 1 FROM notification_outbox WHERE event_id=? AND channel='email' AND status<>'accepted'",
                                           (row["event_id"],)).fetchone()
                if not waiting:
                    store.db.execute("UPDATE items SET notified=1 WHERE id=?", (row["item_id"],))
            if row["kind"] == "digest" and row["channel"] == "email":
                waiting = store.db.execute("SELECT 1 FROM notification_outbox WHERE event_id=? AND channel='email' AND status<>'accepted'",
                                           (row["event_id"],)).fetchone()
                if not waiting:
                    event = store.db.execute("SELECT payload FROM change_events WHERE id=?", (row["event_id"],)).fetchone()
                    for item_id in json.loads(event["payload"]).get("items", []):
                        store.db.execute("UPDATE items SET notified=1 WHERE id=?", (item_id,))
            counts["accepted"] += 1
        except Exception as exc:
            delay = min(3600, 60 * 2 ** min(row["attempts"], 6))
            store.db.execute("UPDATE notification_outbox SET status='pending',lease_until=NULL,next_attempt=?,last_error=? WHERE id=?",
                             (stamp(now + timedelta(seconds=delay)), _redact(exc), row["id"]))
            counts["failed"] += 1
            log.warning("Alert #%d %s: delivery failed; retry saved", row["id"], row["channel"])
        store.commit()
    return counts
