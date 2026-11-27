"""Local Ollama reader: enable switch, setup message, and document text handling."""

from tender_tracker import ollama_reader


def test_enabled_only_via_env(monkeypatch):
    monkeypatch.setenv("AI_READER", "ollama")
    assert ollama_reader.enabled()
    monkeypatch.setenv("AI_READER", "openrouter")
    assert not ollama_reader.enabled()


def test_not_set_up_without_model(monkeypatch, tmp_path):
    monkeypatch.setenv("AI_READER", "ollama")
    monkeypatch.delenv("OLLAMA_MODEL", raising=False)
    ok, message = ollama_reader.read_queue({}, timeout_minutes=1)
    assert not ok and "OLLAMA_MODEL" in message


def test_document_text_skips_pdf_and_truncates(tmp_path):
    pdf = tmp_path / "x.pdf"
    pdf.write_bytes(b"%PDF-1.4")
    assert ollama_reader._document_text({"read_this_file": str(pdf)}, 100) == ""
    txt = tmp_path / "x.txt"
    txt.write_text("a" * 500, encoding="utf-8")
    out = ollama_reader._document_text({"read_this_file": str(txt)}, 100)
    assert len(out) < 500 and out.startswith("a" * 100)
