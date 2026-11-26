"""Live check on this machine: solve a GePNIC download CAPTCHA and fetch the tender document.

Run from the project folder:
    .venv\\Scripts\\python.exe check_captcha.py                 # CPPP, 2 documents
    .venv\\Scripts\\python.exe check_captcha.py --site EPMH --docs 3

It walks the portal (organisation -> department -> tender), solves the download CAPTCHA with local
Tesseract, saves the bundle to output/<SITE>/_captcha_check/ and prints PASS/FAIL with the extracted
text size, so you can confirm both the CAPTCHA solver and the PDF download work on this computer.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path
from urllib.parse import urljoin

from tender_tracker import captcha, documents
from tender_tracker.config import load_settings, load_sites, site_dir
from tender_tracker.discover.nic_portal import TESSDATA, parse_org_list, parse_tenders
from tender_tracker.fetcher import Fetcher


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--site", default="EPCPPP", help="portal code from config/sites.yaml")
    ap.add_argument("--docs", type=int, default=2, help="how many tenders to try")
    args = ap.parse_args()

    settings = load_settings()
    sites = {s.code: s for s in load_sites()}
    site = sites.get(args.site.upper())
    if site is None or not site.nic_orgs:
        print(f"{args.site}: not a configured e-procurement portal")
        return 2
    config = settings.documents.get("captcha") or {}
    command = captcha.tesseract_command(str(config.get("tesseract_path") or ""))
    print("Tesseract binary:", command or "NOT FOUND (install Tesseract-OCR)")
    if not command:
        return 2

    app = site.url.split("?")[0]
    fetcher = Fetcher(site, settings)
    patterns = [re.compile(p, re.I) for p in site.nic_orgs]
    orgs = fetcher.get(app + "?page=FrontEndTendersByOrganisation&service=page")
    departments = parse_org_list(orgs.text, patterns)
    if not departments:
        print("No matching department list (portal changed or blocked)")
        return 2
    name, link = departments[0]
    tenders = parse_tenders(fetcher.get(urljoin(app, link)).text)
    print(f"Portal {site.code}: department '{name}', {len(tenders)} tenders listed")
    if not tenders:
        return 2

    out = site_dir(site.code) / "_captcha_check"
    out.mkdir(parents=True, exist_ok=True)
    ok = 0
    for t in tenders[:max(1, args.docs)]:
        detail = fetcher.get(urljoin(app, t["view"]))
        m = re.search(r'href="([^"]*component=docDownoad[^"]*)"', detail.text)
        if not m:
            print(f"  {t['id']}: no document link on detail page")
            continue
        url = urljoin(app, m.group(1).replace("&amp;", "&"))
        content, error = captcha.solve(fetcher, app, url, config, TESSDATA)
        if not content:
            print(f"  {t['id']}: FAIL - {error}")
            continue
        tmp = out / f"{t['id']}.bin"
        tmp.write_bytes(content)
        kind = documents.sniff_kind(tmp)
        target = out / f"{t['id']}.{kind}"
        tmp.replace(target)
        info = documents.extract_text(target, kind)
        ok += 1
        print(f"  {t['id']}: PASS - {len(content)/1_000_000:.2f} MB {kind}, "
              f"{info.pages} pages, {len(info.text)} chars text -> {target}")
    fetcher.session.close()
    print(f"\nRESULT: {ok} document(s) downloaded with CAPTCHA solved on this machine.")
    print(f"Files: {out}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
