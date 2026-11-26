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
                       "opening": "07-Dec-2026 11:00 AM"}
    assert rows[1]["title"] == "Housekeeping Services" and rows[1]["ref"] == ""
    assert nic_portal.next_page(TENDERS) == "/nicgep/app?component=next&sp=P2"


def test_portal_tenders_are_not_downloaded():
    assert nic_portal.is_portal_tender("https://x.gov.in/nicgep/app?page=FrontEndTendersByOrganisation#nic-tender=1")
    assert not nic_portal.is_portal_tender("https://x.gov.in/tender.pdf")
