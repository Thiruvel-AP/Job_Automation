"""Review decisions, preference log, Excel export."""
import json

from openpyxl import load_workbook

from careerops.reporting.excel import export
from careerops.review import workflow as review


def test_review_edit_approve_reject_and_export(tailored, settings, db, job, tmp_path):
    rep, fb, _ = tailored
    prefs = settings.path("preferences")
    row = db.job(job["id"])
    edits = review.edit_file_for(rep)
    bid = next(iter(edits["bullets"]))
    edits["bullets"][bid] = edits["bullets"][bid] + " Reviewed by hand."
    warns = review.apply_edits(db, row, rep, edits, fb, prefs)
    assert warns == []
    review.approve(db, db.job(job["id"]), rep, prefs)
    assert db.job(job["id"])["status"] == "approved"
    rows = [json.loads(l) for l in prefs.read_text().splitlines()]
    pair = next(r for r in rows if r["kind"] == "edit_pair")
    assert pair["chosen"].endswith("Reviewed by hand.") and pair["rejected"] != pair["chosen"]
    assert any(r["kind"] == "approve" for r in rows)

    n = export(db, settings.path("excel"))
    ws = load_workbook(settings.path("excel"))["Jobs"]
    status_col = [c.value for c in ws[1]].index("Status") + 1
    assert n == 1 and ws["A2"].value == "Northwind Health AI" and ws.cell(2, status_col).value == "approved"


def test_review_header_shows_tier_and_family(tailored, db, job):
    rep, _, _ = tailored
    assert "T1 · AI Engineer" in review.summary(db.job(job["id"]), rep).splitlines()[2]   # right under "Company — Title"


def test_excel_has_tier_columns_sorted_by_post_tailor_key(settings, db, job, tmp_path):
    for jid, tier, pre, ceil in (("x:low", 1, 0.87, 0.65), ("x:t3", 3, 0.80, None), ("x:hi", 1, 0.70, 0.75)):
        db.upsert_job(dict(job, id=jid))
        db.update_job(jid, status="needs_review", family="ai_engineer" if tier == 1 else "data_analyst",
                      tier=tier, ceiling_pre=pre, ceiling=ceil)
    out = tmp_path / "jobs.xlsx"
    export(db, out, floor=settings["roles"]["floor"])
    ws = load_workbook(out)["Jobs"]
    head = [c.value for c in ws[1]]
    assert {"Tier", "Family", "Ceiling (pre)"} <= set(head)
    tiers = [ws.cell(r, head.index("Tier") + 1).value for r in range(2, 5)]
    fams = [ws.cell(r, head.index("Family") + 1).value for r in range(2, 5)]
    pre = [ws.cell(r, head.index("Ceiling (pre)") + 1).value for r in range(2, 5)]
    assert list(zip(tiers, fams, pre)) == [(1, "ai_engineer", 0.70), (3, "data_analyst", 0.80), (1, "ai_engineer", 0.87)]
