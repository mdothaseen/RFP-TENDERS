"""State e-procurement portals built on NIC GePNIC (…/nicgep/app, eprocure.gov.in/eprocure/app).

Only the departments that matter are read: the public "Tenders by Organisation" page lists every department
with live tenders (no CAPTCHA); the departments whose names match `nic_orgs` are opened in the same session
and their tender tables are read. A whole portal is never crawled (thousands of road and building works).

The portal's links carry a session token, so they stop working later. Each tender is therefore saved under
its Tender ID (e.g. 2026_DTEDM_1357824_1) with the department page as its link. The tender's detail page
(value, EMD, fee, dates, work description) is read once in the same session and kept in data/nic/<id>.txt;
the documents step hands that text to the AI. The PDFs themselves sit behind a CAPTCHA and are not fetched.
"""

from __future__ import annotations

import html as htmllib
import logging
import re
from datetime import date, datetime
from pathlib import Path
from urllib.parse import urljoin

from ..config import DATA_DIR

log = logging.getLogger(__name__)

DETAIL_DIR = DATA_DIR / "nic"

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


def _save_detail(fetcher, app: str, tender: dict) -> None:
    """Keep the detail page of a tender not seen before (the link only works in this session)."""
    path = detail_file(tender["id"])
    if path.exists() or not tender.get("view"):
        return
    resp = fetcher.get(urljoin(app, tender["view"]), textual_only=True)
    if resp.error or not resp.status or resp.status >= 400:
        return
    text = detail_text(resp.text)
    if tender["id"] in text:  # the page really is this tender's (not an expired-session page)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")


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
    for name, link in departments:
        url, seen = urljoin(app, link), 0
        for _ in range(MAX_PAGES_PER_ORG):
            page = fetcher.get(url, textual_only=True)
            scanner.scan.pages += 1
            if page.error or not page.status or page.status >= 400:
                scanner._err(f"{name}: {page.error or 'HTTP ' + str(page.status)}")
                break
            for t in parse_tenders(page.text):
                _save_detail(fetcher, app, t)
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
                seen += 1
            nxt = next_page(page.text)
            if not nxt:
                break
            url = urljoin(app, nxt)
        log.info("[%s] %s: %d tenders", site.code, name, seen)
    return True
