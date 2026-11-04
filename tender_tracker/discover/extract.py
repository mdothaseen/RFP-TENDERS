"""Pull links, ticker notices and page text out of an HTML page."""

from __future__ import annotations

import html as html_lib
import re
from dataclasses import dataclass, field

from bs4 import BeautifulSoup, Tag

from ..urls import DOC_EXTS, clean_url

TICKER_RE = re.compile(
    r"ticker|marquee|news-?scroll|scroll(?:ing)?[-_]?(?:news|text|notice)|breaking|flash|headline|"
    r"latest[-_]?news|announcement|notice[-_]?board|whats[-_]?new|blink|news[-_]?ticker|updates?[-_]?scroll",
    re.I,
)
_NOISE_LINE = re.compile(
    r"(visitor|hit counter|page views|last updated|last modified|copyright|©|all rights reserved|"
    r"\b\d{1,2}:\d{2}(:\d{2})?\s*(am|pm)?\b|screen reader|skip to main)",
    re.I,
)
_EXT = "|".join(sorted(DOC_EXTS))
_SCRIPT_DOC = re.compile(
    rf"""["']((?:https?:)?//[^"'\s<>]+?\.(?:{_EXT})|/[^"'\s<>]+?\.(?:{_EXT})|[\w.-]+/[^"'\s<>]+?\.(?:{_EXT}))(?:\?[^"'\s<>]*)?["']""",
    re.I,
)
_JS_URL = re.compile(r"""(?:window\.open|location(?:\.href)?\s*=|location\.replace)\s*\(?\s*['"]([^'"]+)['"]""", re.I)
_META_REFRESH = re.compile(r"url\s*=\s*['\"]?([^'\";]+)", re.I)
_TITLE_NEAR = re.compile(r'"(?:title|name|heading|label|caption)"\s*:\s*"([^"]{6,250})"', re.I)


@dataclass
class FoundLink:
    url: str | None
    text: str
    context: str
    kind: str                  # a, iframe, onclick, script, ticker
    in_ticker: bool = False


@dataclass
class ParsedPage:
    links: list[FoundLink] = field(default_factory=list)
    ticker_texts: list[FoundLink] = field(default_factory=list)
    title: str = ""
    text_lines: list[str] = field(default_factory=list)
    redirect: str | None = None
    link_count: int = 0


def _squash(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def _anchor_text(tag: Tag) -> str:
    text = _squash(tag.get_text(" ", strip=True))
    if not text:
        text = _squash(tag.get("title") or tag.get("aria-label") or "")
    if not text:
        img = tag.find("img")
        if img is not None:
            text = _squash(img.get("alt") or img.get("title") or "")
    return text


def _context(tag: Tag, limit: int = 600) -> str:
    """Text of the closest enclosing block (list item, table row, card) that is still short."""
    best = _squash(tag.get_text(" ", strip=True))
    node = tag
    for _ in range(5):
        node = node.parent
        if node is None or node.name in ("body", "html", "[document]"):
            break
        # A table cell belongs to its row even when it also links the tender's corrigenda; the row holds the date
        # ("EoI ... | CORRIGENDUM-I / Date Extension Corrigendum | Dec 12, 2022").
        if node.name not in ("tr", "li") and not (node.name == "td" and node.find_parent("tr") is not None):
            # A block holding several different links is a list of items, not this item's own text.
            hrefs = {a.get("href") for a in node.find_all("a", href=True, limit=6)}
            if len(hrefs) > 1:
                break
        text = _squash(node.get_text(" ", strip=True))
        if len(text) > limit:
            break
        best = text
        if node.name in ("li", "tr", "article"):
            break
    return best


def _in_ticker(tag: Tag) -> bool:
    for parent in tag.parents:
        if parent.name == "marquee":
            return True
        if parent.name in ("body", "html"):
            break
        attrs = " ".join(parent.get("class") or []) + " " + (parent.get("id") or "")
        if TICKER_RE.search(attrs):
            return True
    return False


def _script_docs(script_text: str, page_url: str) -> list[FoundLink]:
    found = []
    text = script_text.replace("\\u002F", "/").replace("\\/", "/")
    for m in _SCRIPT_DOC.finditer(text):
        url = clean_url(m.group(1), base=page_url)
        if not url:
            continue
        window = text[max(0, m.start() - 400):m.start()]
        titles = _TITLE_NEAR.findall(window)
        title = html_lib.unescape(titles[-1]) if titles else ""
        found.append(FoundLink(url, title, title, "script"))
    return found


def make_soup(html: str) -> BeautifulSoup:
    """lxml is fast, but drops everything after a second <html>/<body> (pages glued from two files)."""
    low = html[:2_000_000].lower()
    parser = "html.parser" if low.count("<body") > 1 or low.count("<html") > 1 else "lxml"
    return BeautifulSoup(html, parser)


def _decompose(tags) -> None:
    for tag in list(tags):
        try:
            if not getattr(tag, "decomposed", False):
                tag.decompose()
        except Exception:  # noqa: BLE001 - already removed with its parent
            pass


def parse_html(html: str, page_url: str, keep_fragments: bool = False) -> ParsedPage:
    page = ParsedPage()
    if not html:
        return page
    soup = make_soup(html)

    if soup.title and soup.title.string:
        page.title = _squash(soup.title.string)

    meta = soup.find("meta", attrs={"http-equiv": re.compile("refresh", re.I)})
    if meta and meta.get("content"):
        m = _META_REFRESH.search(meta["content"])
        if m:
            page.redirect = clean_url(m.group(1), base=page_url)
    if not page.redirect and len(html) < 4000:
        m = _JS_URL.search(html)
        if m:
            page.redirect = clean_url(m.group(1), base=page_url)

    for script in soup.find_all("script"):
        body = script.string or script.get_text() or ""
        low = body.lower()
        if body and (".pdf" in low or ".doc" in low or ".xls" in low or ".zip" in low):
            page.links.extend(_script_docs(body, page_url))
    _decompose(soup(["script", "style", "noscript", "svg", "template"]))

    for a in soup.find_all(["a", "area"], href=True):
        href = a.get("href", "").strip()
        if not href or href == "#" or href.lower().startswith(("javascript:", "mailto:", "tel:")):
            continue
        url = clean_url(href, base=page_url, keep_fragment=keep_fragments)
        if url:
            page.links.append(FoundLink(url, _anchor_text(a), _context(a), "a", _in_ticker(a)))

    for tag in soup.find_all(attrs={"onclick": True}):
        for m in _JS_URL.finditer(tag.get("onclick", "")):
            url = clean_url(m.group(1), base=page_url)
            if url:
                page.links.append(FoundLink(url, _anchor_text(tag), _context(tag), "onclick", _in_ticker(tag)))
    for attr in ("data-href", "data-url", "data-link", "data-file", "data-pdf"):
        for tag in soup.find_all(attrs={attr: True}):
            url = clean_url(tag.get(attr), base=page_url)
            if url:
                page.links.append(FoundLink(url, _anchor_text(tag), _context(tag), "onclick", _in_ticker(tag)))
    for tag in soup.find_all(["iframe", "embed", "object"]):
        src = tag.get("src") or tag.get("data")
        url = clean_url(src, base=page_url) if src else None
        if url:
            page.links.append(FoundLink(url, _squash(tag.get("title") or ""), "", "iframe"))

    page.link_count = sum(1 for link in page.links if link.kind == "a")

    # Ticker / scrolling notices that have no link of their own.
    containers = list(soup.find_all("marquee"))
    containers += [t for t in soup.find_all(class_=TICKER_RE) if t.name not in ("a", "span", "i")]
    containers += [t for t in soup.find_all(id=TICKER_RE) if t.name not in ("a", "span", "i")]
    seen_texts: set[str] = set()
    for box in containers:
        pieces = box.find_all(["li", "p"]) or [box]
        for piece in pieces:
            if piece.find("a", href=True) is not None:
                continue
            text = _squash(piece.get_text(" ", strip=True))
            for part in re.split(r"\s*(?:\||•|★|✦|◆|»)\s*", text):
                part = part.strip()
                if 25 <= len(part) <= 600 and part not in seen_texts:
                    seen_texts.add(part)
                    page.ticker_texts.append(FoundLink(None, part, part, "ticker", True))

    # Text lines for change detection (header, footer and counters removed).
    _decompose(soup.find_all(["footer", "header", "nav"]))
    _decompose(soup.find_all(class_=re.compile(r"footer|counter|visitor|clock|weather|cookie", re.I)))
    body = soup.body or soup
    lines = []
    for line in body.get_text("\n", strip=True).splitlines():
        line = _squash(line)
        if len(line) >= 8 and not _NOISE_LINE.search(line):
            lines.append(line)
    page.text_lines = lines[:3000]
    return page


def main_text(html: str, limit: int = 400_000) -> str:
    """Readable text of an HTML page (for sending a notice page to the AI)."""
    soup = make_soup(html or "")
    head = []
    if soup.title and soup.title.string:
        head.append(_squash(soup.title.string))
    for attrs in ({"property": "og:title"}, {"name": "description"}, {"property": "og:description"}):
        tag = soup.find("meta", attrs=attrs)
        if tag is not None and tag.get("content"):
            head.append(_squash(tag["content"]))
    _decompose(soup(["script", "style", "noscript", "svg", "template", "nav", "footer", "header"]))
    root = soup.find("main") or soup.find("article") or soup.body or soup
    text = root.get_text("\n", strip=True)
    text = "\n".join(dict.fromkeys(h for h in head if h)) + "\n\n" + text
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()[:limit]
