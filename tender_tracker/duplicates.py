"""One file found under several addresses is one item: later copies point to the first (items.duplicate_of).

Two ways the same file shows up twice on one website:
- the same bytes (same sha256) linked from two places;
- a relative link resolved against a "pretty" page address, e.g. /index.php/cut-list/wp-content/uploads/2026/10/x.pdf
  and /wp-content/uploads/2026/10/x.pdf - the server returns the same file for both.
Different files with the same title (each tender's own "Addendum-01") are not duplicates.
"""

from __future__ import annotations

import logging
import re
from urllib.parse import unquote, urlsplit

log = logging.getLogger(__name__)

_FILE_ROOT = re.compile(r"/(wp-content/uploads|sites/default/files|uploads)/", re.I)


def file_identity(url: str | None) -> str | None:
    """Host + the part of the path from the upload folder on, or None when the address has no upload folder."""
    if not url:
        return None
    parts = urlsplit(url)
    path = unquote(parts.path)
    match = _FILE_ROOT.search(path)
    if not match:
        return None
    return (parts.hostname or "").lower().removeprefix("www.") + path[match.start():].lower()


def mark_duplicates(store) -> int:
    """Set duplicate_of on later copies (and clear it when they are no longer copies). Returns how many changed."""
    first: dict[tuple, int] = {}
    changed = 0
    rows = store.db.execute("SELECT id, site_code, url, sha256, duplicate_of, ai_status FROM items "
                            "WHERE removed=0 AND is_document=1 ORDER BY id").fetchall()
    for row in rows:
        keys = []
        if row["sha256"]:
            keys.append(("sha", row["site_code"], row["sha256"]))
        identity = file_identity(row["url"])
        if identity:
            keys.append(("path", row["site_code"], identity))
        primary = next((first[k] for k in keys if k in first), None)
        for key in keys:
            first.setdefault(key, primary or row["id"])
        if primary != row["duplicate_of"]:
            fields = {"duplicate_of": primary}
            if primary and row["ai_status"] in ("pending", "failed"):
                fields.update(ai_status="skipped", ai_error=f"duplicate of #{primary}")
            store.update_item(row["id"], **fields)
            changed += 1
    store.commit()
    if changed:
        log.info("Duplicates: %d items re-pointed to the first copy of the same file", changed)
    return changed
