import pytest

from tender_tracker.classify import classify, content_category, is_vague
from tender_tracker.config import load_keywords
from tender_tracker.discover.extract import parse_html


@pytest.fixture(scope="module")
def kw():
    return load_keywords()


@pytest.mark.parametrize("title,url,expected", [
    ("EOI for Empanelment of Assessment Agency 24 August 2026", "", "EOI"),
    ("August 21st 2026 - Empanelment of Assessment Agencies (RFP)", "", "RFP"),
    ("Request for Proposal (RFP) – Engagement of Internal Auditor for CSDCI", "", "RFP"),
    ("Corrigendum-1 to RFP for Skill Gap Study", "", "Corrigendum"),
    ("Notification for Assessment Agencies for FY 2026-27", "", "Notice"),
    ("Vacancy Advertisement: Regional Manager-South", "", "Recruitment"),
    ("Advertisement for Engagement of Professionals", "", "Recruitment"),
    ("", "https://www.hsscindia.in/uploads/2023/03/HYC_Q9101_v8.0-Industrial-Welder.pdf", "Other"),
    ("Approved Job Role Details 2025 Version 4.0", "", "Other"),
    ("Blacklisting Notice MESC", "", "Notice"),
    ("Tender : Creation of a New Website for Indian Iron and Steel SSC", "", "Tender"),
    ("Hydrocarbon Sector Skill Council is seeking Training partner for execution of Short-Term Training", "", "Empanelment"),
    ("CGSSC invites applications for empanelment of Training Partners for HPKVN", "", "Empanelment"),
    ("निविदा सूचना", "", "Tender"),
])
def test_categories(kw, title, url, expected):
    assert classify(kw, title, "", url).category == expected


def test_relevance(kw):
    cls = classify(kw, "EOI for Empanelment of Assessment Agency 24 August 2026")
    assert cls.relevance == "High"


def test_content_category(kw):
    text = "This Expression of Interest invites ... Earnest Money Deposit ... last date for submission of proposal"
    assert content_category(kw, text) == "EOI"


def test_vague(kw):
    assert is_vague(kw, "Click here")
    assert is_vague(kw, "Download")
    assert not is_vague(kw, "EOI for training partners")


HTML = """
<html><head><title>Test SSC</title>
<script>var data = {"title":"EOI for Mobilisation Partners","file":"\\/uploads\\/eoi-mob.pdf"};</script></head>
<body>
<nav><a href="/tenders">Tenders</a></nav>
<div class="news-ticker"><ul>
  <li>HSSC is seeking Training partner for execution of Short-Term Training target in Andhra Pradesh</li>
  <li><a href="/files/rfp1.pdf">RFP for Skill Gap Study</a></li>
</ul></div>
<table><tr><td>12-09-2026</td><td>Request for Proposal for Onboarding of AAs</td>
<td><a href="/uploads/common/1728366830_c69.pdf">Download</a></td></tr></table>
<footer>Visitors: 123456</footer>
</body></html>
"""


def test_parse_html():
    page = parse_html(HTML, "https://ssc.example.org/")
    urls = {link.url: link for link in page.links}
    assert "https://ssc.example.org/files/rfp1.pdf" in urls
    assert urls["https://ssc.example.org/files/rfp1.pdf"].in_ticker
    row = urls["https://ssc.example.org/uploads/common/1728366830_c69.pdf"]
    assert "Onboarding of AAs" in row.context and "12-09-2026" in row.context
    script = [link for link in page.links if link.kind == "script"]
    assert script and script[0].url == "https://ssc.example.org/uploads/eoi-mob.pdf"
    assert script[0].text == "EOI for Mobilisation Partners"
    assert any("seeking Training partner" in t.text for t in page.ticker_texts)
    assert not any("Visitors" in line for line in page.text_lines)


def test_glued_documents_keep_second_body():
    html = ("<html><head><title>A</title></head><body><a href='/x'>Menu link</a></body></html>"
            "<html><head><style>.t{}</style></head><body><table><tr><td>1</td><td>RFQ for Kits</td>"
            "<td><a href='documents/tender/RFQ 1.pdf'>Download</a></td></tr></table></body></html>")
    page = parse_html(html, "https://www.sportsskills.in/tender_notice.php")
    assert any(link.url.endswith("/documents/tender/RFQ%201.pdf") for link in page.links)


def test_title_cleanup_from_table_row(kw):
    from datetime import date
    from tender_tracker.config import Site, load_settings
    from tender_tracker.discover.crawler import SiteScanner

    site = Site(code="T", name="T", url="https://t.example.org/")
    scanner = SiteScanner(site, load_settings(), kw, set(), today=date(2026, 9, 30))
    ctx = "1 Request for Quotation (RFQ) For Procurement of Kits 10-08-2026 15-08-2026 Download"
    assert scanner._title("Download", ctx, "https://t.example.org/a.pdf") == \
        "Request for Quotation (RFQ) For Procurement of Kits"
    ctx2 = "3 03/08/2026 Request for Proposal (RFP)- Manufacturing Cluster Skill Gap Study View"
    assert scanner._title("View", ctx2, None) == "Request for Proposal (RFP)- Manufacturing Cluster Skill Gap Study"


@pytest.mark.parametrize("text,vague", [
    ("Click here to view EoI document.", True), ("View / Download PDF", True), ("Click here to apply", True),
    ("EOI", True), ("PUBLIC NOTICE - Cyber Security Alert", False), ("EOI Empanelment Solar PV Installation Helper", False),
])
def test_vague_titles(kw, text, vague):
    assert is_vague(kw, text) is vague


def test_serial_number_removed_but_not_day(kw):
    from datetime import date
    from tender_tracker.config import Site, load_settings
    from tender_tracker.discover.crawler import SiteScanner

    scanner = SiteScanner(Site(code="T", name="T", url="https://t.example.org/"), load_settings(), kw, set(),
                          today=date(2026, 9, 30))
    assert scanner._title("1 Request for Proposal for On-boarding of AAs", "", None).startswith("Request for")
    assert scanner._title("25 Sep 2026 Training Provider EOI", "", None).startswith("25 Sep 2026")


def test_ocr_reads_scanned_pdf(tmp_path, monkeypatch):
    """A PDF with only an image of text is read through Tesseract OCR."""
    import pymupdf
    from tender_tracker import documents

    if not documents.TESSDATA.exists():
        pytest.skip("OCR language data not installed")
    monkeypatch.setattr(documents, "OCR_CACHE", tmp_path / "ocr_cache")
    src = pymupdf.open()
    page = src.new_page()
    page.insert_text((72, 100), "EXPRESSION OF INTEREST for Empanelment of Training Partners", fontsize=16)
    pix = page.get_pixmap(dpi=200)
    scanned = pymupdf.open()
    img_page = scanned.new_page(width=page.rect.width, height=page.rect.height)
    img_page.insert_image(img_page.rect, pixmap=pix)
    path = tmp_path / "scan.pdf"
    scanned.save(path)
    documents.OCR.update({"enabled": True, "max_pages": 2, "language": "eng"})
    info = documents.extract_text(path, "pdf")
    assert info.scanned and info.ocr
    assert "EXPRESSION OF INTEREST" in info.text.upper()
