"""Excel output.

output/MASTER_All_SSC.xlsx            every organisation (Dashboard, Tenders-RFP-EOI, All Updates, Site Status)
output/<ORG>/<ORG>_Updates.xlsx       one organisation (Tenders-RFP-EOI, All Updates) + output/<ORG>/Documents/
output/New_Updates/New_<date>.xlsx    only the items found that day (only on days with new items)

The data sheets are never rebuilt: each run appends new rows and updates only the rows whose values
changed (e.g. AI details added, last date extended), matched by the ID column. Columns you add yourself
(e.g. "Applied?") are left untouched. Dashboard and Site Status are summaries and are refreshed every run.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path

from openpyxl import Workbook, load_workbook
from openpyxl.cell.cell import ILLEGAL_CHARACTERS_RE
from openpyxl.formatting.formatting import ConditionalFormattingList
from openpyxl.formatting.rule import FormulaRule
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

from .ai_extract import ai_fields
from .classify import category_rank
from .config import NEW_DIR, OUTPUT_DIR, ROOT, Keywords, Settings, Site, site_dir
from .dates import parse_any, parse_iso

HEADER_FILL = PatternFill("solid", fgColor="1F4E78")
HEADER_FONT = Font(bold=True, color="FFFFFF")
TODAY_FILL = PatternFill("solid", fgColor="FFF2CC")
RED_FILL = PatternFill("solid", fgColor="F8CBAD")
ORANGE_FILL = PatternFill("solid", fgColor="FCE4D6")
GREY_FONT = Font(color="808080")
LINK_FONT = Font(color="0563C1", underline="single")
WRAP = Alignment(wrap_text=True, vertical="top")
TOP = Alignment(vertical="top")

MASTER_NAME = "MASTER_All_SSC.xlsx"
VIA_LABELS = {
    "page": "Website page", "ticker": "Scrolling ticker", "script": "Page data", "sitemap": "Sitemap",
    "wp-media": "Media library", "wp-post": "WordPress post", "wp-search": "Site search",
    "change": "Page content changed",
}
AI_TYPE_MAP = {"RFQ": "RFP", "Circular": "Notice", "Result": "ToT-ToA/Result"}


@dataclass
class Col:
    header: str
    key: str
    width: int
    kind: str = "text"   # text | wrap | date | link | file | days | status | sno


TENDER_COLS = [
    Col("S.No", "sno", 6, "sno"), Col("Found On", "first_seen", 11, "date"), Col("Org", "org", 10),
    Col("Category", "category", 13), Col("Title", "title", 55, "wrap"), Col("Published", "published", 12, "date"),
    Col("Uploaded On", "uploaded", 12, "date"),
    Col("Last Date", "last_date", 12, "date"), Col("Days Left", "days", 9, "days"),
    Col("Status", "status", 13, "status"), Col("Time", "last_time", 8), Col("Pre-bid", "prebid", 12, "date"),
    Col("EMD", "emd", 16, "wrap"), Col("Fee", "fee", 14, "wrap"), Col("Who Can Apply", "who", 30, "wrap"),
    Col("Eligibility", "eligibility", 50, "wrap"), Col("Summary", "summary", 55, "wrap"),
    Col("What To Do", "action", 40, "wrap"), Col("Ref No", "ref", 18, "wrap"),
    Col("How To Submit", "submit", 30, "wrap"), Col("Contact", "contact", 26, "wrap"),
    Col("Relevance", "relevance", 10), Col("Why", "relevance_reason", 30, "wrap"),
    Col("Document", "url", 11, "link"), Col("Local File", "local", 11, "file"),
    Col("Found On Page", "source", 12, "link"), Col("Found Via", "via", 18, "wrap"), Col("Hidden?", "hidden", 8),
    Col("Notes", "notes", 35, "wrap"), Col("ID", "id", 8),
]
UPDATE_COLS = [
    Col("S.No", "sno", 6, "sno"), Col("Found On", "first_seen", 11, "date"), Col("Org", "org", 10),
    Col("Category", "category", 13), Col("Title", "title", 60, "wrap"), Col("Published", "published", 12, "date"),
    Col("Uploaded On", "uploaded", 12, "date"),
    Col("Last Date", "last_date", 12, "date"), Col("Relevance", "relevance", 10), Col("Type", "file_type", 7),
    Col("Document / Link", "url", 13, "link"), Col("Local File", "local", 11, "file"),
    Col("Found On Page", "source", 12, "link"), Col("Found Via", "via", 18, "wrap"), Col("Hidden?", "hidden", 8),
    Col("Summary", "summary", 50, "wrap"), Col("Notes", "notes", 30, "wrap"), Col("ID", "id", 8),
]
SHEETS = [("Tenders-RFP-EOI", TENDER_COLS, lambda r: r["tender"]), ("All Updates", UPDATE_COLS, lambda r: True)]


def _clean(value):
    if isinstance(value, str):
        return ILLEGAL_CHARACTERS_RE.sub("", value)[:3000]
    return value


def _via_text(found_via: str | None) -> str:
    parts = [VIA_LABELS.get(v, v) for v in (found_via or "").split(",") if v]
    return ", ".join(dict.fromkeys(parts))


# ---------------------------------------------------------------------------------------------- records
def build_records(store, keywords: Keywords, settings: Settings, sites: dict[str, Site], run, today: date,
                  where: str = "1=1", params: tuple = ()) -> list[dict]:
    """One dict per item that belongs in Excel (not 'Other', not older than the Excel window)."""
    max_age = int(settings.excel.get("baseline_max_age_days", 365))
    # include_all: every item found on the websites goes into Excel (old ones and "Other" documents too).
    include_all = bool(settings.excel.get("include_all", True))
    oldest = (today - timedelta(days=max_age)).isoformat()
    run_id = run["id"]
    records = []
    extensions = {}
    for child in sorted(store.items("related_item_id IS NOT NULL AND removed=0"), key=lambda r: (r["first_seen"], r["id"])):
        answer = ai_fields(child)
        if (child["content_category"] or child["category"]) != "Corrigendum" and answer.get("doc_type") != "Corrigendum":
            continue
        new_date = parse_any(answer.get("last_date_submission")) or parse_iso(child["deadline_hint"])
        if new_date:
            extensions[child["related_item_id"]] = (new_date, child["id"])
        elif child["ai_status"] != "done":
            extensions[child["related_item_id"]] = (None, child["id"])
    for row in store.items(where, params):
        if row["duplicate_of"]:
            continue  # the same file is already listed under its first address
        ai = ai_fields(row)
        category = row["content_category"] or row["category"] or "Other"
        ai_type = AI_TYPE_MAP.get(ai.get("doc_type", ""), ai.get("doc_type", ""))
        if ai_type and ai_type != "Other":
            category = ai_type
        published = parse_iso(row["published_date"])
        if not include_all:
            if category == "Other":
                continue
            if published and row["published_date"] < oldest and not row["xl_master_hash"]:
                continue
        first_seen = parse_iso(row["first_seen"])
        last_date = parse_any(ai.get("last_date_submission")) if ai else None
        last_source = "AI" if last_date else ""
        if not last_date and row["deadline_hint"]:
            last_date, last_source = parse_iso(row["deadline_hint"]), "website"
        if row["deadline_override"]:
            last_date, last_source = parse_iso(row["deadline_override"]), "updated website listing"
        if row["id"] in extensions:
            last_date, child_id = extensions[row["id"]]
            last_source = f"Corrigendum #{child_id}" + (" (verification pending)" if last_date is None else "")
        old_find = bool(published and published < today - timedelta(days=60))
        if row["baseline"]:
            fresh = bool(published and published >= today - timedelta(days=30))
        else:
            fresh = not old_find
        notes = []
        if row["ai_status"] == "done":
            if row["ai_partial"]:
                notes.append("AI read only the first part")
        elif category in keywords.tender_categories:
            if row["ai_status"] == "failed":
                notes.append(f"AI could not read: {row['ai_error'] or ''}")
            elif row["ai_status"] == "skipped" and row["ai_error"]:
                notes.append(row["ai_error"])
            else:
                notes.append("Not read by AI yet")
        if row["scanned"] and row["doc_status"] == "ok" and row["doc_error"] != "scanned PDF - text read by OCR":
            notes.append("scanned PDF")
        if row["doc_error"] and row["doc_status"] != "ok":
            notes.append(f"download: {row['doc_error']}")
        if row["removed"]:
            notes.append("no longer on the website")
        if last_source == "website":
            notes.append("last date taken from website text")
        if row["uploaded_source"] == "upload folder (month only)":
            notes.append("upload month only (from file address)")
        if not row["baseline"] and old_find:
            notes.append("old document, found for the first time")
        site = sites.get(row["site_code"])
        records.append({
            "id": row["id"],
            # New = never sent in an email yet (also covers items of a run that stopped half-way).
            "is_new": category != "Other" and fresh and (not row["notified"] if not run["baseline"] else row["first_run_id"] == run_id),
            "today_new": first_seen == today and fresh,
            "org": row["site_code"],
            "org_name": site.name if site else row["site_code"],
            "category": category,
            "title": ai.get("title") or row["title"] or "",
            "published": published,
            "uploaded": parse_iso(row["uploaded_date"]),
            "uploaded_month_only": row["uploaded_source"] == "upload folder (month only)",
            "last_date": last_date,
            "last_source": last_source,
            "status": row["user_status"],
            "related_item_id": row["related_item_id"],
            "revision": row["revision"],
            "last_time": ai.get("last_time_submission", ""),
            "prebid": parse_any(ai.get("prebid_date")) if ai.get("prebid_date") else None,
            "emd": ai.get("emd", ""),
            "fee": ai.get("tender_fee", ""),
            "who": ai.get("who_can_apply", ""),
            "eligibility": ai.get("eligibility_summary", ""),
            "summary": ai.get("scope_summary", "") or (row["context"] if row["context"] != row["title"] else ""),
            "ref": ai.get("reference_no", ""),
            "submit": ai.get("submission_mode", ""),
            "contact": ai.get("contact", ""),
            "action": ai.get("action_needed", ""),
            "relevance": ai.get("relevance") or row["relevance"] or "",
            "relevance_reason": ai.get("relevance_reason", ""),
            "url": row["url"],
            "local": row["local_path"],
            # Media-library / sitemap finds have no page that links to them.
            "source": row["source_page"] if row["linked_seen"] else "",
            "via": _via_text(row["found_via"]),
            "hidden": "Yes" if row["hidden"] else "",
            "first_seen": first_seen,
            "notes": "; ".join(n for n in notes if n),
            "tender": category in keywords.tender_categories,
            "file_type": (row["file_ext"] or "").upper() if row["is_document"] else "WEB",
            "hash_master": row["xl_master_hash"],
            "hash_site": row["xl_site_hash"],
        })
    return records


def record_hash(rec: dict) -> str:
    keys = sorted({c.key for c in TENDER_COLS + UPDATE_COLS} - {"sno", "days", "status"})
    payload = json.dumps([str(rec.get(k) or "") for k in keys], ensure_ascii=False)
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()


def _order(records: list[dict]) -> list[dict]:
    return sorted(records, key=lambda r: (category_rank(r["category"]),
                                          (r["published"] or r["first_seen"] or date.min).toordinal() * -1))


# ---------------------------------------------------------------------------------------------- sheets
def _style_header(cell) -> None:
    cell.fill = HEADER_FILL
    cell.font = HEADER_FONT
    cell.alignment = Alignment(vertical="center", wrap_text=True)


def _header_map(ws, cols: list[Col]) -> dict[str, int]:
    """Column number of every managed header; missing headers are added after the last used column."""
    found = {}
    for c in range(1, ws.max_column + 1):
        value = ws.cell(row=1, column=c).value
        if isinstance(value, str) and value.strip():
            found.setdefault(value.strip(), c)
    mapping = {}
    next_free = max(found.values(), default=0) + 1
    for col in cols:
        if col.header in found:
            mapping[col.key] = found[col.header]
        else:
            cell = ws.cell(row=1, column=next_free, value=col.header)
            _style_header(cell)
            ws.column_dimensions[get_column_letter(next_free)].width = col.width
            mapping[col.key] = next_free
            next_free += 1
    return mapping


def _link_target(value: str | None, book_dir: Path, kind: str) -> str | None:
    if not value:
        return None
    if kind == "file":
        target = ROOT / value
        return os.path.relpath(target, book_dir) if target.exists() else None
    return value


def _write_row(ws, r: int, cols: list[Col], cmap: dict[str, int], rec: dict, book_dir: Path) -> None:
    last = get_column_letter(cmap["last_date"]) if "last_date" in cmap else None
    for col in cols:
        cell = ws.cell(row=r, column=cmap[col.key])
        if col.kind == "sno":
            if cell.value in (None, ""):
                cell.value = r - 1
        elif col.kind == "days" and last:
            cell.value = f'=IF({last}{r}="","",{last}{r}-TODAY())'
            cell.number_format = "0"
        elif col.kind == "status" and last:
            cell.value = (f'=IF({last}{r}="","Check document",IF({last}{r}<TODAY(),"Closed",'
                          f'IF({last}{r}-TODAY()<=7,"Closing soon","Open")))')
        elif col.kind == "date":
            value = rec.get(col.key)
            cell.value = value or None
            cell.number_format = "DD-MMM-YYYY"
        elif col.kind in ("link", "file"):
            target = _link_target(rec.get(col.key), book_dir, col.kind)
            if target:
                cell.value = "Open" if col.kind == "link" else "Open file"
                cell.hyperlink = target
                cell.font = LINK_FONT
            else:
                cell.value = None
                cell.hyperlink = None
        else:
            cell.value = _clean(rec.get(col.key, ""))
        cell.alignment = WRAP if col.kind == "wrap" else TOP


def _finish_sheet(ws, cmap: dict[str, int]) -> None:
    rows = ws.max_row
    last_col = get_column_letter(ws.max_column)
    ws.freeze_panes = "F2"
    ws.auto_filter.ref = f"A1:{last_col}{max(rows, 1)}"
    ws.conditional_formatting = ConditionalFormattingList()
    if rows < 2:
        return
    area = f"A2:{last_col}{rows}"
    if "last_date" in cmap:
        L = get_column_letter(cmap["last_date"])
        ws.conditional_formatting.add(area, FormulaRule(
            formula=[f'AND(${L}2<>"",${L}2<TODAY())'], font=GREY_FONT, stopIfTrue=True))
        ws.conditional_formatting.add(area, FormulaRule(
            formula=[f'AND(${L}2<>"",${L}2-TODAY()<=3)'], fill=RED_FILL, stopIfTrue=True))
        ws.conditional_formatting.add(area, FormulaRule(
            formula=[f'AND(${L}2<>"",${L}2-TODAY()<=7)'], fill=ORANGE_FILL, stopIfTrue=True))
    if "first_seen" in cmap:
        F = get_column_letter(cmap["first_seen"])
        ws.conditional_formatting.add(f"{F}2:{F}{rows}", FormulaRule(
            formula=[f"${F}2=TODAY()"], fill=TODAY_FILL, font=Font(bold=True)))


def _sync_sheet(wb, title: str, cols: list[Col], records: list[dict], book_dir: Path) -> int:
    """Append new records / update changed ones. Returns the number of appended rows."""
    ws = wb[title] if title in wb.sheetnames else wb.create_sheet(title)
    new_sheet = ws.max_row == 1 and ws.cell(row=1, column=1).value is None
    cmap = _header_map(ws, cols)
    if new_sheet:
        ws.row_dimensions[1].height = 30
    id_col = cmap["id"]
    rows_by_id = {}
    for r in range(2, ws.max_row + 1):
        value = ws.cell(row=r, column=id_col).value
        if value not in (None, ""):
            try:
                rows_by_id[int(value)] = r
            except (TypeError, ValueError):
                pass
    appended = 0
    next_row = ws.max_row + 1 if not new_sheet else 2
    for rec in records:
        r = rows_by_id.get(rec["id"])
        if r is None:
            r = next_row
            next_row += 1
            appended += 1
        _write_row(ws, r, cols, cmap, rec, book_dir)
    _finish_sheet(ws, cmap)
    return appended


def _open_book(path: Path) -> Workbook:
    if path.exists():
        return load_workbook(path)
    wb = Workbook()
    wb.remove(wb.active)
    return wb


def save_workbook(wb: Workbook, path: Path) -> bool:
    """Save; returns False when the file is open in Excel (the changes are retried next run)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.stem + ".saving.xlsx")
    wb.save(tmp)
    try:
        os.replace(tmp, path)
        return True
    except PermissionError:
        tmp.unlink(missing_ok=True)
        return False


def _sync_book(path: Path, records: list[dict], hash_key: str, extra=None) -> tuple[bool, dict[int, str], int]:
    """Bring one workbook up to date. extra(wb) can refresh summary sheets."""
    if path.exists():
        changed = [r for r in records if r[hash_key] != record_hash(r)]
    else:
        changed = list(records)  # file new or deleted by the user: write everything again
    if not changed and path.exists() and extra is None:
        return True, {}, 0
    wb = _open_book(path)
    appended = 0
    for title, cols, keep in SHEETS:
        subset = _order([r for r in changed if keep(r)])
        added = _sync_sheet(wb, title, cols, subset, path.parent)
        if title == "All Updates":
            appended = added
    if extra is not None:
        extra(wb)
    ok = save_workbook(wb, path)
    return ok, ({r["id"]: record_hash(r) for r in changed} if ok else {}), appended


# ---------------------------------------------------------------------------------------------- summaries
def _replace_sheet(wb, title: str, index: int | None = None):
    if title in wb.sheetnames:
        idx = wb.sheetnames.index(title)
        del wb[title]
        return wb.create_sheet(title, idx)
    return wb.create_sheet(title, index)


def _dashboard(ws, store, run, records, sites: dict[str, Site], today: date, ai_summary: dict | None,
               closing_days: int) -> None:
    ws.column_dimensions["A"].width = 34
    ws.column_dimensions["B"].width = 60
    for col in "CDEFG":
        ws.column_dimensions[col].width = 14
    ws["A1"] = "SSC Tender & Notice Tracker"
    ws["A1"].font = Font(bold=True, size=16, color="1F4E78")
    started = datetime.fromisoformat(run["started"]) if run["started"] else datetime.now()
    ws["A2"] = f"Last run: {started:%d-%b-%Y %H:%M}" + ("  (first run - baseline)" if run["baseline"] else "")
    tenders = [r for r in records if r["tender"]]
    open_t = sorted([r for r in tenders if r["last_date"] and r["last_date"] >= today], key=lambda r: r["last_date"])
    closing = [r for r in open_t if (r["last_date"] - today).days <= closing_days]
    history = {code: store.site_history(code, 1) for code in sites}
    status_counts = {"ok": 0, "partial": 0, "failed": 0}
    for rows in history.values():
        if rows:
            status_counts[rows[0]["status"]] = status_counts.get(rows[0]["status"], 0) + 1
    n_ai, cost_30 = store.ai_cost(since_iso=(today - timedelta(days=30)).isoformat())
    metrics = [
        ("New items today", sum(1 for r in records if r["today_new"])),
        ("New tenders / RFP / EOI today", sum(1 for r in tenders if r["today_new"])),
        ("Open tenders (last date not passed)", len(open_t)),
        (f"Closing within {closing_days} days", len(closing)),
        ("Hidden finds (not linked on crawled pages)", sum(1 for r in records if r["hidden"])),
        ("Websites OK / partial / failed", f"{status_counts['ok']} / {status_counts['partial']} / {status_counts['failed']}"),
        ("Documents read by AI (last 30 days)", n_ai),
        ("AI cost estimate, last 30 days (USD)", round(cost_30, 2)),
    ]
    if ai_summary and ai_summary.get("skipped_reason"):
        metrics.append(("AI note", ai_summary["skipped_reason"]))
    row = 3
    for label, value in metrics:
        row += 1
        ws.cell(row=row, column=1, value=label).font = Font(bold=True)
        ws.cell(row=row, column=2, value=value).alignment = Alignment(horizontal="left")

    row += 2
    ws.cell(row=row, column=1, value="Kaise padhein").font = Font(bold=True, color="1F4E78")
    for tip in [
        "Har SSC ka folder: output\\<SSC>\\ - usme us SSC ki Excel aur Documents (PDF) folder.",
        "Tenders-RFP-EOI / All Updates: roz sirf nayi rows neeche judti hain; 'Found On' = kab mili (aaj wali peeli).",
        "Last Date: red = 3 din ya kam, orange = 7 din ya kam, grey = band. 'Days Left' aur 'Status' apne aap update.",
        "Apne columns (jaise 'Applied?') jod sakte hain - tracker unhe kabhi nahi chhedta.",
        "Hidden? = Yes: file kisi page par link nahi thi (media library / sitemap / site search se mili).",
    ]:
        row += 1
        ws.cell(row=row, column=1, value="•")
        ws.cell(row=row, column=2, value=tip)

    row += 2
    ws.cell(row=row, column=1, value="Open tenders - last date ke hisaab se").font = Font(bold=True, color="1F4E78")
    row += 1
    for c, h in enumerate(["Org", "Title", "Last Date", "Relevance", "Category", "Document"], start=1):
        _style_header(ws.cell(row=row, column=c, value=h))
    for r in open_t[:40]:
        row += 1
        ws.cell(row=row, column=1, value=r["org"])
        ws.cell(row=row, column=2, value=_clean(r["title"][:200])).alignment = WRAP
        cell = ws.cell(row=row, column=3, value=r["last_date"])
        cell.number_format = "DD-MMM-YYYY"
        if (r["last_date"] - today).days <= 3:
            cell.fill = RED_FILL
        elif (r["last_date"] - today).days <= closing_days:
            cell.fill = ORANGE_FILL
        ws.cell(row=row, column=4, value=r["relevance"])
        ws.cell(row=row, column=5, value=r["category"])
        if r["url"]:
            link = ws.cell(row=row, column=6, value="Open")
            link.hyperlink = r["url"]
            link.font = LINK_FONT
    if not open_t:
        row += 1
        ws.cell(row=row, column=2, value="(AI details aane ke baad last date wale tenders yahan dikhenge)")

    row += 2
    ws.cell(row=row, column=1, value="Organisations").font = Font(bold=True, color="1F4E78")
    row += 1
    for c, h in enumerate(["Org", "Organisation", "Last status", "Items", "Tender-type", "New today", "Folder"], start=1):
        _style_header(ws.cell(row=row, column=c, value=h))
    for code, site in sites.items():
        row += 1
        mine = [r for r in records if r["org"] == code]
        hist = history.get(code) or []
        values = [code, site.name, hist[0]["status"] if hist else "not run", len(mine),
                  sum(1 for r in mine if r["tender"]), sum(1 for r in mine if r["today_new"])]
        for c, v in enumerate(values, start=1):
            ws.cell(row=row, column=c, value=v)
        folder = site_dir(code)
        if folder.exists():
            link = ws.cell(row=row, column=7, value="Open folder")
            link.hyperlink = os.path.relpath(folder, OUTPUT_DIR)
            link.font = LINK_FONT
        if hist and hist[0]["status"] == "failed":
            ws.cell(row=row, column=3).fill = RED_FILL


def _site_status(ws, store, sites: dict[str, Site]) -> None:
    cols = [("Org", 10), ("Organisation", 45), ("Website", 30), ("Status", 10), ("Method", 22), ("Pages", 8),
            ("Items", 8), ("New", 7), ("Documents", 10), ("WordPress API", 13), ("Media files", 11),
            ("Sitemap URLs", 12), ("Failed runs in a row", 12), ("Last success", 18), ("Errors", 80)]
    for i, (h, w) in enumerate(cols, start=1):
        _style_header(ws.cell(row=1, column=i, value=h))
        ws.column_dimensions[get_column_letter(i)].width = w
    ws.freeze_panes = "C2"
    for r, (code, site) in enumerate(sites.items(), start=2):
        hist = store.site_history(code, 1)
        h = hist[0] if hist else None
        errors = ""
        if h and h["errors"]:
            try:
                errors = " | ".join(json.loads(h["errors"])[:5])
            except ValueError:
                errors = h["errors"]
        fails = store.consecutive_failures(code)
        values = [code, site.name, site.url, h["status"] if h else "not run", h["method"] if h else "",
                  h["pages"] if h else "", h["items"] if h else "", h["new_items"] if h else "",
                  h["documents"] if h else "", "Yes" if h and h["wp"] else "", h["wp_media"] if h else "",
                  h["sitemap_urls"] if h else "", fails, store.last_success(code) or "", _clean(errors)]
        for c, v in enumerate(values, start=1):
            ws.cell(row=r, column=c, value=v).alignment = TOP
        ws.cell(row=r, column=3).hyperlink = site.url
        ws.cell(row=r, column=3).font = LINK_FONT
        if h and h["status"] == "failed":
            ws.cell(row=r, column=4).fill = RED_FILL
        elif h and h["status"] == "partial":
            ws.cell(row=r, column=4).fill = ORANGE_FILL
        if fails >= 3:
            ws.cell(row=r, column=13).fill = RED_FILL


def _write_new_file(path: Path, records: list[dict]) -> bool:
    wb = Workbook()
    wb.remove(wb.active)
    _sync_sheet(wb, "New", TENDER_COLS, _order(records), path.parent)
    return save_workbook(wb, path)


# ---------------------------------------------------------------------------------------------- entry point
def write_reports(store, keywords: Keywords, settings: Settings, sites: dict[str, Site], run,
                  today: date, ai_summary: dict | None = None) -> dict:
    from .locking import RunLock
    guard = RunLock(OUTPUT_DIR / ".reports.guard")
    if not guard.acquire():
        report = summary_report(store, keywords, settings, sites, run, today)
        report.update(master_new_rows=0, site_new_rows={}, locked=["Excel report worker is already active; next run will sync"])
        return report
    try:
        return _write_reports(store, keywords, settings, sites, run, today, ai_summary)
    finally:
        guard.release()


def _write_reports(store, keywords: Keywords, settings: Settings, sites: dict[str, Site], run,
                  today: date, ai_summary: dict | None = None) -> dict:
    records = build_records(store, keywords, settings, sites, run, today)
    closing_days = int(settings.excel.get("closing_soon_days", 7))
    locked: list[str] = []

    # One workbook per organisation (only when something changed there).
    by_site: dict[str, list[dict]] = {}
    for rec in records:
        by_site.setdefault(rec["org"], []).append(rec)
    site_new_rows = {}
    for code, recs in by_site.items():
        path = site_dir(code) / f"{code}_Updates.xlsx"
        ok, hashes, appended = _sync_book(path, recs, "hash_site")
        if not ok:
            locked.append(str(path))
        for item_id, h in hashes.items():
            store.update_item(item_id, xl_site_hash=h)
        site_new_rows[code] = appended

    # Master workbook: data sheets synced, summary sheets refreshed every run.
    master_path = OUTPUT_DIR / MASTER_NAME

    def summaries(wb):
        _dashboard(_replace_sheet(wb, "Dashboard", 0), store, run, records, sites, today, ai_summary, closing_days)
        _site_status(_replace_sheet(wb, "Site Status"), store, sites)

    ok, hashes, master_new_rows = _sync_book(master_path, records, "hash_master", extra=summaries)
    if not ok:
        locked.append(str(master_path))
    for item_id, h in hashes.items():
        store.update_item(item_id, xl_master_hash=h)
    store.commit()

    # Small file with only today's new items (for email / Telegram).
    today_new = [r for r in records if r["today_new"]]
    new_path = None
    if today_new:
        new_path = NEW_DIR / f"New_{today.isoformat()}.xlsx"
        if not _write_new_file(new_path, today_new):
            locked.append(str(new_path))

    tenders = [r for r in records if r["tender"]]
    open_tenders = sorted([r for r in tenders if r["last_date"] and r["last_date"] >= today],
                          key=lambda r: r["last_date"])
    new = _order([r for r in records if r["is_new"]])
    return {
        "master": master_path, "daily": new_path, "records": records, "new": new,
        "new_tenders": [r for r in new if r["tender"]], "open_tenders": open_tenders,
        "closing": [r for r in open_tenders if (r["last_date"] - today).days <= closing_days],
        "locked": locked, "master_new_rows": master_new_rows, "site_new_rows": site_new_rows,
    }


def summary_report(store, keywords: Keywords, settings: Settings, sites: dict[str, Site], run, today: date) -> dict:
    """Same summary as write_reports, without touching any Excel file (used for the test email)."""
    records = build_records(store, keywords, settings, sites, run, today)
    closing_days = int(settings.excel.get("closing_soon_days", 7))
    tenders = [r for r in records if r["tender"]]
    open_tenders = sorted([r for r in tenders if r["last_date"] and r["last_date"] >= today],
                          key=lambda r: r["last_date"])
    new = _order([r for r in records if r["is_new"]])
    daily = sorted(NEW_DIR.glob("New_*.xlsx"))
    return {
        "master": OUTPUT_DIR / MASTER_NAME, "daily": daily[-1] if daily else None, "records": records, "new": new,
        "new_tenders": [r for r in new if r["tender"]], "open_tenders": open_tenders,
        "closing": [r for r in open_tenders if (r["last_date"] - today).days <= closing_days], "locked": [],
    }
