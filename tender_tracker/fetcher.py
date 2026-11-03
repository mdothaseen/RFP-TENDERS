"""Polite HTTP fetching: one session per site, delay between requests, retries, SSL fallback."""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from urllib.robotparser import RobotFileParser

import requests
import urllib3
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from .config import Settings, Site

log = logging.getLogger(__name__)

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)
_TEXTUAL_TYPES = ("text/html", "application/xhtml", "text/xml", "application/xml", "application/json",
                  "text/plain", "application/rss")

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


# Upper limits for one request in total (read timeouts alone do not stop a slow, trickling server).
PAGE_TOTAL_SECONDS = 120
FILE_TOTAL_SECONDS = 600

@dataclass

class Response:
    url: str
    final_url: str
    status: int | None
    headers: dict = field(default_factory=dict)
    content: bytes = b""
    error: str | None = None
    not_textual: bool = False

    @property
    def ok(self) -> bool:
        return self.error is None and self.status == 200

    @property
    def content_type(self) -> str:
        return (self.headers.get("content-type") or "").lower()

    @property
    def text(self) -> str:
        enc = "utf-8"
        ct = self.content_type
        if "charset=" in ct:
            enc = ct.split("charset=", 1)[1].split(";")[0].strip() or "utf-8"
        try:
            return self.content.decode(enc, errors="replace")
        except LookupError:
            return self.content.decode("utf-8", errors="replace")

    @property
    def looks_blocked(self) -> bool:
        """Cloudflare / bot challenge pages."""
        head = self.content[:6000].lower()
        if b"cf-chl" in head or b"challenge-platform" in head or b"<title>just a moment" in head:
            return True
        if self.status in (403, 429, 503):
            server = (self.headers.get("server") or "").lower()
            return (b"just a moment" in head or b"cf-chl" in head or b"challenge-platform" in head
                    or b"captcha" in head or ("cloudflare" in server and self.status != 429))
        return False


class Robots:
    def __init__(self, parser: RobotFileParser | None):
        self._parser = parser

    def allowed(self, url: str) -> bool:
        if self._parser is None:
            return True
        try:
            return self._parser.can_fetch(USER_AGENT, url)
        except Exception as exc:  # noqa: BLE001 - a broken robots.txt must never stop the crawl
            log.debug("robots.txt check failed for %s (allowed): %s", url, exc)
            return True

    @property
    def sitemaps(self) -> list[str]:
        if self._parser is None:
            return []
        try:
            return list(self._parser.site_maps() or [])
        except Exception as exc:  # noqa: BLE001
            log.debug("Sitemap lines in robots.txt unreadable: %s", exc)
            return []


class Fetcher:
    def __init__(self, site: Site, settings: Settings):
        self.site = site
        self.delay = float(settings.crawl.get("delay_seconds", 1.2))
        self.timeout = float(settings.crawl.get("timeout_seconds", 30))
        self.verify = site.ssl_verify
        self.ssl_fallback_used = not site.ssl_verify
        self.requests_made = 0
        self._last = 0.0
        self._lock = threading.Lock()
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-IN,en;q=0.9,hi;q=0.6",
        })
        retry = Retry(
            total=2, connect=2, read=1, backoff_factor=1.5,
            status_forcelist=(429, 500, 502, 504), allowed_methods=("GET", "HEAD"),
            respect_retry_after_header=True, raise_on_status=False,
        )
        adapter = HTTPAdapter(max_retries=retry, pool_connections=4, pool_maxsize=4)
        self.session.mount("http://", adapter)
        self.session.mount("https://", adapter)

    def _wait(self) -> None:
        with self._lock:
            gap = time.monotonic() - self._last
            if gap < self.delay:
                time.sleep(self.delay - gap)
            self._last = time.monotonic()

    def _request(self, url: str, headers: dict | None, timeout: float | None):
        self._wait()
        self.requests_made += 1
        try:
            return self.session.get(
                url, headers=headers, timeout=(15, timeout or self.timeout), verify=self.verify,
                stream=True, allow_redirects=True,
            )
        except requests.exceptions.SSLError:
            if not self.verify:
                raise
            log.info("[%s] SSL certificate problem, retrying without verification", self.site.code)
            self.verify = False
            self.ssl_fallback_used = True
            self._wait()
            return self.session.get(
                url, headers=headers, timeout=(15, timeout or self.timeout), verify=False,
                stream=True, allow_redirects=True,
            )

    def get(self, url: str, *, max_bytes: int = 6_000_000, textual_only: bool = False,
            headers: dict | None = None, timeout: float | None = None) -> Response:
        """GET a page. With textual_only, binary responses (PDFs etc.) are not downloaded."""
        try:
            r = self._request(url, headers, timeout)
        except requests.RequestException as exc:
            return Response(url, url, None, error=f"{type(exc).__name__}: {str(exc)[:160]}")
        resp_headers = {k.lower(): v for k, v in r.headers.items()}
        ct = resp_headers.get("content-type", "").lower()
        final = r.url
        if textual_only and ct and not any(t in ct for t in _TEXTUAL_TYPES):
            r.close()
            return Response(url, final, r.status_code, resp_headers, b"", not_textual=True)
        chunks, size = [], 0
        started = time.monotonic()
        try:
            for chunk in r.iter_content(65536):
                chunks.append(chunk)
                size += len(chunk)
                if size >= max_bytes:
                    break
                if time.monotonic() - started > PAGE_TOTAL_SECONDS:
                    # A server that keeps trickling bytes never hits the read timeout.
                    return Response(url, final, r.status_code, resp_headers, b"".join(chunks),
                                    error=f"page took longer than {PAGE_TOTAL_SECONDS} s")
        except requests.RequestException as exc:
            return Response(url, final, r.status_code, resp_headers, b"".join(chunks),
                            error=f"{type(exc).__name__}: {str(exc)[:160]}")
        finally:
            r.close()
        return Response(url, final, r.status_code, resp_headers, b"".join(chunks))

    def download(self, url: str, dest: Path, max_bytes: int, headers: dict | None = None) -> tuple[Response, Path | None]:
        """Stream a file to dest. Returns the response (without body) and the saved path."""
        try:
            r = self._request(url, headers, max(self.timeout, 90))
        except requests.RequestException as exc:
            return Response(url, url, None, error=f"{type(exc).__name__}: {str(exc)[:160]}"), None
        resp_headers = {k.lower(): v for k, v in r.headers.items()}
        resp = Response(url, r.url, r.status_code, resp_headers)
        if r.status_code == 304:
            r.close()
            return resp, None
        if r.status_code != 200:
            r.close()
            resp.error = f"HTTP {r.status_code}"
            return resp, None
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_name(dest.name + ".part")
        size = 0
        try:
            with open(tmp, "wb") as fh:
                started = time.monotonic()
                for chunk in r.iter_content(65536):
                    size += len(chunk)
                    if size > max_bytes:
                        resp.error = f"file larger than {max_bytes // 1_000_000} MB"
                        break
                    if time.monotonic() - started > FILE_TOTAL_SECONDS:
                        resp.error = f"download took longer than {FILE_TOTAL_SECONDS} s"
                        break
                    fh.write(chunk)
        except (requests.RequestException, OSError) as exc:
            resp.error = f"{type(exc).__name__}: {str(exc)[:160]}"
        finally:
            r.close()
        if resp.error:
            tmp.unlink(missing_ok=True)
            return resp, None
        tmp.replace(dest)
        return resp, dest

    def head(self, url: str) -> dict | None:
        """Response headers without downloading the file (None on error)."""
        self._wait()
        self.requests_made += 1
        try:
            r = self.session.head(url, timeout=(15, 30), verify=self.verify, allow_redirects=True)
        except requests.RequestException:
            return None
        if r.status_code != 200:
            return None
        return {k.lower(): v for k, v in r.headers.items()}

    def robots(self) -> Robots:
        resp = self.get(self.site.origin + "/robots.txt", max_bytes=500_000, textual_only=True, timeout=20)
        if not resp.ok or "html" in resp.content_type:
            return Robots(None)
        parser = RobotFileParser()
        try:
            parser.parse(resp.text.splitlines())
        except Exception as exc:  # noqa: BLE001
            log.info("[%s] robots.txt could not be parsed, crawling without it: %s", self.site.code, exc)
            return Robots(None)
        return Robots(parser)
