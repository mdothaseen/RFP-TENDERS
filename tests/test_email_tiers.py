"""Summary email grouped by site category (Focused / Regular / Low), with every website's status."""

from datetime import date

import pytest

from tender_tracker.alerts import failure_reason
from tender_tracker.config import _tier
from tender_tracker.notify.email_html import build_email

TODAY = date(2026, 11, 4)


def _rec(org, title, category="Tender", tender=True, relevance="High", last=None, page=False, **extra):
    return {"org": org, "org_name": org, "title": title, "category": category, "tender": tender,
            "relevance": relevance, "last_date": last, "url": f"https://{org.lower()}.example.org/{len(title)}",
            "source": "", "uploaded": None, "page_change": page, **extra}


def _site(code, tier, status="ok", reason=""):
    return {"code": code, "name": f"{code} Council", "url": f"https://{code.lower()}.example.org/", "tier": tier,
            "status": status, "reason": reason}


def _report(new, sites, closing=()):
    return {"new": new, "closing": list(closing), "site_status": sites, "master": "Master.xlsx", "daily": None,
            "locked": []}


def test_tier_values():
    assert _tier(None) == "Regular" and _tier("focused") == "Focused" and _tier("Low") == "Low"
    with pytest.raises(ValueError):
        _tier("Important")


def test_every_category_and_website_status_is_shown():
    sites = [_site("FOC", "Focused"), _site("QUIET", "Focused"), _site("BLOCK", "Focused", "failed",
             "website blocks automatic reading (403)"), _site("REG", "Regular"), _site("LOW", "Low")]
    new = [_rec("FOC", "RFP for assessment agency", last=date(2026, 11, 8), summary="Assessment of 5000 candidates",
                who="Assessment agencies", emd="Rs 50,000", action="Submit bid on GeM"),
           _rec("FOC", "Page updated: https://foc.example.org/tenders/?page=2", "Notice", False, "Low", page=True),
           _rec("REG", "Notice for training partners", "Notice", False, "Medium"),
           _rec("LOW", "Supply of furniture", relevance="Low"),
           _rec("LOW", "Old tender", last=date(2026, 10, 1))]
    subject, body = build_email(_report(new, sites, [new[0]]), {"baseline": 0}, [], None, TODAY, 7, len(sites))

    assert subject.startswith("[Action needed]") and "Focused: 1 new tender" in subject
    for label in ("FOCUSED ORGANISATIONS", "REGULAR ORGANISATIONS", "LOW ORGANISATIONS"):
        assert label in body
    assert body.index("FOCUSED ORGANISATIONS") < body.index("REGULAR ORGANISATIONS") < body.index("LOW ORGANISATIONS")
    # AI details of the document are visible
    assert "Assessment of 5000 candidates" in body and "EMD: Rs 50,000" in body and "Submit bid on GeM" in body
    # quiet and unreadable websites are listed with the reason
    assert "QUIET" in body and "website blocks automatic reading (403)" in body
    # page changes are one summary line, not a row of their own
    assert "1 page changed" in body and "Page updated:" not in body
    # a tender whose last date passed is left out of the lists
    assert "Old tender" not in body and "1 new item with a last date already passed" in body


def test_without_site_status_the_orgs_in_the_items_are_used():
    new = [_rec("ABC", "EOI for skill assessment")]
    report = _report(new, [])
    report.pop("site_status")
    subject, body = build_email(report, {"baseline": 0}, [], None, TODAY, 7, 1)
    assert "REGULAR ORGANISATIONS" in body and "EOI for skill assessment" in body


def test_failure_reason_words():
    assert failure_reason("failed", ["https://x/: HTTP 403 / challenge page"]) == "website blocks automatic reading (403)"
    assert failure_reason("failed", ["scan did not finish within 30 min (the website hung)"]) == "website hung (timeout)"
    assert failure_reason("failed", ["NameResolutionError(...)"]) == "website address not found (DNS)"
    assert failure_reason("partial", []) == "some pages could not be read"
