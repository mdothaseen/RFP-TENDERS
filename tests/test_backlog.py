"""Backlog drain: old closed/low/non-tender retired; old live relevant kept for reading."""

from datetime import date, datetime

from tender_tracker import backlog
from tender_tracker.classify import classify
from tender_tracker.config import load_keywords, load_settings
from tender_tracker.discover.crawler import Found
from tender_tracker.store import Store

TODAY = date(2026, 11, 27)


def _add(store, kw, key, published, deadline, relevance, title):
    f = Found(key=key, site_code="TST", url=f"https://x.org/{key}.pdf", title=title, link_text="",
              context="", source_page="", via={"page"}, linked=True, is_document=True, published=published)
    item_id, _ = store.upsert(f, classify(kw, title), 1, False, datetime.now().isoformat(), {"Tender"})
    fields = {"relevance": relevance}
    if deadline:
        fields["deadline_hint"] = deadline
    store.update_item(item_id, **fields)
    return item_id


def test_retire_old_junk_keeps_old_live_relevant(tmp_path):
    store = Store(tmp_path / "t.db")
    kw, settings = load_keywords(), load_settings()
    ids = {
        "old_closed": _add(store, kw, "old_closed", date(2026, 9, 1), "2026-10-01", "High", "Tender for closed work"),
        "old_low_open": _add(store, kw, "old_low_open", date(2026, 9, 1), "2026-12-01", "Low", "Tender for construction"),
        "old_high_open": _add(store, kw, "old_high_open", date(2026, 9, 1), "2026-12-01", "High", "Tender for assessment"),
        "old_junk": _add(store, kw, "old_junk", date(2026, 9, 1), None, "Medium", "image description"),
        "undated_junk": _add(store, kw, "undated_junk", None, None, "Medium", "Training Calendar"),
        "new_high": _add(store, kw, "new_high", date(2026, 11, 20), None, "High", "Tender for new assessment"),
    }
    store.commit()
    retired = backlog.retire_old_junk(store, settings, kw, TODAY)
    skipped = {k for k, i in ids.items() if store.item(i)["doc_status"] == "skipped"}
    assert skipped == {"old_closed", "old_low_open", "old_junk", "undated_junk"}
    assert retired == 4
    assert store.item(ids["old_high_open"])["doc_status"] is None
    assert store.item(ids["new_high"])["doc_status"] is None
    # idempotent: a second pass retires nothing new
    assert backlog.retire_old_junk(store, settings, kw, TODAY) == 0


def test_drain_works_on_the_last_30_days_with_bigger_rounds(tmp_path):
    from tender_tracker import documents
    store = Store(tmp_path / "t.db")
    kw, settings = load_keywords(), load_settings()
    old = _add(store, kw, "old", date(2026, 9, 1), "2026-12-01", "High", "Tender for old assessment")
    new = _add(store, kw, "new", date(2026, 11, 20), None, "High", "Tender for new assessment")
    store.commit()
    limits = backlog.drain_settings(settings)
    assert limits.documents["max_per_run"] == settings.documents["backlog_per_run"] > settings.documents["max_per_run"]
    assert limits.ai["timeout_minutes"] == settings.documents["backlog_ai_minutes"]
    assert settings.documents["max_per_run"] == 60  # the scan's own limit is untouched
    batch = documents.plan_documents(store, limits, kw, TODAY, window_days=settings.documents["backlog_days"])
    assert {r["id"] for r in batch.rows} == {new}
    store.close()


def test_drain_waits_while_a_scan_runs(tmp_path, monkeypatch):
    store = Store(tmp_path / "t.db")
    monkeypatch.setattr(backlog.locking, "run_in_progress", lambda: True)
    called = []
    monkeypatch.setattr(backlog.enrich, "process_documents", lambda *a, **k: called.append(1) or 0)
    result = backlog.drain(store, load_settings(), load_keywords(), {}, type("A", (), {"workers": 1, "no_ai": True})(),
                           TODAY, 1)
    assert result["skipped"] == "scan running" and not called
    store.close()
