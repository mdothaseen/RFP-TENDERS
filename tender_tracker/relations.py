"""Conservative tender/corrigendum linking: exact reference and organisation only."""

import re

from .ai_extract import ai_fields
from .events import record_item_event


def link_corrigenda(store) -> int:
    rows = store.items("ai_status='done' AND removed=0")
    originals = {}
    for row in rows:
        answer = ai_fields(row)
        reference = re.sub(r"\s+", "", answer.get("reference_no", "")).casefold()
        category = answer.get("doc_type") or row["content_category"] or row["category"]
        if len(reference) >= 5 and category != "Corrigendum":
            originals.setdefault((row["site_code"], reference), []).append(row["id"])
    count = 0
    for row in rows:
        answer = ai_fields(row)
        if (answer.get("doc_type") or row["content_category"] or row["category"]) != "Corrigendum":
            continue
        reference = re.sub(r"\s+", "", answer.get("reference_no", "")).casefold()
        matches = originals.get((row["site_code"], reference), [])
        target = row["related_item_id"] or (matches[0] if len(matches) == 1 else None)
        if target:
            if not row["related_item_id"]:
                store.update_item(row["id"], related_item_id=target)
                count += 1
            deadline = answer.get("last_date_submission")
            if deadline:
                record_item_event(store, target, "deadline_changed",
                    {"deadline": deadline, "corrigendum_url": row["url"], "corrigendum_id": row["id"]},
                    event_key=f"corrigendum-deadline:{target}:{row['id']}:{deadline}")
    store.commit()
    return count
