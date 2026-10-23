"""Alerts: email, Telegram and Windows popup. Each channel is skipped quietly when not configured."""

from __future__ import annotations

import html
import logging
from pathlib import Path
from dataclasses import dataclass, field
from datetime import date

from ..config import env_bool
from .email_html import build_email, uploaded_text

log = logging.getLogger(__name__)


@dataclass
class Digest:
    subject: str
    headline: str
    text: str
    html: str
    telegram: list[str] = field(default_factory=list)
    toast: str = ""
    has_news: bool = False


def _fmt(d: date | None) -> str:
    return d.strftime("%d-%b-%Y") if d else "-"


def build_digest(report: dict, run, problems: list[str], ai_summary: dict | None, today: date,
                 closing_days: int, sites_checked: int = 38) -> Digest:
    new = report["new"]
    new_tenders = report["new_tenders"]
    other_new = [r for r in new if not r["tender"]]
    closing = report["closing"]
    baseline = bool(run["baseline"])
    day = today.strftime("%d %b %Y")

    if baseline:
        tender_total = sum(1 for r in report["records"] if r["tender"])
        headline = (f"Pehli run (baseline) poori hui: {len(report['records'])} items mile, "
                    f"{tender_total} tender/RFP/EOI, {len(report['open_tenders'])} abhi open.")
    elif new:
        headline = f"{len(new_tenders)} naye tender/RFP/EOI, {len(other_new)} doosre updates."
    else:
        headline = "Sab websites check hui - aaj kuch naya nahi mila."
    subject = f"SSC Tenders - {day} - " + (
        f"{len(new_tenders)} naye tenders, {len(other_new)} updates" if not baseline else "baseline complete")

    counts: dict[str, int] = {}
    for r in other_new:
        counts[r["category"]] = counts.get(r["category"], 0) + 1

    # ---------------- plain text
    lines = [headline, ""]
    if new_tenders:
        lines.append("NAYE TENDERS / RFP / EOI:")
        for i, r in enumerate(new_tenders[:40], start=1):
            lines.append(f"{i}. [{r['org']}] {r['category']}: {r['title'][:160]}")
            lines.append(f"   Uploaded on: {uploaded_text(r) or '-'} | Last date: {_fmt(r['last_date'])} | "
                         f"Relevance: {r['relevance'] or '-'}")
            if r["action"]:
                lines.append(f"   What to do: {r['action'][:200]}")
            if r["url"]:
                lines.append(f"   {r['url']}")
        lines.append("")
    if counts:
        lines.append("Doosre naye updates: " + ", ".join(f"{k}: {v}" for k, v in sorted(counts.items())))
        lines.append("")
    if closing:
        lines.append(f"JALDI BAND HONE WALE ({closing_days} din ke andar):")
        for r in closing[:20]:
            lines.append(f"- [{r['org']}] {r['title'][:140]} - last date {_fmt(r['last_date'])}")
        lines.append("")
    if problems:
        lines.append("Website problems:")
        lines.extend(f"- {p}" for p in problems)
        lines.append("")
    if ai_summary and ai_summary.get("skipped_reason"):
        lines.append(f"AI note: {ai_summary['skipped_reason']}")
    if report.get("locked"):
        lines.append("Note: ye Excel files khuli thi, unme naye items agle run mein judenge: "
                     + ", ".join(Path(p).name for p in report["locked"]))
    lines.append(f"Excel: {report['master']}")
    text = "\n".join(lines)

    # ---------------- HTML (email)
    esc = html.escape
    subject, html_body = build_email(report, run, problems, ai_summary, today, closing_days, sites_checked)

    # ---------------- Telegram (HTML, 4096 chars per message)
    tg = [f"<b>SSC Tenders - {esc(day)}</b>\n{esc(headline)}"]
    for i, r in enumerate(new_tenders[:40], start=1):
        link = f" <a href=\"{esc(r['url'])}\">open</a>" if r["url"] else ""
        tg.append(f"{i}. <b>[{esc(r['org'])}]</b> {esc(r['category'])}: {esc(r['title'][:180])}\n"
                  f"   Uploaded: {uploaded_text(r) or '-'} | Last date: <b>{_fmt(r['last_date'])}</b>{link}")
    if counts:
        tg.append("Doosre updates: " + esc(", ".join(f"{k}: {v}" for k, v in sorted(counts.items()))))
    if closing:
        tg.append("<b>Jaldi band hone wale:</b>\n" + "\n".join(
            f"- [{esc(r['org'])}] {esc(r['title'][:120])} - {_fmt(r['last_date'])}" for r in closing[:15]))
    if problems:
        tg.append("<b>Website problems:</b>\n" + "\n".join(f"- {esc(p)}" for p in problems[:15]))
    messages, current = [], ""
    for block in tg:
        if len(current) + len(block) + 2 > 3900:
            messages.append(current)
            current = ""
        current = f"{current}\n\n{block}" if current else block
    if current:
        messages.append(current)

    toast = headline
    if closing:
        toast += f" {len(closing)} tender {closing_days} din mein band honge."
    return Digest(subject, headline, text, html_body, messages, toast, bool(new) or baseline)


def send_all(digest: Digest, report: dict, settings, test: bool = False) -> dict:
    from . import email_notify, telegram_notify, windows_notify

    results = {}
    when_empty = bool(settings.notify.get("when_empty", True))
    send_quiet = digest.has_news or when_empty or test
    attachment = report.get("daily")
    if send_quiet:
        results["email"] = _safe(email_notify.send, digest, [report.get("master"), attachment])
        results["telegram"] = _safe(telegram_notify.send, digest, attachment)
    if env_bool("WINDOWS_TOAST", True) and (digest.has_news or test):
        results["windows"] = _safe(windows_notify.send, digest, report.get("master"))
    return results


def _safe(fn, *args) -> str:
    try:
        return fn(*args)
    except Exception as exc:  # noqa: BLE001 - one channel failing must not stop the others
        log.exception("notification failed: %s", fn.__module__)
        return f"failed: {exc}"
