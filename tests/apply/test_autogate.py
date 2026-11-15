"""The gate in front of every auto-submit (apply/autogate.py): ATS, format, faithfulness, level, sponsorship, kind, duplicates, daily cap.
All of them must pass; every failed one is named in one `AUTO BLOCKED: ...` line. Database, archive and logs are under tmp_path; the PDF
inspection is stubbed except in the tests marked slow, which use real PDFs."""
import csv
import json
from datetime import date, datetime
from pathlib import Path

import pytest
import yaml
from conftest import PROFILE_FIX

from careerops import ats_structure
from careerops.apply import autogate, autolog
from careerops.tailoring import doccheck

TODAY = date(2026, 10, 21)
APPLICANT = yaml.safe_load((PROFILE_FIX / "applicant.yaml").read_text(encoding="utf-8"))
FB = yaml.safe_load((PROFILE_FIX / "fact_bank.yaml").read_text(encoding="utf-8"))


def make_job(**over):
    base = {"id": "greenhouse:acme-1", "source": "greenhouse", "company": "Acme Ltd", "title": "Data Scientist", "location": "Bristol, UK",
            "url": "https://boards.greenhouse.io/acme/jobs/1", "apply_url": "https://boards.greenhouse.io/acme/jobs/1",
            "apply_kind": "greenhouse", "description": "We build forecasting tools in Python. " * 30, "status": "needs_review",
            "applied_at": None, "out_dir": "/x"}
    return {**base, **over}


def make_rep(**over):
    base = {"ats": {"strict": 74.0, "ceiling": 90.0}, "final": {"chosen_attempt": 1}, "attempts": [{"violations": [], "warnings": []}],
            "letter": {"status": "off"}, "cv_pdf": "/x/cv.pdf",
            "context": {"id": {"name": "Ada Lovelace", "email": "ada.lovelace@example.org"}, "profile": "p", "skills": {"a": ["b"]},
                        "experience": [{}], "projects": [{}], "education": [{}]}}
    return {**base, **over}


@pytest.fixture(autouse=True)
def pdf_is_fine(monkeypatch, request):
    """The gate tests do not render PDFs: the PDF checks say all is well, except in the tests marked slow, which use real ones."""
    if request.node.get_closest_marker("slow") is None:
        monkeypatch.setattr(autogate, "pdf_problems", lambda *a, **k: [])
        # v3.3: the structural ATS check reads a real PDF; it has its own tests (tests/test_ats_structure_v33.py)
        monkeypatch.setattr(ats_structure, "for_report", lambda *a, **k: ats_structure.JobStructure(ats_structure.Structure("cv", 100), None, []))
    monkeypatch.setattr(doccheck, "for_report", lambda *a, **k: doccheck.DocCheck())        # the document check has its own tests (below, and test_doccheck.py)


def run(settings, db, job=None, rep=None, **kw):
    job = make_job(**(job or {}))
    return autogate.evaluate(db, settings, job, make_rep(**(rep or {})), FB, today=TODAY, **kw)


def names(g):
    return [x.name for x in g.failed]


def test_a_clean_job_passes_every_gate(settings, db):
    g = run(settings, db)
    assert g.ok and g.failed == [] and g.line() == ""
    assert [x.name for x in g.items] == ["ATS", "format", "faithfulness", "level", "sponsorship", "kind", "duplicates", "daily cap", "doc check",
                                         "ats structure", "number lints"]


def test_every_failed_gate_is_named_in_one_line_in_table_order(settings, db):
    g = run(settings, db, job={"title": "Senior Data Scientist"}, rep={"ats": {"strict": 58.0, "ceiling": 90.0}})
    assert g.ok is False
    assert g.line() == "AUTO BLOCKED: ATS 58 < 70; level: senior"


def test_the_ats_gate_uses_the_strict_score_and_the_setting(settings, db):
    assert run(settings, db, rep={"ats": {"strict": 70.0, "ceiling": 99.0}}).ok
    g = run(settings, db, rep={"ats": {"strict": 69.9, "ceiling": 99.0}})
    assert names(g) == ["ATS"] and "ATS 69.9 < 70" in g.line()
    settings.raw["apply"]["min_ats_strict"] = 80
    assert names(run(settings, db, rep={"ats": {"strict": 74.0, "ceiling": 99.0}})) == ["ATS"]
    assert names(run(settings, db, rep={"ats": None})) == ["ATS"]                 # no score = not 70


@pytest.mark.parametrize("title,level", [("Senior Data Scientist", "senior"), ("Lead AI Engineer", "lead"), ("Principal ML Engineer", "lead"),
                                         ("Engineering Manager, AI", "manager"), ("Head of Data", "manager"), ("Sr. Data Scientist", "senior")])
def test_a_manager_lead_or_senior_title_is_blocked(settings, db, title, level):
    g = run(settings, db, job={"title": title})
    assert names(g) == ["level"] and g.line() == f"AUTO BLOCKED: level: {level}"


@pytest.mark.parametrize("title", ["Data Scientist", "AI Engineer", "Machine Learning Engineer (Graduate)", "Junior Data Scientist"])
def test_other_titles_have_level_one_and_pass(settings, db, title):
    assert run(settings, db, job={"title": title}).ok


@pytest.mark.parametrize("sentence", ["Unfortunately we are unable to sponsor visas for this role.", "No sponsorship is available.",
                                      "Candidates must be able to work in the UK without sponsorship."])
def test_a_no_sponsorship_line_in_the_jd_blocks(settings, db, sentence):
    g = run(settings, db, job={"description": "We build things. " * 30 + sentence})
    assert names(g) == ["sponsorship"] and "no sponsorship" in g.line()


def test_a_jd_that_offers_or_never_mentions_sponsorship_passes(settings, db):
    assert run(settings, db, job={"description": "We are a licensed sponsor and can sponsor visas. " * 20}).ok
    assert run(settings, db).ok


@pytest.mark.parametrize("kind", ["greenhouse", "ashby", "lever"])
def test_the_three_kinds_may_be_auto_submitted(settings, db, kind):
    assert run(settings, db, job={"apply_kind": kind}).ok


@pytest.mark.parametrize("kind", ["workday", "employer_other", "aggregator_only"])
def test_every_other_kind_is_blocked(settings, db, kind):
    g = run(settings, db, job={"apply_kind": kind})
    assert names(g) == ["kind"] and kind in g.line()


def test_a_job_without_a_stored_kind_gets_it_from_its_apply_page(settings, db):
    assert run(settings, db, job={"apply_kind": None, "apply_url": "https://jobs.lever.co/acme/123"}).ok
    g = run(settings, db, job={"apply_kind": None, "apply_url": "https://acme.wd3.myworkdayjobs.com/en/job/1"})
    assert names(g) == ["kind"]
    g = run(settings, db, job={"apply_kind": None, "apply_url": None, "url": None})
    assert names(g) == ["kind"] and "no application page" in g.line()


def test_the_kinds_come_from_the_setting(settings, db):
    settings.raw["apply"]["kinds"] = ["lever"]
    assert names(run(settings, db, job={"apply_kind": "greenhouse"})) == ["kind"]


# ------------------------------------------------------------------ format and faithfulness
def test_a_pdf_problem_is_a_failed_format_gate_and_every_problem_is_printed(settings, db, monkeypatch):
    monkeypatch.setattr(autogate, "pdf_problems", lambda *a, **k: ["3 pages, not 2", "font Foo is not embedded"])
    g = run(settings, db)
    assert names(g) == ["format"] and g.line() == "AUTO BLOCKED: format: 3 pages, not 2; font Foo is not embedded"


def test_the_pdf_is_checked_against_the_fact_bank_identity_and_the_sections_the_cv_has(settings, db, monkeypatch):
    seen = {}
    monkeypatch.setattr(autogate, "pdf_problems", lambda pdf, **k: seen.update(pdf=pdf, **k) or [])
    rep = make_rep()
    rep["context"].update(publications=[{}], languages=["x"], certifications=[], achievements=[])
    autogate.evaluate(db, settings, make_job(), rep, FB, today=TODAY)
    assert seen["name"] == FB["identity"]["name"] and seen["email"] == FB["identity"]["email"]
    assert seen["headings"] == ["Profile", "Skills", "Work Experience", "Projects", "Education", "Publications", "Languages"]


def test_a_missing_cv_pdf_is_a_failed_format_gate(settings, db, monkeypatch):
    monkeypatch.undo()
    monkeypatch.setattr(doccheck, "for_report", lambda *a, **k: doccheck.DocCheck())
    g = run(settings, db, rep={"cv_pdf": None})
    assert names(g) == ["format", "ats structure"] and "no CV PDF" in g.line()          # v3.3: nothing to check is never a pass


def test_a_claim_guard_violation_or_warning_in_the_chosen_attempt_blocks(settings, db):
    bad = {"attempts": [{"violations": [], "warnings": []}, {"violations": ["B1: invented 'Kubernetes'"], "warnings": []}],
           "final": {"chosen_attempt": 2}}
    g = run(settings, db, rep=bad)
    assert names(g) == ["faithfulness"] and "1 claim-guard violation" in g.line()
    warn = {"attempts": [{"violations": [], "warnings": ["wording: reverted", "x"]}], "final": {"chosen_attempt": 1}}
    g = run(settings, db, rep=warn)
    assert names(g) == ["faithfulness"] and "2 claim-guard warnings" in g.line()


BLOCKED = {"attempt": 1, "warnings": [], "proposal": {"rephrase": {"p_ql1": "Built the pipeline for 3.1m rows", "p_ql2": "Used Kubernetes"}},
           "violations": ["p_ql1: new numbers ['3.1m']", "p_ql2: keyword 'Kubernetes' not evidenced by source"]}


def final_cv(**bullets):
    return {"id": {"name": "Ada Lovelace", "email": "ada.lovelace@example.org"}, "profile": "p", "skills": {"a": ["b"]}, "experience": [{}],
            "projects": [{"bullet_ids": list(bullets), "bullets": list(bullets.values())}], "education": [{}]}


def test_rewrites_the_guard_blocked_are_information_and_do_not_gate(settings, db):
    rep = make_rep(attempts=[BLOCKED], context=final_cv(p_ql1="Built the pipeline", p_ql2="Used Python"))
    g = autogate.evaluate(db, settings, make_job(), rep, FB, today=TODAY)
    assert g.ok and g.line() == ""
    faith = next(x for x in g.items if x.name == "faithfulness")
    assert "2 blocked rewrites" in faith.info and "0 issues in the final CV" in faith.info


def test_an_issue_in_the_final_cv_gates_next_to_the_blocked_rewrites(settings, db):
    rec = {**BLOCKED, "violations": [*BLOCKED["violations"], "p_ql9: deny-listed term 'Spark'"]}
    g = run(settings, db, rep={"attempts": [rec], "context": final_cv(p_ql1="Built the pipeline", p_ql9="Used Spark")})
    assert names(g) == ["faithfulness"] and "1 claim-guard violation" in g.line() and "2 claim-guard" not in g.line()
    assert "2 blocked rewrites" in next(x for x in g.items if x.name == "faithfulness").info


def test_a_violating_rewrite_that_reached_the_final_cv_is_an_issue_there(settings, db):
    g = run(settings, db, rep={"attempts": [BLOCKED], "context": final_cv(p_ql1="Built the pipeline for 3.1m rows", p_ql2="Used Python")})
    assert names(g) == ["faithfulness"] and "1 claim-guard violation" in g.line()


def test_an_attempt_that_was_not_chosen_does_not_count(settings, db):
    rep = {"attempts": [{"violations": ["old"], "warnings": ["old"]}, {"violations": [], "warnings": []}], "final": {"chosen_attempt": 2}}
    assert run(settings, db, rep=rep).ok


@pytest.mark.parametrize("letter,ok", [({"status": "ok"}, True), ({"status": "off"}, True), ({"status": "none"}, True), ({"status": "declined"}, True),
                                       ({"status": "fallback"}, False), ({"status": "pending"}, False), ({"status": "ok", "problems": ["x"]}, False)])
def test_a_letter_that_exists_must_have_passed_its_own_claim_check(settings, db, letter, ok):
    g = run(settings, db, rep={"letter": letter})
    assert g.ok is ok and (ok or names(g) == ["faithfulness"])


def test_a_report_without_attempts_cannot_be_called_faithful(settings, db):
    assert names(run(settings, db, rep={"attempts": [], "final": {}})) == ["faithfulness"]


# ------------------------------------------------------------------ duplicates
def test_a_job_already_applied_in_the_database_is_a_duplicate(settings, db):
    job = make_job()
    db.upsert_job(job)
    db.update_job(job["id"], status="applied", applied_at="2026-10-01T10:00:00+00:00")
    g = run(settings, db, job={"status": "applied", "applied_at": "2026-10-01T10:00:00+00:00"})
    assert names(g) == ["duplicates"] and "already applied" in g.line()


def test_a_job_whose_form_was_submitted_but_not_confirmed_is_a_duplicate_too(settings, db):
    assert names(run(settings, db, job={"status": "submit_unconfirmed"})) == ["duplicates"]


def test_the_archive_index_row_for_this_job_is_a_duplicate(settings, db):
    root = settings.path("applications_auto")
    (root / "Acme Ltd" / "Data Scientist").mkdir(parents=True)
    db.upsert_job(make_job())
    db.conn.execute("INSERT INTO archive_index VALUES (?,?,?,?,?)", ("Acme Ltd/Data Scientist", "role", "acme", "greenhouse:acme-1", "x"))
    db.conn.commit()
    g = run(settings, db)
    assert names(g) == ["duplicates"] and "archive" in g.line()


def test_a_company_and_role_folder_in_the_archive_is_a_duplicate_even_for_another_job_id(settings, db):
    (settings.path("applications") / "Acme Ltd" / "Data Scientist").mkdir(parents=True)
    g = run(settings, db, job={"id": "greenhouse:acme-2"})
    assert names(g) == ["duplicates"] and "Acme Ltd" in g.line()
    assert run(settings, db, job={"title": "AI Engineer"}).ok                       # another role at the same company is fine


def test_another_job_row_of_the_same_company_and_title_that_was_applied_is_a_duplicate(settings, db):
    other = make_job(id="reed:999", source="reed")
    db.upsert_job(other)
    db.update_job("reed:999", status="applied", applied_at="2026-09-30T10:00:00+00:00")
    g = run(settings, db)
    assert names(g) == ["duplicates"] and "reed:999" in g.line()


def test_a_job_the_aiapply_export_lists_is_a_duplicate(settings, db):
    p = settings.path("aiapply_applied")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("company,title\nAcme Ltd,Data Scientist\n", encoding="utf-8")
    g = run(settings, db)
    assert names(g) == ["duplicates"] and "AIApply" in g.line()


def test_a_row_for_this_job_in_the_auto_log_is_a_duplicate_but_a_no_submit_row_is_not(settings, db):
    autolog.append(settings, job=make_job(), ats_strict=74.0, screenshots=["a.png"], result=autolog.NOT_SUBMITTED, when=datetime(2026, 10, 20, 9))
    assert run(settings, db).ok
    autolog.append(settings, job=make_job(), ats_strict=74.0, screenshots=["a.png"], result=autolog.UNCONFIRMED, when=datetime(2026, 10, 20, 10))
    assert names(run(settings, db)) == ["duplicates"]


def test_a_tracker_row_for_this_job_is_a_duplicate(settings, db):
    g = run(settings, db, tracker=lambda: {"greenhouse:acme-1", "x"})
    assert names(g) == ["duplicates"] and "tracker" in g.line()
    assert run(settings, db, tracker=lambda: {"x"}).ok


def test_an_unreadable_tracker_blocks_a_real_submit_but_not_a_fill_that_never_submits(settings, db):
    def broken():
        raise RuntimeError("no Google sign-in")
    g = run(settings, db, tracker=broken)
    assert names(g) == ["duplicates"] and "tracker could not be read" in g.line()
    assert run(settings, db, tracker=broken, submitting=False).ok


# ------------------------------------------------------------------ daily cap
def _log(settings, n, day, result=autolog.SUBMITTED):
    for i in range(n):
        autolog.append(settings, job=make_job(id=f"greenhouse:x{day.day}-{i}"), ats_strict=75.0, screenshots=[], result=result,
                       when=datetime(day.year, day.month, day.day, 9, i))


def test_the_daily_cap_counts_todays_clicked_submits_only(settings, db):
    _log(settings, 4, TODAY)
    assert run(settings, db).ok
    _log(settings, 1, TODAY, autolog.UNCONFIRMED)
    g = run(settings, db)
    assert names(g) == ["daily cap"] and g.line() == "AUTO BLOCKED: daily cap: 5 of 5 auto-submits used today"


def test_other_days_and_unsubmitted_rows_do_not_count(settings, db):
    _log(settings, 9, date(2026, 10, 20))
    _log(settings, 9, TODAY, autolog.NOT_SUBMITTED)
    _log(settings, 9, TODAY, autolog.NEEDS_USER)
    assert run(settings, db).ok


def test_the_cap_is_a_setting(settings, db):
    settings.raw["apply"]["max_auto_per_day"] = 1
    _log(settings, 1, TODAY)
    assert names(run(settings, db)) == ["daily cap"]
    settings.raw["apply"]["max_auto_per_day"] = 0
    assert names(run(settings, db)) == ["daily cap"]


# ------------------------------------------------------------------ settings
def test_defaults_when_the_apply_block_is_missing(settings):
    del settings.raw["apply"]
    c = autogate.config(settings)
    assert (c.mode, c.min_ats_strict, c.max_auto_per_day, c.confirm_first_n, c.kinds) == ("assist", 70.0, 5, 3, ("greenhouse", "ashby", "lever"))


def test_the_built_in_default_is_assist_never_auto(tmp_path):
    """The default is tested on a settings file with no apply section; never on the real config/settings.yaml, where the user sets
    `apply.mode: auto` on purpose."""
    from careerops.config import load_settings
    bare = tmp_path / "settings.yaml"
    bare.write_text("paths: {}\n", encoding="utf-8")
    c = autogate.config(load_settings(bare))
    assert c.mode == "assist" and c.min_ats_strict == 70 and c.max_auto_per_day == 5 and c.confirm_first_n == 3
    assert c.kinds == ("greenhouse", "ashby", "lever")


@pytest.mark.parametrize("bad", [{"mode": "yes"}, {"mode": "AUTO "}, {"min_ats_strict": "high"}, {"max_auto_per_day": -1}, {"kinds": "greenhouse"}])
def test_a_bad_apply_setting_is_refused_with_its_name(settings, bad):
    settings.raw["apply"].update(bad)
    if bad.get("mode") == "AUTO ":
        assert autogate.config(settings).mode == "auto"                      # case and spaces are forgiven
        return
    with pytest.raises(autogate.GateConfigError, match=next(iter(bad))):
        autogate.config(settings)


# ------------------------------------------------------------------ the log
def test_the_auto_log_has_the_seven_columns_and_is_appended_never_rewritten(settings):
    autolog.append(settings, job=make_job(), ats_strict=74.04, screenshots=["a.png", "b.png"], result=autolog.SUBMITTED, when=datetime(2026, 10, 21, 9, 30))
    autolog.append(settings, job=make_job(id="x:2", company="Zeta, Inc", title='Sr "DS"'), ats_strict=71.0, screenshots=[], result=autolog.NEEDS_USER,
                   when=datetime(2026, 10, 21, 9, 45))
    p = settings.path("live") / "applied_auto.csv"
    assert p == autolog.path(settings)
    rows = list(csv.reader(p.read_text(encoding="utf-8").splitlines()))
    assert rows[0] == ["time", "job id", "company", "role", "ATS strict", "screenshots", "result"]
    assert rows[1] == ["2026-10-21T09:30:00", "greenhouse:acme-1", "Acme Ltd", "Data Scientist", "74.0", "a.png;b.png", "submitted"]
    assert rows[2][2:4] == ["Zeta, Inc", 'Sr "DS"'] and rows[2][5:] == ["", "needs_user"]
    assert len(autolog.rows(settings)) == 2


def test_a_damaged_log_row_is_skipped_not_fatal(settings):
    p = autolog.path(settings)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("time,job id,company,role,ATS strict,screenshots,result\nnot-a-date,j,c,r,1,,submitted\n", encoding="utf-8")
    assert autolog.clicked_on(autolog.rows(settings), TODAY) == 0 and autolog.clicked_total(autolog.rows(settings)) == 1


# ------------------------------------------------------------------ the PDF checks, on real PDFs
def test_fonts_are_embedded_only_when_every_row_says_so():
    ok = ("name type encoding emb sub uni object ID\n----\nAAA+Serif CID TrueType (OT) Identity-H yes yes yes 12 0\n"
          "BBB+Sans Type 1C (CID) Identity-H yes yes yes 14 0\n")
    assert autogate.unembedded_fonts(ok) == []
    bad = ok + "Helvetica Type 1 WinAnsi no no no 9 0\n"
    assert autogate.unembedded_fonts(bad) == ["Helvetica"]
    assert autogate.unembedded_fonts("name type\n----\n") == ["(no fonts listed)"]


def _tex(body: str) -> str:
    return "\\documentclass[11pt,a4paper]{article}\\usepackage{fontspec}\\usepackage[margin=0.6in]{geometry}\\pagestyle{empty}\\begin{document}" + body + "\\end{document}"


@pytest.mark.slow
def test_a_real_tailored_cv_passes_the_format_checks(tailored):
    rep, fb, _ = tailored
    ctx = rep["context"]
    got = autogate.pdf_problems(Path(rep["cv_pdf"]), name=fb["identity"]["name"], email=fb["identity"]["email"],
                                headings=autogate.expected_headings(ctx))
    assert got == []


@pytest.mark.slow
def test_a_one_page_pdf_two_overlapping_words_and_a_missing_name_are_each_named(tmp_path):
    from careerops.tailoring.render import compile_tex
    one = compile_tex(_tex("Ada Lovelace ada@example.org \\section*{PROFILE} text"), tmp_path, "one")
    got = autogate.pdf_problems(one, name="Ada Lovelace", email="ada@example.org", headings=["Profile"])
    assert got == ["1 page, not 2"]
    two = compile_tex(_tex("Ada Lovelace ada@example.org \\section*{PROFILE} text \\newpage more"), tmp_path, "two")
    assert autogate.pdf_problems(two, name="Ada Lovelace", email="ada@example.org", headings=["Profile"]) == []
    got = autogate.pdf_problems(two, name="Grace Hopper", email="grace@example.org", headings=["Profile", "Skills"])
    assert any("name" in p for p in got) and any("email" in p for p in got) and any("Skills" in p for p in got)
    clash = compile_tex(_tex("Ada Lovelace \\section*{PROFILE} Overlapping\\kern-2.5em Shifted text \\newpage more"), tmp_path, "clash")
    got = autogate.pdf_problems(clash, name="Ada Lovelace", email="", headings=["Profile"])
    assert any("overlap" in p for p in got)


@pytest.mark.slow
def test_headings_out_of_order_and_text_past_the_margin_are_named(tmp_path):
    from careerops.tailoring.render import compile_tex
    swapped = compile_tex(_tex("Ada Lovelace \\section*{SKILLS} a \\section*{PROFILE} b \\newpage more"), tmp_path, "swap")
    got = autogate.pdf_problems(swapped, name="Ada Lovelace", email="", headings=["Profile", "Skills"])
    assert any("order" in p for p in got)
    wide = compile_tex(_tex("Ada Lovelace \\section*{PROFILE} \\hspace*{16cm}\\mbox{pastthemargin} \\newpage more"), tmp_path, "wide")
    got = autogate.pdf_problems(wide, name="Ada Lovelace", email="", headings=["Profile"])
    assert any("margin" in p for p in got)


# ------------------------------------------------------------------ the document check (Document rules, A7): always blocks auto-submit
def test_a_document_check_failure_blocks_auto_submit_and_is_named(settings, db, monkeypatch):
    monkeypatch.setattr(doccheck, "for_report", lambda *a, **k: doccheck.DocCheck(["PROFILE 84/100 words: add profile sentences to fact_bank.yaml → profile.sentences", "x"]))
    g = run(settings, db)
    assert names(g) == ["doc check"] and not g.ok
    first = "PROFILE 84/100 words: add profile sentences to fact_bank.yaml → profile.sentences"
    assert g.line() == f"AUTO BLOCKED: doc check: 2 failure(s), first: {first[:70]}"
    assert g.failed[0].info.startswith("DOC CHECK: FAILED (2)")


def test_a_passing_document_check_adds_its_line_and_blocks_nothing(settings, db):
    g = run(settings, db)
    assert g.ok and [x.info for x in g.items if x.name == "doc check"] == ["DOC CHECK: pass"]


def test_no_setting_can_switch_the_document_check_off(settings, db, monkeypatch):
    monkeypatch.setattr(doccheck, "for_report", lambda *a, **k: doccheck.DocCheck(["HOOK headline"]))
    for key in ("doc_check", "skip_doc_check", "require_doc_check"):
        settings.raw["apply"][key] = False
    assert names(run(settings, db)) == ["doc check"]


def test_a_check_that_cannot_run_blocks_too(settings, db, monkeypatch):
    def boom(*a, **k):
        raise OSError("no PDF")
    monkeypatch.setattr(doccheck, "for_report", boom)
    g = run(settings, db)
    assert names(g) == ["doc check"] and "the check could not run" in g.failed[0].info


def test_the_gate_is_given_the_report_the_fact_bank_and_the_job(settings, db, monkeypatch):
    seen = []
    monkeypatch.setattr(doccheck, "for_report", lambda rep, s, fb, job=None: seen.append((rep, fb, job)) or doccheck.DocCheck())
    run(settings, db)
    (rep, fb, job), = seen
    assert rep["ats"]["strict"] == 74.0 and fb is FB and job["id"] == "greenhouse:acme-1"
