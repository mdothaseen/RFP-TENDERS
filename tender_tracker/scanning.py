"""Scan the websites in parallel and save what each one shows (new links and changed tender pages)."""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import replace
from datetime import date, datetime
from urllib.parse import urlsplit

from . import events
from .changes import meaningful_diff
from .classify import classify
from .discover.crawler import Found, SiteScan, scan_site
from .excel_report import build_records
from .store import Store

log = logging.getLogger("tender_tracker")


def save_scan(store: Store, scan: SiteScan, keywords, run_id: int, baseline: bool, now: str,
              ai_categories: set[str], today: date) -> int:
    new = 0
    for found in scan.items.values():
        cls = classify(keywords, found.title, found.context, found.url or "")
        _, is_new = store.upsert(found, cls, run_id, baseline, now, ai_categories)
        new += int(is_new)

    # Seed pages whose text changed without a new link (e.g. "last date extended").
    for key, (page_url, text) in scan.page_texts.items():
        text = text[:200_000]
        saved = store.db.execute("SELECT text,updated,revision FROM page_texts WHERE key=?", (key,)).fetchone()
        observed = scan.page_observed.get(key) or datetime.now().isoformat(timespec="microseconds")
        if saved and saved["updated"] > observed:
            continue
        old = saved["text"] if saved else None
        if old is not None and old != text and text:
            snippet = meaningful_diff(old, text, keywords)
            if snippet:
                version = saved["revision"] + 1
                title = f"Page updated: {page_url}"
                change = Found(key=f"change:{key}:{version}", site_code=scan.site.code, url=page_url,
                    title=title, link_text="", context=snippet, source_page=page_url, via={"change"}, linked=True,
                    is_document=False, published=today, date_source="page change")
                item_id, is_new = store.upsert(change, classify(keywords, snippet, text[:2000]), run_id, baseline, now, ai_categories)
                events.record_item_event(store, item_id, "page_changed", {"diff": snippet},
                                         event_key=f"page:{key}:{version}")
                new += int(is_new)
        if text:
            store.save_page_text(key, scan.site.code, text, observed)
    return new


def _light_seeds(store: Store, selected: dict) -> dict:
    """A light check only re-reads each site's known tender pages."""
    watched = {}
    for code, site in selected.items():
        pages = [r[0] for r in store.db.execute("""SELECT DISTINCT source_page FROM items WHERE site_code=?
            AND linked_seen=1 AND category IN ('Tender','RFP','EOI','Empanelment','Corrigendum')
            AND source_page IS NOT NULL ORDER BY last_seen DESC LIMIT 5""", (code,))]
        watched[code] = replace(site, seeds=list(dict.fromkeys(site.seeds + [p for p in pages if site.owns(urlsplit(p).hostname or '')])))
    return watched


def scan_sites(store: Store, settings, keywords, selected: dict, run_id: int, baseline: bool, deep: bool,
               today: date, args, notify_enabled: bool = False) -> tuple[int, list[SiteScan]]:
    light = bool(args.light)
    workers = args.workers or int(settings.monitoring.get("light_workers", 3) if light else settings.crawl.get("workers", 6))
    if light:
        selected = _light_seeds(store, selected)
    known = {code: store.known_wp_ids(code) for code in selected}
    # A newly added site's first scan is its baseline: its old items are not reported as new.
    scanned_before = store.site_codes_with_items()
    ai_categories = set(settings.ai.get("categories") or [])
    now = datetime.now().isoformat(timespec="seconds")
    scans, new_total = [], 0
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        futures = {
            pool.submit(scan_site, site, settings, keywords, known[code], deep, today, args.max_pages, light=light): code
            for code, site in selected.items()
        }
        for fut in as_completed(futures):
            code = futures[fut]
            try:
                scan = fut.result()
            except Exception as exc:  # noqa: BLE001
                log.exception("[%s] scan failed", code)
                scan = SiteScan(selected[code], status="failed", errors=[str(exc)[:200]], error_count=1,
                                finished=datetime.now())
            site_baseline = baseline or code not in scanned_before
            if site_baseline and not baseline:
                log.info("[%s] first scan of this site: saved as its baseline", code)
            store.commit()
            store.db.execute("BEGIN IMMEDIATE")
            new_items = save_scan(store, scan, keywords, run_id, site_baseline, now, ai_categories, today)
            store.record_site_run(run_id, scan, new_items)
            store.commit()
            new_total += new_items
            scans.append(scan)
            log.info("[%s] saved: %d items, %d new (%d/%d sites done)", code, len(scan.items), new_items,
                     len(scans), len(selected))
            if notify_enabled:
                records = build_records(store, keywords, settings, selected, store.run(run_id), today,
                                        "site_code=? AND notified=0", (code,))
                events.queue_discoveries(store, records, settings)
                events.queue_changes(store, settings)
                if not args.defer_enrichment:
                    events.drain_outbox(store, limit=2, time_budget_seconds=5)
    # Absence in a budget-limited crawl is not evidence that a document was removed.
    store.commit()
    return new_total, scans
