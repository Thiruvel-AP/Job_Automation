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
    assert n == 1 and ws["A2"].value == "Northwind Health AI" and ws["D2"].value == "approved"
