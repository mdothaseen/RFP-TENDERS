"""Find dates in listing text, file names and URLs (Indian day-month-year order)."""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from email.utils import parsedate_to_datetime

from .urls import filename_from_url

_MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6, "jul": 7, "aug": 8,
    "sep": 9, "sept": 9, "oct": 10, "nov": 11, "dec": 12,
}
_MON = (r"(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?"
        r"|sept?(?:ember)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)")
_SEP = r"[\s._,/-]*"
_ORD = r"(?:st|nd|rd|th)?"

_PATTERNS = [
    ("iso", re.compile(r"(?<!\d)(20\d{2})[-/.](\d{1,2})[-/.](\d{1,2})(?!\d)")),
    ("d_mon_y", re.compile(rf"(?<![a-z\d])(\d{{1,2}}){_ORD}{_SEP}{_MON}\.?{_SEP}(20\d{{2}}|\d{{2}})(?![\d:])", re.I)),
    ("mon_d_y", re.compile(rf"(?<![a-z]){_MON}\.?{_SEP}(\d{{1,2}}){_ORD},?{_SEP}(20\d{{2}})(?!\d)", re.I)),
    ("dmy", re.compile(r"(?<![\d.])(\d{1,2})[-/.](\d{1,2})[-/.]((?:20)?\d{2})(?![\d.])")),
    ("d_mon", re.compile(rf"(?<![a-z\d])(\d{{1,2}}){_ORD}{_SEP}{_MON}\b(?![\s._,/-]*\d)", re.I)),
]

_DEADLINE_BEFORE = re.compile(
    r"\b(last\s*date|due\s*date|deadline|closing|closes?|close\s*on|till|until|upto|up\s*to|"
    r"on\s*or\s*before|before|by|extended\s*(?:to|till|upto)?|submission|submit|valid\s*till|"
    r"end\s*date|apply\s*by)\W{0,12}$",
    re.I,
)
_PUBLISHED_BEFORE = re.compile(
    r"(dated|dt\.?|date\s*of\s*issue|issued\s*on|issue\s*date|published|posted|uploaded|updated\s*on|date)\W{0,6}$",
    re.I,
)


def _month(token: str) -> int:
    return _MONTHS.get(token.lower()[:4] if token.lower().startswith("sept") else token.lower()[:3], 0)


def _year(token: str) -> int:
    y = int(token)
    return 2000 + y if y < 100 else y


def _safe_date(y: int, m: int, d: int) -> date | None:
    try:
        return date(y, m, d)
    except ValueError:
        return None


def find_dates(text: str, today: date | None = None) -> list[tuple[date, int, int]]:
    """All plausible dates in text as (date, start, end), in text order."""
    today = today or date.today()
    found: list[tuple[date, int, int]] = []
    taken: list[tuple[int, int]] = []
    if not text:
        return found
    for kind, pattern in _PATTERNS:
        for m in pattern.finditer(text):
            start, end = m.span()
            if any(start < e and end > s for s, e in taken):
                continue
            g = m.groups()
            dt = None
            if kind == "iso":
                dt = _safe_date(int(g[0]), int(g[1]), int(g[2]))
            elif kind == "dmy":
                d, mo, y = int(g[0]), int(g[1]), _year(g[2])
                if mo > 12 and d <= 12:
                    d, mo = mo, d
                dt = _safe_date(y, mo, d)
            elif kind == "d_mon_y":
                dt = _safe_date(_year(g[2]), _month(g[1]), int(g[0]))
            elif kind == "mon_d_y":
                dt = _safe_date(int(g[2]), _month(g[0]), int(g[1]))
            elif kind == "d_mon":
                mo, d = _month(g[1]), int(g[0])
                dt = _safe_date(today.year, mo, d)
                if dt and dt > today + timedelta(days=45):
                    dt = _safe_date(today.year - 1, mo, d)
            if not dt or not (2010 <= dt.year <= today.year + 2):
                continue
            found.append((dt, start, end))
            taken.append((start, end))
    found.sort(key=lambda t: t[1])
    return found


def listing_dates(text: str, today: date | None = None) -> tuple[date | None, date | None]:
    """(published, deadline) guessed from a listing line such as '30 Aug | EOI ... last date 15 Sep'."""
    today = today or date.today()
    published = deadline = None
    later = []
    for dt, start, _ in find_dates(text, today):
        before = text[max(0, start - 40):start]
        if _DEADLINE_BEFORE.search(before):
            deadline = deadline or dt
        elif _PUBLISHED_BEFORE.search(before):
            published = published or dt
        elif published is None and dt <= today + timedelta(days=2):
            published = dt
        elif deadline is None and dt > today:
            deadline = dt
        else:
            later.append(dt)
    if deadline is None and published is not None:
        # Table rows: "Issue date | Last date" - the later date is the last date, even if it has passed.
        after = [d for d in later if d > published]
        if after:
            deadline = after[0]
    return published, deadline


_UPLOAD_PATH = re.compile(r"/(20\d{2})/(0[1-9]|1[0-2])/")
# Upload time stamps in file names: "1728366830_c69.pdf", "17873281556a88769b1ec4eEoI.pdf" (time + uniqid).
_EPOCH = re.compile(r"^(1[5-9]\d{8})(?=[_\-]|[0-9a-f]{13}|\.|$)|(?<!\d)(1[5-9]\d{8})(?!\d)", re.I)


def date_from_url(url: str, today: date | None = None) -> tuple[date | None, str]:
    """Best date hint from a URL: /uploads/YYYY/MM/, epoch-prefixed file names, dates in file names."""
    today = today or date.today()
    name = filename_from_url(url)
    m = _EPOCH.search(name)
    if m:
        try:
            dt = datetime.fromtimestamp(int(m.group(1) or m.group(2))).date()
            if 2016 <= dt.year <= today.year + 1:
                return dt, "file name"
        except (OverflowError, OSError, ValueError):
            pass
    readable = re.sub(r"[_+]", " ", name.rsplit(".", 1)[0] if "." in name else name)
    for dt, _, _ in find_dates(readable, today):
        if dt <= today + timedelta(days=2):
            return dt, "file name"
    m = _UPLOAD_PATH.search(url)
    if m:
        dt = _safe_date(int(m.group(1)), int(m.group(2)), 1)
        if dt and dt <= today + timedelta(days=31):
            return dt, "upload folder"
    return None, ""


def parse_iso(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.strip().replace("Z", "+00:00")[:19]).date()
    except ValueError:
        pass
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})", value.strip())
    return _safe_date(*map(int, m.groups())) if m else None


def parse_http_date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return parsedate_to_datetime(value).date()
    except (TypeError, ValueError, IndexError):
        return None


def parse_any(value: str | None, today: date | None = None) -> date | None:
    """ISO first, then any format find_dates understands (used for AI output)."""
    if not value:
        return None
    iso = parse_iso(value)
    if iso:
        return iso
    hits = find_dates(value, today)
    return hits[0][0] if hits else None


def upload_date_from_url(url: str, today: date | None = None) -> tuple[date | None, str]:
    """Upload time that sites store in the file address: a time stamp in the file name, or /uploads/YYYY/MM/."""
    today = today or date.today()
    name = filename_from_url(url)
    m = _EPOCH.search(name)
    if m:
        try:
            dt = datetime.fromtimestamp(int(m.group(1) or m.group(2))).date()
            if 2016 <= dt.year <= today.year + 1:
                return dt, "file name time stamp"
        except (OverflowError, OSError, ValueError):
            pass
    m = _UPLOAD_PATH.search(url)
    if m:
        dt = _safe_date(int(m.group(1)), int(m.group(2)), 1)
        if dt and dt <= today + timedelta(days=31):
            return dt, "upload folder (month only)"
    return None, ""
