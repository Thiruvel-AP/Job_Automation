"""Review's `e` on a company letter: the letter text opens in $EDITOR (default nano); on save the same guards run. A letter that passes is used; one
that fails shows its reasons and is used only if `use anyway` is typed in full, else the person edits again (or discards the edit). No PDF is
rendered here (render is faked), no editor is started, no model is asked."""
import json
from dataclasses import asdict
from pathlib import Path

import pytest

from careerops.review import letter_edit
from careerops.tailoring import company_letter as CL
from careerops.tailoring import tailor
from test_company_letter import JD, JOB, TEAM, body, world  # noqa: F401  (world is a fixture)


@pytest.fixture
def rig(world, settings, db, tmp_path, monkeypatch):
    """A tailored company-letter job: its report, the job row, a fake renderer, and an editor that writes what the test says."""
    from careerops.tailoring import render

    out = tmp_path / "out"
    out.mkdir()
    paragraphs = [world.plan.p1, *body(world, 400), world.plan.p5]
    rep = {"job_id": JOB["id"], "keywords": [asdict(k) for k in world.kws], "hook": world.hk.to_dict(), "family_line": "",
           "claimable": world.claimable, "team": TEAM, "out_dir": str(out), "cv_pdf": str(out / "cv.pdf"), "letter_pdf": str(out / "Ada Cover Letter.pdf"),
           "context": {"experience": [{"bullet_ids": sorted(world.chosen)}], "projects": []},
           "letter": {"mode": "company", "status": "ok", "paragraphs": paragraphs, "problems": [], "words": CL.words(paragraphs), "notes": [], "retry": {},
                      "evidence": [], "hook_source": "library"}}
    world.settings.raw["review"]["judge"] = "off"
    db.upsert_job({k: JOB[k] for k in ("id", "source", "company", "title", "location", "url", "description")})
    db.update_job(JOB["id"], status="needs_review", out_dir=str(out), letter_score=0.5)
    made = []

    def fake_render(data, folder, name):
        made.append((list(data["paragraphs"]), name))
        p = Path(folder) / f"{name}.pdf"
        p.write_text("\n\n".join(data["paragraphs"]), encoding="utf-8")
        return p

    monkeypatch.setattr(render, "render_letter", fake_render)
    monkeypatch.setattr(render, "page_count", lambda p: 1)
    monkeypatch.setattr(render, "pdf_text", lambda p: Path(p).read_text(encoding="utf-8"))
    monkeypatch.setattr(tailor.note_mod, "write", lambda *a, **k: None)
    monkeypatch.setattr(tailor, "pdf_text", lambda p: Path(p).read_text(encoding="utf-8") if Path(p).exists() else "")

    class Rig:
        pass
    r = Rig()
    r.world, r.rep, r.db, r.out, r.made, r.opened = world, rep, db, out, made, []
    r.job = db.job(JOB["id"])

    def editor_writes(*texts):
        """An editor that replaces the file's text by the next of `texts` (a list of paragraphs, or a string) each time it is opened."""
        queue = list(texts)

        def edit(path):
            r.opened.append(Path(path).read_text(encoding="utf-8"))
            t = queue.pop(0) if len(queue) > 1 else queue[0]
            Path(path).write_text(t if isinstance(t, str) else "\n\n".join(t), encoding="utf-8")
        return edit
    r.editor_writes = editor_writes

    def scripted(*answers):
        it = iter(answers)
        prompts = []

        def ask(prompt=""):
            prompts.append(prompt)
            return next(it)
        ask.prompts = prompts
        return ask
    r.scripted = scripted
    return r


def run(rig, edit, ask):
    lines: list[str] = []
    rep = letter_edit.edit_letter(rig.db, rig.job, rig.rep, rig.world.fb, rig.world.settings, ask, edit=edit, out=lines.append)
    return rep, lines


# ------------------------------------------------------------------ the editor
def test_the_editor_is_nano_unless_editor_says_otherwise(monkeypatch):
    monkeypatch.delenv("EDITOR", raising=False)
    assert letter_edit.editor_command() == ["nano"]
    monkeypatch.setenv("EDITOR", "code -w")
    assert letter_edit.editor_command() == ["code", "-w"]


def test_the_editor_gets_the_five_paragraphs_separated_by_blank_lines(rig):
    rig_edit = rig.editor_writes(rig.rep["letter"]["paragraphs"])
    run(rig, rig_edit, rig.scripted())
    assert rig.opened[0].split("\n\n") == rig.rep["letter"]["paragraphs"]


# ------------------------------------------------------------------ a letter that passes the guards is used
def test_an_edit_that_passes_every_guard_is_used_without_a_question(rig):
    edited = [*rig.rep["letter"]["paragraphs"]]
    edited[2] = edited[2] + " Those were the parts I enjoyed most."
    ask = rig.scripted()
    rep, lines = run(rig, rig.editor_writes(edited), ask)
    assert rep["letter"]["paragraphs"] == edited and rep["letter"]["status"] == "ok" and rep["letter"]["edited"] is True
    assert rep["letter"]["overridden"] == [] and ask.prompts == []
    assert rig.made[-1][0] == edited and rep["letter_pdf"].endswith("Ada Cover Letter.pdf")
    assert json.loads((rig.out / "report.json").read_text(encoding="utf-8"))["letter"]["paragraphs"] == edited
    assert any("passed every guard" in l for l in lines)
    assert rig.db.job(JOB["id"])["letter_score"] is not None and rig.db.job(JOB["id"])["status"] == "needs_review"


def test_the_edit_is_logged(rig):
    edited = [*rig.rep["letter"]["paragraphs"]]
    edited[3] += " I would be glad to explain it."
    run(rig, rig.editor_writes(edited), rig.scripted())
    events = [json.loads(r["detail"]) for r in rig.db.conn.execute("SELECT detail FROM events WHERE job_id=? AND stage='review'", (JOB["id"],))
              if r["detail"].startswith("{")]
    assert {"letter_edited": True, "overridden": 0} in events


# ------------------------------------------------------------------ a letter that fails: reasons, then the full phrase or another edit
def bad_text(rig) -> list[str]:
    edited = [*rig.rep["letter"]["paragraphs"]]
    edited[2] = edited[2] + " The result was robust for the team."
    return edited


def test_a_failing_edit_shows_its_reasons_and_use_anyway_typed_in_full_installs_it(rig):
    ask, bad = rig.scripted("use anyway"), bad_text(rig)
    rep, lines = run(rig, rig.editor_writes(bad), ask)
    assert rep["letter"]["paragraphs"] == bad and rep["letter"]["edited"] is True
    assert rep["letter"]["overridden"] and "vague filler 'robust' is not in the evidence" in rep["letter"]["overridden"]
    assert any("vague filler 'robust' is not in the evidence" in l for l in lines)
    assert "use anyway" in ask.prompts[0] and len(rig.opened) == 1


@pytest.mark.parametrize("typed", ["use", "y", "yes", "anyway", "use any way", "useanyway", "ok"])
def test_anything_short_of_the_full_phrase_edits_again_and_never_installs(rig, typed):
    ask = rig.scripted(typed, "discard")
    rep, _ = run(rig, rig.editor_writes(bad_text(rig)), ask)
    assert len(rig.opened) == 2 and rep["letter"]["paragraphs"] == rig.rep["letter"]["paragraphs"] and not rep["letter"].get("edited")
    assert rig.made == []


def test_enter_edits_again_and_a_clean_second_edit_is_used(rig):
    good = [*rig.rep["letter"]["paragraphs"]]
    good[2] += " Those were the parts I enjoyed most."
    ask, bad = rig.scripted(""), bad_text(rig)
    rep, _ = run(rig, rig.editor_writes(bad, good), ask)
    assert len(rig.opened) == 2 and rep["letter"]["paragraphs"] == good and rep["letter"]["overridden"] == []
    assert rig.opened[1] == "\n\n".join(bad)                      # the second session starts from the first edit, not from scratch


def test_the_phrase_is_not_asked_for_a_letter_that_passes(rig):
    ask = rig.scripted()
    run(rig, rig.editor_writes(rig.rep["letter"]["paragraphs"]), ask)
    assert ask.prompts == []


def test_a_letter_with_the_wrong_number_of_paragraphs_is_refused_until_it_is_fixed(rig):
    ask = rig.scripted("", "discard")
    rep, lines = run(rig, rig.editor_writes("just one paragraph"), ask)
    assert any("expected 5 paragraphs" in l for l in lines) and rep["letter"]["paragraphs"] == rig.rep["letter"]["paragraphs"]
    ask2 = rig.scripted("use anyway", "discard")
    rep2, _ = run(rig, rig.editor_writes("just one paragraph"), ask2)
    assert rep2["letter"]["paragraphs"] == rig.rep["letter"]["paragraphs"] and not rep2["letter"].get("edited")    # 'use anyway' cannot install a letter that is not five paragraphs


# ------------------------------------------------------------------ a job with no letter
def test_a_job_with_no_letter_starts_from_the_last_rejected_draft_and_its_opening_and_closing(rig):
    draft = body(rig.world, 400, p2="Anaplan sells boats.")
    rig.rep["letter"] = {"mode": "company", "status": "none", "paragraphs": [], "problems": ["x"], "opening": rig.world.plan.p1,
                         "closing": rig.world.plan.p5, "draft_body": draft, "notes": [], "retry": {}}
    rig.rep["letter_pdf"] = None
    run(rig, rig.editor_writes([rig.world.plan.p1, *draft, rig.world.plan.p5]), rig.scripted("discard"))
    assert rig.opened[0].split("\n\n") == [rig.world.plan.p1, *draft, rig.world.plan.p5]


def test_a_job_with_no_letter_and_no_draft_starts_from_its_opening_and_closing(rig):
    rig.rep["letter"] = {"mode": "company", "status": "none", "paragraphs": [], "problems": [], "opening": "OPEN.", "closing": "CLOSE?", "notes": [], "retry": {}}
    rig.rep["letter_pdf"] = None
    run(rig, rig.editor_writes("OPEN.\n\n\n\n\n\nCLOSE?"), rig.scripted("discard"))
    assert rig.opened[0].startswith("OPEN.") and rig.opened[0].rstrip().endswith("CLOSE?")


def test_discard_leaves_the_report_and_the_files_alone(rig):
    before = json.dumps(rig.rep, sort_keys=True, default=str)
    rep, _ = run(rig, rig.editor_writes(bad_text(rig)), rig.scripted("discard"))
    assert json.dumps(rep, sort_keys=True, default=str) == before and not (rig.out / "report.json").exists() and rig.made == []
