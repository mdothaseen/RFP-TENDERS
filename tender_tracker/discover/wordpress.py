"""WordPress REST API: media library (every uploaded file), recent posts and site search."""

from __future__ import annotations

import html
import json
import logging
import re
from dataclasses import dataclass

from ..fetcher import Fetcher

log = logging.getLogger(__name__)

_JSON = {"Accept": "application/json"}
_SKIP_TYPES = {
    "attachment", "wp_block", "wp_template", "wp_template_part", "wp_navigation", "nav_menu_item",
    "wp_global_styles", "wp_font_family", "wp_font_face", "elementor_library", "e-landing-page",
    "e-floating-buttons", "wpcf7_contact_form", "oembed_cache", "user_request", "customize_changeset",
}
SEARCH_TERMS = ["tender", "rfp", "eoi", "expression of interest", "empanelment", "quotation", "corrigendum",
                "request for proposal"]


@dataclass
class WPItem:
    title: str
    url: str
    date: str | None
    kind: str          # media | post | search
    wp_id: int | None = None
    excerpt: str = ""


def _plain(value) -> str:
    if isinstance(value, dict):
        value = value.get("rendered", "")
    text = re.sub(r"<[^>]+>", " ", str(value or ""))
    return re.sub(r"\s+", " ", html.unescape(text)).strip()


class WordPressAPI:
    def __init__(self, fetcher: Fetcher, origin: str):
        self.fetcher = fetcher
        self.origin = origin.rstrip("/")
        self.style = None  # "pretty" (/wp-json/...) or "query" (?rest_route=...)

    def _url(self, route: str, params: str = "") -> str:
        if self.style == "query":
            return f"{self.origin}/?rest_route={route}" + (f"&{params}" if params else "")
        return f"{self.origin}/wp-json{route}" + (f"?{params}" if params else "")

    def _get_json(self, route: str, params: str = ""):
        resp = self.fetcher.get(self._url(route, params), headers=_JSON, max_bytes=25_000_000, timeout=45)
        if resp.status != 200 or not resp.content:
            return None, resp
        body = resp.content.lstrip()
        if body[:1] not in (b"[", b"{"):
            return None, resp
        try:
            return json.loads(resp.text), resp
        except ValueError:
            return None, resp

    def detect(self) -> bool:
        for style in ("pretty", "query"):
            self.style = style
            data, resp = self._get_json("/wp/v2/media", "per_page=1&_fields=id")
            if isinstance(data, list) and ("x-wp-total" in resp.headers or data):
                return True
        self.style = None
        return False

    def media(self, known_ids: set[int], max_pages: int) -> list[WPItem]:
        """Uploaded documents, newest first. Stops early once a full page is already known."""
        items: list[WPItem] = []
        for page in range(1, max_pages + 1):
            data, resp = self._get_json(
                "/wp/v2/media",
                f"media_type=application&per_page=100&page={page}&orderby=date&order=desc"
                "&_fields=id,date,title,source_url,mime_type,post,link",
            )
            if not isinstance(data, list) or not data:
                break
            new_on_page = 0
            for m in data:
                url = m.get("source_url") or m.get("link")
                if not url:
                    continue
                wp_id = m.get("id")
                if wp_id not in known_ids:
                    new_on_page += 1
                items.append(WPItem(_plain(m.get("title")), url, m.get("date"), "media", wp_id))
            if known_ids and new_on_page == 0:
                break
            try:
                total_pages = int(resp.headers.get("x-wp-totalpages") or 0)
            except ValueError:
                total_pages = 0
            if total_pages and page >= total_pages:
                break
        return items

    def recent_posts(self, per_type: int = 20, max_types: int = 12) -> list[WPItem]:
        """Latest entries of every public post type (posts, pages and custom types such as 'tenders')."""
        types, _ = self._get_json("/wp/v2/types")
        bases = []
        if isinstance(types, dict):
            for slug, info in types.items():
                if slug in _SKIP_TYPES or not isinstance(info, dict):
                    continue
                base = info.get("rest_base") or slug
                bases.append(base)
        if not bases:
            bases = ["posts", "pages"]
        items: list[WPItem] = []
        for base in bases[:max_types]:
            data, _ = self._get_json(
                f"/wp/v2/{base}",
                f"per_page={per_type}&orderby=date&order=desc&_fields=id,date,link,title,excerpt",
            )
            if not isinstance(data, list):
                continue
            for p in data:
                if p.get("link"):
                    items.append(WPItem(_plain(p.get("title")), p["link"], p.get("date"), "post",
                                        None, _plain(p.get("excerpt"))[:400]))
        return items

    def search(self, terms: list[str] | None = None) -> list[WPItem]:
        items: list[WPItem] = []
        for term in terms or SEARCH_TERMS:
            data, _ = self._get_json("/wp/v2/search", f"search={term.replace(' ', '+')}&per_page=20")
            if not isinstance(data, list):
                continue
            for r in data:
                if r.get("url"):
                    items.append(WPItem(_plain(r.get("title")), r["url"], None, "search"))
        return items
