"""Loads config/*.yaml and the .env file."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

import yaml
from dotenv import load_dotenv

from .urls import base_domain

ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = ROOT / "config"
DATA_DIR = ROOT / "data"
TEXT_DIR = DATA_DIR / "text"          # extracted document text (internal, for the AI)
OUTPUT_DIR = ROOT / "output"          # MASTER Excel + one folder per organisation
NEW_DIR = OUTPUT_DIR / "New_Updates"  # one small Excel per day that had new items
LOG_DIR = ROOT / "logs"
DB_PATH = DATA_DIR / "tracker.db"


def site_dir(code: str) -> Path:
    """output/<CODE>/ : that organisation's Excel and its Documents folder."""
    return OUTPUT_DIR / re.sub(r'[<>:"/\\|?*]+', "_", code)

# Real environment variables (e.g. GitHub Actions secrets) win over .env values.
load_dotenv(ROOT / ".env", override=False)


def env(name: str, default: str = "") -> str:
    return (os.environ.get(name) or default).strip()


def env_bool(name: str, default: bool = False) -> bool:
    value = env(name)
    if not value:
        return default
    return value.lower() in ("1", "true", "yes", "on")


@dataclass
class Site:
    code: str
    name: str
    url: str
    seeds: list[str] = field(default_factory=list)
    js: bool = False
    ssl_verify: bool = True
    extra_hosts: list[str] = field(default_factory=list)
    exclude: list[str] = field(default_factory=list)
    keep_fragments: bool = False
    notes: str = ""

    @property
    def host(self) -> str:
        return (urlsplit(self.url).hostname or "").lower()

    @property
    def origin(self) -> str:
        parts = urlsplit(self.url)
        return f"{parts.scheme}://{parts.netloc}"

    @property
    def domain(self) -> str:
        return base_domain(self.host)

    def owns(self, host: str) -> bool:
        """True if a link on this host belongs to the site (any subdomain, or an extra host)."""
        host = (host or "").lower()
        return base_domain(host) == self.domain or host in self.extra_hosts


@dataclass
class Keywords:
    categories: dict[str, list[re.Pattern]]
    other_patterns: list[re.Pattern]
    tender_categories: list[str]
    relevance_high: list[re.Pattern]
    relevance_medium: list[re.Pattern]
    vague_titles: list[re.Pattern]
    content_phrases: dict[str, list[re.Pattern]]
    crawl_exclude: list[re.Pattern]
    crawl_priority: list[re.Pattern]
    external_doc_hosts: list[str]
    relevance_low: list[re.Pattern] = field(default_factory=list)
    attachment_titles: list[re.Pattern] = field(default_factory=list)


@dataclass
class Settings:
    company_profile: str
    crawl: dict
    documents: dict
    ai: dict
    excel: dict
    notify: dict
    monitoring: dict = field(default_factory=dict)
    backup: dict = field(default_factory=dict)


def _read_yaml(name: str) -> dict:
    with open(CONFIG_DIR / name, encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def _compile(patterns) -> list[re.Pattern]:
    return [re.compile(p, re.I) for p in (patterns or [])]


def load_sites() -> list[Site]:
    data = _read_yaml("sites.yaml")
    sites = []
    for raw in data.get("sites", []):
        sites.append(Site(
            code=str(raw["code"]).strip(),
            name=str(raw["name"]).strip(),
            url=str(raw["url"]).strip(),
            seeds=[str(s).strip() for s in raw.get("seeds") or []],
            js=bool(raw.get("js", False)),
            ssl_verify=bool(raw.get("ssl_verify", True)),
            extra_hosts=[str(h).lower() for h in raw.get("extra_hosts") or []],
            exclude=[str(e) for e in raw.get("exclude") or []],
            keep_fragments=bool(raw.get("keep_fragments", False)),
            notes=str(raw.get("notes") or ""),
        ))
    codes = [s.code for s in sites]
    duplicates = {c for c in codes if codes.count(c) > 1}
    if duplicates:
        raise ValueError(f"Duplicate site codes in sites.yaml: {sorted(duplicates)}")
    return sites


def load_keywords() -> Keywords:
    data = _read_yaml("keywords.yaml")
    return Keywords(
        categories={name: _compile(p) for name, p in (data.get("categories") or {}).items()},
        other_patterns=_compile(data.get("other_patterns")),
        tender_categories=list(data.get("tender_categories") or []),
        relevance_high=_compile(data.get("relevance_high")),
        relevance_medium=_compile(data.get("relevance_medium")),
        vague_titles=_compile(data.get("vague_titles")),
        content_phrases={k: _compile(v) for k, v in (data.get("content_phrases") or {}).items()},
        crawl_exclude=_compile(data.get("crawl_exclude")),
        crawl_priority=_compile(data.get("crawl_priority")),
        external_doc_hosts=[h.lower() for h in data.get("external_doc_hosts") or []],
        relevance_low=_compile(data.get("relevance_low")),
        attachment_titles=_compile(data.get("attachment_titles")),
    )


def load_settings() -> Settings:
    data = _read_yaml("settings.yaml")
    return Settings(
        company_profile=str(data.get("company_profile") or "").strip(),
        crawl=data.get("crawl") or {},
        documents=data.get("documents") or {},
        ai=data.get("ai") or {},
        excel=data.get("excel") or {},
        notify=data.get("notify") or {},
        monitoring=data.get("monitoring") or {},
        backup=data.get("backup") or {},
    )


def ensure_dirs() -> None:
    for d in (DATA_DIR, TEXT_DIR, OUTPUT_DIR, LOG_DIR):
        d.mkdir(parents=True, exist_ok=True)
