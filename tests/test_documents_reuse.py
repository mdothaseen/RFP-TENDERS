"""Finished documents are never downloaded or OCR'd twice."""

import json
import logging
import subprocess
import threading
from datetime import date
from types import SimpleNamespace

import pymupdf
import pytest

from tender_tracker import documents
from tender_tracker.classify import classify
from tender_tracker.config import Site, load_keywords, load_settings
from tender_tracker.discover.crawler import Found
from tender_tracker.store import Store


def test_ocr_runs_once_per_file(tmp_path, monkeypatch):
    monkeypatch.setattr(documents, "OCR_CACHE", tmp_path / "cache")
    monkeypatch.setattr(documents, "TESSDATA", tmp_path)
    calls = []
    monkeypatch.setattr(documents, "_run_ocr", lambda path, pages, lang: calls.append(path) or ("OCR TEXT", True))
    first, copy = tmp_path / "a.pdf", tmp_path / "b.pdf"
    first.write_bytes(b"%PDF same file")
    copy.write_bytes(b"%PDF same file")
    assert documents.ocr_pdf(first) == "OCR TEXT"
    assert documents.ocr_pdf(copy) == "OCR TEXT"
    assert calls == [first]


class FakeFetcher:
    def __init__(self, size: int, body: bytes):
        self.size, self.body, self.downloads = size, body, 0

    def head(self, url):
        return {"content-length": str(self.size), "last-modified": "Tue, 22 Sep 2026 10:00:00 GMT"}

    def download(self, url, dest, max_bytes):
        self.downloads += 1
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(self.body)
        return SimpleNamespace(headers={}, error=None), dest


def test_left_over_file_is_reused_not_downloaded(tmp_path, monkeypatch):
    monkeypatch.setattr(documents, "site_dir", lambda code: tmp_path / code)
    monkeypatch.setattr(documents, "TEXT_DIR", tmp_path / "text")
    monkeypatch.setattr(documents, "ROOT", tmp_path)
    doc = pymupdf.open()
    doc.new_page().insert_text((72, 72), "Request for Proposal for skill gap study. " * 5)
    body = doc.tobytes()
    folder = tmp_path / "TST" / "Documents"
    folder.mkdir(parents=True)
    left_over = folder / "2026-09-20_rfp.pdf"
    left_over.write_bytes(body)
    row = {"id": 7, "doc_attempts": 0, "is_document": 1, "url": "https://t.example.org/files/rfp.pdf",
           "site_code": "TST", "published_date": "2026-09-20", "first_seen": "2026-10-01T10:00:00",
           "title": "RFP", "file_ext": "pdf", "uploaded_source": None, "category": "RFP"}
    settings, keywords = load_settings(), load_keywords()

    fetcher = FakeFetcher(len(body), body)
    fields = documents.process_item(fetcher, row, settings, keywords, claimed=set())
    assert fetcher.downloads == 0 and fields["doc_status"] == "ok"
    assert fields["local_path"].endswith("2026-09-20_rfp.pdf")
    assert fields["uploaded_date"] == "2026-09-22"

    # A file with the same name that already belongs to another item is not taken: download as usual.
    other = FakeFetcher(len(body), body)
    fields = documents.process_item(other, dict(row, id=8), settings, keywords,
                                    claimed={documents.rel(left_over)})
    assert other.downloads == 1 and fields["local_path"].endswith("2026-09-20_rfp_8.pdf")


def test_each_result_is_handed_over_as_soon_as_it_is_done(monkeypatch, caplog):
    caplog.set_level(logging.INFO, logger=documents.__name__)
    monkeypatch.setattr(documents, "process_item",
                        lambda fetcher, row, settings, keywords, render, claimed: {"doc_status": "ok"})
    sites = {c: Site(code=c, name=c, url=f"https://{c.lower()}.example.org/") for c in ("A", "B")}
    rows = [{"id": i, "site_code": "A" if i % 2 else "B", "url": f"https://x.org/{i}.pdf"} for i in range(6)]
    saved = []
    out = documents.run_documents(rows, sites, load_settings(), load_keywords(), workers=2,
                                  on_result=lambda item_id, fields: saved.append((item_id, threading.current_thread())))
    assert sorted(i for i, _ in saved) == list(range(6)) and len(out) == 6
    assert all(thread is threading.main_thread() for _, thread in saved)
    assert "Documents 6/6 saved" in caplog.text


@pytest.fixture
def queue_store(tmp_path):
    store = Store(tmp_path / "queue.db")
    yield store
    store.close()


def _pending(store, title, found="2026-10-01T10:00:00", published=None):
    key = str(len(store.items()) + 1)
    item = Found(key=key, site_code="TST", url=f"https://t.example.org/{key}.pdf", title=title,
                 link_text=title, context="", source_page="https://t.example.org/", via={"page"},
                 linked=True, is_document=True, file_ext="pdf", published=published)
    kw = load_keywords()
    return store.upsert(item, classify(kw, title), 1, False, found, set(kw.tender_categories))[0]


def test_unknown_backlog_cannot_fill_regular_document_batch(queue_store):
    settings, kw = load_settings(), load_keywords()
    settings.documents.update(max_per_run=4, unknown_per_run=1)
    unknown = [_pending(queue_store, f"Unclassified document {i}") for i in range(5)]
    tender = _pending(queue_store, "EOI for Assessment Agencies", found="2026-10-22T10:00:00")
    batch = documents.plan_documents(queue_store, settings, kw, date(2026, 10, 22))
    assert batch.priority_pending == 1 and batch.unknown_pending == 5
    assert batch.rows[0]["id"] == tender
    assert len(batch.rows) == 2
    assert all(queue_store.item(i)["doc_status"] is None for i in unknown)  # Deferred, never discarded.


def test_new_tenders_and_untried_unknowns_come_before_old_retries(queue_store):
    settings, kw = load_settings(), load_keywords()
    settings.documents.update(max_per_run=4, unknown_per_run=1)
    old = _pending(queue_store, "RFP for Training Project", published=date(2026, 10, 1))
    recent = _pending(queue_store, "EOI for Assessment Agencies", found="2026-10-22T09:00:00")
    retry = _pending(queue_store, "Unknown document", found="2026-10-22T10:00:00")
    fresh = _pending(queue_store, "Unknown attachment", found="2026-10-21T10:00:00")
    queue_store.update_item(retry, doc_status="error", doc_attempts=2)
    ids = [r["id"] for r in documents.select_items(queue_store, settings, kw, date(2026, 10, 22))]
    assert ids == [recent, old, fresh]


def test_finished_documents_are_not_selected_again_and_deep_drains_more(queue_store):
    settings, kw = load_settings(), load_keywords()
    settings.documents.update(max_per_run=3, unknown_per_run=1, deep_max_per_run=10, deep_unknown_per_run=5)
    done = _pending(queue_store, "EOI for Assessment Agencies")
    exhausted = _pending(queue_store, "RFP for Training Project")
    queue_store.update_item(done, doc_status="ok")
    queue_store.update_item(exhausted, doc_status="error", doc_attempts=3)
    for i in range(4):
        _pending(queue_store, f"Unclassified file {i}")
    normal = documents.select_items(queue_store, settings, kw, date(2026, 10, 22))
    deep = documents.select_items(queue_store, settings, kw, date(2026, 10, 22), deep=True)
    assert len(normal) == 1 and len(deep) == 4
    assert not {done, exhausted} & {r["id"] for r in deep}


def test_content_classified_tender_does_not_consume_unknown_quota(queue_store):
    settings, kw = load_settings(), load_keywords()
    settings.documents.update(max_per_run=4, unknown_per_run=0)
    hidden = _pending(queue_store, "Unclassified document")
    queue_store.update_item(hidden, content_category="EOI")
    assert [r["id"] for r in documents.select_items(queue_store, settings, kw, date(2026, 10, 22))] == [hidden]


def test_document_budget_leaves_unstarted_work_pending(monkeypatch, caplog):
    settings, kw = load_settings(), load_keywords()
    settings.documents["time_budget_seconds"] = 1
    clock = [0.0]
    monkeypatch.setattr(documents.time, "monotonic", lambda: clock[0])

    def slow(fetcher, row, settings, keywords, render, claimed):
        clock[0] = 2.0
        return {"doc_status": "ok"}

    monkeypatch.setattr(documents, "process_item", slow)
    site = Site(code="TST", name="TST", url="https://t.example.org/")
    rows = [{"id": i, "site_code": "TST", "url": f"https://t.example.org/{i}.pdf"} for i in range(3)]
    saved = []
    caplog.set_level(logging.INFO, logger=documents.__name__)
    results = documents.run_documents(rows, {"TST": site}, settings, kw, workers=1,
                                      on_result=lambda i, fields: saved.append(i))
    assert saved == [0] and len(results) == 1
    assert "2 unstarted items kept for the next scan" in caplog.text


def test_incomplete_ocr_is_not_cached(tmp_path, monkeypatch):
    monkeypatch.setattr(documents, "TESSDATA", tmp_path)
    monkeypatch.setattr(documents, "OCR_CACHE", tmp_path / "cache")
    monkeypatch.setattr(documents, "_run_ocr", lambda *args: ("PARTIAL TEXT", False))
    path = tmp_path / "scan.pdf"
    path.write_bytes(b"%PDF stub")
    assert documents.ocr_pdf(path) == "PARTIAL TEXT"
    assert not documents.OCR_CACHE.exists()


def test_ocr_timeout_recovers_pages_and_releases_slot(tmp_path, monkeypatch):
    monkeypatch.setattr(documents, "TESSDATA", tmp_path)
    monkeypatch.setattr(documents, "OCR_CACHE", tmp_path / "cache")
    monkeypatch.setattr(documents, "OCR", {"timeout_seconds": 2})
    monkeypatch.setattr(documents, "_OCR_LOCK", threading.Lock())

    def timeout(command, **kwargs):
        assert 0 < kwargs["timeout"] <= 2
        from pathlib import Path
        Path(command[-1]).write_text(json.dumps({"text": "PAGE ONE", "complete": False}), encoding="utf-8")
        raise subprocess.TimeoutExpired(command, kwargs["timeout"])

    monkeypatch.setattr(documents.subprocess, "run", timeout)
    text, complete = documents._run_ocr(tmp_path / "scan.pdf", 30, "eng")
    assert text == "PAGE ONE" and not complete
    assert documents._OCR_LOCK.acquire(blocking=False)
    documents._OCR_LOCK.release()


def test_native_ocr_failure_returns_incomplete_instead_of_crashing(tmp_path, monkeypatch):
    monkeypatch.setattr(documents, "TESSDATA", tmp_path)
    monkeypatch.setattr(documents, "OCR_CACHE", tmp_path / "cache")
    monkeypatch.setattr(documents.subprocess, "run", lambda *args, **kwargs:
                        SimpleNamespace(returncode=139, stderr=b"native OCR failure"))
    assert documents._run_ocr(tmp_path / "scan.pdf", 30, "eng") == ("", False)


def test_ocr_retry_reuses_its_saved_pdf(tmp_path, monkeypatch):
    monkeypatch.setattr(documents, "ROOT", tmp_path)
    monkeypatch.setattr(documents, "TEXT_DIR", tmp_path / "text")
    monkeypatch.setattr(documents, "site_dir", lambda code: tmp_path / code)
    doc = pymupdf.open()
    doc.new_page()
    body = doc.tobytes()
    info = iter([documents.TextInfo(kind="pdf", scanned=True, retry=True, error="OCR time limit"),
                 documents.TextInfo(text="COMPLETE TEXT", kind="pdf")])
    monkeypatch.setattr(documents, "extract_text", lambda *args: next(info))
    row = {"id": 7, "doc_attempts": 0, "is_document": 1, "url": "https://t.example.org/scan.pdf",
           "site_code": "TST", "published_date": None, "first_seen": "2026-10-22T10:00:00",
           "title": "RFP", "file_ext": "pdf", "uploaded_source": None, "category": "RFP"}
    settings, kw = load_settings(), load_keywords()
    fetcher = FakeFetcher(len(body), body)
    first = documents.process_item(fetcher, row, settings, kw, claimed=set())
    assert first["doc_status"] == "error" and first["local_path"]
    second = documents.process_item(fetcher, dict(row, **first), settings, kw, claimed={first["local_path"]})
    assert second["doc_status"] == "ok" and second["doc_attempts"] == 2
    assert fetcher.downloads == 1 and first["local_path"] == second["local_path"]
