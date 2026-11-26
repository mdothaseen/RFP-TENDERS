"""Download documents, extract their text and re-check the category from the content."""

from __future__ import annotations

import json
import hashlib
import logging
import queue
import re
import subprocess
import sys
import tempfile
import threading
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path

from .ai_extract import STALE
from .discover.nic_portal import detail_file, is_portal_tender
from .classify import classify, content_category, is_vague
from .config import DATA_DIR, ROOT, TEXT_DIR, Keywords, Settings, Site, site_dir
from .dates import parse_http_date
from .discover.extract import main_text
from .fetcher import Fetcher
from .renderer import Renderer
from .urls import filename_from_url, gdrive_file_id, google_doc_export, host_of, url_ext

log = logging.getLogger(__name__)

_UNSAFE = re.compile(r'[<>:"/\\|?*\x00-\x1f]+')
TEXT_KINDS = {"pdf", "docx", "xlsx", "pptx", "zip", "html", "txt", "csv"}


@dataclass
class TextInfo:
    text: str = ""
    kind: str = ""
    pages: int = 0
    scanned: bool = False
    primary: Path | None = None   # PDF to send to the AI when the text layer is missing
    error: str | None = None
    ocr: bool = False             # text came from OCR
    retry: bool = False           # downloaded file is kept, extraction should be retried


@dataclass
class DocumentBatch:
    rows: list
    priority_pending: int
    unknown_pending: int


def rel(path: Path | None) -> str | None:
    if path is None:
        return None
    try:
        return path.resolve().relative_to(ROOT).as_posix()
    except ValueError:
        return str(path)


def sniff_kind(path: Path, fallback: str = "") -> str:
    with open(path, "rb") as fh:
        head = fh.read(2048)
    if head.startswith(b"%PDF") or b"%PDF-" in head[:1024]:
        return "pdf"
    if head.startswith(b"PK"):
        try:
            with zipfile.ZipFile(path) as zf:
                names = zf.namelist()
        except zipfile.BadZipFile:
            return fallback or "zip"
        if any(n.startswith("word/") for n in names):
            return "docx"
        if any(n.startswith("xl/") for n in names):
            return "xlsx"
        if any(n.startswith("ppt/") for n in names):
            return "pptx"
        return "zip"
    if head.startswith(b"\xd0\xcf\x11\xe0"):
        return fallback if fallback in ("doc", "xls", "ppt") else "doc"
    low = head.lstrip().lower()
    if low.startswith((b"<!doctype html", b"<html", b"<head", b"<body")) or b"<html" in low[:600]:
        return "html"
    if fallback in ("txt", "csv"):
        return fallback
    return fallback or "unknown"


def _pdf_text(path: Path, max_chars: int) -> TextInfo:
    import pymupdf as fitz

    info = TextInfo(kind="pdf", primary=path)
    with fitz.open(path) as doc:
        info.pages = doc.page_count
        parts, total, read = [], 0, 0
        for page in doc:
            t = page.get_text("text") or ""
            parts.append(t)
            total += len(t)
            read += 1
            if total >= max_chars:
                break
    info.text = "\n".join(parts)[:max_chars]
    info.scanned = info.pages > 0 and len(info.text.strip()) / max(read, 1) < 100
    if info.scanned and OCR.get("enabled", True):
        ocr_text, complete = _ocr_result(path, int(OCR.get("max_pages", 30)),
                                        str(OCR.get("language", "eng+hin")))
        if not complete:
            info.retry = True
            info.error = "OCR incomplete (time limit or OCR error); saved file will be retried"
        if len(ocr_text.strip()) > len(info.text.strip()):
            info.text = ("[Text read by OCR from a scanned PDF]\n" + ocr_text)[:max_chars]
            info.ocr = True
    return info


# OCR settings (filled from settings.yaml by run_documents); Tesseract language data lives in data/tessdata.
OCR: dict = {}
# One OCR child at a time bounds memory use; a stuck native OCR call can be killed safely.
_OCR_LOCK = threading.Lock()
TESSDATA = ROOT / "data" / "tessdata"
# OCR text of every file already read, by file content (sha256): the same file is never OCR'd twice.
OCR_CACHE = DATA_DIR / "ocr_cache"


def ocr_pdf(path: Path, max_pages: int = 30, language: str = "eng+hin") -> str:
    """Text of a scanned PDF via Tesseract OCR (first max_pages pages). Empty string if OCR is unavailable."""
    return _ocr_result(path, max_pages, language)[0]


def _ocr_result(path: Path, max_pages: int, language: str) -> tuple[str, bool]:
    if not TESSDATA.exists():
        return "", True  # OCR is unavailable; the original PDF remains available for AI/manual reading.
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    cached = OCR_CACHE / f"{digest}_{max_pages}_{language}.txt"
    if cached.exists():
        return cached.read_text(encoding="utf-8"), True
    text, complete = _run_ocr(path, max_pages, language)
    if complete and text.strip():
        OCR_CACHE.mkdir(parents=True, exist_ok=True)
        cached.write_text(text, encoding="utf-8")
    return text, complete


def _run_ocr(path: Path, max_pages: int, language: str) -> tuple[str, bool]:
    """Run native OCR in a child process so its time limit can actually stop it."""
    timeout = max(1.0, float(OCR.get("timeout_seconds", 90)))
    started = time.monotonic()
    if not _OCR_LOCK.acquire(timeout=timeout):
        log.warning("OCR busy for %.0fs; deferred %s", timeout, path.name)
        return "", False
    try:
        remaining = timeout - (time.monotonic() - started)
        if remaining <= 0:
            return "", False
        log.info("OCR: %s, up to %d pages, %s (%.0fs remaining)", path.name, max_pages, language, remaining)
        OCR_CACHE.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix=".work-", dir=OCR_CACHE) as folder:
            result_file = Path(folder) / "result.json"
            command = [sys.executable, str(Path(__file__).with_name("ocr_worker.py")),
                       str(path.resolve()), str(TESSDATA.resolve()), str(max_pages), language, str(result_file)]
            timed_out = False
            try:
                proc = subprocess.run(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                      stderr=subprocess.PIPE, timeout=remaining,
                                      creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            except subprocess.TimeoutExpired:
                timed_out = True
                log.warning("OCR time limit reached for %s; keeping completed pages for retry", path.name)
            result = json.loads(result_file.read_text(encoding="utf-8")) if result_file.exists() else {}
            complete = not timed_out and proc.returncode == 0 and bool(result.get("complete"))
            if not complete and not timed_out:
                detail = result.get("error") or proc.stderr.decode("utf-8", errors="replace")[-300:]
                log.warning("OCR failed for %s: %s", path.name, str(detail)[:300])
            return str(result.get("text") or ""), complete
    except (OSError, ValueError) as exc:
        log.warning("OCR failed for %s: %s", path.name, str(exc)[:150])
        return "", False
    finally:
        _OCR_LOCK.release()


def _docx_text(path: Path) -> str:
    from docx import Document

    doc = Document(str(path))
    lines = [p.text for p in doc.paragraphs if p.text.strip()]
    for table in doc.tables:
        for row in table.rows:
            cells = [c.text.strip() for c in row.cells if c.text.strip()]
            if cells:
                lines.append(" | ".join(dict.fromkeys(cells)))
    return "\n".join(lines)


def _xlsx_text(path: Path, max_chars: int) -> str:
    from openpyxl import load_workbook

    wb = load_workbook(str(path), read_only=True, data_only=True)
    lines, total = [], 0
    try:
        for ws in wb.worksheets:
            lines.append(f"## Sheet: {ws.title}")
            for row in ws.iter_rows(values_only=True):
                cells = [str(c).strip() for c in row if c is not None and str(c).strip()]
                if cells:
                    line = " | ".join(cells)
                    lines.append(line)
                    total += len(line)
                    if total > max_chars:
                        return "\n".join(lines)
    finally:
        wb.close()
    return "\n".join(lines)


def _pptx_text(path: Path) -> str:
    out = []
    with zipfile.ZipFile(path) as zf:
        slides = sorted(n for n in zf.namelist() if re.match(r"ppt/slides/slide\d+\.xml$", n))
        for name in slides:
            xml = zf.read(name).decode("utf-8", errors="replace")
            out.append(" ".join(re.findall(r"<a:t>([^<]*)</a:t>", xml)))
    return "\n".join(out)


def extract_text(path: Path, kind: str, max_chars: int = 400_000, depth: int = 0) -> TextInfo:
    try:
        if kind == "pdf":
            return _pdf_text(path, max_chars)
        if kind == "docx":
            return TextInfo(_docx_text(path)[:max_chars], "docx")
        if kind == "xlsx":
            return TextInfo(_xlsx_text(path, max_chars)[:max_chars], "xlsx")
        if kind == "pptx":
            return TextInfo(_pptx_text(path)[:max_chars], "pptx")
        if kind in ("txt", "csv"):
            return TextInfo(path.read_text(encoding="utf-8", errors="replace")[:max_chars], kind)
        if kind == "html":
            return TextInfo(main_text(path.read_text(encoding="utf-8", errors="replace"), max_chars), "html")
        if kind == "zip" and depth == 0:
            return _zip_text(path, max_chars)
    except Exception as exc:  # noqa: BLE001 - corrupt or protected files
        return TextInfo(kind=kind, error=f"could not read {kind}: {str(exc)[:150]}")
    return TextInfo(kind=kind, error=f"{kind or 'unknown'} files cannot be read automatically")


def _zip_text(path: Path, max_chars: int) -> TextInfo:
    out_dir = path.with_name(path.stem + "_files")
    info = TextInfo(kind="zip")
    parts = []
    with zipfile.ZipFile(path) as zf:
        for member in zf.infolist()[:40]:
            name = member.filename
            if member.is_dir() or url_ext(name) not in ("pdf", "docx", "xlsx", "pptx", "txt", "csv"):
                continue
            if member.file_size > 60_000_000:
                continue
            target = out_dir / _UNSAFE.sub("_", Path(name).name)
            target.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(member) as src, open(target, "wb") as dst:
                dst.write(src.read())
            sub = extract_text(target, sniff_kind(target, url_ext(name)), max_chars, depth=1)
            parts.append(f"===== {Path(name).name} =====\n{sub.text}")
            info.pages += sub.pages
            if sub.retry:
                info.retry, info.error = True, sub.error
            if sub.kind == "pdf" and info.primary is None:
                info.primary = target
                info.scanned = sub.scanned
    info.text = "\n\n".join(parts)[:max_chars]
    return info


def _safe_name(name: str, default: str = "document") -> str:
    name = _UNSAFE.sub("_", name or "").strip(" ._") or default
    if len(name) > 110:
        stem, dot, ext = name.rpartition(".")
        name = (stem[:100] + dot + ext) if dot and len(ext) <= 5 else name[:110]
    return name


def _download_url(url: str) -> str:
    file_id = gdrive_file_id(url)
    if file_id:
        return f"https://drive.usercontent.google.com/download?id={file_id}&export=download&confirm=t"
    return google_doc_export(url) or url


def _docs_folder(site_code: str) -> Path:
    return site_dir(site_code) / "Documents"


def _base_path(folder: Path, row, name: str) -> Path:
    """'2026-09-25_EOI for Empanelment.pdf' (date = published date, else the day it was found)."""
    stamp = (row["published_date"] or row["first_seen"] or "")[:10] or "undated"
    return folder / _safe_name(f"{stamp}_{name}")


def _with_id(path: Path, row) -> Path:
    stem, dot, ext = path.name.rpartition(".")
    return path.with_name(f"{stem}_{row['id']}.{ext}" if dot else f"{path.name}_{row['id']}")


def _final_path(folder: Path, row, name: str) -> Path:
    path = _base_path(folder, row, name)
    return _with_id(path, row) if path.exists() else path


def _left_over_file(folder: Path, row, name: str, claimed: set[str]) -> Path | None:
    """This item's file, already on disk from a run that stopped before saving it (not used by any item)."""
    if not name or not url_ext(name):
        return None
    base = _base_path(folder, row, name)
    for path in (_with_id(base, row), base):
        if path.is_file() and rel(path) not in claimed:
            return path
    return None


def process_item(fetcher: Fetcher, row, settings: Settings, keywords: Keywords, render=None,
                 claimed: set[str] | None = None) -> dict:
    """Download (or fetch the page of) one item and return the DB fields to update.

    Files go to output/<ORG>/Documents/; the extracted text goes to data/text/ (used only by the AI).
    render: optional callable url -> html, used for pages of JavaScript websites.
    claimed: files already used by other items (a file on disk that nobody uses is a left-over to reuse).
    """
    max_bytes = int(settings.documents.get("max_download_mb", 50)) * 1_000_000
    max_chars = int(settings.ai.get("max_text_chars", 400_000))
    attempts = (row["doc_attempts"] or 0) + 1
    TEXT_DIR.mkdir(parents=True, exist_ok=True)
    text_path = TEXT_DIR / f"{row['id']}_r{dict(row).get('revision', 0)}.txt"

    if is_portal_tender(row["url"]):
        # e-procurement tender: its detail page was saved during the scan (the portal link has expired since)
        saved = detail_file(row["url"])
        if not saved.exists():
            return {"doc_status": "error", "doc_attempts": attempts, "doc_error": "portal detail page not saved"}
        text = saved.read_text(encoding="utf-8")[:max_chars]
        text_path.write_text(text, encoding="utf-8")
        return {"doc_status": "html", "doc_attempts": attempts, "doc_error": None, "text_path": rel(text_path),
                "text_chars": len(text), "scanned": 0}

    if not row["is_document"]:
        html = render(row["url"]) if render is not None else None
        if not html:
            resp = fetcher.get(row["url"], textual_only=True)
            if not resp.ok:
                return {"doc_status": "error", "doc_attempts": attempts,
                        "doc_error": resp.error or f"HTTP {resp.status}"}
            html = resp.text
        text = main_text(html, max_chars)
        text_path.write_text(text, encoding="utf-8")
        return {"doc_status": "html", "doc_attempts": attempts, "doc_error": None, "text_path": rel(text_path),
                "text_chars": len(text), "scanned": 0}

    folder = _docs_folder(row["site_code"])
    url = _download_url(row["url"])
    site = getattr(fetcher, "site", None)
    if (site is not None and url.startswith("http://") and site.url.startswith("https://")
            and site.owns(host_of(url))):
        # Old http:// links of an https site (e.g. dcmsme.gov.in): port 80 hangs ~45 s and then fails.
        url = "https://" + url[len("http://"):]
    name = filename_from_url(row["url"])
    if gdrive_file_id(row["url"]) or google_doc_export(row["url"]):
        name = ""

    # Already downloaded by a run that stopped half-way? Reuse it when the website's file has the same size.
    saved, headers, reused = None, {}, False
    own_path = dict(row).get("local_path")
    if own_path and (ROOT / own_path).is_file():
        saved, reused = ROOT / own_path, True
        log.info("[%s] saved file reused: %s", row["site_code"], saved.name)
    left_over = _left_over_file(folder, row, name, claimed or set()) if saved is None else None
    if left_over is not None:
        head = fetcher.head(url)
        size = int((head or {}).get("content-length") or 0)
        if head is not None and size in (0, left_over.stat().st_size):
            saved, headers, reused = left_over, head, True
            log.info("[%s] already downloaded, not fetched again: %s", row["site_code"], left_over.name)

    if saved is None:
        partial = folder / f".download_{row['id']}"
        resp, saved = fetcher.download(url, partial, max_bytes)
        if saved is None:
            status = "too_big" if resp.error and "larger than" in resp.error else "error"
            return {"doc_status": status, "doc_attempts": attempts, "doc_error": resp.error}
        headers = resp.headers

        disposition = headers.get("content-disposition", "")
        m = re.search(r"filename\*=(?:UTF-8'')?([^;]+)|filename=\"?([^\";]+)", disposition, re.I)
        if m and not name:
            from urllib.parse import unquote
            name = unquote(m.group(1) or m.group(2)).strip()
    kind = sniff_kind(saved, url_ext(name) or row["file_ext"] or "")
    if kind == "html":
        # A web page instead of the file: removed document, login page or "not found" page.
        saved.unlink(missing_ok=True)
        return {"doc_status": "not_file", "doc_attempts": attempts,
                "doc_error": "link does not open a file (removed from the site, or needs login)"}
    if not reused:
        name = name or (row["title"] or "document")[:90]
        if kind != "unknown" and not url_ext(name):
            name = f"{name}.{kind}"
        final = _final_path(folder, row, name)
        saved.replace(final)
        saved = final
    if claimed is not None:
        claimed.add(rel(saved))

    sha = hashlib.sha256(saved.read_bytes()).hexdigest()
    log.info("[%s] reading #%s %s (%.1f MB)", row["site_code"], row["id"], saved.name,
             saved.stat().st_size / 1_000_000)
    info = extract_text(saved, kind, max_chars)
    fields = {
        "doc_status": "error" if info.retry else "ok",
        "doc_attempts": attempts, "doc_error": info.error, "local_path": rel(saved), "sha256": sha,
        "doc_size": saved.stat().st_size, "doc_pages": info.pages, "text_chars": len(info.text),
        "scanned": int(info.scanned), "file_ext": kind,
    }
    if not reused:
        fields.update(etag=headers.get("etag"), last_modified=headers.get("last-modified"),
                      last_checked=datetime.now().isoformat(timespec="seconds"),
                      last_full_check=datetime.now().isoformat(timespec="seconds"))
    if info.ocr:
        fields["doc_error"] = info.error or "scanned PDF - text read by OCR"
    if info.text:
        text_path.write_text(info.text, encoding="utf-8")
        fields["text_path"] = rel(text_path)
    # The server's "Last-Modified" time of a static file is usually when it was uploaded.
    server_date = parse_http_date(headers.get("last-modified"))
    if server_date and server_date < date.today() and "?" not in row["url"]:
        if not row["published_date"]:
            fields["published_date"] = server_date.isoformat()
            fields["date_source"] = "server date"
        fields.update(_better_upload(row, server_date))
    suggested = content_category(keywords, info.text) if info.text else None
    if suggested and row["category"] not in keywords.tender_categories:
        fields["content_category"] = suggested
    return fields


def _better_upload(row, server_date: date) -> dict:
    """Use the server file date unless a more exact upload date is already known."""
    from .store import UPLOAD_PRECISION
    if UPLOAD_PRECISION.get(row["uploaded_source"] or "", 0) >= UPLOAD_PRECISION["server file date"]:
        return {}
    return {"uploaded_date": server_date.isoformat(), "uploaded_source": "server file date"}


def fill_upload_dates(store, sites: dict[str, Site], settings: Settings, keywords: Keywords, today: date,
                      limit: int = 150, workers: int = 6) -> int:
    """Ask the website (HEAD request, no download) for the upload date of files that are shown in Excel
    but were not downloaded."""
    window = (today - timedelta(days=int(settings.excel.get("baseline_max_age_days", 365)))).isoformat()
    rows = [r for r in store.items(
        "is_document=1 AND removed=0 AND url IS NOT NULL AND uploaded_date IS NULL AND uploaded_source IS NULL "
        "AND doc_status IS NULL "
        "AND category <> 'Other' AND (published_date IS NULL OR published_date >= ?)", (window,))
        if "?" not in (r["url"] or "") and not gdrive_file_id(r["url"])][:limit]
    by_site: dict[str, list] = {}
    for row in rows:
        by_site.setdefault(row["site_code"], []).append(row)

    def work(code: str, site_rows: list):
        site = sites.get(code)
        if site is None:
            return []
        fetcher = Fetcher(site, settings)
        out = []
        for row in site_rows:
            info = fetcher.head(row["url"])
            server_date = parse_http_date(info.get("last-modified")) if info else None
            if server_date and server_date < today:
                out.append((row["id"], {"uploaded_date": server_date.isoformat(), "uploaded_source": "server file date"}))
            else:
                out.append((row["id"], {"uploaded_source": "not available"}))
        return out

    results = []
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        for fut in as_completed([pool.submit(work, c, r) for c, r in by_site.items()]):
            results.extend(fut.result())
    for item_id, fields in results:
        store.update_item(item_id, **fields)
    store.commit()
    return sum(1 for _, f in results if f.get("uploaded_date"))


def reconcile_files(store) -> int:
    """Requeue lost local assets without deleting a surviving download or AI answer."""
    count = 0
    for row in store.items("removed=0 AND doc_status IN ('ok','html')"):
        missing = any(row[name] and not (ROOT / row[name]).is_file() for name in ("local_path", "text_path"))
        if missing:
            store.update_item_revision(row["id"], row["revision"], doc_status=None, doc_attempts=0,
                                       doc_error="Saved asset missing; queued for recovery")
            count += 1
    store.commit()
    return count


def plan_documents(store, settings: Settings, keywords: Keywords, today: date, deep: bool = False) -> DocumentBatch:
    """Budget uncertain documents separately so old manuals cannot fill every regular scan."""
    categories = set(settings.documents.get("download_categories") or [])
    ai_categories = set(settings.ai.get("categories") or [])
    window = today - timedelta(days=int(settings.documents.get("baseline_days", 365)))
    rows = store.items(
        "removed=0 AND url IS NOT NULL AND (doc_status IS NULL OR (doc_status='error' AND doc_attempts < 3))")
    priority, uncertain = [], []
    for row in rows:
        published = row["published_date"]
        recent = (published and published >= window.isoformat()) or (not published and row["linked_seen"])
        if not recent:
            continue
        if (row["ai_error"] or "").startswith(STALE) or row["duplicate_of"]:  # closed / old / a second copy
            continue
        category = row["content_category"] or row["category"]
        if row["is_document"]:
            unknown = category == "Other" and classify(keywords, row["title"] or "", "", "").category == "Other" \
                and not any(p.search(row["title"] or "") for p in keywords.other_patterns)
            if category in categories:
                priority.append(row)
            elif unknown or is_vague(keywords, row["title"] or ""):
                uncertain.append(row)
        elif category in ai_categories and row["url"]:
            priority.append(row)
    order = {c: i for i, c in enumerate(keywords.tender_categories + ["Notice", "Recruitment"])}

    def fresh(row):
        return max(_ordinal(row["published_date"]), _ordinal(row["uploaded_date"]),
                   _ordinal(row["first_seen"]))

    priority.sort(key=lambda r: (0 if (r["content_category"] or r["category"]) in ai_categories else 1,
                                 0 if (r["deadline_hint"] or "") >= today.isoformat() else 1,
                                 -fresh(r), order.get(r["content_category"] or r["category"], 9), r["id"]))
    uncertain.sort(key=lambda r: (bool(r["doc_attempts"]), -fresh(r),
                                  not is_vague(keywords, r["title"] or ""), -r["id"]))
    cap = max(0, int(settings.documents.get("deep_max_per_run" if deep else "max_per_run", 300 if deep else 60)))
    unknown_cap = max(0, int(settings.documents.get("deep_unknown_per_run" if deep else "unknown_per_run",
                                                   100 if deep else 10)))
    chosen = priority[:cap]
    chosen.extend(uncertain[:min(unknown_cap, cap - len(chosen))])
    return DocumentBatch(chosen, len(priority), len(uncertain))


def select_items(store, settings: Settings, keywords: Keywords, today: date, deep: bool = False) -> list:
    return plan_documents(store, settings, keywords, today, deep).rows


def _ordinal(iso: str | None) -> int:
    try:
        return date.fromisoformat(iso[:10]).toordinal() if iso else 0
    except ValueError:
        return 0


def run_documents(rows, sites: dict[str, Site], settings: Settings, keywords: Keywords, workers: int = 6,
                  on_result=None, claimed: set[str] | None = None):
    """Fetch all selected items, one polite worker per site. Returns (item_id, fields) pairs.

    on_result(item_id, fields) is called in the calling thread as soon as each item is done, so the
    result is saved even if the run stops later (and the file is not downloaded / OCR'd again).
    """
    OCR.clear()
    OCR.update(settings.documents.get("ocr") or {})
    claimed = set() if claimed is None else claimed
    finished: queue.Queue = queue.Queue()
    by_site: dict[str, list] = {}
    for row in rows:
        by_site.setdefault(row["site_code"], []).append(row)
    started = time.monotonic()
    budget = max(0.0, float(settings.documents.get("time_budget_seconds", 300)))
    heartbeat = max(1.0, float(settings.documents.get("progress_seconds", 15)))
    active: dict[int, tuple[str, float]] = {}
    active_lock = threading.Lock()

    def work(code: str, site_rows: list):
        site = sites.get(code)
        if site is None:
            return []
        fetcher = Fetcher(site, settings)
        renderer = None

        def render(url: str) -> str | None:
            nonlocal renderer
            if not site.js or not site.owns(host_of(url)):
                return None
            if renderer is None:
                renderer = Renderer(ignore_https_errors=not site.ssl_verify)
            result = renderer.render(url)
            return result.html if len(result.html) > 1500 else None

        try:
            for row in site_rows:
                if budget and time.monotonic() - started >= budget:
                    break  # Unstarted items stay pending in SQLite; completed results are committed immediately.
                with active_lock:
                    active[row["id"]] = (code, time.monotonic())
                label = str(dict(row).get("title") or row["url"])[:120]
                log.info("[%s] starting document #%s: %s", code, row["id"], label)
                try:
                    fields = process_item(fetcher, row, settings, keywords, render, claimed)
                except Exception as exc:  # noqa: BLE001
                    log.exception("[%s] document %s failed", code, row["url"])
                    fields = {"doc_status": "error", "doc_attempts": (row["doc_attempts"] or 0) + 1,
                              "doc_error": f"{type(exc).__name__}: {str(exc)[:150]}"}
                with active_lock:
                    active.pop(row["id"], None)
                finished.put((row["id"], fields))
        finally:
            if renderer is not None:
                renderer.close()
            fetcher.session.close()

    out = []
    last_progress = time.monotonic()

    def take(item) -> None:
        nonlocal last_progress
        out.append(item)
        if on_result is not None:
            on_result(*item)
        last_progress = time.monotonic()
        log.info("Documents %d/%d saved | #%s %s | %.0fs elapsed", len(out), len(rows), item[0],
                 item[1].get("doc_status", "unknown"), last_progress - started)

    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        futures = [pool.submit(work, code, site_rows) for code, site_rows in by_site.items()]
        while True:
            try:
                take(finished.get(timeout=1))
            except queue.Empty:
                if all(f.done() for f in futures):
                    break
                now = time.monotonic()
                if now - last_progress >= heartbeat:
                    with active_lock:
                        waiting = ", ".join(f"{code} #{item_id} ({now - since:.0f}s)"
                                            for item_id, (code, since) in active.items())
                    log.info("Documents %d/%d saved | working: %s | %.0fs elapsed", len(out), len(rows),
                             waiting or "finishing workers", now - started)
                    last_progress = now
        while not finished.empty():
            take(finished.get_nowait())
        for fut in futures:
            fut.result()
    if len(out) < len(rows):
        log.info("Document time budget reached; %d unstarted items kept for the next scan", len(rows) - len(out))
    return out
