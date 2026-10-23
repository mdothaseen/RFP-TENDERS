"""Read tender documents with the Claude API and pull out the details for the spreadsheet."""

from __future__ import annotations

import base64
import json
import logging
from datetime import date, datetime, timedelta
from pathlib import Path

import anthropic
from pydantic import BaseModel, ValidationError

from .config import ROOT, Settings, env
from .dates import parse_iso

log = logging.getLogger(__name__)

DOC_TYPES = ["Tender", "RFP", "RFQ", "EOI", "Empanelment", "Corrigendum", "Notice", "Circular",
             "Recruitment", "Result", "Other"]
# USD per million tokens (input, output) - used only for the cost estimate shown in Excel.
PRICES = {
    "claude-opus-5-5": (4.0, 20.0), "claude-opus-5": (5.0, 25.0), "claude-sonnet-5-5": (2.0, 10.0),
    "claude-sonnet-5": (2.0, 10.0), "claude-haiku-4-5": (1.0, 5.0), "claude-fable-5-1": (10.0, 50.0),
}
FALLBACK_BETA = "server-side-fallback-2026-07-01"

_FIELDS = {
    "doc_type": "What kind of document this is.",
    "title": "Clean, complete title of the document.",
    "issuing_organization": "Organisation that issued it.",
    "reference_no": "Reference / tender / file number, if printed.",
    "issue_date": "Date of issue, YYYY-MM-DD.",
    "last_date_submission": "Final date to submit the bid / proposal / application, YYYY-MM-DD. For a corrigendum that extends it, the new date.",
    "last_time_submission": "Time on the last date, HH:MM 24-hour.",
    "prebid_date": "Pre-bid meeting or last date for queries, YYYY-MM-DD.",
    "bid_opening_date": "Technical bid opening date, YYYY-MM-DD.",
    "other_key_dates": "Any other important dates, short.",
    "emd": "Earnest money deposit / bid security, as written (with currency), or 'Not required'.",
    "tender_fee": "Tender / processing / application fee, as written.",
    "estimated_value": "Estimated contract value or budget, as written.",
    "contract_period": "Duration of the assignment / empanelment.",
    "who_can_apply": "Type of organisation or person that can apply.",
    "eligibility_summary": "Key eligibility criteria as short points separated by '; ' (turnover, experience, registrations, certifications).",
    "scope_summary": "2-3 plain sentences: what work is asked for, where, how much / how many.",
    "location": "States / districts / places covered.",
    "submission_mode": "How and where to submit (email address, portal, GeM, physical address).",
    "contact": "Contact person, email and phone.",
    "corrigendum_of": "If this is a corrigendum/addendum, the title or reference of the original document.",
    "relevance": "High / Medium / Low relevance for the company profile.",
    "relevance_reason": "One short sentence explaining the relevance.",
    "action_needed": "One line: what the company should do and by when.",
}


class TenderInfo(BaseModel):
    doc_type: str
    title: str
    issuing_organization: str
    reference_no: str
    issue_date: str
    last_date_submission: str
    last_time_submission: str
    prebid_date: str
    bid_opening_date: str
    other_key_dates: str
    emd: str
    tender_fee: str
    estimated_value: str
    contract_period: str
    who_can_apply: str
    eligibility_summary: str
    scope_summary: str
    location: str
    submission_mode: str
    contact: str
    corrigendum_of: str
    relevance: str
    relevance_reason: str
    action_needed: str


def _schema() -> dict:
    props = {}
    for name, desc in _FIELDS.items():
        prop = {"type": "string", "description": desc}
        if name == "doc_type":
            prop["enum"] = DOC_TYPES
        elif name == "relevance":
            prop["enum"] = ["High", "Medium", "Low"]
        props[name] = prop
    return {"type": "object", "properties": props, "required": list(_FIELDS), "additionalProperties": False}


SCHEMA = _schema()

SYSTEM = """You read documents published by Indian Sector Skill Councils, NSDC and NCVET: tenders, RFPs, RFQs, \
EOIs, empanelment calls, corrigenda, notices, circulars and recruitment notices. You fill one row of a tender \
tracking spreadsheet for the company described below.

Company profile (use it only for the relevance fields):
{profile}

Rules:
- Use only what the document says. If something is not in the document, return an empty string for that field.
- Dates as YYYY-MM-DD, times as HH:MM (24-hour). When a corrigendum changes a date, give the new date.
- last_date_submission is the final date to submit the bid, proposal or application - not the pre-bid or opening date.
- Keep amounts exactly as written, with the currency.
- Write short, plain English that a busy reader can scan."""


def cost_usd(model: str, input_tokens: int, output_tokens: int) -> float | None:
    price = PRICES.get(model)
    if not price:
        return None
    return input_tokens / 1e6 * price[0] + output_tokens / 1e6 * price[1]


class ClaudeReader:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.model = env("TRACKER_CLAUDE_MODEL", "claude-opus-5-5")
        self.effort = env("TRACKER_CLAUDE_EFFORT", "low").lower()
        self.use_fallbacks = True
        self.use_effort = not self.model.startswith(("claude-haiku-4", "claude-sonnet-4-5"))
        # An explicit base URL, so an ANTHROPIC_BASE_URL set by other tools (e.g. a Claude Code session)
        # never redirects the tracker's requests. TRACKER_ANTHROPIC_BASE_URL can point to a proxy if needed.
        self.client = anthropic.Anthropic(
            max_retries=3, timeout=600,
            base_url=env("TRACKER_ANTHROPIC_BASE_URL", "https://api.anthropic.com"),
        )
        self.system = SYSTEM.format(profile=settings.company_profile or "Not given.")

    def _pdf_block(self, path: Path, max_pages: int) -> tuple[dict, bool]:
        import pymupdf as fitz

        data = path.read_bytes()
        partial = False
        with fitz.open(path) as doc:
            if doc.page_count > max_pages or len(data) > 20_000_000:
                partial = True
                doc.select(list(range(min(max_pages, doc.page_count))))
                data = doc.tobytes(garbage=3, deflate=True)
        block = {"type": "document",
                 "source": {"type": "base64", "media_type": "application/pdf",
                            "data": base64.standard_b64encode(data).decode("ascii")}}
        return block, partial

    def _call(self, content: list[dict]):
        kwargs = dict(
            model=self.model, max_tokens=16000, system=self.system,
            messages=[{"role": "user", "content": content}],
            output_config={"format": {"type": "json_schema", "schema": SCHEMA}},
        )
        if self.use_effort:
            kwargs["output_config"]["effort"] = self.effort
        if self.use_fallbacks:
            kwargs["betas"] = [FALLBACK_BETA]
            kwargs["fallbacks"] = "default"
        return self.client.beta.messages.create(**kwargs)

    def read(self, row, org_name: str) -> tuple[TenderInfo, dict]:
        """Returns the extracted info and usage/meta. Raises on failure."""
        max_chars = int(self.settings.ai.get("max_text_chars", 400_000))
        max_pages = int(self.settings.ai.get("max_pdf_pages", 100))
        partial = False
        content: list[dict] = []
        local = ROOT / row["local_path"] if row["local_path"] else None
        has_text = bool(row["text_path"]) and (row["text_chars"] or 0) >= 500
        if row["scanned"] and not has_text and local is not None and local.suffix.lower() == ".pdf" and local.exists():
            block, partial = self._pdf_block(local, max_pages)
            content.append(block)
        else:
            text = (ROOT / row["text_path"]).read_text(encoding="utf-8", errors="replace")
            if len(text) >= max_chars:
                partial = True
            content.append({"type": "text", "text": f"<document>\n{text[:max_chars]}\n</document>"})
        meta = [
            f"Organisation: {org_name}",
            f"Title on the website: {row['title'] or ''}",
            f"Text next to the link on the website: {(row['context'] or '')[:500]}",
            f"Link: {row['url'] or ''}",
            f"Date shown on the website: {row['published_date'] or 'unknown'}",
            f"Today's date: {date.today().isoformat()}",
        ]
        if partial:
            meta.append("Note: the document is long; only its first part is included.")
        meta.append("Fill in every field for this document.")
        content.append({"type": "text", "text": "\n".join(meta)})

        for attempt in range(3):
            try:
                response = self._call(content)
                break
            except anthropic.BadRequestError as exc:
                message = str(exc).lower()
                if self.use_fallbacks and "fallback" in message:
                    log.warning("Refusal fallbacks not accepted for %s; continuing without them", self.model)
                    self.use_fallbacks = False
                    continue
                if self.use_effort and "effort" in message:
                    self.use_effort = False
                    continue
                raise
        else:
            raise RuntimeError("request was rejected")

        if response.stop_reason == "refusal":
            raise RuntimeError("Claude declined to read this document")
        if response.stop_reason == "max_tokens":
            raise RuntimeError("answer was cut off (max_tokens)")
        text = next((b.text for b in response.content if getattr(b, "type", "") == "text"), "")
        try:
            info = TenderInfo.model_validate(json.loads(text))
        except (ValueError, ValidationError) as exc:
            raise RuntimeError(f"could not parse the answer: {str(exc)[:120]}") from exc
        usage = response.usage
        in_tokens = (usage.input_tokens or 0) + (getattr(usage, "cache_creation_input_tokens", 0) or 0) \
            + (getattr(usage, "cache_read_input_tokens", 0) or 0)
        out_tokens = usage.output_tokens or 0
        return info, {"model": response.model or self.model, "input_tokens": in_tokens,
                      "output_tokens": out_tokens, "partial": partial}


def select_items(store, settings: Settings, baseline: bool, today: date) -> list:
    categories = set(settings.ai.get("categories") or [])
    max_attempts = int(settings.ai.get("max_attempts", 3))
    since_baseline = (today - timedelta(days=int(settings.ai.get("baseline_days", 45)))).isoformat()
    since_recent = (today - timedelta(days=int(settings.ai.get("recent_days", 120)))).isoformat()
    rows = store.items("removed=0 AND ai_status IN ('pending','failed') AND ai_attempts < ?", (max_attempts,))
    read_from = str(settings.ai.get("read_from") or "")
    chosen = []
    for row in rows:
        if read_from and (row["first_seen"] or "")[:10] < read_from:
            store.update_item(row["id"], ai_status="skipped",
                              ai_error=f"found before {read_from} - not read by AI")
            continue
        category = row["content_category"] or row["category"]
        if category not in categories:
            continue
        if row["doc_status"] not in ("ok", "html"):
            continue
        published = row["published_date"]
        deadline = row["deadline_hint"]
        # Items from a site's first scan (baseline) are read only when clearly recent.
        row_baseline = baseline or bool(row["baseline"])
        since = since_baseline if row_baseline else since_recent
        fresh = (published and published >= since) or (deadline and deadline >= today.isoformat()) \
            or (not published and row["linked_seen"] and not row["baseline"])
        if not fresh:
            continue
        if not row["text_path"] and not row["scanned"]:
            store.update_item(row["id"], ai_status="skipped", ai_error="document text could not be read")
            continue
        chosen.append(row)
    rank = {"High": 0, "Medium": 1, "Low": 2}
    chosen.sort(key=lambda r: (rank.get(r["relevance"], 3), -(parse_iso(r["published_date"]) or date.min).toordinal()))
    return chosen[: int(settings.ai.get("max_docs_per_run", 40))]


def run_ai(store, settings: Settings, rows: list, sites: dict, run_id: int) -> dict:
    """Read the selected documents. Returns counters for the run summary."""
    summary = {"done": 0, "failed": 0, "copied": 0, "cost": 0.0, "skipped_reason": ""}
    if not rows:
        return summary
    if not (env("ANTHROPIC_API_KEY") or env("ANTHROPIC_AUTH_TOKEN")):
        summary["skipped_reason"] = "ANTHROPIC_API_KEY is not set in .env - documents were not read by AI"
        log.warning(summary["skipped_reason"])
        return summary
    reader = ClaudeReader(settings)
    for row in rows:
        budget = float(settings.ai.get("daily_cost_limit_usd", 2.0))
        if budget > 0 and store.ai_cost(since_iso=date.today().isoformat())[1] >= budget:
            summary["skipped_reason"] = "Daily estimated API cost limit reached; pending documents kept for later"
            break
        now = datetime.now().isoformat(timespec="seconds")
        if row["sha256"]:
            twin = store.find_by_sha(row["sha256"], row["id"])
            if twin is not None:
                if store.update_item_revision(row["id"], row["revision"], ai_status="done", ai_json=twin["ai_json"],
                        ai_model=twin["ai_model"], ai_at=now, ai_error=None, ai_partial=twin["ai_partial"]):
                    summary["copied"] += 1
                store.commit()
                continue
        site = sites.get(row["site_code"])
        try:
            info, meta = reader.read(row, site.name if site else row["site_code"])
        except anthropic.AuthenticationError:
            summary["skipped_reason"] = "the Anthropic API key was rejected - check ANTHROPIC_API_KEY in .env"
            log.error(summary["skipped_reason"])
            break
        except anthropic.PermissionDeniedError as exc:
            summary["skipped_reason"] = f"Anthropic API permission problem: {exc}"
            log.error(summary["skipped_reason"])
            break
        except (anthropic.APIStatusError, anthropic.APIConnectionError, RuntimeError, OSError) as exc:
            log.warning("[%s] AI could not read %s: %s", row["site_code"], row["url"], exc)
            store.update_item_revision(row["id"], row["revision"], ai_status="failed", ai_attempts=(row["ai_attempts"] or 0) + 1,
                              ai_error=str(exc)[:300], ai_at=now)
            store.commit()
            summary["failed"] += 1
            continue
        cost = cost_usd(meta["model"], meta["input_tokens"], meta["output_tokens"])
        saved = store.update_item_revision(row["id"], row["revision"], ai_status="done", ai_json=info.model_dump_json(), ai_model=meta["model"],
                          ai_at=now, ai_error=None, ai_attempts=(row["ai_attempts"] or 0) + 1,
                          ai_partial=int(meta["partial"]))
        store.log_ai_usage(run_id, row["id"], meta["model"], meta["input_tokens"], meta["output_tokens"], cost)
        store.commit()
        summary["done"] += int(saved)
        summary["cost"] += cost or 0.0
        log.info("[%s] AI read: %s (last date %s)", row["site_code"], info.title[:80], info.last_date_submission)
    return summary


def ai_fields(row) -> dict:
    """The AI answer of an item as a dict (empty when not read yet)."""
    if not row["ai_json"]:
        return {}
    try:
        return json.loads(row["ai_json"])
    except ValueError:
        return {}


# ---------------------------------------------------------------------------------------------- Claude app mode
QUEUE_FILE = ROOT / "data" / "ai_queue.json"
RESULTS_FILE = ROOT / "data" / "ai_results.json"


def export_queue(store, settings: Settings, sites: dict, today: date) -> int:
    """Write data/ai_queue.json: the documents the Claude app should read, with instructions and fields."""
    rows = select_items(store, settings, False, today)
    store.commit()
    items = []
    for row in rows:
        site = sites.get(row["site_code"])
        has_text = bool(row["text_path"]) and (row["text_chars"] or 0) >= 500
        scanned_pdf = row["scanned"] and not has_text and row["local_path"] and row["local_path"].lower().endswith(".pdf")
        items.append({
            "id": row["id"],
            "revision": row["revision"],
            "organisation": site.name if site else row["site_code"],
            "title_on_website": row["title"] or "",
            "text_next_to_link": (row["context"] or "")[:500],
            "link": row["url"] or "",
            "date_on_website": row["published_date"] or "",
            "read_this_file": str(ROOT / (row["local_path"] if scanned_pdf else row["text_path"])),
            "file_is_scanned_pdf": bool(scanned_pdf),
        })
    QUEUE_FILE.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "created": datetime.now().isoformat(timespec="seconds"),
        "company_profile": settings.company_profile,
        "instructions": [
            "Read each file in read_this_file and fill every field listed in 'fields' for that item.",
            "Use only what the document says; unknown fields = empty string.",
            "Dates YYYY-MM-DD, times HH:MM (24h). For a corrigendum that changes a date, give the new date.",
            "last_date_submission = final date to submit the bid/proposal/application (not pre-bid/opening).",
            "doc_type must be one of: " + ", ".join(DOC_TYPES) + ". relevance must be High, Medium or Low "
            "(judged against company_profile).",
            f"Write a JSON list of objects {{\"id\": <id>, \"revision\": <revision from queue>, <all fields>}} to {RESULTS_FILE}.",
        ],
        "fields": _FIELDS,
        "items": items,
    }
    QUEUE_FILE.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return len(items)


def import_results(store, path: Path, expected: dict[int, int] | None = None) -> tuple[int, list[str]]:
    """Store answers written by the Claude app. Returns (saved, problems)."""
    data = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(data, dict):
        data = data.get("items") or data.get("results") or []
    saved, problems = 0, []
    now = datetime.now().isoformat(timespec="seconds")
    for entry in data:
        try:
            item_id = int(entry.get("id"))
            values = {k: str(entry.get(k) or "") for k in _FIELDS}
            if values["doc_type"] not in DOC_TYPES:
                values["doc_type"] = "Other"
            if values["relevance"] not in ("High", "Medium", "Low"):
                values["relevance"] = "Medium"
            info = TenderInfo.model_validate(values)
        except (TypeError, ValueError, ValidationError) as exc:
            problems.append(f"{entry.get('id')}: {str(exc)[:100]}")
            continue
        if store.item(item_id) is None:
            problems.append(f"{item_id}: unknown id")
            continue
        revision = expected.get(item_id) if expected is not None else entry.get("revision", 0)
        try:
            revision = int(revision) if revision is not None else None
        except (TypeError, ValueError):
            revision = None
        if revision is None or not store.update_item_revision(item_id, revision, ai_status="done", ai_json=info.model_dump_json(),
                                                             ai_model="claude-app", ai_at=now, ai_error=None):
            problems.append(f"{item_id}: document changed while being read; stale AI answer discarded")
            continue
        saved += 1
    store.commit()
    return saved, problems
