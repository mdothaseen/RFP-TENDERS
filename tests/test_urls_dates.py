from datetime import date

from tender_tracker.dates import date_from_url, find_dates, listing_dates
from tender_tracker.urls import (
    canonical_key, clean_url, doc_urls_in_query, gdrive_file_id, is_document_url, title_from_filename,
)

TODAY = date(2026, 9, 30)


def test_clean_url_basics():
    assert clean_url("https://www.nsdcindia.org:443/active-tender") == "https://www.nsdcindia.org/active-tender"
    assert clean_url("/pdf/a b.pdf", base="https://x.org/y/") == "https://x.org/pdf/a%20b.pdf"
    assert clean_url("mailto:a@b.c") is None
    assert clean_url("javascript:void(0)") is None
    # typo links "file..pdf" point to "file.pdf"
    assert clean_url("https://cgssc.org/u/CORRIGENDUM-1..pdf") == "https://cgssc.org/u/CORRIGENDUM-1.pdf"


def test_canonical_key_merges_variants():
    a = canonical_key("https://www.bwssc.in/career/?utm_source=x")
    b = canonical_key("http://bwssc.in/career")
    assert a == b
    assert canonical_key("https://texskill.in/notice2025.php#notice13", keep_fragment=True).endswith("#notice13")


def test_document_detection():
    assert is_document_url("https://x.org/a/EOI.PDF")
    assert is_document_url("https://drive.google.com/file/d/14K3S6BQQtbNPMFqAs7EpSXuJSL-IPWJQ/view?usp=sharing")
    assert gdrive_file_id("https://drive.google.com/file/d/14K3S6BQQtbNPMFqAs7EpSXuJSL-IPWJQ/view") == \
        "14K3S6BQQtbNPMFqAs7EpSXuJSL-IPWJQ"
    assert not is_document_url("https://x.org/tenders/")
    assert doc_urls_in_query("https://www.essc-india.org/flipbook.html?pdf=newsletters/Newsletter July 2026.pdf") == [
        "https://www.essc-india.org/newsletters/Newsletter%20July%202026.pdf"]


def test_title_from_filename():
    url = "https://www.sscamh.com/public/uploads/notifications/17873281556a88769b1ec4eEoI - TP Empanelment - Maharashtra.pdf"
    assert title_from_filename(url) == "EoI - TP Empanelment - Maharashtra"
    assert title_from_filename("https://www.lsssdc.in/uploads/common/1728366830_c69831b371fe668268b1.pdf") == ""
    assert title_from_filename("https://www.sscamh.com/u/1755876415_68a88c3fc44e5RPF for CA Firms.pdf") == "RPF for CA Firms"


def test_find_dates_formats():
    assert find_dates("2024-09-24 Request for Proposal", TODAY)[0][0] == date(2024, 9, 24)
    assert find_dates("August 21st 2026 - Empanelment", TODAY)[0][0] == date(2026, 8, 21)
    assert find_dates("EOI for AA-RASCI-24Aug2026", TODAY)[0][0] == date(2026, 8, 24)
    assert find_dates("Jul 9th 2026 - Achievement", TODAY)[0][0] == date(2026, 7, 9)
    assert find_dates("dated 05/10/2026", TODAY)[0][0] == date(2026, 10, 5)
    assert find_dates("30 Aug | EOI for Mobilization", TODAY)[0][0] == date(2026, 8, 30)
    assert find_dates("HYC_Q9101_v8.0 Industrial Welder", TODAY) == []


def test_listing_dates_roles():
    published, deadline = listing_dates("12 Sep 2026 EOI for TPs. Last date: 15 Oct 2026", TODAY)
    assert published == date(2026, 9, 12)
    assert deadline == date(2026, 10, 15)


def test_date_from_url():
    assert date_from_url("https://cgssc.org/wp-content/uploads/2026/06/x.pdf", TODAY) == (date(2026, 6, 1), "upload folder")
    d, source = date_from_url("https://www.lsssdc.in/uploads/common/1728366830_c69831b371fe668268b1.pdf", TODAY)
    assert source == "file name" and d.year == 2024


def test_listing_dates_table_row_with_past_last_date():
    text = "1 Request for Quotation (RFQ) For Nirbhaya Self-Defense Kits 10-08-2026 15-08-2026 Download"
    assert listing_dates(text, TODAY) == (date(2026, 8, 10), date(2026, 8, 15))


def test_epoch_uniqid_file_names():
    d, _ = date_from_url("https://www.sscamh.com/u/17873281556a88769b1ec4eEoI - TP Empanelment.pdf", TODAY)
    assert d == date(2026, 8, 21)
