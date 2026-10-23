"""Read XML sitemaps (robots.txt, /sitemap.xml, /sitemap_index.xml, /wp-sitemap.xml)."""

from __future__ import annotations

import gzip
import html
import logging
import re

from ..fetcher import Fetcher

log = logging.getLogger(__name__)

_URL_BLOCK = re.compile(r"<url>(.*?)</url>", re.S | re.I)
_SITEMAP_BLOCK = re.compile(r"<sitemap>(.*?)</sitemap>", re.S | re.I)
_LOC = re.compile(r"<loc>\s*(?:<!\[CDATA\[)?\s*(.*?)\s*(?:\]\]>)?\s*</loc>", re.S | re.I)
_LASTMOD = re.compile(r"<lastmod>\s*(.*?)\s*</lastmod>", re.S | re.I)
_SKIP_CHILD = re.compile(r"(image|video|author|user|users|tag|category|product_cat)[-_]?sitemap|wp-sitemap-users|wp-sitemap-taxonomies", re.I)


def collect(fetcher: Fetcher, origin: str, extra: list[str], max_urls: int = 6000,
            max_files: int = 40) -> list[tuple[str, str | None]]:
    """All <loc> URLs (with <lastmod>) from the site's sitemaps."""
    queue = list(dict.fromkeys(extra + [
        origin + "/sitemap.xml", origin + "/sitemap_index.xml", origin + "/wp-sitemap.xml",
    ]))
    seen: set[str] = set()
    out: list[tuple[str, str | None]] = []
    out_seen: set[str] = set()
    while queue and len(seen) < max_files and len(out) < max_urls:
        url = queue.pop(0)
        if url in seen:
            continue
        seen.add(url)
        resp = fetcher.get(url, max_bytes=25_000_000, timeout=45)
        if not resp.ok or not resp.content:
            continue
        body = resp.content
        if body[:2] == b"\x1f\x8b":
            try:
                body = gzip.decompress(body)
            except OSError:
                continue
        text = body.decode("utf-8", errors="replace")
        head = text[:3000].lower()
        if "<sitemapindex" in head:
            for block in _SITEMAP_BLOCK.findall(text):
                m = _LOC.search(block)
                if m:
                    child = html.unescape(m.group(1).strip())
                    if not _SKIP_CHILD.search(child) and child not in seen:
                        queue.append(child)
        elif "<urlset" in head:
            for block in _URL_BLOCK.findall(text):
                m = _LOC.search(block)
                if not m:
                    continue
                loc = html.unescape(m.group(1).strip())
                if loc in out_seen:
                    continue
                out_seen.add(loc)
                lm = _LASTMOD.search(block)
                out.append((loc, lm.group(1).strip() if lm else None))
                if len(out) >= max_urls:
                    break
    return out
