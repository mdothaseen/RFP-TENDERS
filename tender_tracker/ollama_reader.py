"""Reads the queued documents through a local Ollama model (e.g. Google Gemma) - free, offline, no login.

Turned on only by the .env of the computer that runs Ollama:
    AI_READER=ollama
    OLLAMA_MODEL=gemma3:4b          (any `ollama list` model that fits in RAM/VRAM)
    OLLAMA_HOST=http://localhost:11434   (optional; default local)
    OLLAMA_MAX_CHARS=60000          (optional; document text sent per request)

It reads data/ai_queue.json (the same queue Claude Code / OpenRouter read) and writes
data/ai_results.json in the same format, so ai_extract.import_results saves the answers as usual.
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path

import requests

from .ai_extract import QUEUE_FILE, RESULTS_FILE
from .config import env
from .openrouter_reader import SYSTEM, _parse

log = logging.getLogger(__name__)


def enabled() -> bool:
    return env("AI_READER").lower() == "ollama"


def _host() -> str:
    return (env("OLLAMA_HOST") or "http://localhost:11434").rstrip("/")


def _document_text(item: dict, max_chars: int) -> str:
    path = Path(item.get("read_this_file") or "")
    if item.get("file_is_scanned_pdf") or not path.exists() or path.suffix.lower() == ".pdf":
        return ""
    text = path.read_text(encoding="utf-8", errors="replace")
    if len(text) > max_chars:
        text = text[:max_chars] + "\n[... rest of the document not sent ...]"
    return text


def _ask(session: requests.Session, model: str, system: str, user: str) -> tuple[dict | None, str]:
    body = {"model": model, "stream": False, "options": {"temperature": 0},
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}
    problem = ""
    for attempt in range(2):
        try:
            resp = session.post(f"{_host()}/api/chat", json=body, timeout=300)
        except requests.RequestException as exc:
            return None, f"network error: {type(exc).__name__}"
        if resp.status_code == 404:
            return None, f"model '{model}' not found in Ollama (run: ollama pull {model})"
        if resp.status_code >= 400:
            return None, f"HTTP {resp.status_code}"
        try:
            content = resp.json()["message"]["content"] or ""
        except (ValueError, KeyError, TypeError):
            return None, "empty answer"
        fields = _parse(content)
        if fields is not None:
            return fields, ""
        problem = "answer was not JSON"
        # Small local models sometimes wrap or truncate the JSON; ask once more, strictly.
        body["messages"] = body["messages"][:2] + [
            {"role": "assistant", "content": content},
            {"role": "user", "content": "Reply again with ONLY the single JSON object, no other text."}]
    return None, problem


def read_queue(settings, timeout_minutes: int = 20) -> tuple[bool, str]:
    """Reads data/ai_queue.json with local Ollama. Returns (results file written, message)."""
    model = env("OLLAMA_MODEL")
    if not model:
        return False, "Ollama is not set up: OLLAMA_MODEL is needed in .env (e.g. gemma3:4b)"
    try:
        max_chars = int(env("OLLAMA_MAX_CHARS", "60000"))
    except ValueError:
        max_chars = 60000
    queue = json.loads(QUEUE_FILE.read_text(encoding="utf-8"))
    decisions = json.dumps(queue.get("past_decisions") or {}, ensure_ascii=False)[:3000]
    system = SYSTEM.format(fields=json.dumps(queue["fields"], ensure_ascii=False, indent=1),
                           profile=queue.get("company_profile", ""), decisions=decisions)
    session = requests.Session()
    RESULTS_FILE.unlink(missing_ok=True)
    deadline = time.monotonic() + timeout_minutes * 60
    results, skipped, stop = [], 0, ""
    log.info("Ollama (%s) is reading %d queued documents", model, len(queue["items"]))
    for item in queue["items"]:
        if time.monotonic() > deadline:
            stop = f"time limit of {timeout_minutes} min reached"
            break
        text = _document_text(item, max_chars)
        if not text:
            skipped += 1
            continue
        user = (f"Organisation: {item['organisation']}\nTitle on website: {item['title_on_website']}\n"
                f"Text next to the link: {item['text_next_to_link']}\nDate on website: {item['date_on_website']}\n"
                f"Link: {item['link']}\n\n<document>\n{text}\n</document>")
        fields, problem = _ask(session, model, system, user)
        if problem.startswith("model '"):
            stop = problem
            break
        if fields is None:
            log.warning("Ollama could not read #%s: %s", item["id"], problem)
            continue
        results.append({"id": item["id"], "revision": item["revision"], **fields})
    if results:
        RESULTS_FILE.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    message = f"read {len(results)} of {len(queue['items'])} documents"
    if skipped:
        message += f"; {skipped} scanned PDF(s) without text skipped"
    if stop:
        message += f"; stopped: {stop}"
    return bool(results), message
