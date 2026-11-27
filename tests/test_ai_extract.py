"""Saving the Claude app's answers and choosing which documents it reads."""

import json

from tender_tracker import ai_extract
from tender_tracker.config import load_settings


def test_import_results_from_claude_app(tmp_path):
    from datetime import datetime
    from tender_tracker.classify import classify
    from tender_tracker.config import load_keywords
    from tender_tracker.discover.crawler import Found
    from tender_tracker.store import Store

    store = Store(tmp_path / "t.db")
    kw = load_keywords()
    f = Found(key="k1", site_code="TST", url="https://x.org/eoi.pdf", title="EOI for AAs", link_text="",
              context="", source_page="", via={"page"}, linked=True, is_document=True)
    item_id, _ = store.upsert(f, classify(kw, f.title), 1, False, datetime.now().isoformat(), {"EOI"})
    answer = {"id": item_id, "doc_type": "EOI", "title": "EOI for AAs", "last_date_submission": "2026-10-20",
              "relevance": "High", "emd": "Nil"}
    (tmp_path / "r.json").write_text(json.dumps([answer, {"id": 999}]), encoding="utf-8")
    saved, problems = ai_extract.import_results(store, tmp_path / "r.json")
    assert saved == 1 and problems and "999" in problems[0]
    row = store.item(item_id)
    assert row["ai_status"] == "done" and row["ai_model"] == "claude-app"
    assert json.loads(row["ai_json"])["last_date_submission"] == "2026-10-20"


def test_new_site_first_scan_old_items_not_sent_to_ai(tmp_path):
    from datetime import date, datetime
    from tender_tracker.classify import classify
    from tender_tracker.config import load_keywords
    from tender_tracker.discover.crawler import Found
    from tender_tracker.store import Store

    store = Store(tmp_path / "t.db")
    kw = load_keywords()
    settings = load_settings()
    settings.ai["read_from"] = ""
    ids = {}
    for key, site_baseline, published in [("old", True, None), ("recent", True, date(2026, 9, 25)),
                                          ("later", False, None)]:
        f = Found(key=key, site_code="NEW", url=f"https://x.org/{key}.pdf", title=f"Tender for {key} work",
                  link_text="", context="", source_page="", via={"page"}, linked=True, is_document=True,
                  published=published)
        ids[key], _ = store.upsert(f, classify(kw, f.title), 1, site_baseline, datetime.now().isoformat(), {"Tender"})
        store.update_item(ids[key], doc_status="ok", text_path="x.txt")
    chosen = {row["id"] for row in ai_extract.select_items(store, settings, False, date(2026, 10, 2))}
    assert chosen == {ids["recent"], ids["later"]}
    assert store.site_codes_with_items() == {"NEW"}


def test_closed_and_old_tenders_leave_the_queue(tmp_path):
    from datetime import date, datetime
    from tender_tracker import documents
    from tender_tracker.classify import classify
    from tender_tracker.config import load_keywords
    from tender_tracker.discover.crawler import Found
    from tender_tracker.store import Store

    store = Store(tmp_path / "t.db")
    kw, settings = load_keywords(), load_settings()
    today = date(2026, 11, 4)
    cases = {  # key: (site baseline, published, deadline, expected to stay pending)
        "closed": (False, date(2026, 10, 20), "2026-10-30", False),
        "old": (False, date(2026, 5, 1), None, False),
        "old_but_open": (False, date(2026, 5, 1), "2026-11-20", True),
        "first_scan_undated": (True, None, None, False),
        "recent": (False, date(2026, 10, 25), None, True),
        "later_undated": (False, None, None, True),
    }
    ids = {}
    for key, (site_baseline, published, _, _) in cases.items():
        f = Found(key=key, site_code="TST", url=f"https://x.org/{key}.pdf", title=f"Tender for {key} work",
                  link_text="", context="", source_page="", via={"page"}, linked=True, is_document=True,
                  published=published)
        ids[key], _ = store.upsert(f, classify(kw, f.title), 1, site_baseline, datetime.now().isoformat(), {"Tender"})
        if cases[key][2]:
            store.update_item(ids[key], deadline_hint=cases[key][2])
    store.commit()

    assert ai_extract.retire_stale(store, settings, today) == 3
    for key, (*_, stays) in cases.items():
        row = store.item(ids[key])
        assert (row["ai_status"] == "pending") is stays, key
        assert stays or row["ai_error"].startswith(ai_extract.STALE)
    planned = {r["id"] for r in documents.plan_documents(store, settings, kw, today).rows}
    # Downloads honour documents.baseline_days: an old-but-open tender stays in the AI queue but is
    # not re-downloaded once its published date is outside the window (undated links still qualify).
    from datetime import timedelta
    window = (today - timedelta(days=int(settings.documents.get("baseline_days", 365)))).isoformat()
    expect = {ids[k] for k, (_, published, _, stays) in cases.items()
              if stays and (published is None or published.isoformat() >= window)}
    assert planned == expect
    assert ai_extract.retire_stale(store, settings, today) == 0


def test_ollama_prefilter_skips_documents_without_our_work(tmp_path):
    """With a slow local model, a document whose first ~2 pages never mention training/assessment is not read."""
    from datetime import date, datetime
    from tender_tracker.classify import classify
    from tender_tracker.config import load_keywords
    from tender_tracker.discover.crawler import Found
    from tender_tracker.store import Store

    store = Store(tmp_path / "t.db")
    kw, settings = load_keywords(), load_settings()
    texts = {
        "assessment": "Notice inviting tender. Selection of third party assessment agency for PMKVY candidates.",
        "late_mention": "Notice inviting tender for civil works. " + "x " * 5000 + "assessment agency",
        "furniture": "Notice inviting tender for supply of office furniture and chairs.",
    }
    ids = {}
    for key, text in texts.items():
        f = Found(key=key, site_code="TST", url=f"https://x.org/{key}.pdf", title=f"Tender No. {len(key)}",
                  link_text="", context="", source_page="", via={"page"}, linked=True, is_document=True,
                  published=date(2026, 11, 20))
        ids[key], _ = store.upsert(f, classify(kw, "Tender"), 1, False, datetime.now().isoformat(), {"Tender"})
        path = tmp_path / f"{key}.txt"
        path.write_text(text, encoding="utf-8")
        store.update_item(ids[key], doc_status="ok", text_path=str(path), category="Tender")
    store.commit()

    settings.ai["prefilter_chars"] = 6000
    chosen = {r["id"] for r in ai_extract.select_items(store, settings, False, date(2026, 11, 27))}
    assert chosen == {ids["assessment"]}
    for key in ("late_mention", "furniture"):
        row = store.item(ids[key])
        assert row["ai_status"] == "skipped" and row["relevance"] == "Low" and row["ai_error"].startswith("prefilter")

    # Without the setting (Claude on the laptop) nothing is filtered.
    settings.ai["prefilter_chars"] = 0
    store.update_item(ids["furniture"], ai_status="pending", ai_error=None)
    assert ids["furniture"] in {r["id"] for r in ai_extract.select_items(store, settings, False, date(2026, 11, 27))}
    store.close()
