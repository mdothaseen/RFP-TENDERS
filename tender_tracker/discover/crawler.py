"""Scan one website with every discovery layer and return what was found.

Runs inside a worker thread: it owns its Fetcher and (lazily) its Renderer and never touches the database.
"""

from __future__ import annotations

import heapq
import time
import itertools
import logging
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from urllib.parse import unquote, urlsplit

from ..classify import classify, is_vague
from ..config import Keywords, Settings, Site
from ..dates import date_from_url, listing_dates, parse_iso, upload_date_from_url
from ..fetcher import Fetcher, Response
from ..renderer import Renderer
from ..urls import (
    canonical_key, clean_url, doc_urls_in_query, filename_from_url, host_of, is_document_url, text_key,
    title_from_filename, url_ext,
)
from . import sitemap
from .extract import FoundLink, parse_html
from .wordpress import WordPressAPI

log = logging.getLogger(__name__)

LINKED_VIA = {"page", "ticker", "script"}
_VAGUE_WORDS = re.compile(
    r"\b(click here|download|view|read more|more|pdf|new|open|details|apply now|apply here|register now)\b", re.I)
_LEADING_SNO_DATE = re.compile(
    r"^\s*(?:\d{1,4}[.)]?\s+)?(?:\d{1,2}[-/.]\d{1,2}[-/.]\d{2,4}|\d{4}-\d{2}-\d{2})\s*[-|:]?\s*"
    r"|^\s*\d{1,3}[.)]\s+")
_LEADING_SNO = re.compile(
    r"^\s*\d{1,3}[.)]?\s+(?!(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\b)(?=[^\W\d_])", re.I)
_NOT_ITEM_PATH = re.compile(
    r"/(testimonials?|success-?stor(y|ies)|blogs?|gallery|photos?|videos?|events?|news|press|courses?|"
    r"people-stories|in-the-news)(/|$)")
_STRONG_RECRUIT = re.compile(r"vacanc|recruit|hiring|walk[- ]?in|position|advertis|opening|apply|applications? (are )?invited", re.I)
_TRAILING_DATES = re.compile(r"(?:\s+(?:\d{1,2}[-/.]\d{1,2}[-/.]\d{2,4}|\d{4}-\d{2}-\d{2}))+\s*$")
_CT_EXT = {
    "application/pdf": "pdf", "application/msword": "doc", "application/zip": "zip",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "docx",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": "xlsx",
    "application/vnd.ms-excel": "xls",
}


@dataclass
class Found:
    key: str
    site_code: str
    url: str | None
    title: str
    link_text: str
    context: str
    source_page: str
    via: set[str]
    linked: bool
    is_document: bool
    file_ext: str = ""
    published: date | None = None
    date_source: str = ""
    deadline: date | None = None
    wp_id: int | None = None
    uploaded: date | None = None        # when the file was put on the website
    uploaded_source: str = ""


@dataclass
class SiteScan:
    site: Site
    items: dict[str, Found] = field(default_factory=dict)
    status: str = "ok"
    method: str = "static"
    pages: int = 0
    rendered: int = 0
    robots_skipped: int = 0
    sitemap_urls: int = 0
    wp: bool = False
    wp_media: int = 0
    errors: list[str] = field(default_factory=list)
    error_count: int = 0
    page_texts: dict[str, tuple[str, str]] = field(default_factory=dict)   # key -> (url, text)
    page_observed: dict[str, str] = field(default_factory=dict)
    ssl_fallback: bool = False
    started: datetime = field(default_factory=datetime.now)
    finished: datetime | None = None


def _squash(text: str | None) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def _tidy_title(title: str) -> str:
    t = re.sub(r"[_]+", " ", title or "")
    t = re.sub(r"\s*-\s*-\s*", " - ", t)
    return _squash(t).strip(" -")


class SiteScanner:
    def __init__(self, site: Site, settings: Settings, keywords: Keywords, known_wp_ids: set[int],
                 deep: bool = False, today: date | None = None, max_pages: int | None = None, light: bool = False):
        self.site = site
        self.settings = settings
        self.kw = keywords
        self.known_wp_ids = known_wp_ids
        self.deep = deep
        self.light = light
        self.today = today or date.today()
        limits = settings.crawl.get("light" if light else ("deep" if deep else "daily"), {})
        self.max_depth = int(limits.get("max_depth", 3))
        self.max_pages = int(max_pages or limits.get("max_pages", 150))
        self.js_max = int(limits.get("js_max_pages", 50))
        self.extra_budget = 40 if deep else 25
        self.fetcher = Fetcher(site, settings)
        self.renderer: Renderer | None = None
        self.use_js = site.js
        self.scan = SiteScan(site, method="browser" if site.js else "static")
        self.robots = None
        self.queue: list = []
        self.enqueued: set[str] = set()
        self.fetched: set[str] = set()
        self.counter = itertools.count()
        self.exclude = list(keywords.crawl_exclude) + [re.compile(re.escape(x), re.I) for x in site.exclude]
        self.record_categories = set(keywords.tender_categories) | {"Notice", "Recruitment"}
        self._main_host = site.host.removeprefix("www.")

    # ------------------------------------------------------------------ public
    def run(self) -> SiteScan:
        home_ok = False
        try:
            if self.settings.crawl.get("respect_robots", True):
                self.robots = self.fetcher.robots()
            self._enqueue(self.site.url, 0, -1, seed=True)
            for seed in self.site.seeds:
                self._enqueue(seed, 1, 0, seed=True)
            home_ok = self._crawl()
            if not self.light:
                self._step("sitemaps", self._sitemaps)
                self._step("wordpress", self._wordpress)
        except Exception as exc:  # noqa: BLE001 - one site must never stop the others
            log.exception("[%s] scan crashed", self.site.code)
            self._err(f"scan crashed: {exc}")
        finally:
            if self.renderer is not None:
                self.renderer.close()
            self.scan.finished = datetime.now()
            self.scan.ssl_fallback = self.fetcher.ssl_fallback_used
            self.fetcher.session.close()
        if not self.scan.items and self.scan.pages == 0:
            self.scan.status = "failed"
        elif not home_ok or self.scan.error_count > max(5, self.scan.pages // 3):
            self.scan.status = "partial"
        return self.scan

    # ------------------------------------------------------------------ helpers
    def _step(self, name: str, fn) -> None:
        try:
            fn()
        except Exception as exc:  # noqa: BLE001
            log.exception("[%s] %s step failed", self.site.code, name)
            self._err(f"{name} failed: {exc}")

    def _err(self, message: str) -> None:
        self.scan.error_count += 1
        if len(self.scan.errors) < 15:
            self.scan.errors.append(message[:220])

    def _excluded(self, url: str) -> bool:
        return any(p.search(url) for p in self.exclude)

    def _crawl_host_ok(self, host: str) -> bool:
        host = host.lower()
        return host.removeprefix("www.") == self._main_host or host in self.site.extra_hosts

    def _priority(self, text: str, url: str) -> int:
        s = f"{text} {unquote(url)}"
        for i, pattern in enumerate(self.kw.crawl_priority):
            if pattern.search(s):
                return i
        return 5

    def _enqueue(self, url: str | None, depth: int, prio: int, seed: bool = False, text: str = "",
                 source: str = "") -> None:
        if not url or is_document_url(url):
            return
        if not seed and (not self._crawl_host_ok(host_of(url)) or self._excluded(url)):
            return
        key = canonical_key(url)
        if key in self.enqueued:
            return
        self.enqueued.add(key)
        heapq.heappush(self.queue, (prio, next(self.counter), url, depth, seed, text, source))

    # ------------------------------------------------------------------ crawl
    def _crawl(self) -> bool:
        home_ok = False
        limit = float(self.settings.crawl.get("site_max_minutes", 20)) * 60
        started = time.monotonic()
        while self.queue and self.scan.pages < self.max_pages:
            if time.monotonic() - started > limit:
                self._err(f"site time limit reached ({limit / 60:.0f} min); remaining pages next scan")
                break
            _, _, url, depth, seed, text, source = heapq.heappop(self.queue)
            if self.robots is not None and not self.robots.allowed(url):
                self.scan.robots_skipped += 1
                continue
            if self.use_js and self.scan.rendered >= self.js_max:
                break
            loaded = self._load(url, text=text, source=source, is_home=(url == self.site.url))
            if loaded is None:
                continue
            final_url, html = loaded
            if url == self.site.url:
                home_ok = True
            self._process(final_url, html, depth, seed, url)
        return home_ok

    def _load(self, url: str, text: str = "", source: str = "", is_home: bool = False):
        key = canonical_key(url)
        if key in self.fetched:
            return None
        self.fetched.add(key)
        if self.use_js:
            return self._render(url)
        resp = self.fetcher.get(url, textual_only=True)
        if resp.not_textual:
            self._add_doc_from_response(url, resp, text, source)
            return None
        if resp.error or resp.status is None:
            self._err(f"{url}: {resp.error}")
            return None
        if resp.looks_blocked or (is_home and resp.status in (403, 503)):
            log.info("[%s] blocked (HTTP %s) at %s - switching to browser", self.site.code, resp.status, url)
            self.use_js = True
            self.scan.method = "browser (site blocked plain requests)"
            return self._render(url)
        if resp.status != 200:
            self._err(f"{url}: HTTP {resp.status}")
            return None
        html = resp.text
        if is_home and not self.site.js:
            anchors = html.lower().count("<a ")
            if len(html) < 5000 or anchors < 15:
                log.info("[%s] homepage has %d links / %d bytes - rendering with browser",
                         self.site.code, anchors, len(html))
                self.use_js = True
                self.scan.method = "browser (auto)"
                rendered = self._render(url)
                if rendered is not None:
                    return rendered
        self.scan.pages += 1
        return resp.final_url, html

    def _render(self, url: str):
        if self.renderer is None:
            self.renderer = Renderer(ignore_https_errors=not self.fetcher.verify)
        self.fetcher._wait()
        result = self.renderer.render(url)
        self.scan.rendered += 1
        if result.error or result.status != 200 or not result.html.strip() or Response(
                url, result.final_url, result.status, content=result.html.encode("utf-8")).looks_blocked:
            self._err(f"{url}: {result.error or f'HTTP {result.status} / challenge page'}")
            return None
        self.scan.pages += 1
        return result.final_url, result.html

    def _process(self, page_url: str, html: str, depth: int, seed: bool, requested_url: str) -> None:
        parsed = parse_html(html, page_url, keep_fragments=self.site.keep_fragments)
        if parsed.redirect:
            self._enqueue(parsed.redirect, depth, -1, seed=seed)
        if seed or self._priority("", requested_url) < 5:
            self.scan.page_texts[canonical_key(requested_url)] = (requested_url, "\n".join(parsed.text_lines))
            self.scan.page_observed[canonical_key(requested_url)] = datetime.now().isoformat(timespec="microseconds")
        for link in parsed.links:
            self._handle_link(link, page_url, depth, seed)
        for ticker in parsed.ticker_texts:
            self._add_text_item(ticker, page_url)

    def _handle_link(self, link: FoundLink, page_url: str, depth: int, seed: bool) -> None:
        url = link.url
        if not url:
            return
        host = host_of(url)
        own = self.site.owns(host)
        via = "ticker" if link.in_ticker else ("script" if link.kind == "script" else "page")
        for doc in doc_urls_in_query(url):
            if self.site.owns(host_of(doc)):
                self._add_item(doc, link.text, link.context, page_url, via, True, True)
        if is_document_url(url):
            if own or host in ("drive.google.com", "docs.google.com") or seed or link.in_ticker:
                self._add_item(url, link.text, link.context, page_url, via, True, True)
            return
        if not own:
            if any(host == h or host.endswith("." + h) for h in self.kw.external_doc_hosts):
                cls = classify(self.kw, link.text, link.context, url)
                if link.in_ticker or cls.category in self.record_categories:
                    self._add_item(url, link.text, link.context, page_url, via, True, False)
            return
        if self._looks_like_item(link.text, url) and not self._is_home_or_self(url, page_url):
            self._add_item(url, link.text, link.context, page_url, via, True, False)
        if depth + 1 <= self.max_depth:
            self._enqueue(url, depth + 1, self._priority(link.text, url), text=link.text, source=page_url)

    def _is_home_or_self(self, url: str, page_url: str) -> bool:
        """Placeholder links (href="/" or back to the same page) are not notices."""
        key = canonical_key(url)
        if key == canonical_key(page_url):
            return True
        parts = urlsplit(url)
        return parts.path in ("", "/", "/index.php", "/index.html", "/home", "/index2.php") and not parts.query

    def _looks_like_item(self, text: str, url: str) -> bool:
        """A link to a web page counts as a notice only if its text reads like one."""
        t = _squash(text)
        if len(t) < 20 or len(t.split()) < 4:
            return False
        if _NOT_ITEM_PATH.search(urlsplit(url).path.lower()):
            return False
        category = classify(self.kw, t, "", url).category
        if category == "Recruitment" and not _STRONG_RECRUIT.search(t):
            return False  # "job" / "career" alone appears in testimonials and course pages
        return category in self.record_categories

    # ------------------------------------------------------------------ items
    def _title(self, text: str, context: str, url: str | None) -> str:
        t = _squash(text)
        if not t or is_vague(self.kw, t):
            c = _squash(_VAGUE_WORDS.sub(" ", _squash(context))).strip(" -|:")
            # Table rows: "3 03/08/2026 Request for Proposal ... 15-08-2026" -> "Request for Proposal ..."
            c = _LEADING_SNO_DATE.sub("", c).strip(" -|:")
            c = _LEADING_SNO.sub("", c)
            c = _TRAILING_DATES.sub("", c).strip(" -|:")
            if len(c) >= 8:
                t = c
            elif url:
                t = title_from_filename(url) or t
        else:
            t = _LEADING_SNO.sub("", t)  # "1 Request for Proposal ..." -> "Request for Proposal ..."
        return t[:300]

    def _add_item(self, url: str, text: str, context: str, source_page: str, via: str, linked: bool,
                  is_doc: bool, published: date | None = None, date_source: str = "",
                  wp_id: int | None = None, fallback_date: date | None = None,
                  fallback_source: str = "") -> Found:
        keep_fragment = self.site.keep_fragments and not is_doc
        key = canonical_key(url, keep_fragment=keep_fragment)
        title = self._title(text, context, url if is_doc else None) or title_from_filename(url) or url
        pub, deadline = listing_dates(title, self.today)
        if context and context != title:
            p2, d2 = listing_dates(context, self.today)
            pub, deadline = pub or p2, deadline or d2
        src = "listing text" if pub else ""
        if published:
            pub, src = published, date_source
        if not pub:
            pub, src = date_from_url(url, self.today)
        if not pub and fallback_date:
            pub, src = fallback_date, fallback_source
        existing = self.scan.items.get(key)
        if existing is not None:
            existing.via.add(via)
            if linked and not existing.linked:
                existing.linked = True
                existing.source_page = source_page
            if title and (not existing.title or is_vague(self.kw, existing.title)
                          or existing.title == title_from_filename(url)) and not is_vague(self.kw, title):
                existing.title = title
            if len(context or "") > len(existing.context):
                existing.context = (context or "")[:600]
            if published:
                existing.published, existing.date_source = published, date_source
            elif not existing.published and pub:
                existing.published, existing.date_source = pub, src
            existing.deadline = existing.deadline or deadline
            existing.wp_id = existing.wp_id or wp_id
            if via == "wp-media" and published:
                existing.uploaded, existing.uploaded_source = published, "website media library"
            elif is_doc and not existing.uploaded:
                existing.uploaded, existing.uploaded_source = upload_date_from_url(url, self.today)
            return existing
        uploaded, uploaded_source = (upload_date_from_url(url, self.today) if is_doc else (None, ""))
        if via == "wp-media" and published:
            uploaded, uploaded_source = published, "website media library"
        ext = url_ext(url) if is_doc else ""
        if is_doc and not ext and ("drive.google" in url or "docs.google" in url):
            ext = "gdrive"
        found = Found(
            key=key, site_code=self.site.code, url=url, title=title, link_text=_squash(text)[:300],
            context=(context or "")[:600], source_page=source_page, via={via}, linked=linked,
            is_document=is_doc, file_ext=ext, published=pub, date_source=src, deadline=deadline, wp_id=wp_id,
            uploaded=uploaded, uploaded_source=uploaded_source,
        )
        self.scan.items[key] = found
        return found

    def _add_text_item(self, link: FoundLink, page_url: str) -> None:
        cls = classify(self.kw, link.text, "", "")
        if cls.category not in self.record_categories:
            return
        key = text_key(self.site.code, link.text)
        if key in self.scan.items:
            return
        pub, deadline = listing_dates(link.text, self.today)
        self.scan.items[key] = Found(
            key=key, site_code=self.site.code, url=None, title=link.text[:300], link_text=link.text[:300],
            context=link.text[:600], source_page=page_url, via={"ticker"}, linked=True, is_document=False,
            published=pub, date_source="listing text" if pub else "", deadline=deadline,
        )

    def _add_doc_from_response(self, url: str, resp: Response, text: str, source: str) -> None:
        ct = resp.content_type.split(";")[0].strip()
        disposition = resp.headers.get("content-disposition", "")
        m = re.search(r"filename\*?=(?:UTF-8'')?\"?([^\";]+)", disposition, re.I)
        name = unquote(m.group(1)) if m else ""
        found = self._add_item(resp.final_url or url, text or name, "", source or url, "page", True, True)
        if not found.file_ext:
            found.file_ext = _CT_EXT.get(ct, url_ext(name) or ct.split("/")[-1][:8])

    # ------------------------------------------------------------------ sitemaps
    def _sitemaps(self) -> None:
        start = self.robots.sitemaps if self.robots is not None else []
        locs = sitemap.collect(self.fetcher, self.site.origin, start,
                               max_urls=int(self.settings.crawl.get("sitemap_max_urls", 6000)))
        self.scan.sitemap_urls = len(locs)
        source = self.site.origin + "/sitemap.xml"
        extra = 0
        for loc, lastmod in locs:
            url = clean_url(loc)
            if not url or not self.site.owns(host_of(url)) or self._excluded(url):
                continue
            lm = parse_iso(lastmod)
            if is_document_url(url):
                self._add_item(url, "", "", source, "sitemap", False, True,
                               fallback_date=lm, fallback_source="sitemap lastmod")
                continue
            if canonical_key(url) in self.fetched:
                continue
            path = unquote(urlsplit(url).path)
            slug = re.sub(r"[/_\-.]+", " ", path).strip()
            cls = classify(self.kw, slug, "", "")
            if cls.category not in set(self.kw.tender_categories) | {"Notice"}:
                continue
            last_part = re.sub(r"[_\-.]+", " ", filename_from_url(url)).strip()
            if len(last_part.split()) >= 4 and cls.category in self.kw.tender_categories:
                self._add_item(url, last_part, "", source, "sitemap", False, False,
                               fallback_date=lm, fallback_source="sitemap lastmod")
            if extra < self.extra_budget and not (self.use_js and self.scan.rendered >= self.js_max):
                loaded = self._load(url)
                if loaded is not None:
                    extra += 1
                    self._process(loaded[0], loaded[1], self.max_depth, False, url)

    # ------------------------------------------------------------------ wordpress
    def _wordpress(self) -> None:
        api = WordPressAPI(self.fetcher, self.site.origin)
        if not api.detect():
            return
        self.scan.wp = True
        media = api.media(self.known_wp_ids, int(self.settings.crawl.get("wp_media_max_pages", 40)))
        self.scan.wp_media = len(media)
        media_source = self.site.origin + "/wp-json/wp/v2/media"
        for m in media:
            url = clean_url(m.url)
            if not url:
                continue
            title = _tidy_title(m.title) or title_from_filename(url)
            self._add_item(url, title, "", media_source, "wp-media", False, True,
                           published=parse_iso(m.date), date_source="upload date", wp_id=m.wp_id)
        extra = 0
        for post in api.recent_posts() + api.search():
            url = clean_url(post.url)
            if not url or not self.site.owns(host_of(url)) or self._excluded(url):
                continue
            cls = classify(self.kw, post.title, post.excerpt, url)
            if cls.category not in self.record_categories:
                continue
            via = "wp-post" if post.kind == "post" else "wp-search"
            source = self.site.origin + ("/wp-json/wp/v2/posts" if post.kind == "post" else "/?s=")
            self._add_item(url, post.title, post.excerpt, source, via, False, False,
                           published=parse_iso(post.date), date_source="post date" if post.date else "")
            if (canonical_key(url) not in self.fetched and extra < self.extra_budget
                    and not (self.use_js and self.scan.rendered >= self.js_max)):
                loaded = self._load(url)
                if loaded is not None:
                    extra += 1
                    self._process(loaded[0], loaded[1], self.max_depth, False, url)


def scan_site(site: Site, settings: Settings, keywords: Keywords, known_wp_ids: set[int], deep: bool,
              today: date, max_pages: int | None = None, light: bool = False) -> SiteScan:
    log.info("[%s] scanning %s", site.code, site.url)
    scan = SiteScanner(site, settings, keywords, known_wp_ids, deep, today, max_pages, light=light).run()
    log.info("[%s] done: %s, %d pages (%s), %d items, %d errors", site.code, scan.status, scan.pages,
             scan.method, len(scan.items), scan.error_count)
    return scan
