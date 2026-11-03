"""`--reclassify`: apply the current keywords.yaml to every saved item (category, relevance, clean title).

A scan only ever upgrades a saved item, so a stricter rule (e.g. "Price Bid" is an attachment, "hostel repair" is
Low) reaches old items only through this. Documents Claude has already read keep Claude's answer.
"""

from __future__ import annotations

import logging
from collections import Counter

from .classify import classify
from .discover.crawler import _tidy_title
from .scanning import page_change_class

log = logging.getLogger("tender_tracker")


def reclassify(store, keywords, settings) -> Counter:
    ai_categories = set(settings.ai.get("categories") or [])
    changes: Counter = Counter()
    rows = store.db.execute("SELECT id, key, title, context, url, category, content_category, relevance, "
                            "ai_status, ai_error FROM items WHERE removed=0").fetchall()
    for row in rows:
        title = _tidy_title(row["title"] or "") or row["title"]
        if row["key"].startswith("change:"):
            cls = page_change_class(keywords, row["context"] or "")
        else:
            cls = classify(keywords, title or "", row["context"] or "", row["url"] or "")
        fields = {}
        if title != row["title"]:
            fields["title"] = title
            changes["title cleaned"] += 1
        if cls.category != row["category"]:
            fields["category"] = cls.category
            changes[f"category {row['category']} -> {cls.category}"] += 1
        if cls.relevance != row["relevance"]:
            fields["relevance"] = cls.relevance
            changes[f"relevance {row['relevance']} -> {cls.relevance}"] += 1
        content = row["content_category"]
        if "tender attachment" in cls.matched and content in ai_categories:
            fields["content_category"] = None  # a bid form mentions "technical bid"; it is still not a tender
            content = None
        if (cls.category not in ai_categories and content not in ai_categories
                and row["ai_status"] in ("pending", "failed")):
            fields.update(ai_status="skipped", ai_error="reclassified: not tender-type")
            changes["removed from Claude's queue"] += 1
        if fields:
            store.update_item(row["id"], **fields)
    store.commit()
    log.info("Reclassified %d items: %s", len(rows), dict(changes.most_common(12)))
    return changes
