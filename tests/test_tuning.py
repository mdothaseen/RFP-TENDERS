"""Tuning on real titles from the 4 Nov 2026 scan: relevance, tender attachments, noise, titles, page changes."""

from datetime import datetime

import pytest

from tender_tracker.classify import classify
from tender_tracker.config import Site, load_keywords, load_settings
from tender_tracker.discover.crawler import Found, SiteScanner, _tidy_title
from tender_tracker.fetcher import Response
from tender_tracker.reclassify import reclassify
from tender_tracker.scanning import page_change_class
from tender_tracker.store import Store

KW = load_keywords()


@pytest.mark.parametrize("title, category, relevance", [
    ("OFFICE ORDER : Onboarding of Assessment Agencies", "Empanelment", "High"),
    ("EOI-TPA-Empanelement.pdf", "EOI", "High"),
    ("RFP for Skill Gap Study in Odisha", "RFP", "Medium"),
    ("EoI for Empanelment of Bihar Based Training Providers", "EOI", "High"),
    ("27 Oct 2026 Vendor Request for Proposal for training of beneficiaries in Beauty & Wellness", "RFP", "High"),
    ("RFP for the Skill Development Training Programme under the REC CSR initiative.", "RFP", "High"),
    ("NIT- OAW-2333", "Tender", "Medium"),  # title says nothing: stays visible until Claude reads it
    ("Repairing and Repainting of STUDENTS' ROOMs for winter vacation 2026 in VARIOUS hostels of IITG", "Tender", "Low"),
    ("Quotation for Purchase of Drawing Board", "RFP", "Low"),
    ("Tender for Procurement of Chemistry Laboratory Equipment", "Tender", "Low"),
    ("Comprehensive Annual Operation & Maintenance service of sewage treatment system of IIT Guwahati", "Tender", "Low"),
])
def test_relevance_follows_the_assessment_profile(title, category, relevance):
    cls = classify(KW, title, "Tenders / e-Tender notices")  # the listing these titles were found on
    assert (cls.category, cls.relevance) == (category, relevance)


@pytest.mark.parametrize("title", [
    "Price Bid", "Technical Bid", "Declaration GST", "Declaration Litigation", "Declaration Blacklisting",
    "Specificaitons", "Terms and Conditions Purchase of Goods", "Annexure-II", "BOQ",
])
def test_tender_attachments_are_not_tenders(title):
    assert classify(KW, title).category == "Other"


@pytest.mark.parametrize("title", ["E-Tender Notice (NIT)", "Technical Bid for Assessment of PMKVY candidates",
                                   "Declaration of results of empanelment of assessment agencies"])
def test_real_tenders_are_kept(title):
    assert classify(KW, title).category != "Other"


@pytest.mark.parametrize("title", ["Revised Result of D.Voc Mechanical 2nd Semester", "Date Sheet for December 2026",
                                   "Admit card for ODL examination", "Syllabus B.Voc Retail"])
def test_university_student_pages_are_noise(title):
    assert classify(KW, title).category == "Other"


def test_student_pages_are_not_crawled():
    excluded = lambda url: any(p.search(url) for p in KW.crawl_exclude)
    assert excluded("https://svsu.ac.in/admissions/") and excluded("https://x.ac.in/date-sheet.php")
    assert not excluded("https://svsu.ac.in/tenders/") and not excluded("https://x.org/notice-board/")


@pytest.mark.parametrize("raw, clean", [
    ("RFP for Nabarangpur Cluster Odisha Size: 847.91 KB | Language: English | Uploading Date: 23-10-2026",
     "RFP for Nabarangpur Cluster Odisha"),
    ("PDF file: EOI FOR CENTRALIZED EMPANELMENT OF BACK-END PARTNERS", "EOI FOR CENTRALIZED EMPANELMENT OF BACK-END PARTNERS"),
    ("Tender documents for Chemestry Labortory-20261026100733", "Tender documents for Chemestry Labortory"),
    ("Expression of Interest (EOI) � PMKVY 4.0 � Power Sector.", "Expression of Interest (EOI) - PMKVY 4.0 - Power Sector."),
    ("Notice for Language Lab Setup", "Notice for Language Lab Setup"),
])
def test_titles_are_cleaned(raw, clean):
    assert _tidy_title(raw) == clean


def test_page_change_is_never_a_new_tender():
    assert page_change_class(KW, "+ Walk-in interview for Accountant on 12 Nov").category == "Notice"
    assert page_change_class(KW, "+ Last date extended till 30 November 2026 for the EOI").category == "Notice"


def _scanner(monkeypatch, responses):
    site = Site(code="TST", name="Test", url="https://t.example.org/")
    scanner = SiteScanner(site, load_settings(), KW, set())
    scanner.settings.crawl["respect_robots"] = False
    monkeypatch.setattr(scanner, "_sitemaps", lambda: None)
    monkeypatch.setattr(scanner, "_wordpress", lambda: None)
    calls = iter(responses)
    monkeypatch.setattr(scanner.fetcher, "get", lambda url, **kw: next(calls)(url))
    return scanner


def test_old_404_links_do_not_make_a_site_partial(monkeypatch):
    home = "<html>" + "".join(f'<a href="/old{i}">Old notice {i}</a>' for i in range(20)) + "x" * 6000 + "</html>"
    page = lambda url: Response(url, url, 200, {"content-type": "text/html"}, home.encode())
    gone = lambda url: Response(url, url, 404, {"content-type": "text/html"}, b"not found")
    scan = _scanner(monkeypatch, [page] + [gone] * 20).run()
    assert scan.error_count == 20 and scan.status == "ok"


def test_a_site_that_stops_answering_is_left(monkeypatch):
    home = "<html>" + "".join(f'<a href="/p{i}">Notice {i}</a>' for i in range(20)) + "x" * 6000 + "</html>"
    page = lambda url: Response(url, url, 200, {"content-type": "text/html"}, home.encode())
    slow = lambda url: Response(url, url, None, error="ReadTimeout: read timed out")
    scan = _scanner(monkeypatch, [page] + [slow] * 20).run()
    assert any("stopped answering" in e for e in scan.errors)
    assert scan.error_count == 4  # three timeouts, then the site is left for the next scan


def test_reclassify_applies_new_rules_to_saved_items(tmp_path):
    store = Store(tmp_path / "t.db")
    settings = load_settings()
    ids = {}
    for key, title in [("bid", "Price Bid"), ("repair", "Repairing of hostel rooms"), ("aa", "EOI for assessment agencies")]:
        f = Found(key=key, site_code="TST", url=f"https://x.org/{key}.pdf", title=title, link_text=title,
                  context="", source_page="", via={"page"}, linked=True, is_document=True)
        ids[key], _ = store.upsert(f, classify(KW, title), 1, False, datetime.now().isoformat(), {"Tender", "EOI"})
    store.update_item(ids["bid"], category="Tender", relevance="Medium", ai_status="pending")
    store.update_item(ids["repair"], relevance="Medium")
    store.commit()

    reclassify(store, KW, settings)
    bid, repair, aa = (store.item(ids[k]) for k in ("bid", "repair", "aa"))
    assert bid["category"] == "Other" and bid["ai_status"] == "skipped"
    assert repair["relevance"] == "Low"
    assert aa["relevance"] == "High" and aa["ai_status"] == "pending"


def test_same_file_under_two_addresses_is_listed_once(tmp_path):
    from tender_tracker.duplicates import file_identity, mark_duplicates
    assert file_identity("https://svsu.ac.in/index.php/cut-list/wp-content/uploads/2026/10/Office%20Order%20122.pdf")         == file_identity("https://svsu.ac.in/wp-content/uploads/2026/10/Office%20Order%20122.pdf")
    store = Store(tmp_path / "t.db")
    ids = {}
    for key, url in [("a", "https://svsu.ac.in/wp-content/uploads/2026/10/Office%20Order%20122.pdf"),
                     ("b", "https://svsu.ac.in/index.php/cut-list/wp-content/uploads/2026/10/Office%20Order%20122.pdf"),
                     ("c", "https://x.org/files/eoi-copy.pdf"), ("d", "https://x.org/files/eoi.pdf"),
                     ("e", "https://dmrc.example/documents/11455/Addendum-01-CPD-165.pdf"),
                     ("f", "https://dmrc.example/documents/11480/Addendum-01-RS-POD-01.pdf")]:
        title = "Addendum-01" if key in "ef" else "Extension of last date"
        f = Found(key=key, site_code="TST", url=url, title=title, link_text=title, context="", source_page="",
                  via={"page"}, linked=True, is_document=True)
        ids[key], _ = store.upsert(f, classify(KW, title), 1, False, datetime.now().isoformat(), {"Corrigendum"})
    store.update_item(ids["c"], sha256="same")
    store.update_item(ids["d"], sha256="same")
    store.commit()
    assert mark_duplicates(store) == 2
    dup = {k: store.item(i)["duplicate_of"] for k, i in ids.items()}
    assert dup == {"a": None, "b": ids["a"], "c": None, "d": ids["c"], "e": None, "f": None}
    assert store.item(ids["b"])["ai_status"] == "skipped"
    assert mark_duplicates(store) == 0


def test_a_hung_website_cannot_hold_the_scan():
    import threading
    from tender_tracker.scanning import run_with_deadline
    never = threading.Event()

    def start(code, site):
        if code == "HUNG":
            never.wait(30)  # e.g. a browser call that never returns
        if code == "BAD":
            raise RuntimeError("broken page")
        return f"scan of {code}"

    sites = {"OK1": None, "HUNG": None, "BAD": None, "OK2": None}
    got = {code: (scan, type(exc).__name__ if exc else None)
           for code, scan, exc in run_with_deadline(sites, start, workers=2, deadline=1.5)}
    never.set()
    assert got == {"OK1": ("scan of OK1", None), "OK2": ("scan of OK2", None),
                   "BAD": (None, "RuntimeError"), "HUNG": (None, "TimeoutError")}


@pytest.mark.parametrize("title, category", [
    ("NIT- OAW-2333", "Tender"), ("E-Tender Notice (NIT)", "Tender"), ("NIT No. 12/2026 for printing", "Tender"),
    ("Control of Multi-Input Converter (NIT Durgapur report)", "Other"),
])
def test_nit_means_a_tender_notice_not_an_institute(title, category):
    assert classify(KW, title).category == category


def test_undated_old_links_are_not_new():
    from datetime import date
    from tender_tracker.excel_report import _looks_old_undated
    today = date(2026, 11, 4)
    entry = {"CA": {"coa.gov.in"}}
    row = lambda title, url, run, page: {"title": title, "url": url, "first_run_id": run, "site_code": "CA",
                                         "source_page": page}
    assert _looks_old_undated(row("Tender Documents NATA 2022", "https://coa.gov.in/f?id=1", 55, "https://coa.gov.in/"),
                              today, set(), entry)
    assert _looks_old_undated(row("[ PDF, 525.22 KB]", "https://niwe.res.in/static/pdf/RFP_2025.pdf", 55, ""), today,
                              set(), entry)
    # A deep scan's inner page (archive) vs. the homepage
    assert _looks_old_undated(row("Revised Tender Document", "https://coa.gov.in/f?id=2", 54,
                                  "https://coa.gov.in/sitemap.php?lang=1"), today, {54}, entry)
    assert not _looks_old_undated(row("Revised Tender Document", "https://coa.gov.in/f?id=2", 54,
                                      "https://www.coa.gov.in/"), today, {54}, entry)
    assert not _looks_old_undated(row("EOI for assessment agencies 2026-27", "https://coa.gov.in/f?id=3", 55,
                                      "https://coa.gov.in/notices"), today, {54}, entry)
