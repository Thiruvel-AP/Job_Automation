"""v2.4.1 in the gated auto-apply (autoapply.run) on the RightShip-shaped form: a required field that is EMPTY, LOST or NEEDS USER blocks the submit before any
submit control is reached; a cover letter is written when the form asks for one and none exists; its guard failure prints the exact reason (optional: left
empty, required: hand-off). Chromium on a local page, no network, nothing submitted anywhere."""
import pytest
from test_autoapply import APPLICANT, chromium, world  # noqa: F401  (the fixtures and the runner of the auto-apply tests)
from test_greenhouse_form import PAGE

from careerops.apply import autolog
from careerops.apply.letter_demand import Made

pytestmark = pytest.mark.slow          # these tests drive Chromium
REASON = "3 draft(s) rejected by the company guards; last: P4: number(s) about you not in the chosen bullets: ['20%']"


@pytest.fixture
def form(tmp_path):
    p = tmp_path / "greenhouse_eu.html"
    p.write_text(PAGE, encoding="utf-8")
    return p


def without_rights(form):
    """The form without the working-rights drop-down (answers.yaml has no entry for it, which asks the person in auto mode)."""
    import re
    form.write_text(re.sub(r"<div><label for=\"rights\">.*?</div>", "", form.read_text(encoding="utf-8"), flags=re.S), encoding="utf-8")
    return form


def test_a_needs_user_field_blocks_the_submit_before_a_submit_control_is_reached(world, form):
    r = world.run(without_rights(form), {"Submit?": "y"})
    assert r.status == autolog.NEEDS_USER and world.submit_calls == 0
    assert "specify the type of" in r.reason and "`visa_type` entry" in r.reason            # strict: the first thing only the person can answer stops the run
    assert any("BLOCKS AUTO-SUBMIT" in line for line in world.out)
    assert world.state["values"]["visa_type"] == ""                                  # and the Graduate-visa paragraph was not put there instead


def test_the_table_is_printed_before_the_gate(world, form):
    world.run(without_rights(form), submit=False)
    text = "\n".join(world.out)
    assert "REQUIRED FIELDS" in text and text.index("REQUIRED FIELDS") < text.index("STOPPED")


def test_a_question_nobody_answered_stops_the_run_and_the_table_still_shows(world, form):
    r = world.run(form, submit=False)                                    # the visa-type box has no answers.yaml entry; the working-rights one is applicant.yaml's
    assert r.status == autolog.NEEDS_USER and "specify the type of" in r.reason and "working rights" not in r.reason and world.submit_calls == 0
    assert any("working rights" in line and ("VERIFIED" in line or "FILLED" in line) for line in world.out), world.out          # picked from applicant.yaml
    assert any("REQUIRED FIELDS" in line for line in world.out)


def test_a_letter_that_could_not_be_written_prints_its_exact_reason_and_the_run_goes_on(world, form):
    r = world.run(form, submit=False, rep=world.rep(), letter_maker=lambda ctl: Made(None, None, REASON))
    assert any(REASON in line for line in world.out)
    assert not any(line.strip().lower() in ("no letter", "letter: no letter") for line in world.out)
    assert world.state["values"]["cover_letter"] == ""


def test_a_letter_written_now_is_uploaded_through_the_file_input(world, form, tmp_path):
    out = tmp_path / "out"
    pdf = out / "Ada Lovelace Cover Letter.pdf"
    pdf.write_bytes(b"%PDF-1.4\n%%EOF")
    made = Made(str(pdf), "Hi team,\n\nBody.\n\nRegards,\n\nAda", "", {"letter": {"paragraphs": ["x"]}})
    world.run(form, submit=False, rep=world.rep(), letter_maker=lambda ctl: made)
    assert world.state["values"]["cover_letter"] == "Ada Lovelace Cover Letter.pdf"
    assert any("written now, before any browser opens" in line for line in world.out)


def test_a_required_letter_that_fails_its_guards_is_handed_off_with_the_reason(world, form):
    required = form.read_text(encoding="utf-8").replace("<h4>Cover Letter</h4>", "<h4>Cover Letter*</h4>").replace(
        '<input type="file" id="cover_letter" name="cover_letter" style="display:none">',
        '<input type="file" id="cover_letter" name="cover_letter" style="display:none" required aria-required="true">')
    form.write_text(required, encoding="utf-8")
    h = world.handoff()
    r = world.run(form, rep=world.rep(), letter_maker=lambda ctl: Made(None, None, REASON), handoff=h)
    assert r.status == autolog.NEEDS_USER and r.handed_off and REASON in r.reason
    assert world.submit_calls == 0
