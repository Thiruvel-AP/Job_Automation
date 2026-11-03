"""v2.5 plan first: the Greenhouse question schema (apply/gh_schema.py) and the answer plan (apply/answerplan.py), made with no browser.
A saved RightShip-like schema with react-select options; MockLLM for the motivation draft; no network (the fetch is a stub)."""
import json
from pathlib import Path

import pytest
from conftest import PROFILE_FIX
from test_freetext import AD_PHRASE, BOOK, FB, REP, WHY, llm_with
from test_greenhouse_form import APPLICANT, JOB_ROW, book, plan_for

from careerops.apply import answerplan as AP
from careerops.apply import answers as answers_mod
from careerops.apply import freetext as ft
from careerops.apply import gh_schema
from careerops.apply import planfirst

SCHEMA = json.loads((Path(__file__).parent.parent / "fixtures" / "apply" / "greenhouse_schema_rightship.json").read_text(encoding="utf-8"))


# ------------------------------------------------------------------ the schema
@pytest.mark.parametrize("url,token,job_id,eu", [
    ("https://job-boards.eu.greenhouse.io/rightship/jobs/4982444101", "rightship", "4982444101", True),
    ("https://job-boards.greenhouse.io/anthropic/jobs/5423029008?gh_src=x", "anthropic", "5423029008", False),
    ("https://boards.greenhouse.io/embed/job_app?for=acme&token=123", "acme", "123", False),
])
def test_a_greenhouse_url_gives_its_board_and_the_one_api_for_both_regions(url, token, job_id, eu):
    b = gh_schema.parse(url)
    assert (b.token, b.job_id, b.eu) == (token, job_id, eu)
    assert b.api_url == f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs/{job_id}?questions=true"      # there is no boards-api.eu host


def test_other_urls_are_not_greenhouse_boards():
    assert gh_schema.parse("https://jobs.lever.co/acme/1") is None and gh_schema.parse("https://boards-api.greenhouse.io/v1/boards/a/jobs/1") is None


def test_the_schema_is_fetched_once_cached_and_read_back(tmp_path):
    board, calls = gh_schema.parse(JOB_ROW["url"]), []
    got = gh_schema.load(tmp_path, board, lambda u: calls.append(u) or SCHEMA, out=lambda s: None)
    assert calls == [board.api_url] and (tmp_path / gh_schema.CACHE).is_file()
    again = gh_schema.load(tmp_path, board, lambda u: pytest.fail("the cache is read, nothing is fetched"), out=lambda s: None)
    assert [f.name for f in again] == [f.name for f in got]
    rights = next(f for f in got if f.name == "question_7001")
    assert rights.type == "select" and rights.options == ["Citizen", "Permanent Resident", "Work Visa", "Require sponsorship"]


def test_a_refused_or_failed_fetch_falls_back_to_the_page_scan(tmp_path):
    from careerops.discovery.http import Blocked

    out = []

    def refused(url):
        raise Blocked("robots.txt of boards-api.greenhouse.io disallows /v1/")
    assert gh_schema.load(tmp_path, gh_schema.parse(JOB_ROW["url"]), refused, out=out.append) is None
    assert any("planned from a page scan" in line for line in out) and not (tmp_path / gh_schema.CACHE).exists()


# ------------------------------------------------------------------ the plan
def sources(settings, visa_type="", letter=True, llm=None, tmp_path=None):
    free = ft.for_job(JOB_ROW, REP, FB, llm or llm_with(WHY, ad_phrases=[AD_PHRASE]), settings, book(visa_type), ask=lambda p: "", out=lambda s: None)
    lp = None
    if letter and tmp_path is not None:
        lp = tmp_path / "Ada Cover Letter.pdf"
        lp.write_bytes(b"%PDF-1.4\n%%EOF")
    return AP.Sources(APPLICANT, book(visa_type), JOB_ROW, plan_for(), "/x/Ada CV.pdf", str(lp) if lp else None, "Hi team,\n\nBody." if lp else None,
                      "" if lp else "3 drafts rejected", free)


def schema_plan(settings, tmp_path, **kw):
    qs = AP.questions_from_schema(gh_schema.fields_of(SCHEMA))
    return AP.build(qs, sources(settings, tmp_path=tmp_path, **kw), "schema", JOB_ROW["url"], out=lambda s: None)


def row(plan, label_start):
    return next(r for r in plan.rows if r.q.label.startswith(label_start))


def test_working_rights_are_work_visa_from_applicant_yaml(settings, tmp_path):
    r = row(schema_plan(settings, tmp_path), "What are your working rights")
    assert (r.answer, r.status) == ("Work Visa", AP.READY) and r.source == "applicant:visa.by_country.GB"


def test_how_heard_is_the_company_website(settings, tmp_path):
    r = row(schema_plan(settings, tmp_path), "How did you hear")
    assert (r.answer, r.status) == ("Company website", AP.READY)


def test_the_city_is_bristol_typed_then_picked_from_the_live_list(settings, tmp_path):
    r = row(schema_plan(settings, tmp_path), "Location (City)")
    assert (r.answer, r.typed, r.pick, r.status) == ("Bristol", "Bristol", "city", AP.READY) and r.source == "answers:current_location"


def test_the_standard_fields_come_from_applicant_yaml(settings, tmp_path):
    p = schema_plan(settings, tmp_path)
    assert row(p, "First Name").answer == APPLICANT["first_name"] and row(p, "Email").source == "applicant"


def test_the_attestation_is_never_answered(settings, tmp_path):
    r = row(schema_plan(settings, tmp_path), "I declare")
    assert r.status == AP.NEEDS_USER and r.source == "attestation" and not r.answer


def test_the_visa_type_needs_you_until_answers_yaml_has_it(settings, tmp_path):
    assert row(schema_plan(settings, tmp_path), "If you answered 'Work Visa'").status == AP.NEEDS_USER
    r = row(schema_plan(settings, tmp_path, visa_type="Graduate visa"), "If you answered 'Work Visa'")
    assert (r.answer, r.status, r.source) == ("Graduate visa", AP.READY, "answers:visa_type")


def test_the_motivation_answer_is_drafted_before_any_browser_and_waits_for_approval(settings, tmp_path):
    r = row(schema_plan(settings, tmp_path), "What motivated you")
    assert r.status == AP.DRAFT and r.answer == WHY and r.shown.startswith("(draft, ")


def test_the_files_go_to_their_own_uploads_and_the_paste_box_of_an_upload_is_not_a_question(settings, tmp_path):
    p = schema_plan(settings, tmp_path)
    assert row(p, "Resume/CV").answer == "Ada CV.pdf" and row(p, "Cover Letter").answer == "Ada Cover Letter.pdf"
    assert [r.q.type for r in p.rows if r.q.label in ("Resume/CV", "Cover Letter")] == ["file", "file"]
    assert not any(r.q.key == "hidden_x" for r in p.rows)


def test_without_a_letter_the_cover_letter_upload_says_why(settings, tmp_path):
    r = row(schema_plan(settings, tmp_path, letter=False), "Cover Letter")
    assert r.status == AP.BLANK and "3 drafts rejected" in r.why                  # optional: left empty, with the reason


def test_an_answer_no_option_matches_needs_you_with_the_options_listed(settings, tmp_path):
    q = AP.Question("question_9", "combobox", "How did you hear about us?", True, ["Billboard", "Radio"])
    r = AP.build([q], sources(settings, tmp_path=tmp_path), "schema", out=lambda s: None).rows[0]
    assert r.status == AP.NEEDS_USER and "Billboard" in r.why


def test_the_table_and_answers_to_paste_are_written_from_the_plan(settings, tmp_path):
    p = schema_plan(settings, tmp_path)
    lines = p.table()
    assert lines[0].split()[:3] == ["#", "req", "type"] and any("Work Visa" in line and "READY" in line for line in lines)
    path = AP.write_paste(p, tmp_path / "out", JOB_ROW, "/x/Ada CV.pdf", None)
    text = path.read_text(encoding="utf-8")
    assert path.name == "answers_to_paste.txt" and "Work Visa" in text and WHY in text and "(DRAFT, not approved)" in text
    assert "attestation: you tick it yourself" in text


def test_e_n_types_an_answer_and_save_stores_it_through_answers_save(settings, tmp_path):
    p = schema_plan(settings, tmp_path)
    n = row(p, "If you answered 'Work Visa'").n
    book_path = tmp_path / "answers.yaml"
    book_path.write_text("answers:\n", encoding="utf-8")
    replies = iter([f"e {n}", "Graduate visa", "save", "v 1", "q"])
    with pytest.raises(AP.Quit):
        AP.confirm(p, lambda prompt: next(replies), out=lambda s: None, book_path=book_path)
    r = row(p, "If you answered 'Work Visa'")
    assert (r.answer, r.status) == ("Graduate visa", AP.READY) and r.source.startswith("answers:saved_")
    assert "Graduate visa" in book_path.read_text(encoding="utf-8")


def test_y_approves_each_draft_through_freetexts_own_menu(settings, tmp_path):
    src = sources(settings, tmp_path=tmp_path)
    p = AP.build(AP.questions_from_schema(gh_schema.fields_of(SCHEMA)), src, "schema", out=lambda s: None)
    src.freetext.ask = lambda prompt: "y"                                          # the draft's own [y] use
    AP.confirm(p, lambda prompt: "y", out=lambda s: None, freetext=src.freetext)
    r = row(p, "What motivated you")
    assert (r.status, r.source, r.answer) == (AP.READY, "freetext (approved)", WHY)


def test_a_draft_you_do_not_approve_needs_you(settings, tmp_path):
    src = sources(settings, tmp_path=tmp_path)
    p = AP.build(AP.questions_from_schema(gh_schema.fields_of(SCHEMA)), src, "schema", out=lambda s: None)
    src.freetext.ask = lambda prompt: "s" if "[y]" in prompt else ""               # I'll write it, then nothing typed
    AP.confirm(p, lambda prompt: "y", out=lambda s: None, freetext=src.freetext)
    assert row(p, "What motivated you").status == AP.NEEDS_USER


def test_questions_come_from_the_cached_schema_before_any_browser(settings, tmp_path):
    folder = tmp_path / "out"
    folder.mkdir()
    (folder / gh_schema.CACHE).write_text(json.dumps(SCHEMA), encoding="utf-8")
    qs = planfirst.questions_for(JOB_ROW, folder, fetch_json=None, headless=lambda: pytest.fail("no browser when the schema is there"),
                                 out=lambda s: None)
    assert qs.origin == "schema" and any(q.key == "question_7001" for q in qs.items) and planfirst.wants_letter(qs)


def test_the_letter_is_written_before_the_browser_when_the_form_has_a_cover_letter_field(tmp_path):
    from careerops.apply.letter_demand import Made

    pdf = tmp_path / "L.pdf"
    pdf.write_bytes(b"%PDF")
    calls = []
    rep, letter, text, why = planfirst.letter_first({}, None, None, lambda ctl: calls.append(ctl) or Made(str(pdf), "Body", "", None), True,
                                                    out=lambda s: None)
    assert calls == [None] and letter == str(pdf) and text == "Body" and why == ""
    rep, letter, text, why = planfirst.letter_first({}, None, None, lambda ctl: pytest.fail("no cover-letter field"), False, out=lambda s: None)
    assert letter is None
