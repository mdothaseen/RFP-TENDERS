"""URL helpers: cleaning, dedupe keys, document detection."""

from __future__ import annotations

import hashlib
import re
from urllib.parse import parse_qsl, quote, unquote, urlencode, urljoin, urlsplit, urlunsplit

DOC_EXTS = {
    "pdf", "doc", "docx", "xls", "xlsx", "ppt", "pptx", "zip", "rar", "odt", "ods", "rtf", "csv",
}
TWO_LEVEL_SUFFIXES = {
    "gov.in", "org.in", "co.in", "ac.in", "nic.in", "net.in", "edu.in", "res.in", "gen.in",
    "firm.in", "ind.in", "co.uk", "org.uk", "com.au",
}
_TRACKING = re.compile(r"^(utm_|fbclid$|gclid$|mc_cid$|mc_eid$|_ga$)", re.I)
_GDRIVE_FILE = re.compile(r"drive\.google\.com/(?:file/d/|open\?id=|uc\?(?:.*&)?id=)([\w-]{20,})")
_GDOC = re.compile(r"docs\.google\.com/(document|spreadsheets|presentation)/d/([\w-]{20,})")
_PATH_SAFE = "/%:@!$&'()*+,;=-._~"
_DOUBLE_DOT_EXT = re.compile(r"\.{2,}(?=(?:pdf|docx?|xlsx?|pptx?|zip|rar)$)", re.I)
_QUERY_SAFE = "=&%:/?+,;@!$'()*-._~"


def base_domain(host: str | None) -> str:
    """'www.nsdcindia.org' -> 'nsdcindia.org', 'ncvet.gov.in' -> 'ncvet.gov.in'."""
    host = (host or "").lower().strip(".")
    parts = host.split(".")
    if len(parts) >= 3 and ".".join(parts[-2:]) in TWO_LEVEL_SUFFIXES:
        return ".".join(parts[-3:])
    return ".".join(parts[-2:])


def host_of(url: str | None) -> str:
    try:
        return (urlsplit(url or "").hostname or "").lower()
    except ValueError:
        return ""


def clean_url(url: str | None, base: str | None = None, keep_fragment: bool = False) -> str | None:
    """Absolute, fetchable http(s) URL, or None."""
    if not url:
        return None
    url = url.strip().strip("'\"").replace("\\", "/")
    if not url or url.startswith(("javascript:", "mailto:", "tel:", "data:", "whatsapp:")):
        return None
    if base:
        try:
            url = urljoin(base, url)
        except ValueError:
            return None
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        return None
    scheme = parts.scheme.lower()
    host = (parts.hostname or "").lower()
    if scheme not in ("http", "https") or not host or " " in host:
        return None
    netloc = host
    if port and not ((scheme == "https" and port == 443) or (scheme == "http" and port == 80)):
        netloc = f"{host}:{port}"
    path = quote(unquote(parts.path or "/"), safe=_PATH_SAFE)
    # Some sites link "file..pdf" (typo) while the real file is "file.pdf".
    path = _DOUBLE_DOT_EXT.sub(".", path)
    query = quote(parts.query, safe=_QUERY_SAFE) if parts.query else ""
    fragment = parts.fragment if keep_fragment else ""
    return urlunsplit((scheme, netloc, path, query, fragment))


def canonical_key(url: str, keep_fragment: bool = False) -> str:
    """Dedupe key: ignores http/https, www., default ports, duplicate slashes and tracking params."""
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    path = re.sub(r"/{2,}", "/", unquote(parts.path or "/"))
    if len(path) > 1:
        path = path.rstrip("/")
    query_pairs = sorted(
        (k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if not _TRACKING.match(k)
    )
    key = host + path
    if query_pairs:
        key += "?" + urlencode(query_pairs)
    if keep_fragment and parts.fragment:
        key += "#" + parts.fragment
    return key


def text_key(site_code: str, text: str) -> str:
    """Dedupe key for items that have no link (e.g. ticker text)."""
    norm = re.sub(r"\W+", " ", text.lower()).strip()
    return f"text:{site_code}:{hashlib.sha1(norm.encode('utf-8')).hexdigest()[:16]}"


def url_ext(url: str | None) -> str:
    try:
        path = unquote(urlsplit(url or "").path)
    except ValueError:
        return ""
    name = path.rsplit("/", 1)[-1]
    if "." not in name:
        return ""
    return name.rsplit(".", 1)[-1].lower().strip()


def gdrive_file_id(url: str) -> str | None:
    m = _GDRIVE_FILE.search(url or "")
    return m.group(1) if m else None


def google_doc_export(url: str) -> str | None:
    m = _GDOC.search(url or "")
    if not m:
        return None
    kind, doc_id = m.groups()
    return f"https://docs.google.com/{kind}/d/{doc_id}/export?format=pdf"


def is_document_url(url: str | None) -> bool:
    if not url:
        return False
    if url_ext(url) in DOC_EXTS:
        return True
    return bool(gdrive_file_id(url) or google_doc_export(url))


def doc_urls_in_query(url: str) -> list[str]:
    """'flipbook.html?pdf=news/Letter.pdf' -> ['.../news/Letter.pdf']."""
    found = []
    try:
        pairs = parse_qsl(urlsplit(url).query, keep_blank_values=False)
    except ValueError:
        return found
    for _, value in pairs:
        if url_ext(value) in DOC_EXTS:
            cleaned = clean_url(value, base=url)
            if cleaned:
                found.append(cleaned)
    return found


def filename_from_url(url: str) -> str:
    try:
        path = unquote(urlsplit(url).path)
    except ValueError:
        return ""
    return path.rstrip("/").rsplit("/", 1)[-1]


_EPOCH_PREFIX = re.compile(r"^(1[5-9]\d{8})[_-]?(?:[0-9a-f]{13})?[_-]?", re.I)


def title_from_filename(url: str) -> str:
    """Readable title from a file name: '1787328155...EoI - TP Empanelment.pdf' -> 'EoI - TP Empanelment'."""
    name = filename_from_url(url)
    if "." in name:
        name = name.rsplit(".", 1)[0]
    name = _EPOCH_PREFIX.sub("", name)
    name = re.sub(r"[_+]+", " ", name)
    name = re.sub(r"(?<=[a-z])-(?=[a-z])", " ", name, flags=re.I) if name.count("-") > 2 else name
    name = re.sub(r"\s+", " ", name).strip(" -")
    if re.fullmatch(r"(?=.*\d)[0-9a-f]{6,}", name, re.I):
        return ""  # only a random id is left
    return name


def matches_any(patterns, text: str) -> bool:
    return any(p.search(text) for p in patterns)


__all__ = [
    "base_domain", "host_of", "clean_url", "canonical_key", "text_key", "url_ext", "gdrive_file_id",
    "google_doc_export", "is_document_url", "doc_urls_in_query", "filename_from_url",
    "title_from_filename", "matches_any", "DOC_EXTS", "urljoin",
]
