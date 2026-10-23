"""Headless Chromium (Playwright) for websites that build their pages with JavaScript.

Playwright objects are not thread-safe: create, use and close a Renderer in one thread.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from .fetcher import USER_AGENT

log = logging.getLogger(__name__)

_BLOCKED_RESOURCES = {"image", "media", "font"}


@dataclass
class RenderResult:
    url: str
    final_url: str
    html: str = ""
    status: int | None = None
    error: str | None = None


class Renderer:
    def __init__(self, ignore_https_errors: bool = False, timeout_ms: int = 45000):
        self.ignore_https_errors = ignore_https_errors
        self.timeout_ms = timeout_ms
        self._pw = None
        self._browser = None
        self._context = None
        self.pages_rendered = 0

    def _ensure(self) -> None:
        if self._context is not None:
            return
        from playwright.sync_api import sync_playwright

        self._pw = sync_playwright().start()
        self._browser = self._pw.chromium.launch(headless=True)
        self._context = self._browser.new_context(
            user_agent=USER_AGENT,
            ignore_https_errors=self.ignore_https_errors,
            viewport={"width": 1366, "height": 900},
            accept_downloads=False,
            locale="en-IN",
        )
        self._context.route(
            "**/*",
            lambda route: route.abort() if route.request.resource_type in _BLOCKED_RESOURCES else route.continue_(),
        )

    def render(self, url: str) -> RenderResult:
        try:
            self._ensure()
        except Exception as exc:  # noqa: BLE001 - browser missing etc.
            return RenderResult(url, url, error=f"browser start failed: {exc}")
        page = self._context.new_page()
        result = RenderResult(url, url)
        try:
            response = page.goto(url, wait_until="domcontentloaded", timeout=self.timeout_ms)
            result.status = response.status if response else None
            try:
                page.wait_for_load_state("networkidle", timeout=12000)
            except Exception:  # noqa: BLE001 - busy pages never go idle; content is still usable
                pass
            for _ in range(3):
                page.mouse.wheel(0, 2500)
                page.wait_for_timeout(600)
            result.html = page.content()
            result.final_url = page.url
            self.pages_rendered += 1
        except Exception as exc:  # noqa: BLE001
            result.error = f"{type(exc).__name__}: {str(exc).splitlines()[0][:160]}"
            try:
                result.html = page.content()
                result.final_url = page.url
            except Exception:  # noqa: BLE001
                pass
        finally:
            try:
                page.close()
            except Exception:  # noqa: BLE001
                pass
        return result

    def close(self) -> None:
        for obj, method in ((self._context, "close"), (self._browser, "close"), (self._pw, "stop")):
            if obj is not None:
                try:
                    getattr(obj, method)()
                except Exception:  # noqa: BLE001
                    pass
        self._pw = self._browser = self._context = None
