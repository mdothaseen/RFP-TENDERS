"""Professional HTML email for the daily digest (table layout + inline styles for email clients)."""

from __future__ import annotations

import html
from datetime import date
from pathlib import Path

NAVY = "#312E81"      # deep indigo
ACCENT = "#4F46E5"    # links, buttons, top bar
INK = "#0F172A"
BORDER = "#E2E8F0"
MUTED = "#64748B"
PAGE = "#F1F5F9"
REL_STYLE = {
    "High": ("#FDE2E1", "#B42318"),
    "Medium": ("#FEF0C7", "#B54708"),
    "Low": ("#EEF2F6", "#475467"),
}


def esc(value) -> str:
    return html.escape(str(value or ""))


def _fmt(d: date | None) -> str:
    return f"<span style='white-space:nowrap'>{d:%d %b %Y}</span>" if d else "—"


def uploaded_text(r: dict) -> str:
    """When the file was put on the website ('Sep 2026' when only the upload folder's month is known)."""
    d = r.get("uploaded")
    if not d:
        return ""
    return d.strftime("%b %Y") if r.get("uploaded_month_only") else d.strftime("%d %b %Y")


def _uploaded(r: dict) -> str:
    text = uploaded_text(r)
    if not text:
        return f"<span style='color:{MUTED}'>—</span>"
    if r.get("uploaded_month_only"):
        return f"<span style='white-space:nowrap'>{esc(text)}</span><div style='color:{MUTED};font-size:11px'>month only</div>"
    return f"<span style='white-space:nowrap'>{esc(text)}</span>"


def _days_left(d: date | None, today: date) -> str:
    if not d:
        return "—"
    n = (d - today).days
    if n < 0:
        return "Closed"
    if n == 0:
        return "<b style='color:#B42318'>Today</b>"
    color = "#B42318" if n <= 3 else "#B54708" if n <= 7 else "#1F2937"
    return f"<b style='color:{color}'>{n} day{'s' if n != 1 else ''}</b>"


def _pill(relevance: str) -> str:
    bg, fg = REL_STYLE.get(relevance or "", ("#EEF2F6", "#475467"))
    return (f"<span style='background:{bg};color:{fg};border-radius:999px;padding:3px 10px;"
            f"font-size:11px;font-weight:600;white-space:nowrap'>&#9679; {esc(relevance or 'n/a')}</span>")


def _chip(text: str, strong: bool = False) -> str:
    color, weight = (INK, "700") if strong else (MUTED, "500")
    return (f"<span style='background:#F8FAFC;border:1px solid {BORDER};border-radius:6px;padding:2px 7px;"
            f"font-size:12px;font-weight:{weight};color:{color};white-space:nowrap'>{esc(text)}</span>")


def _title(r: dict, limit: int = 150) -> str:
    """Title as a clickable link to the document (or the page it was found on), plus a clear 'View' link."""
    title = esc((r.get("title") or "")[:limit])
    link = r.get("url") or r.get("source")
    if not link:
        return f"<b>{title}</b>"
    is_doc = r.get("file_type") not in (None, "", "WEB")
    label = f"Open {esc(r['file_type'])}" if is_doc else "Open page"
    return (f"<a href='{esc(link)}' target='_blank' style='color:{INK};text-decoration:none;font-weight:600;"
            f"font-size:14px;line-height:1.4'>{title}</a><div style='margin-top:6px'><a href='{esc(link)}' "
            f"target='_blank' style='display:inline-block;font-size:12px;font-weight:600;color:{ACCENT};"
            f"text-decoration:none;border:1px solid #C7D2FE;background:#EEF2FF;border-radius:6px;padding:3px 10px'>"
            f"{label} &#8599;</a></div>")


def _card(value, label: str, color: str) -> str:
    return (f"<td width='25%' style='padding:6px'><div style='border:1px solid {BORDER};"
            f"border-top:3px solid {color};border-radius:12px;padding:16px 14px;background:#FFFFFF'>"
            f"<div style='font-size:12px;color:{MUTED};font-weight:500'>{esc(label)}</div>"
            f"<div style='font-size:30px;font-weight:700;color:{color};margin-top:6px;line-height:1'>{esc(value)}</div>"
            f"</div></td>")


def _section(title: str, subtitle: str = "") -> str:
    sub = f"<div style='font-size:12px;color:{MUTED}'>{esc(subtitle)}</div>" if subtitle else ""
    return (f"<tr><td style='padding:28px 28px 10px 28px'><div style='font-size:17px;font-weight:700;"
            f"color:{INK}'>{esc(title)}</div>{sub}</td></tr>")


def _section_html(title_html: str, subtitle: str = "") -> str:
    """Like _section, but the title may contain HTML entities (emoji dots)."""
    sub = f"<div style='font-size:12px;color:{MUTED};margin-top:2px'>{esc(subtitle)}</div>" if subtitle else ""
    return (f"<tr><td style='padding:28px 28px 10px 28px'><div style='font-size:17px;font-weight:700;"
            f"color:{INK}'>{title_html}</div>{sub}</td></tr>")


def _table(headers: list[str], rows: list[list[str]], widths: list[str] | None = None) -> str:
    th = "".join(
        f"<th align='left' style='background:#F8FAFC;color:{MUTED};font-size:11px;font-weight:600;"
        f"text-transform:uppercase;letter-spacing:.4px;padding:10px 12px;border-bottom:1px solid {BORDER};"
        f"{'width:' + widths[i] + ';' if widths else ''}'>{esc(h)}</th>" for i, h in enumerate(headers))
    body = []
    for n, row in enumerate(rows):
        bg = "#FFFFFF"
        tds = "".join(f"<td valign='top' style='padding:14px 12px;font-size:13px;color:{INK};"
                      f"border-bottom:1px solid {BORDER};"
                      f"background:{bg}'>{cell}</td>" for cell in row)
        body.append(f"<tr>{tds}</tr>")
    return (f"<tr><td style='padding:0 28px'><table width='100%' cellpadding='0' cellspacing='0' "
            f"style='border-collapse:separate;border-spacing:0;border:1px solid {BORDER};border-radius:12px;"
            f"overflow:hidden'><tr>{th}</tr>{''.join(body)}</table>"
            f"</td></tr>")


def build_email(report: dict, run, problems: list[str], ai_summary: dict | None, today: date,
                closing_days: int, sites_checked: int) -> tuple[str, str]:
    """Returns (subject, html)."""
    rank = {"High": 0, "Medium": 1, "Low": 2}
    # Tenders whose last date has already passed are left out of the email (they stay in Excel).
    already_closed = [r for r in report["new_tenders"] if r["last_date"] and r["last_date"] < today]
    new_tenders = sorted([r for r in report["new_tenders"] if r not in already_closed], key=lambda r: (
        rank.get(r["relevance"], 3), (r["last_date"] or date.max).toordinal()))
    other_new = [r for r in report["new"] if not r["tender"]]
    closing = report["closing"]
    open_tenders = report["open_tenders"]
    high_new = [r for r in new_tenders if r["relevance"] == "High"]
    urgent = [r for r in closing if r["relevance"] in ("High", "Medium")]
    baseline = bool(run["baseline"])

    # ---- verdict: does the reader need to open the Excel?
    if high_new or urgent:
        bits = []
        if high_new:
            bits.append(f"{len(high_new)} high-relevance new tender{'s' if len(high_new) != 1 else ''}")
        if urgent:
            bits.append(f"{len(urgent)} relevant tender{'s' if len(urgent) != 1 else ''} closing within {closing_days} days")
        verdict = ("#B42318", "#FEF3F2", "ACTION NEEDED", " and ".join(bits) + ". Please review the details below.")
    elif new_tenders:
        verdict = ("#B54708", "#FFFAEB", "REVIEW",
                   f"{len(new_tenders)} new tender{'s' if len(new_tenders) != 1 else ''} found, none marked high "
                   "relevance. A quick look at the table below is enough.")
    else:
        verdict = ("#067647", "#ECFDF3", "NO ACTION NEEDED",
                   "No new tenders today. You do not need to open the Excel.")
    if baseline:
        verdict = (NAVY, "#EEF4FF", "FIRST RUN COMPLETE",
                   f"All {sites_checked} websites were read for the first time. Tenders from the last 30 days are "
                   "listed below; from tomorrow you will only get what is new.")

    day = today.strftime("%d %b %Y")
    subject = f"SSC Tender Update | {day} | "
    if baseline:
        subject += "First run complete"
    elif new_tenders or closing:
        subject += f"{len(new_tenders)} new tender{'s' if len(new_tenders) != 1 else ''}"
        if closing:
            subject += f" · {len(closing)} closing this week"
        if high_new:
            subject = "[Action needed] " + subject
    else:
        subject += "No new tenders"

    failed = len(problems)
    color, bg, label, message = verdict
    rows = [
        f"<tr><td style='height:4px;background:{ACCENT};font-size:0;line-height:0'>&nbsp;</td></tr>",
        f"<tr><td style='padding:26px 28px 6px 28px'>"
        f"<div style='font-size:11px;font-weight:700;letter-spacing:1.2px;color:{ACCENT}'>TENDER TRACKER</div>"
        f"<div style='font-size:24px;font-weight:700;color:{INK};margin-top:6px'>SSC Tender &amp; Notice Update</div>"
        f"<div style='font-size:13px;color:{MUTED};margin-top:6px'>{esc(day)} &nbsp;&#183;&nbsp; {sites_checked} "
        f"websites checked &nbsp;&#183;&nbsp; Sector Skill Councils, NSDC, NCVET and awarding bodies</div></td></tr>",
        f"<tr><td style='padding:18px 28px 6px 28px'><div style='background:{bg};padding:14px 16px;"
        f"border-radius:12px'><div style='font-size:11px;font-weight:700;color:{color};letter-spacing:.8px'>"
        f"&#9679; {esc(label)}</div><div style='font-size:14px;color:{INK};margin-top:6px;line-height:1.5'>"
        f"{esc(message)}</div></div></td></tr>",
        "<tr><td style='padding:12px 22px'><table width='100%' cellpadding='0' cellspacing='0'><tr>"
        + _card(len(new_tenders), "New tenders / RFP / EOI", NAVY)
        + _card(len(closing), f"Closing in {closing_days} days", "#B42318" if closing else NAVY)
        + _card(len(open_tenders), "Open tenders in total", NAVY)
        + _card(f"{sites_checked - failed}/{sites_checked}", "Websites checked OK", "#B42318" if failed else "#067647")
        + "</tr></table></td></tr>",
    ]

    if new_tenders:
        def tender_rows(items):
            out = []
            for r in items:
                detail = _title(r)
                extra = r.get("action") or r.get("who") or ""
                if extra:
                    detail += f"<div style='color:{MUTED};font-size:12px;margin-top:6px'>{esc(extra[:180])}</div>"
                out.append([_chip(r["org"], True), _chip(r["category"]), detail, _uploaded(r),
                            _fmt(r["last_date"]), _days_left(r["last_date"], today), _pill(r["relevance"])])
            return out

        headers = ["Org", "Type", "Title / what to do", "Uploaded on", "Last date", "Time left", "Relevance"]
        widths = ["8%", "9%", "39%", "12%", "12%", "9%", "11%"]
        # Relevance groups: the reader starts with what fits the company; Low is only counted.
        groups = [
            ("High", "&#128308; For you: high relevance", "Matches your work directly. Review these first."),
            ("Medium", "&#128993; Maybe for you: medium relevance", "Related work you could bid for."),
        ]
        for level, heading, note in groups:
            items = [r for r in new_tenders if r["relevance"] == level]
            if not items:
                continue
            shown = items[:25]
            subtitle = note + " Click a title to open the document."
            if len(items) > len(shown):
                subtitle += f" Showing {len(shown)} of {len(items)}; the rest are in the attached Excel."
            rows.append(_section_html(heading, subtitle))
            rows.append(_table(headers, tender_rows(shown), widths))
        rest = [r for r in new_tenders if r["relevance"] not in ("High", "Medium")]
        if rest or already_closed:
            bits = []
            if rest:
                bits.append(f"<b>{len(rest)}</b> new tender{'s' if len(rest) != 1 else ''} with low or no relevance")
            if already_closed:
                bits.append(f"<b>{len(already_closed)}</b> with a last date already passed")
            rows.append(f"<tr><td style='padding:16px 28px 0 28px'><div style='background:#F8FAFC;border:1px solid "
                        f"{BORDER};border-radius:12px;padding:12px 16px;font-size:13px;color:{MUTED}'>&#9898; "
                        f"{' and '.join(bits)}: not listed here, all of them are in the attached Excel.</div></td></tr>")

    if closing:
        rows.append(_section(f"Closing within {closing_days} days", "Tenders already announced earlier."))
        rows.append(_table(["Org", "Title", "Uploaded on", "Last date", "Time left", "Relevance"], [
            [_chip(r["org"], True), _title(r, 130), _uploaded(r), _fmt(r["last_date"]),
             _days_left(r["last_date"], today), _pill(r["relevance"])] for r in closing[:15]],
            ["9%", "45%", "13%", "13%", "10%", "10%"]))

    if other_new:
        by_cat: dict[str, list[dict]] = {}
        for r in other_new:
            by_cat.setdefault(r["category"], []).append(r)
        cat_rows = []
        for cat, items in sorted(by_cat.items(), key=lambda kv: -len(kv[1])):
            examples = "<br>".join(f"<span style='color:{MUTED}'>{esc(r['org'])}:</span> {esc(r['title'][:90])}"
                                   for r in items[:2])
            cat_rows.append([f"<b>{esc(cat)}</b>", f"<b>{len(items)}</b>", examples])
        rows.append(_section("Other new updates", "Notices, circulars, recruitment, ToT/ToA and news (for information)."))
        rows.append(_table(["Type", "Count", "Examples"], cat_rows, ["20%", "10%", "70%"]))

    orgs: dict[str, list[int]] = {}
    for r in report["new"]:
        entry = orgs.setdefault(r["org"], [0, 0])
        entry[0 if r["tender"] else 1] += 1
    if orgs and not baseline:
        rows.append(_section("New items by organisation"))
        rows.append(_table(["Org", "New tenders", "Other updates"], [
            [f"<b>{esc(org)}</b>", str(c[0]), str(c[1])]
            for org, c in sorted(orgs.items(), key=lambda kv: (-kv[1][0], -kv[1][1]))], ["40%", "30%", "30%"]))

    notes = []
    if problems:
        notes.append("<b>Websites that could not be read:</b><br>" + "<br>".join(esc(p) for p in problems[:10]))
    if ai_summary and ai_summary.get("skipped_reason"):
        notes.append(f"<b>AI note:</b> {esc(ai_summary['skipped_reason'])}")
    if report.get("locked"):
        notes.append("<b>Excel files were open during the update</b>; their new rows will be added in the next run.")
    if notes:
        rows.append(_section("Notes"))
        rows.append(f"<tr><td style='padding:0 24px;font-size:13px;color:#1F2937'>{'<br><br>'.join(notes)}</td></tr>")

    attachment = (f"Today's new items: attached <b>{esc(Path(report['daily']).name)}</b>. "
                  if report.get("daily") else "")
    rows.append(
        f"<tr><td style='padding:24px 28px 28px 28px;font-size:12px;line-height:1.6;color:{MUTED}'>"
        f"{attachment}Full history: attached <b>{esc(Path(report['master']).name)}</b> (on the laptop: "
        f"{esc(report['master'])}, one folder per organisation with its Excel and documents).<br><b>Uploaded on</b> = the date the file was put on the website (from the website's media "
        f"library, the file name or the web server); &#8212; means the website does not show it.<br>"
        f"Relevance is judged by AI against your company profile. Always check the original "
        f"document before applying.</td></tr>")

    body = (
        f"<html><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>"
        f"</head><body style='margin:0;padding:0;background:{PAGE}'>"
        f"<table width='100%' cellpadding='0' cellspacing='0' style='background:{PAGE};padding:12px 0'>"
        "<tr><td align='center'>"
        "<table width='100%' cellpadding='0' cellspacing='0' style='max-width:1200px;width:100%;background:#FFFFFF;"
        "font-family:Inter,Segoe UI,Roboto,Arial,sans-serif;border:1px solid #E2E8F0;border-radius:16px;"
        "overflow:hidden'>"
        + "".join(rows)
        + "</table></td></tr></table></body></html>"
    )
    return subject, body
