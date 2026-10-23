"""Store + append-only Excel on a temporary folder (no network)."""

import json
from datetime import date, datetime

from openpyxl import load_workbook

from tender_tracker import excel_report
from tender_tracker.classify import classify
from tender_tracker.config import Site, load_keywords, load_settings
from tender_tracker.discover.crawler import Found, SiteScan
from tender_tracker.notify import build_digest
from tender_tracker.store import Store

TODAY = date(2026, 9, 30)


def _found(key, title, via, linked, published=None, deadline=None):
    return Found(key=key, site_code="TST", url=f"https://ssc.example.org/{key}.pdf", title=title,
                 link_text=title, context=title, source_page="https://ssc.example.org/tenders",
                 via={via}, linked=linked, is_document=True, file_ext="pdf", published=published,
                 date_source="listing text" if published else "", deadline=deadline)


def _ids(ws):
    header = [c.value for c in ws[1]]
    col = header.index("ID")
    return [row[col] for row in ws.iter_rows(min_row=2, values_only=True)]


def test_store_and_append_only_excel(tmp_path, monkeypatch):
    kw, settings = load_keywords(), load_settings()
    monkeypatch.setattr(excel_report, "OUTPUT_DIR", tmp_path)
    monkeypatch.setattr(excel_report, "NEW_DIR", tmp_path / "New_Updates")
    monkeypatch.setattr(excel_report, "site_dir", lambda code: tmp_path / code)
    store = Store(tmp_path / "t.db")
    ai = set(settings.ai["categories"])
    site = Site(code="TST", name="Test SSC", url="https://ssc.example.org/")

    # ---- run 1 (baseline)
    run1 = store.start_run("full", True, False)
    now = datetime(2026, 9, 30, 9, 30).isoformat()
    eoi = _found("eoi1", "EOI for Empanelment of Assessment Agencies", "page", True, date(2026, 9, 20),
                 deadline=date(2026, 10, 3))
    hidden = _found("rfp1", "RFP for Skill Gap Study", "wp-media", False, date(2026, 9, 25))
    for f in (eoi, hidden):
        assert store.upsert(f, classify(kw, f.title), run1, True, now, ai)[1]
    again = _found("rfp1", "RFP for Skill Gap Study", "page", True)
    assert not store.upsert(again, classify(kw, again.title), run1, True, now, ai)[1]
    rows = {r["key"]: r for r in store.items()}
    assert rows["rfp1"]["hidden"] == 0 and rows["rfp1"]["published_date"] == "2026-09-25"
    assert rows["eoi1"]["ai_status"] == "pending" and rows["eoi1"]["relevance"] == "High"
    store.record_site_run(run1, SiteScan(site, finished=datetime.now()), 2)
    store.finish_run(run1, "done", 2)

    report = excel_report.write_reports(store, kw, settings, {"TST": site}, store.run(run1), TODAY)
    master = tmp_path / excel_report.MASTER_NAME
    site_book = tmp_path / "TST" / "TST_Updates.xlsx"
    assert master.exists() and site_book.exists() and report["daily"] is not None
    wb = load_workbook(master)
    assert wb.sheetnames == ["Dashboard", "Tenders-RFP-EOI", "All Updates", "Site Status"]
    tenders = wb["Tenders-RFP-EOI"]
    assert sorted(_ids(tenders)) == [1, 2]
    assert tenders["I2"].value.startswith("=IF(H2")
    # The user adds an own column and a remark.
    tenders.cell(row=1, column=40, value="Applied?")
    tenders.cell(row=2, column=40, value="Haan")
    wb.save(master)

    digest = build_digest(report, store.run(run1), ["XYZ: website nahi khuli"], None, TODAY, 7)
    assert "First run complete" in digest.subject and "FIRST RUN COMPLETE" in digest.html and all(len(m) < 4096 for m in digest.telegram)

    # ---- run 2: one new item, AI details for an existing one
    run2 = store.start_run("full", False, False)
    new = _found("eoi2", "EOI for Training Partners in Bihar", "page", True, date(2026, 9, 29))
    store.upsert(new, classify(kw, new.title), run2, False, now, ai)
    answer = {"doc_type": "EOI", "title": "EOI for Empanelment of Assessment Agencies",
              "last_date_submission": "2026-10-10", "emd": "Rs. 10,000", "relevance": "High"}
    store.update_item(1, ai_status="done", ai_json=json.dumps(answer))
    store.finish_run(run2, "done", 1)
    report2 = excel_report.write_reports(store, kw, settings, {"TST": site}, store.run(run2), TODAY)
    assert report2["master_new_rows"] == 1 and not report2["locked"]

    wb = load_workbook(master)
    tenders = wb["Tenders-RFP-EOI"]
    ids = _ids(tenders)
    assert sorted(ids) == [1, 2, 3] and len(ids) == 3           # appended, nothing duplicated
    header = [c.value for c in tenders[1]]
    row_of_1 = ids.index(1) + 2
    assert tenders.cell(row=row_of_1, column=header.index("EMD") + 1).value == "Rs. 10,000"   # updated in place
    assert tenders.cell(row=2, column=40).value == "Haan"      # user's own column untouched
    assert len(_ids(load_workbook(site_book)["All Updates"])) == 3

    # ---- run 3: nothing changed -> organisation file is not rewritten
    mtime = site_book.stat().st_mtime_ns
    excel_report.write_reports(store, kw, settings, {"TST": site}, store.run(run2), TODAY)
    assert site_book.stat().st_mtime_ns == mtime
    store.close()
