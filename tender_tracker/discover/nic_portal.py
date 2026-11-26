"""State e-procurement portals built on NIC GePNIC (…/nicgep/app, eprocure.gov.in/eprocure/app).

Only the departments that matter are read: the public "Tenders by Organisation" page lists every department
with live tenders (no CAPTCHA); the departments whose names match `nic_orgs` are opened in the same session
and their tender tables are read. A whole portal is never crawled (thousands of road and building works).

The portal's links carry a session token, so they stop working later. Each tender is therefore saved under
its Tender ID (e.g. 2026_DTEDM_1357824_1) with the department page as its link. The tender's detail page
(value, EMD, fee, dates, work description) is read once in the same session and kept in data/nic/<id>.txt.
The document bundle sits behind a CAPTCHA; it is solved in the same session (see captcha.py) and stored as
output/<ORG>/Documents/<Tender ID>.zip for the documents step and the AI to read. When the CAPTCHA cannot be
solved, the detail-page text is used instead.
"""

from __future__ import annotations

import html as htmllib
import hashlib
import logging
import re
import time
from datetime import date, datetime
from pathlib import Path
from urllib.parse import urljoin

from ..config import DATA_DIR, ROOT, site_dir

log = logging.getLogger(__name__)

DETAIL_DIR = DATA_DIR / "nic"
TESSDATA = DATA_DIR / "tessdata"

ORG_PAGE = "?page=FrontEndTendersByOrganisation&service=page"
MARK = "#nic-tender="           # url fragment that marks a portal tender (no document to download)
MAX_PAGES_PER_ORG = 10

_ROW = re.compile(r"<tr[^>]*>(.*?)</tr>", re.S | re.I)
_CELL = re.compile(r"<td[^>]*>(.*?)</td>", re.S | re.I)
_HREF = re.compile(r"href=\"([^\"]+)\"", re.I)
_TAG = re.compile(r"<[^>]+>")
_TENDER_ID = re.compile(r"\[(\d{4}_[A-Za-z0-9]+_\d+_\d+)\]\s*$")
_BRACKETS = re.compile(r"\[([^\[\]]*)\]")
_NEXT = re.compile(r"<a[^>]+id=\"linkFwd\"[^>]+href=\"([^\"]+)\"|<a[^>]+href=\"([^\"]+)\"[^>]+id=\"linkFwd\"", re.I)


def is_portal_tender(url: str | None) -> bool:
    return MARK in (url or "")


def detail_file(url_or_id: str) -> Path:
    tid = url_or_id.split(MARK)[-1]
    return DETAIL_DIR / f"{re.sub(r'[^A-Za-z0-9_]', '_', tid)}.txt"


def detail_text(page: str) -> str:
    """Readable text of a tender's detail page (scripts, menus and the page frame removed)."""
    page = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", page, flags=re.S | re.I)
    text = " ".join(htmllib.unescape(_TAG.sub(" ", page)).split())
    start = text.find("Basic Details")
    return text[start:] if start >= 0 else text


def _text(cell: str) -> str:
    return " ".join(htmllib.unescape(_TAG.sub(" ", cell)).split())


def _rows(page: str):
    for row in _ROW.findall(page):
        cells = [_text(c) for c in _CELL.findall(row)]
        if len(cells) >= 3 and cells[0].isdigit():
            yield cells, [htmllib.unescape(h) for h in _HREF.findall(row)]


def _when(text: str) -> date | None:
    try:
        return datetime.strptime(text.strip()[:11], "%d-%b-%Y").date()
    except ValueError:
        return None


def parse_org_list(page: str, patterns: list[re.Pattern]) -> list[tuple[str, str]]:
    """(department name, link) for the departments whose names match."""
    out = []
    for cells, links in _rows(page):
        if links and any(p.search(cells[1]) for p in patterns):
            out.append((cells[1], links[0]))
    return out


def parse_tenders(page: str) -> list[dict]:
    """Rows of a department's tender table: published, closing, title, reference, tender id, detail link."""
    out = []
    for cells, links in _rows(page):
        if len(cells) < 5:
            continue
        # "[title] [reference][tender id]"; the column after it names the department chain.
        block = next((c for c in cells if _TENDER_ID.search(c)), "")
        tid = _TENDER_ID.search(block)
        if not tid:
            continue
        parts = [p.strip() for p in _BRACKETS.findall(block)]
        title = parts[0] if parts else block
        ref = parts[1] if len(parts) > 2 else ""
        view = next((h for h in links if "FrontEndViewTender" in h), "")
        out.append({"id": tid.group(1), "title": title, "ref": ref,
                    "published": _when(cells[1]), "closing": _when(cells[2]), "opening": cells[3], "view": view})
    return out


def next_page(page: str) -> str | None:
    m = _NEXT.search(page)
    return htmllib.unescape(m.group(1) or m.group(2)) if m else None


def _save_detail(fetcher, app: str, tender: dict) -> str | None:
    """Keep the detail page of a tender not seen before (the link only works in this session).
    Returns the raw detail HTML (for its document links), or None."""
    path = detail_file(tender["id"])
    if not tender.get("view"):
        return None
    resp = fetcher.get(urljoin(app, tender["view"]), textual_only=True)
    if resp.error or not resp.status or resp.status >= 400:
        return None
    text = detail_text(resp.text)
    if tender["id"] in text:  # the page really is this tender's (not an expired-session page)
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
        return resp.text
    return None


def _attach_documents(scanner, app: str, tender: dict, found, detail_html: str | None, budget: list) -> None:
    """Solve the download CAPTCHA and keep the tender's document bundle next to the other sites' files.

    The portal's links carry a session token, so this must happen here, in the scanning session. The
    zip is stored under the Tender ID; a later scan reuses it instead of solving the CAPTCHA again.
    """
    from .. import captcha

    config = scanner.settings.documents.get("captcha") or {}
    if not config.get("enabled", True) or detail_html is None or budget[0] <= 0:
        return
    if time.monotonic() > budget[1]:
        return
    folder = site_dir(scanner.site.code) / "Documents"
    target = folder / f"{tender['id']}.zip"
    if target.is_file():
        found.local_path = target.resolve().relative_to(ROOT).as_posix()
        found.sha256 = hashlib.sha256(target.read_bytes()).hexdigest()
        found.doc_size = target.stat().st_size
        found.file_ext, found.is_document = "zip", True
        return
    m = re.search(r'href="([^"]*component=docDownoad[^"]*)"', detail_html)
    if not m:
        return
    budget[0] -= 1
    content, error = captcha.solve(scanner.fetcher, app, urljoin(app, m.group(1).replace("&amp;", "&")),
                                   config, TESSDATA)
    max_bytes = int(scanner.settings.documents.get("max_download_mb", 50)) * 1_000_000
    if not content or len(content) > max_bytes:
        log.info("[%s] %s: documents not fetched (%s)", scanner.site.code, tender["id"], error or "too big")
        return
    folder.mkdir(parents=True, exist_ok=True)
    target.write_bytes(content)
    found.local_path = target.resolve().relative_to(ROOT).as_posix()
    found.sha256 = hashlib.sha256(content).hexdigest()
    found.doc_size = len(content)
    found.file_ext, found.is_document = "zip", True
    log.info("[%s] %s: documents saved (%.1f MB)", scanner.site.code, tender["id"], len(content) / 1_000_000)


def scan_portal(scanner) -> bool:
    """Called by SiteScanner for a site with nic_orgs. Returns True when the department list was read."""
    site, fetcher = scanner.site, scanner.fetcher
    app = site.url.split("?")[0]
    org_url = app + ORG_PAGE
    resp = fetcher.get(org_url, textual_only=True)
    scanner.scan.pages += 1
    if resp.error or not resp.status or resp.status >= 400:
        scanner._err(f"{org_url}: {resp.error or 'HTTP ' + str(resp.status)}")
        return False
    patterns = [re.compile(p, re.I) for p in site.nic_orgs]
    departments = parse_org_list(resp.text, patterns)
    if not departments and "FrontEndTendersByOrganisation" not in resp.text:
        scanner._err(f"{org_url}: department list not shown (CAPTCHA or changed page)")
        return False
    captcha_config = scanner.settings.documents.get("captcha") or {}
    budget = [int(captcha_config.get("max_docs_per_run", 12)),
              time.monotonic() + float(captcha_config.get("time_budget_seconds", 180))]
    docs_folder = site_dir(site.code) / "Documents"
    for name, link in departments:
        url, seen = urljoin(app, link), 0
        for _ in range(MAX_PAGES_PER_ORG):
            page = fetcher.get(url, textual_only=True)
            scanner.scan.pages += 1
            if page.error or not page.status or page.status >= 400:
                scanner._err(f"{name}: {page.error or 'HTTP ' + str(page.status)}")
                break
            for t in parse_tenders(page.text):
                have_zip = (docs_folder / f"{t['id']}.zip").is_file()
                detail_html = None if have_zip else _save_detail(fetcher, app, t)
                title = t["title"] + (f" (Ref: {t['ref']}; Tender ID: {t['id']})" if t["ref"]
                                      else f" (Tender ID: {t['id']})")
                context = (f"e-Tender on {site.name} | Department: {name} | Tender ID: {t['id']} | "
                           f"Closing: {t['closing'] or ''} | Bid opening: {t['opening']}")
                found = scanner._add_item(org_url + MARK + t["id"], title, context, org_url, "nic-portal",
                                          linked=True, is_doc=False, published=t["published"],
                                          date_source="e-procurement portal")
                found.title = title
                if t["closing"]:
                    found.deadline = t["closing"]
                _attach_documents(scanner, app, t, found, detail_html, budget)
                seen += 1
            nxt = next_page(page.text)
            if not nxt:
                break
            url = urljoin(app, nxt)
        log.info("[%s] %s: %d tenders", site.code, name, seen)
    return True
