"""Keyword classification of links and documents."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .config import Keywords
from .urls import filename_from_url, host_of

# Display order, most important first.
CATEGORY_ORDER = [
    "Tender", "RFP", "EOI", "Empanelment", "Corrigendum", "Notice", "Recruitment", "ToT-ToA/Result", "News", "Other",
]


@dataclass
class Classification:
    category: str
    matched: list[str] = field(default_factory=list)
    relevance: str = "Low"

    def is_tender(self, keywords: Keywords) -> bool:
        return self.category in keywords.tender_categories


def _readable_filename(url: str) -> str:
    name = filename_from_url(url or "")
    return re.sub(r"[_+.\-]+", " ", name)


def _first_category(keywords: Keywords, text: str, only: list[str] | None = None) -> tuple[str | None, list[str]]:
    for name, patterns in keywords.categories.items():
        if only is not None and name not in only:
            continue
        hits = [p.pattern for p in patterns if p.search(text)]
        if hits:
            return name, hits
    return None, []


def relevance(keywords: Keywords, category: str, text: str) -> str:
    """First guess before Claude reads the document (Claude's answer replaces it).

    High   = the company's own work (relevance_high), in a tender-type item
    Medium = work close to it (relevance_medium), or a tender whose title does not say what the work is
    Low    = work the company does not do (relevance_low), and everything that is not tender-type
    """
    high = any(p.search(text) for p in keywords.relevance_high)
    tender = category in keywords.tender_categories
    if high:
        return "High" if tender else "Medium"
    if not tender:
        return "Low"
    if any(p.search(text) for p in keywords.relevance_medium):
        return "Medium"
    if any(p.search(text) for p in keywords.relevance_low):
        return "Low"
    return "Medium"


def classify(keywords: Keywords, title: str, context: str = "", url: str = "") -> Classification:
    """Category from the title first, then the file name, then the surrounding text."""
    title = title or ""
    fname = _readable_filename(url)
    head = f"{title} {fname}"
    everything = f"{head} {context or ''}"
    path = url.lower() if url else ""

    # "Price Bid", "Declaration GST", "Annexure-II": parts of a tender listed next to it, not tenders themselves.
    if any(p.search(title.strip()) for p in keywords.attachment_titles):
        return Classification("Other", ["tender attachment"], "Low")
    category, hits = _first_category(keywords, head, keywords.tender_categories)
    if not category:
        if any(p.search(head) for p in keywords.other_patterns):
            return Classification("Other", [], "Low")
        if re.search(r"/blogs?/|/news/|/gallery/|/events?/", path) and not host_of(url).startswith("drive."):
            category, hits = "News", ["url path"]
        else:
            category, hits = _first_category(keywords, head)
    if not category:
        category, hits = _first_category(keywords, everything)
    if not category:
        category = "Other"
    return Classification(category, hits, relevance(keywords, category, everything))


def content_category(keywords: Keywords, text: str) -> str | None:
    """Category suggested by the first pages of a document, if it is clearly tender-type."""
    head = (text or "")[:12000]
    best, best_hits = None, 0
    for category, patterns in keywords.content_phrases.items():
        hits = sum(1 for p in patterns if p.search(head))
        if hits > best_hits:
            best, best_hits = category, hits
    return best


_FILLER = re.compile(
    r"\b(click|here|to|apply|now|view|download|pdf|docx?|documents?|the|open|read|more|details|link|file|"
    r"this|see|please|for|and|or|of|eoi|rfp|notice|new|latest|attachment|form)\b|[^\w\s]", re.I)


def is_vague(keywords: Keywords, text: str) -> bool:
    """True for link texts such as 'Click here to view EoI document.' that do not name the document."""
    t = (text or "").strip().lower()
    if len(t) < 4:
        return True
    if any(p.search(t) for p in keywords.vague_titles):
        return True
    meaningful = [w for w in _FILLER.sub(" ", t).split() if len(w) > 1]
    return len(meaningful) < 2


def category_rank(category: str) -> int:
    try:
        return CATEGORY_ORDER.index(category)
    except ValueError:
        return len(CATEGORY_ORDER)
