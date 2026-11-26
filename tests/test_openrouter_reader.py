"""OpenRouter reader (24x7 server): reads the same queue as Claude and writes the same results file."""

import json

from tender_tracker import ai_extract, openrouter_reader


class _Resp:
    def __init__(self, status, content=""):
        self.status_code = status
        self._content = content

    def json(self):
        return {"choices": [{"message": {"content": self._content}}]}


def _queue(tmp_path, monkeypatch, n=2):
    text = tmp_path / "doc.txt"
    text.write_text("RFP for third party assessment agency. Last date 2026-12-10.", encoding="utf-8")
    queue = tmp_path / "ai_queue.json"
    results = tmp_path / "ai_results.json"
    items = [{"id": i, "revision": 0, "organisation": "Test SSC", "title_on_website": "RFP", "text_next_to_link": "",
              "link": "https://x.example/rfp.pdf", "date_on_website": "", "read_this_file": str(text),
              "file_is_scanned_pdf": False} for i in range(1, n + 1)]
    queue.write_text(json.dumps({"company_profile": "Assessment agency", "past_decisions": {},
                                 "fields": ai_extract._FIELDS, "items": items}), encoding="utf-8")
    monkeypatch.setattr(openrouter_reader, "QUEUE_FILE", queue)
    monkeypatch.setattr(openrouter_reader, "RESULTS_FILE", results)
    monkeypatch.setattr(openrouter_reader.time, "sleep", lambda s: None)
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    monkeypatch.setenv("OPENROUTER_MODEL", "some/model:free")
    return results


def test_answers_are_written_in_the_claude_format(tmp_path, monkeypatch):
    results = _queue(tmp_path, monkeypatch)
    answer = "```json\n" + json.dumps({"doc_type": "RFP", "relevance": "High", "last_date_submission": "2026-12-10"}) + "\n```"
    monkeypatch.setattr(openrouter_reader.requests.Session, "post", lambda self, url, json, timeout: _Resp(200, answer))
    ok, message = openrouter_reader.read_queue(None)
    data = json.loads(results.read_text(encoding="utf-8"))
    assert ok and message.startswith("read 2 of 2")
    assert data[0]["id"] == 1 and data[0]["revision"] == 0 and data[0]["relevance"] == "High"


def test_rate_limit_stops_and_keeps_what_was_read(tmp_path, monkeypatch):
    results = _queue(tmp_path, monkeypatch, n=3)
    replies = iter([_Resp(200, '{"doc_type": "RFP"}'), _Resp(429)])
    monkeypatch.setattr(openrouter_reader.requests.Session, "post", lambda self, url, json, timeout: next(replies))
    ok, message = openrouter_reader.read_queue(None)
    assert ok and "busy or at its limit" in message
    assert len(json.loads(results.read_text(encoding="utf-8"))) == 1


def test_not_set_up_and_off_by_default(tmp_path, monkeypatch):
    _queue(tmp_path, monkeypatch)
    monkeypatch.delenv("OPENROUTER_API_KEY")
    monkeypatch.delenv("AI_READER", raising=False)
    assert not openrouter_reader.enabled()
    ok, message = openrouter_reader.read_queue(None)
    assert not ok and "not set up" in message


def test_busy_free_model_hands_over_to_the_paid_one(tmp_path, monkeypatch):
    results = _queue(tmp_path, monkeypatch, n=1)
    monkeypatch.setenv("OPENROUTER_MODEL", "free/model:free, cheap/paid")
    asked = []

    def post(self, url, json, timeout):
        asked.append(json["model"])
        return _Resp(429) if json["model"].endswith(":free") else _Resp(200, '{"doc_type": "EOI"}')

    monkeypatch.setattr(openrouter_reader.requests.Session, "post", post)
    ok, message = openrouter_reader.read_queue(None)
    assert ok and asked == ["free/model:free", "cheap/paid"] and "cheap/paid: 1" in message
    assert json.loads(results.read_text(encoding="utf-8"))[0]["doc_type"] == "EOI"
