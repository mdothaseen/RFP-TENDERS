"""State e-procurement portals (NIC GePNIC): only the chosen departments' tender tables are read."""

import re
from datetime import date

from tender_tracker.discover import nic_portal

ORGS = """<table>
<tr><td>1</td><td>Agriculture Marketing Board,Pune(Maharashtra)</td><td>3</td></tr>
<tr><td>2</td><td><a href="/nicgep/app?component=%24DirectLink&amp;sp=SKILL">Skill Development and Entrepreneurship Department</a></td><td>1</td></tr>
<tr><td>3</td><td><a href="/nicgep/app?component=%24DirectLink&amp;sp=PWD">Public Works Department</a></td><td>90</td></tr>
<tr><td>4</td><td><a href="/nicgep/app?component=%24DirectLink&amp;sp=LAB">Chemical Examiners Laboratory</a></td><td>2</td></tr>
</table>"""

TENDERS = """<table><tr><td>S.No</td><td>e-Published Date</td></tr>
<tr><td>1</td><td>21-Nov-2026 04:30 PM</td><td>04-Dec-2026 03:00 PM</td><td>07-Dec-2026 11:00 AM</td><td></td>
<td><a href="/x">[Selection of Assessment Agency for PMKVY]</a> [SDED/RFP/2026/12][2026_SDED_1357824_1]</td>
<td>Skill Development and Entrepreneurship Department||Mantralaya</td></tr>
<tr><td>2</td><td>20-Nov-2026 06:55 PM</td><td>30-Nov-2026 05:00 PM</td><td>01-Dec-2026 05:00 PM</td><td></td>
<td>[Housekeeping Services][2026_SDED_1357512_1]</td><td>Skill Development</td></tr>
</table><a id="linkFwd" href="/nicgep/app?component=next&amp;sp=P2">&gt;</a>"""

PATTERNS = [re.compile(p, re.I) for p in ("skill|kaushal", r"\blabou?r\b")]


def test_only_matching_departments_are_opened():
    found = nic_portal.parse_org_list(ORGS, PATTERNS)
    assert [name for name, _ in found] == ["Skill Development and Entrepreneurship Department"]
    assert found[0][1] == "/nicgep/app?component=%24DirectLink&sp=SKILL"


def test_tender_rows_give_title_reference_dates_and_id():
    rows = nic_portal.parse_tenders(TENDERS)
    assert rows[0] == {"id": "2026_SDED_1357824_1", "title": "Selection of Assessment Agency for PMKVY",
                       "ref": "SDED/RFP/2026/12", "published": date(2026, 11, 21), "closing": date(2026, 12, 4),
                       "opening": "07-Dec-2026 11:00 AM", "view": ""}
    assert rows[1]["title"] == "Housekeeping Services" and rows[1]["ref"] == ""
    assert nic_portal.next_page(TENDERS) == "/nicgep/app?component=next&sp=P2"


def test_portal_tenders_are_not_downloaded():
    assert nic_portal.is_portal_tender("https://x.gov.in/nicgep/app?page=FrontEndTendersByOrganisation#nic-tender=1")
    assert not nic_portal.is_portal_tender("https://x.gov.in/tender.pdf")


def test_detail_page_text_is_kept_for_the_ai(tmp_path, monkeypatch):
    from tender_tracker import documents
    monkeypatch.setattr(nic_portal, "DETAIL_DIR", tmp_path / "nic")
    monkeypatch.setattr(documents, "TEXT_DIR", tmp_path / "text")
    page = ("<html><script>var x=1;</script><div>Menu</div><b>Basic Details</b> Tender ID 2026_SDED_1_1 "
            "EMD Amount in &#8377; 40,00,000 Work Description Assessment of trainees</html>")

    class F:
        def get(self, url, textual_only=False):
            return type("R", (), {"error": None, "status": 200, "text": page})()

    nic_portal._save_detail(F(), "https://x.gov.in/nicgep/app", {"id": "2026_SDED_1_1", "view": "/v"})
    saved = nic_portal.detail_file("2026_SDED_1_1").read_text(encoding="utf-8")
    assert saved.startswith("Basic Details") and "40,00,000" in saved and "var x" not in saved

    row = {"id": 7, "revision": 0, "doc_attempts": 0, "is_document": 0,
           "url": "https://x.gov.in/nicgep/app?page=FrontEndTendersByOrganisation&service=page#nic-tender=2026_SDED_1_1"}
    fields = documents.process_item(None, row, type("S", (), {"documents": {}, "ai": {}})(), None)
    assert fields["doc_status"] == "html" and fields["text_chars"] == len(saved)
