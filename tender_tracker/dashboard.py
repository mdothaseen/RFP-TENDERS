"""Small authenticated, loopback-only dashboard; run behind TLS or an SSH tunnel on a VPS."""

from __future__ import annotations

import base64
import hmac
import json
import logging
from datetime import date
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from .config import env
from .excel_report import build_records
from .health import snapshot
from .store import Store

log = logging.getLogger(__name__)
STATUSES = {"new", "seen", "applied", "ignore"}


def data(store, settings, keywords, sites, query: dict) -> dict:
    run = store.db.execute("SELECT * FROM runs ORDER BY id DESC LIMIT 1").fetchone() or {"id": 0, "baseline": 0}
    records = build_records(store, keywords, settings, sites, run, date.today())
    text = query.get("q", [""])[0][:200].casefold()
    relevance = query.get("relevance", [""])[0]
    status = query.get("status", [""])[0]
    all_items = records
    records = [r for r in records if (not text or text in f"{r['org']} {r['title']} {r['summary']}".casefold())
               and (not relevance or r["relevance"] == relevance) and (not status or r["status"] == status)
               and (status or r["status"] != "ignore")]
    records.sort(key=lambda r: (not r["tender"], r["last_date"] is None, r["last_date"] or date.max, -r["id"]))
    health = snapshot(store, sites, settings)
    outbox = [dict(r) for r in store.db.execute("""SELECT o.id,o.channel,o.status,o.attempts,o.next_attempt,
        o.accepted_at,o.last_error,e.kind,e.item_id FROM notification_outbox o JOIN change_events e ON e.id=o.event_id
        ORDER BY o.id DESC LIMIT 30""")]
    active = [r for r in all_items if r["tender"] and r["status"] not in ("ignore", "applied")
              and not store.item(r["id"])["removed"]]
    return {"items": records[:200], "matching": len(records), "health": health, "outbox": outbox,
            "counts": {"open": sum(bool(r["last_date"] and r["last_date"] >= date.today()) for r in active),
                       "unconfirmed": sum(r["last_date"] is None for r in active),
                       "pending": health["outbox"]["pending"],
                       "site_attention": sum(s["stale"] or s["status"] != "ok" for s in health["sites"])}}


def handler(db_path: Path, settings, keywords, sites: dict, username: str, password: str):
    expected = "Basic " + base64.b64encode(f"{username}:{password}".encode()).decode()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            log.info("Dashboard %s %s", self.command, urlsplit(self.path).path)

        def send(self, status, content, content_type="application/json; charset=utf-8"):
            body = content if isinstance(content, bytes) else json.dumps(content, default=str).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'")
            if status == 401:
                self.send_header("WWW-Authenticate", 'Basic realm="Tender tracker", charset="UTF-8"')
            self.end_headers()
            self.wfile.write(body)

        def authorized(self):
            supplied = self.headers.get("Authorization", "")
            if len(supplied) > 1024 or not hmac.compare_digest(supplied.encode(), expected.encode()):
                self.send(401, {"error": "Authentication required"})
                return False
            return True

        def do_GET(self):
            if not self.authorized():
                return
            url = urlsplit(self.path)
            if url.path == "/":
                self.send(200, Path(__file__).with_name("web").joinpath("dashboard.html").read_bytes(), "text/html; charset=utf-8")
                return
            if url.path not in ("/api/dashboard", "/healthz"):
                self.send(404, {"error": "Not found"})
                return
            store = Store(db_path)
            try:
                if url.path == "/healthz":
                    state = snapshot(store, sites, settings)
                    self.send(200 if state["status"] == "ok" else 503, state)
                else:
                    self.send(200, data(store, settings, keywords, sites, parse_qs(url.query)))
            except Exception:
                log.exception("Dashboard read failed")
                self.send(500, {"error": "Dashboard data could not be loaded; check server logs"})
            finally:
                store.close()

        def do_POST(self):
            if not self.authorized():
                return
            origin = self.headers.get("Origin")
            if self.headers.get("X-Tracker-Request") != "dashboard" or (origin and urlsplit(origin).netloc != self.headers.get("Host")):
                self.send(403, {"error": "Request origin rejected"})
                return
            parts = urlsplit(self.path).path.strip("/").split("/")
            if len(parts) != 4 or parts[:2] != ["api", "items"] or parts[3] not in ("status", "link"):
                self.send(404, {"error": "Not found"})
                return
            store = None
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= 8192:
                    raise ValueError("Invalid request size")
                payload = json.loads(self.rfile.read(length))
                item_id = int(parts[2])
                store = Store(db_path)
                item = store.item(item_id)
                if item is None:
                    self.send(404, {"error": "Unknown item"})
                    return
                if parts[3] == "status":
                    status = payload.get("status")
                    if status not in STATUSES:
                        raise ValueError("Choose new, seen, applied or ignore")
                    store.update_item(item_id, user_status=status)
                else:
                    related = int(payload["related_item_id"]) if payload.get("related_item_id") else None
                    if related is not None:
                        parent = store.item(related)
                        from .ai_extract import ai_fields
                        category = ai_fields(item).get("doc_type") or item["content_category"] or item["category"]
                        parent_category = (ai_fields(parent).get("doc_type") or parent["content_category"] or parent["category"]) if parent else ""
                        if category != "Corrigendum" or parent is None or parent["site_code"] != item["site_code"] or related == item_id or parent_category == "Corrigendum":
                            raise ValueError("Link a corrigendum to an original tender from the same organisation")
                    store.update_item(item_id, related_item_id=related)
                store.commit()
                self.send(200, {"ok": True})
            except (ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
                self.send(400, {"error": str(exc)[:200]})
            finally:
                if store is not None:
                    store.close()
    return Handler


def serve(db_path: Path, settings, keywords, sites, port: int = 8765):
    password = env("DASHBOARD_PASSWORD")
    if len(password) < 12:
        raise ValueError("Set DASHBOARD_PASSWORD to at least 12 characters in .env before starting the dashboard")
    server = ThreadingHTTPServer(("127.0.0.1", port), handler(db_path, settings, keywords, sites,
                                                            env("DASHBOARD_USER", "admin"), password))
    log.info("Dashboard: http://127.0.0.1:%d (authenticated; use TLS reverse proxy or SSH tunnel on VPS)", port)
    try:
        server.serve_forever()
    finally:
        server.server_close()
