"""v3.7: plain wording is the default CV style (`cv.style`, `live --style full` for one run), the ATS fallback that puts `text` back where plain
loses keywords, the number gate (lint only) and the doctor's read-only checks of the fact bank's plain lines. Fixture banks only: no real settings,
no real fact bank, no network, no model."""
import copy

import pytest
import yaml

from careerops import bands, cli, setup
from careerops.apply import autogate
from careerops.tailoring import numberlint
from careerops.tailoring import plain as P
from careerops.tailoring.scorer import Keyword


def bullet(bid, text, plain=None, **extra):
    b = {"id": bid, "text": text, "terms": []}
    if plain is not None:
        b["plain"] = plain
    return {**b, **extra}


def bank(*bullets):
    return {"experience": [{"id": "r1", "title": "Role", "org": "Org", "bullets": list(bullets)}], "projects": []}


def printed(fb, kws=()):
    """The bullets as the CV prints them in the default style: {id: string}."""
    out, _ = P.plain_bank(fb, list(kws), cap=0)
    return {b["id"]: b["text"] for b in out["experience"][0]["bullets"]}


# ------------------------------------------------------------------ 1. the style
def test_a_bullet_with_plain_prints_plain_by_default_and_text_under_full():
    fb = bank(bullet("a1", "Built a Python pipeline that cut runtime from 4 hours to 20 minutes.", "I made a data job 12 times faster (Python)."))
    assert P.style({"cv": {}}) == "plain" and P.style({}) == "plain"                     # key missing: plain
    assert printed(fb)["a1"] == "I made a data job 12 times faster (Python)."
    assert P.style({"cv": {"style": "full"}}) != "plain"                                 # full: tailor never builds the plain bank
    assert P.style({"cv": {"style": "plain"}}) == "plain"


def test_a_bullet_without_plain_prints_text_under_both_styles():
    fb = bank(bullet("a1", "Built a Python pipeline that cut runtime by 40%."))
    assert printed(fb)["a1"] == "Built a Python pipeline that cut runtime by 40%."
    assert "a1" in P.plain_bank(fb, [], cap=0)[1]["missing"]


def test_verbatim_does_not_block_plain():
    fb = bank(bullet("a1", "Contributed the MCP implementation for 3 agents.", "I helped build the agent connector for 3 agents.", verbatim=True))
    assert printed(fb)["a1"] == "I helped build the agent connector for 3 agents."


def test_live_style_full_overrides_one_run_and_does_not_touch_settings(monkeypatch, settings):
    seen = {}

    class Stop(Exception):
        pass

    def fake_ctx(args):
        return settings, None

    def stop(args, s, auto=False):
        seen["style"] = s.raw["cv"]["style"]
        raise Stop

    monkeypatch.setattr(cli, "_ctx", fake_ctx)
    monkeypatch.setattr(cli, "_region_choice", stop)
    settings.raw["cv"]["style"] = "plain"
    with pytest.raises(Stop):
        cli.cmd_live(cli.argparse.Namespace(yes=False, max=1, config=None, auto=False, no_submit=False, confirm=False, include_unapproved=False,
                                            mock=False, url=None, clipboard=False, id=None, style="full"))
    assert seen["style"] == "full"


# ------------------------------------------------------------------ 2. the ATS fallback
KWS = [Keyword("alpha", 3.0, True), Keyword("beta", 2.0, True), Keyword("gamma", 1.0, False), Keyword("sql", 44.0, True)]


def cv_ctx(fb, plain_fb_info):
    pf, info = plain_fb_info
    es = [{"title": "", "org": "", "bullet_ids": [b["id"] for b in pf["experience"][0]["bullets"]], "bullets": [b["text"] for b in pf["experience"][0]["bullets"]]}]
    return {"profile": "", "skills": {"Data": "SQL"}, "experience": es, "projects": []}, info


def fallback_case():
    fb = bank(bullet("b1", "Used beta on 5 jobs.", "I ran 5 jobs."), bullet("a1", "Used alpha on 9 jobs.", "I ran 9 jobs."),
              bullet("c1", "Used gamma on 2 jobs.", "I ran 2 jobs."))
    ctx, info = cv_ctx(fb, P.plain_bank(fb, KWS, cap=0))
    return ctx, info


def test_plain_losing_12_points_switches_the_heaviest_bullet_first_and_stops_within_5():
    ctx, info = fallback_case()
    plain_s, full_s = P.strict_both(ctx, info, KWS, [])
    assert round(full_s - plain_s) == 12
    switched = P.style_fallback(ctx, info, KWS, [])
    assert switched == ["a1", "b1"]                                  # alpha (6 points) then beta (4): the gap is 2; gamma stays plain
    plain_s, full_s = P.strict_both(ctx, info, KWS, [])
    assert full_s - plain_s <= P.MAX_STYLE_GAP
    assert ctx["experience"][0]["bullets"] == ["Used beta on 5 jobs.", "Used alpha on 9 jobs.", "I ran 2 jobs."]
    assert ctx["skills"] == {"Data": "SQL"}                          # Skills are never touched


def test_the_fallback_is_repeatable_and_leaves_a_small_gap_alone():
    first, second = fallback_case(), fallback_case()
    assert P.style_fallback(*first[:1], first[1], KWS, []) == P.style_fallback(second[0], second[1], KWS, [])
    assert first[0] == second[0]
    again = P.style_fallback(first[0], first[1], KWS, [])            # already within 5: nothing more is switched
    assert again == []
    fb = bank(bullet("g1", "Used gamma on 2 jobs.", "I ran 2 jobs."))
    ctx, info = cv_ctx(fb, P.plain_bank(fb, KWS, cap=0))
    assert P.style_fallback(ctx, info, KWS, []) == [] and ctx["experience"][0]["bullets"] == ["I ran 2 jobs."]


def test_equal_gains_go_to_the_smaller_bullet_id():
    kws = [Keyword("alpha", 10.0, True), Keyword("beta", 10.0, True), Keyword("sql", 30.0, True)]
    fb = bank(bullet("z9", "Used beta here 1.", "I ran 1 job."), bullet("m1", "Used alpha here 2.", "I ran 2 jobs."))
    ctx, info = cv_ctx(fb, P.plain_bank(fb, kws, cap=0))
    assert P.style_fallback(ctx, info, kws, [], max_gap=5.0)[0] == "m1"


# ------------------------------------------------------------------ 3. the number gate
def test_bullet_no_number_lists_exactly_the_bullets_without_a_digit_or_a_result():
    fb = bank(bullet("n1", "Led the team."), bullet("n2", "Led the team.", result="adopted by the whole team"), bullet("n3", "Led 4 people."))
    ctx = {"experience": [{"bullet_ids": ["n1", "n2", "n3"], "bullets": ["Led the team.", "Led the team.", "Led 4 people."]}], "projects": []}
    found = numberlint.lints(ctx, fb, None)
    assert [(x["code"], x["severity"], x["ids"]) for x in found] == [("bullet-no-number", "info", ["n1"])]
    assert found[0]["fix"] == "add a number to the fact bank" and not numberlint.has_warn(found)


def test_a_letter_with_two_numbers_raises_a_warn_and_the_band_drops_from_auto_to_review():
    ctx = {"experience": [], "projects": []}
    letter = "I cut a job from 4 hours to 20 minutes at 180 Degrees Consulting, and tested it on 65 cases. Could we spend 20 minutes on it?"
    assert numberlint.letter_numbers(letter) == {"4", "65"}
    found = numberlint.lints(ctx, {}, letter)
    assert [(x["code"], x["severity"]) for x in found] == [("letter-few-numbers", "warn")]
    auto = bands.classify(80, 80, bands.Bands())
    assert auto.outcome == "auto" and bands.demote(auto, numberlint.has_warn(found)).outcome == "review"
    gate = autogate._number_lints({"lints": found})
    assert gate.ok is False
    ok = numberlint.lints(ctx, {}, "It took 4 hours, then 12 minutes, across 65 cases.")
    assert not ok and bands.demote(auto, numberlint.has_warn(ok)).outcome == "auto" and autogate._number_lints({"lints": ok}).ok
    assert bands.demote(bands.classify(55, 80, bands.Bands()), True).outcome == "review"      # only auto moves; no letter, no letter lint
    assert numberlint.lints(ctx, {}, None) == []


# ------------------------------------------------------------------ 4. doctor
def doctor_rows(tmp_path, fb):
    path = tmp_path / "fact_bank.yaml"
    path.write_text(yaml.safe_dump(fb, allow_unicode=True), encoding="utf-8")
    return setup.plain_fact_checks(path)


def test_doctor_flags_a_new_number_a_31_word_plain_and_a_headline_with_two_numbers(tmp_path):
    fb = bank(bullet("p1", "Cut a job to 20 minutes.", "I cut a job to 25 minutes."),
              bullet("p2", "Wrote a long thing 3 times.", " ".join(["word"] * 30) + " 3"),
              bullet("p3", "Used tools 3 times.", "I used tools 3 times (Python, SQL, AWS)."))
    fb["hook_library"] = [{"id": "h1", "headline": "Cut cost 40% and time 20% for a team", "profile_line": "Short line."}]
    rows = doctor_rows(tmp_path, fb)
    names = [n for n, ok, _ in rows]
    assert all(ok == "!!" for _, ok, _ in rows)                                    # [!!] lines; the doctor still exits 0
    assert any(n.startswith("fact bank bullet p1:") and "25" in n for n in names)
    assert any(n.startswith("fact bank bullet p2:") and "31 words" in n for n in names)
    assert any(n.startswith("fact bank bullet p3:") and "closing brackets" in n for n in names)
    assert any(n.startswith("hook h1 headline:") and "more than one number" in n for n in names)


def test_doctor_accepts_about_65_against_65_and_859_494_against_859494(tmp_path):
    assert P.numbers_of("about 65") == P.numbers_of("~65") == {"65"} and P.numbers_of("859,494") == P.numbers_of("859494") == {"859494"}
    fb = bank(bullet("q1", "Checked it with ~65 tests.", "I checked it with about 65 tests (Python)."),
              bullet("q2", "Read 859494 rows.", "I read 859,494 rows (Python, SQL)."))
    fb["hook_library"] = [{"id": "h1", "headline": "Cut a run from 4 → 1 hours", "profile_line": "A short line."}]
    rows = doctor_rows(tmp_path, fb)
    assert rows and all(ok is True for _, ok, _ in rows), rows


def test_doctor_checks_the_profile_line_and_the_letter_template_length(tmp_path):
    fb = bank()
    fb["hook_library"] = [{"id": "h1", "headline": "Cut a run to 1 hour", "profile_line": " ".join(["w"] * 36)}]
    fb["families"] = [{"id": "f1", "letter_template": "Hi {company} team,\n\n{company_problem_sentence} Short.\n\nCould we spend 20 minutes on {question}?\n\nRegards,\nName"}]
    names = [n for n, ok, _ in doctor_rows(tmp_path, fb)]
    assert any("profile_line is 36 words" in n for n in names) and any("letter_template of family f1" in n for n in names)
