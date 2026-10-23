"""`--add-site URL "Name"`: add a website to config/sites.yaml without editing the file by hand."""

from __future__ import annotations

import re

from .config import CONFIG_DIR, load_settings, load_sites
from .fetcher import Fetcher
from .urls import base_domain, clean_url, host_of


def _make_code(name: str, url: str, taken: set[str]) -> str:
    """'Power Sector Skill Council (PSSC)' -> 'PSSC'; otherwise initials, else the domain name."""
    m = re.search(r"\(([A-Za-z0-9&\-]{2,12})\)", name)
    code = m.group(1) if m else ""
    if not code:
        words = [w for w in re.findall(r"[A-Za-z]+", name) if w.lower() not in ("of", "and", "the", "for", "in")]
        code = "".join(w[0] for w in words)[:8] if len(words) >= 2 else ""
    if not code:
        code = base_domain(host_of(url)).split(".")[0][:10]
    code = re.sub(r"[^A-Za-z0-9-]", "", code).upper() or "SITE"
    candidate, n = code, 2
    while candidate in taken:
        candidate, n = f"{code}{n}", n + 1
    return candidate


def add_site(url: str, name: str, code: str | None = None) -> int:
    url = clean_url(url if "://" in url else "https://" + url)
    if not url:
        print("Website ka link sahi nahi hai. Example: https://www.example.org")
        return 2
    sites = load_sites()
    for s in sites:
        if base_domain(s.host) == base_domain(host_of(url)):
            print(f"Ye website pehle se list mein hai: {s.code} ({s.name})")
            return 1
    code = (code or _make_code(name, url, {s.code for s in sites})).upper()
    if code in {s.code for s in sites}:
        print(f"Code {code} pehle se use ho raha hai, doosra code dein.")
        return 2

    # Quick check: does the site open, and is it built with JavaScript?
    from .config import Site
    probe = Site(code=code, name=name, url=url)
    resp = Fetcher(probe, load_settings()).get(url, textual_only=True)
    if resp.error or not resp.status or resp.status >= 400:
        print(f"Warning: website abhi nahi khuli ({resp.error or 'HTTP ' + str(resp.status)}). Phir bhi add kar raha hoon.")
        js = False
    else:
        html = resp.text
        js = len(html) < 5000 or html.lower().count("<a ") < 15
        url = clean_url(resp.final_url) or url

    safe_name = name.replace('"', "'")
    block = (f"\n  - code: {code}\n    name: \"{safe_name}\"\n    url: {url}\n"
             + ("    js: true\n" if js else "")
             + "    seeds: []\n")
    with open(CONFIG_DIR / "sites.yaml", "a", encoding="utf-8") as fh:
        fh.write(block)
    load_sites()  # validates the file
    print(f"Added: {code}  {name}  {url}" + ("  (JavaScript site)" if js else ""))
    print(f"Test karne ke liye:  .venv\\Scripts\\python -m tender_tracker --site {code}")
    print("Kal ke daily run se ye website apne aap check hogi.")
    return 0
