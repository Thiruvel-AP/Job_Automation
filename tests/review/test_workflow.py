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


# ---------------------------------------------------------------- A1: fallback letters need a deliberate approval
def _fallback_job(settings, db, job):
    from careerops.config import load_yaml
    from careerops.llm import MockLLM
    from careerops.tailoring.tailor import tailor_job

    def boom(ctx):
        raise RuntimeError("provider quota exhausted")
    fb = load_yaml(settings.path("fact_bank"))
    rep = tailor_job(job, fb, MockLLM({"cover_letter": boom}), settings, settings.path("outputs"))
    db.upsert_job(job)
    db.update_job(job["id"], status="needs_review", out_dir=rep["out_dir"], tier=1, family="ai_engineer")
    return db.job(job["id"]), rep, fb


def test_review_refuses_plain_approve_of_a_fallback_letter(settings, db, job):
    import pytest
    row, rep, _ = _fallback_job(settings, db, job)
    prefs = settings.path("preferences")
    assert review.letter_status(rep) == "fallback"
    assert "LETTER IS A FALLBACK — regenerate (l) before approving" in review.summary(row, rep)
    for confirm in (None, "", "yes", "SEND FALLBACK please"):
        with pytest.raises(review.FallbackLetterError):
            review.approve(db, row, rep, prefs, confirm=confirm)
    assert db.job(job["id"])["status"] == "needs_review" and not prefs.exists()


def test_typing_send_fallback_approves_and_is_logged_as_an_event(settings, db, job):
    row, rep, _ = _fallback_job(settings, db, job)
    review.approve(db, row, rep, settings.path("preferences"), confirm="send fallback")
    assert db.job(job["id"])["status"] == "approved"
    ev = [r["detail"] for r in db.conn.execute("SELECT detail FROM events WHERE job_id=? AND stage='review'", (job["id"],))]
    assert any("fallback" in d for d in ev)


def test_regenerating_the_letter_clears_the_banner_and_allows_a_plain_approve(settings, db, job):
    from careerops.llm import MockLLM
    row, rep, fb = _fallback_job(settings, db, job)
    rep = review.regenerate_letter(db, row, rep, fb, MockLLM(), settings)
    assert review.letter_status(rep) == "ok" and "LETTER IS A FALLBACK" not in review.summary(db.job(job["id"]), rep)
    review.approve(db, db.job(job["id"]), rep, settings.path("preferences"))
    assert db.job(job["id"])["status"] == "approved"
    assert db.job(job["id"])["letter_score"] is not None


def test_old_reports_without_a_letter_status_are_judged_by_their_problems():
    assert review.letter_status({"letter": {"problems": []}}) == "ok"
    assert review.letter_status({"letter": {"problems": ["x", "used fallback letter"]}}) == "fallback"


def test_interactive_review_asks_for_send_fallback_and_l_regenerates(settings, db, job, monkeypatch, capsys):
    from careerops.llm import MockLLM
    row, rep, fb = _fallback_job(settings, db, job)
    answers = iter(["a", "nope", "l", "a"])                       # approve? refused -> regenerate -> approve
    prompts = []
    monkeypatch.setattr("builtins.input", lambda p="": prompts.append(p) or next(answers))
    review.run(db, settings, fb, make_llm=lambda: MockLLM())
    assert db.job(job["id"])["status"] == "approved"
    assert any("send fallback" in p for p in prompts)
