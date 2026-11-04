"""HTML summary email (table layout + inline styles for email clients).

Layout: verdict, one summary row per category (Focused / Regular / Low), tenders closing soon, then one section per
category: the status of every organisation in it, what is new organisation by organisation, the organisations with
nothing new, and the websites that could not be read (with the reason).
"""

from __future__ import annotations

import html
import re
from datetime import date
from pathlib import Path

from ..config import TIERS

ACCENT = "#4F46E5"
INK = "#0F172A"
BORDER = "#E2E8F0"
MUTED = "#64748B"
RED, AMBER, GREEN_INK = "#B42318", "#B54708", "#067647"
REL_STYLE = {"High": ("#FDE2E1", RED), "Medium": ("#FEF0C7", AMBER), "Low": ("#EEF2F6", "#475467")}
TIER_STYLE = {"Focused": (RED, "#FEF3F2"), "Regular": (AMBER, "#FFFAEB"), "Low": ("#475467", "#F8FAFC")}
TIER_NOTE = {"Focused": "Your priority organisations: full details.",
             "Regular": "Every organisation's status, with details of what is new.",
             "Low": "Every organisation's status, with the main new items."}
DETAIL_ROWS = {"Focused": 15, "Regular": 8, "Low": 5}   # new items listed per organisation
GREEN_CHIP = ("#ECFDF3", GREEN_INK, "#ABEFC6")
RED_CHIP = ("#FEF3F2", RED, "#FECDCA")
NOT_READ = ("failed", "partial")
TD = f"padding:8px 10px;border-bottom:1px solid {BORDER}"   # font and colour come from the outer table
SMALL = f"color:{MUTED};font-size:11px;line-height:1.35"


def esc(value) -> str:
    return html.escape(str(value or ""))


def uploaded_text(r: dict) -> str:
    """When the file was put on the website ('Sep 2026' when only the upload folder's month is known)."""
    d = r.get("uploaded")
    if not d:
        return ""
    return d.strftime("%b %Y") if r.get("uploaded_month_only") else d.strftime("%d %b %Y")


def _plural(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


def _num(n: int, color: str) -> str:
    return f"<b style='color:{color}'>{n}</b>" if n else f"<span style='color:{MUTED}'>0</span>"


def _chip(text: str, bg: str = "#F8FAFC", fg: str = INK, border: str = BORDER) -> str:
    return (f"<span style='display:inline-block;margin:2px;background:{bg};color:{fg};border:1px solid {border};"
            f"border-radius:6px;padding:2px 7px;font-size:12px;font-weight:600'>{esc(text)}</span>")


def _pill(relevance: str) -> str:
    bg, fg = REL_STYLE.get(relevance or "", REL_STYLE["Low"])
    return (f"<span style='background:{bg};color:{fg};border-radius:999px;padding:2px 9px;font-size:11px;"
            f"font-weight:600;white-space:nowrap'>{esc(relevance or 'n/a')}</span>")


def _last_date(d: date | None, today: date) -> str:
    if not d:
        return f"<span style='color:{MUTED}'>&#8212;</span>"
    n = (d - today).days
    color = RED if n <= 3 else AMBER if n <= 7 else INK
    left = "Today" if n == 0 else "Closed" if n < 0 else f"{_plural(n, 'day')} left"
    return f"<span style='white-space:nowrap'>{d:%d %b %Y}</span><div style='color:{color};font-size:11px;font-weight:700'>{left}</div>"


def _uploaded(r: dict) -> str:
    text = uploaded_text(r)
    if not text:
        return f"<span style='color:{MUTED}'>&#8212;</span>"
    extra = f"<div style='{SMALL}'>month only</div>" if r.get("uploaded_month_only") else ""
    return f"<span style='white-space:nowrap'>{esc(text)}</span>{extra}"


def _link(r: dict, limit: int = 160) -> str:
    title = esc((r.get("title") or "")[:limit])
    target = r.get("url") or r.get("source")
    if not target:
        return f"<b>{title}</b>"
    return f"<a href='{esc(target)}' target='_blank' style='color:{INK};font-weight:600;text-decoration:none'>{title}</a>"


def _facts(r: dict) -> str:
    """What the AI read in the document: scope, who can apply, money, reference and what to do."""
    out = ""
    if r.get("summary"):
        out += f"<div style='color:#334155;font-size:12px;margin-top:5px;line-height:1.45'>{esc(r['summary'][:260])}</div>"
    if r.get("who"):
        out += f"<div style='font-size:12px;margin-top:4px'><b style='color:{MUTED}'>Who can apply:</b> {esc(r['who'][:180])}</div>"
    money = [(k, r.get(f)) for k, f in (("Ref", "ref"), ("EMD", "emd"), ("Fee", "fee"), ("Value", "value")) if r.get(f)]
    if money:
        out += "<div style='margin-top:5px'>" + "".join(_chip(f"{k}: {str(v)[:40]}") for k, v in money) + "</div>"
    if r.get("action"):
        out += f"<div style='font-size:12px;margin-top:5px;color:{ACCENT};font-weight:600'>&#10148; {esc(r['action'][:180])}</div>"
    return out


def _relevance(r: dict) -> str:
    why = r.get("relevance_reason") or ""
    return _pill(r.get("relevance")) + (f"<div style='{SMALL};margin-top:4px'>{esc(why[:110])}</div>" if why else "")


def _table(headers: list[str], rows: list[list[str]], widths: list[str]) -> str:
    th = "".join(f"<th align='left' style='background:#F8FAFC;color:{MUTED};font-size:11px;font-weight:600;"
                 f"text-transform:uppercase;padding:8px 10px;border-bottom:1px solid {BORDER};width:{w}'>{esc(h)}</th>"
                 for h, w in zip(headers, widths))
    body = "".join("<tr>" + "".join(f"<td valign='top' style='{TD}'>{c}</td>" for c in row) + "</tr>" for row in rows)
    return (f"<table width='100%' cellpadding='0' cellspacing='0' style='border:1px solid {BORDER};border-radius:10px;"
            f"border-collapse:separate;border-spacing:0;overflow:hidden;font-size:13px;color:{INK}'><tr>{th}</tr>{body}</table>")


def _short_url(url: str) -> str:
    return re.sub(r"^https?://(www\.)?", "", url or "")[:55]


def _order(items: list[dict]) -> list[dict]:
    rank = {"High": 0, "Medium": 1, "Low": 2}
    return sorted(items, key=lambda r: (bool(r.get("page_change")), not r["tender"], rank.get(r["relevance"], 3),
                                        (r["last_date"] or date.max).toordinal()))


class _Category:
    """Everything one category (Focused / Regular / Low) shows in the email."""

    def __init__(self, name: str, sites: list[dict], items: list[dict], today: date):
        self.name, self.sites, self.today = name, sites, today
        self.items = _order(items)
        self.by_org: dict[str, list[dict]] = {}
        for r in self.items:
            self.by_org.setdefault(r["org"], []).append(r)
        self.failed = [s for s in sites if s["status"] in NOT_READ]
        self.quiet = [s for s in sites if s["code"] not in self.by_org and s["status"] not in NOT_READ]

    def count(self, test) -> int:
        return sum(1 for r in self.items if test(r))

    def closing(self, r: dict, days: int = 7) -> bool:
        return bool(r["last_date"] and 0 <= (r["last_date"] - self.today).days <= days)


def _site_cell(s: dict) -> str:
    if s["status"] in NOT_READ:
        word = "partly read" if s["status"] == "partial" else "not read"
        return f"<b style='color:{RED}'>&#10007; {word}</b><div style='{SMALL}'>{esc(s['reason'])}</div>"
    if s["status"] == "ok":
        return f"<b style='color:{GREEN_INK}'>&#10003; OK</b>"
    return f"<span style='color:{MUTED}'>{esc(s['reason'] or s['status'])}</span>"


def _org_cell(s: dict) -> str:
    return (f"<a href='{esc(s['url'])}' target='_blank' style='color:{INK};font-weight:700;text-decoration:none'>"
            f"{esc(s['code'])}</a><div style='{SMALL}'>{esc(s['name'][:60])}</div>")


def _scoreboard(cat: _Category) -> str:
    def key(s):
        its = cat.by_org.get(s["code"], [])
        return (-sum(r["tender"] for r in its), -len(its), s["status"] not in NOT_READ, s["code"])

    rows = []
    for s in sorted(cat.sites, key=key):
        its = cat.by_org.get(s["code"], [])
        real = [r for r in its if not r.get("page_change")]
        n_t = sum(r["tender"] for r in real)
        pages = len(its) - len(real)
        if real:
            latest = _link(real[0], 80)
        else:
            latest = f"<span style='color:{MUTED}'>{'Only page changes' if pages else 'Nothing new'}</span>"
        rows.append([_org_cell(s), _site_cell(s), _num(n_t, RED), _num(len(real) - n_t, INK), _num(pages, MUTED),
                     _num(sum(cat.closing(r) for r in real if r["tender"]), AMBER), latest])
    return _table(["Organisation", "Website", "Tenders", "Updates", "Pages changed", "Closing ≤7d", "Latest"], rows,
                  ["19%", "15%", "8%", "8%", "9%", "9%", "32%"])


def _org_details(cat: _Category, names: dict, urls: dict) -> str:
    out = []
    ordered = sorted(cat.by_org.items(), key=lambda kv: (-sum(r["tender"] for r in kv[1]), -len(kv[1]), kv[0]))
    for code, its in ordered:
        real = [r for r in its if not r.get("page_change")]
        pages = [r for r in its if r.get("page_change")]
        n_t = sum(r["tender"] for r in real)
        counts = " · ".join(x for x in (_plural(n_t, "tender") if n_t else "",
                                        _plural(len(real) - n_t, "update") if len(real) - n_t else "",
                                        _plural(len(pages), "page change") if pages else "") if x)
        name = names.get(code) or its[0].get("org_name") or code
        site_link = (f" <a href='{esc(urls[code])}' target='_blank' style='font-size:12px;color:{ACCENT}'>website &#8599;</a>"
                     if urls.get(code) else "")
        out.append(f"<div style='margin:18px 0 6px;padding-top:10px;border-top:1px dashed {BORDER}'>"
                   f"<span style='font-size:15px;font-weight:800;color:{INK}'>{esc(code)}</span> <span style='color:{MUTED};"
                   f"font-size:12px'>{esc(name if name != code else '')}{' · ' if name != code else ''}{counts}</span>"
                   f"{site_link}</div>")
        shown = real[:DETAIL_ROWS[cat.name]]
        if shown:
            out.append(_table(["Type", "Title and details", "Uploaded", "Last date", "Relevance"],
                              [[_chip(r["category"]), _link(r) + _facts(r), _uploaded(r), _last_date(r["last_date"], cat.today),
                                _relevance(r)] for r in shown], ["10%", "52%", "11%", "12%", "15%"]))
        if len(real) > len(shown):
            out.append(f"<div style='font-size:12px;color:{MUTED};margin-top:4px'>+ {len(real) - len(shown)} more from "
                       f"{esc(code)} in the attached Excel</div>")
        if pages:
            links = " · ".join(f"<a href='{esc(p['url'])}' target='_blank' style='color:{ACCENT}'>{esc(_short_url(p['url']))}</a>"
                               for p in pages[:6])
            more = f" · +{len(pages) - 6} more" if len(pages) > 6 else ""
            out.append(f"<div style='margin-top:6px;font-size:12px;color:{MUTED}'>&#8635; <b>{_plural(len(pages), 'page')} "
                       f"changed</b> (no new document found on them): {links}{more}</div>")
    return "".join(out)


def _status_lists(cat: _Category) -> str:
    out = (f"<div style='margin-top:16px;font-size:13px'><b style='color:{GREEN_INK}'>&#10003; Checked, nothing new "
           f"({len(cat.quiet)}):</b><br>" + ("".join(_chip(s["code"], *GREEN_CHIP) for s in cat.quiet) or "&#8212;") + "</div>")
    if cat.failed:
        out += (f"<div style='margin-top:10px;font-size:13px'><b style='color:{RED}'>&#9888; Could not be read, please check "
                f"these websites yourself ({len(cat.failed)}):</b>"
                + "".join(f"<div style='margin:3px 0'>{_chip(s['code'], *RED_CHIP)} <a href='{esc(s['url'])}' target='_blank' "
                          f"style='color:{ACCENT};font-size:12px'>{esc(s['url'])}</a> <span style='color:{MUTED};font-size:12px'>"
                          f"· {esc(s['reason'])}</span></div>" for s in cat.failed) + "</div>")
    return out


def _site_list(report: dict, items: list[dict]) -> list[dict]:
    """Every website with its latest status; without a status list (tests), the organisations seen in the items."""
    if report.get("site_status"):
        return report["site_status"]
    seen = {}
    for r in items:
        seen.setdefault(r["org"], {"code": r["org"], "name": r.get("org_name") or r["org"], "url": "",
                                   "tier": r.get("tier") or "Regular", "status": "ok", "reason": ""})
    return list(seen.values())


def build_email(report: dict, run, problems: list[str], ai_summary: dict | None, today: date,
                closing_days: int, sites_checked: int) -> tuple[str, str]:
    """Returns (subject, html)."""
    baseline = bool(run["baseline"])
    already_closed = [r for r in report["new"] if r["last_date"] and r["last_date"] < today]
    new = [r for r in report["new"] if not (r["last_date"] and r["last_date"] < today)]
    sites = _site_list(report, new)
    tier_of = {s["code"]: s["tier"] for s in sites}
    names = {s["code"]: s["name"] for s in sites}
    urls = {s["code"]: s["url"] for s in sites if s["url"]}
    cats = {t: _Category(t, [s for s in sites if s["tier"] == t],
                         [r for r in new if tier_of.get(r["org"], "Regular") == t], today) for t in TIERS}
    closing = sorted(report["closing"], key=lambda r: (TIERS.index(tier_of.get(r["org"], "Regular")), r["last_date"]))
    focused_tenders = cats["Focused"].count(lambda r: r["tender"])
    high_new = sum(c.count(lambda r: r["tender"] and r["relevance"] == "High") for c in cats.values())
    urgent = [r for r in closing if r["relevance"] in ("High", "Medium")]
    with_news = sum(len(c.by_org) for c in cats.values())
    not_read = sum(len(c.failed) for c in cats.values())
    day = today.strftime("%d %b %Y")

    # ---- verdict and subject
    if baseline:
        verdict = ("#312E81", "#EEF4FF", "FIRST RUN COMPLETE",
                   f"All {sites_checked} websites were read for the first time. Tenders from the last 30 days are "
                   "listed below; from the next scan you will only get what is new.")
        subject = f"SSC Tender Update | {day} | First run complete"
    else:
        if focused_tenders or high_new or urgent:
            bits = []
            if focused_tenders:
                bits.append(f"{_plural(focused_tenders, 'new tender')} from your focused organisations")
            if high_new:
                bits.append(_plural(high_new, "high-relevance new tender"))
            if urgent:
                bits.append(f"{_plural(len(urgent), 'relevant tender')} closing within {closing_days} days")
            verdict = (RED, "#FEF3F2", "ACTION NEEDED", "; ".join(bits) + ".")
        elif with_news:
            verdict = (AMBER, "#FFFAEB", "REVIEW", f"{_plural(with_news, 'organisation')} with something new, "
                       "none of it urgent. The status of every website is below.")
        else:
            verdict = (GREEN_INK, "#ECFDF3", "NO ACTION NEEDED", "Nothing new on any website. The status of every "
                       "website is below.")
        subject = (f"SSC Tender Update | {day} | Focused: {_plural(focused_tenders, 'new tender')} · "
                   f"{with_news} orgs with news · {len(closing)} closing in {closing_days} days")
        if verdict[2] == "ACTION NEEDED":
            subject = "[Action needed] " + subject

    color, bg, label, message = verdict
    o = [f"<tr><td style='height:4px;background:{ACCENT};font-size:0;line-height:0'>&nbsp;</td></tr><tr><td style='padding:16px 8px 20px'>",
         f"<div style='font-size:11px;font-weight:800;letter-spacing:1.2px;color:{ACCENT}'>TENDER TRACKER</div>",
         f"<div style='font-size:24px;font-weight:800;color:{INK};margin-top:4px'>SSC Tender &amp; Notice Update · {esc(day)}</div>",
         f"<div style='color:{MUTED};font-size:13px;margin:4px 0 12px'>All {len(sites) or sites_checked} websites checked · "
         f"{len(sites) - not_read} read OK · {not_read} not read · {_plural(with_news, 'organisation')} with something new</div>",
         f"<div style='background:{bg};border-radius:10px;padding:12px 14px;margin-bottom:12px'><div style='font-size:11px;"
         f"font-weight:800;color:{color};letter-spacing:.8px'>&#9679; {esc(label)}</div><div style='font-size:14px;color:{INK};"
         f"margin-top:4px;line-height:1.5'>{esc(message)}</div></div>"]

    summary = []
    for t, c in cats.items():
        summary.append([f"<b style='color:{TIER_STYLE[t][0]}'>{t}</b>", str(len(c.sites)),
                        _num(len(c.sites) - len(c.failed), GREEN_INK), _num(len(c.failed), RED), _num(len(c.by_org), INK),
                        _num(c.count(lambda r: r["tender"]), RED),
                        _num(c.count(lambda r: not r["tender"] and not r.get("page_change")), INK),
                        _num(sum(1 for r in closing if tier_of.get(r["org"], "Regular") == t), AMBER)])
    o.append(_table(["Category", "Websites", "Read OK", "Not read", "Orgs with news", "New tenders", "Other updates",
                     f"Closing ≤{closing_days}d"], summary, ["14%", "11%", "11%", "11%", "13%", "13%", "13%", "14%"]))

    if closing:
        o.append(f"<div style='margin:22px 0 8px;font-size:16px;font-weight:800;color:{INK}'>&#9200; Closing within "
                 f"{closing_days} days <span style='font-weight:400;font-size:12px;color:{MUTED}'>all categories, "
                 "including tenders announced earlier</span></div>")
        o.append(_table(["Category", "Org", "Title and details", "Last date", "Relevance"], [
            [f"<b style='color:{TIER_STYLE[tier_of.get(r['org'], 'Regular')][0]};font-size:12px'>"
             f"{tier_of.get(r['org'], 'Regular')}</b>", _chip(r["org"]), _link(r, 140) + _facts(r),
             _last_date(r["last_date"], today), _relevance(r)] for r in closing[:25]],
            ["10%", "9%", "54%", "12%", "15%"]))
        if len(closing) > 25:
            o.append(f"<div style='font-size:12px;color:{MUTED};margin-top:4px'>+ {len(closing) - 25} more in the attached Excel</div>")

    for t, c in cats.items():
        if not c.sites and not c.items:
            continue
        fg, tint = TIER_STYLE[t]
        o.append(f"<div style='margin:34px 0 8px;background:{tint};border-left:6px solid {fg};padding:12px 16px;"
                 f"border-radius:10px'><div style='font-size:18px;font-weight:800;color:{fg}'>{t.upper()} ORGANISATIONS</div>"
                 f"<div style='font-size:13px;color:{INK};margin-top:2px'>{len(c.sites)} checked · <b>{len(c.by_org)}</b> with "
                 f"news · {len(c.quiet)} nothing new · <b style='color:{RED}'>{len(c.failed)}</b> not read</div>"
                 f"<div style='font-size:12px;color:{MUTED}'>{TIER_NOTE[t]}</div></div>")
        if c.sites:
            o.append(f"<div style='font-size:13px;font-weight:700;color:{INK};margin:12px 0 6px'>1. Status of every "
                     f"{t.lower()} organisation</div>")
            o.append(_scoreboard(c))
        o.append(f"<div style='font-size:13px;font-weight:700;color:{INK};margin:20px 0 0'>2. What is new, organisation by "
                 "organisation</div>")
        o.append(_org_details(c, names, urls) or f"<div style='color:{MUTED};font-size:13px;margin-top:6px'>Nothing new "
                 "in this category.</div>")
        if c.sites:
            o.append(_status_lists(c))

    if already_closed:
        o.append(f"<div style='margin-top:20px;background:#F8FAFC;border:1px solid {BORDER};border-radius:10px;padding:10px 14px;"
                 f"font-size:13px;color:{MUTED}'>{_plural(len(already_closed), 'new item')} with a last date already passed: "
                 "not listed here, they are in the attached Excel.</div>")

    # Failed websites are already shown in their category; the notes keep the rest (e.g. Claude logged out).
    notes = [esc(p) for p in problems if not (report.get("site_status") and "website could not be read" in p)]
    if ai_summary and ai_summary.get("skipped_reason"):
        notes.append(f"<b>AI note:</b> {esc(ai_summary['skipped_reason'])}")
    if report.get("locked"):
        notes.append("<b>Excel files were open during the update</b>; their new rows will be added in the next run.")
    if notes:
        o.append(f"<div style='margin-top:22px;font-size:16px;font-weight:800;color:{INK}'>Notes</div>"
                 f"<div style='font-size:13px;color:#1F2937;margin-top:6px;line-height:1.5'>{'<br>'.join(notes)}</div>")

    attachment = f"Today's new items: attached <b>{esc(Path(report['daily']).name)}</b>. " if report.get("daily") else ""
    o.append(f"<div style='margin-top:28px;padding-top:14px;border-top:1px solid {BORDER};font-size:12px;color:{MUTED};"
             f"line-height:1.6'>{attachment}Full history: attached <b>{esc(Path(report['master']).name)}</b> (on the laptop: "
             f"{esc(report['master'])}).<br><b>Uploaded</b> = the date the file was put on the website; &#8212; means the "
             "website does not show it. <b>Pages changed</b> = the page text changed but no new document was found on it.<br>"
             "Relevance is judged by AI against your company profile. Always check the original document before applying."
             "</div></td></tr>")

    # Full width of the mail window: no centred max-width column and no grey margin around it.
    body = (f"<html><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'></head>"
            f"<body style='margin:0;padding:0;background:#FFFFFF'><table width='100%' cellpadding='0' cellspacing='0' "
            f"style='width:100%;background:#FFFFFF;font-family:Segoe UI,Roboto,Arial,sans-serif;font-size:13px;color:{INK}'>"
            f"{''.join(o)}</table></body></html>")
    return subject, body
