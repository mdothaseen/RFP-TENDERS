"""ClaudeReader request/response handling with a fake API client (no network, no API key needed)."""

import json
from types import SimpleNamespace

from tender_tracker import ai_extract
from tender_tracker.config import load_settings

ANSWER = {field: "" for field in ai_extract._FIELDS}
ANSWER.update({"doc_type": "EOI", "title": "EOI for Empanelment of Assessment Agencies",
               "last_date_submission": "2026-10-15", "emd": "Rs. 50,000", "relevance": "High"})


class FakeMessages:
    def __init__(self):
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(
            stop_reason="end_turn", model=kwargs["model"],
            content=[SimpleNamespace(type="thinking", thinking=""),
                     SimpleNamespace(type="text", text=json.dumps(ANSWER))],
            usage=SimpleNamespace(input_tokens=12000, output_tokens=900, cache_creation_input_tokens=0,
                                  cache_read_input_tokens=0),
        )


class FakeClient:
    def __init__(self, **_):
        self.beta = SimpleNamespace(messages=FakeMessages())


def test_reader_builds_request_and_parses(tmp_path, monkeypatch):
    monkeypatch.setattr(ai_extract.anthropic, "Anthropic", FakeClient)
    monkeypatch.setenv("TRACKER_CLAUDE_EFFORT", "low")
    monkeypatch.setenv("TRACKER_CLAUDE_MODEL", "claude-opus-5-5")
    monkeypatch.setattr(ai_extract, "ROOT", tmp_path)
    (tmp_path / "doc.txt").write_text("EXPRESSION OF INTEREST ... last date 15.10.2026", encoding="utf-8")
    row = {"local_path": None, "scanned": 0, "text_path": "doc.txt", "text_chars": 5000, "title": "EOI for AAs",
           "context": "EOI for AAs 30 Sep", "url": "https://x.org/eoi.pdf", "published_date": "2026-09-30"}
    reader = ai_extract.ClaudeReader(load_settings())
    info, meta = reader.read(row, "Test SSC")
    assert info.last_date_submission == "2026-10-15" and info.relevance == "High"
    call = reader.client.beta.messages.calls[0]
    assert call["model"] == "claude-opus-5-5"
    assert call["fallbacks"] == "default" and call["betas"] == [ai_extract.FALLBACK_BETA]
    assert call["output_config"]["format"]["type"] == "json_schema"
    assert call["output_config"]["effort"] == "low"
    assert "<document>" in call["messages"][0]["content"][0]["text"]
    assert meta["input_tokens"] == 12000
    assert round(ai_extract.cost_usd("claude-opus-5-5", 12000, 900), 4) == 0.066


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
