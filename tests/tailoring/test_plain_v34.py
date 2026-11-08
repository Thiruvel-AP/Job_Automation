"""v3.4 plain style: a `plain:` version of each bullet for a non-technical first reader, approved by the user, with the JD keywords kept for the ATS.
The checks on a candidate, the review flow (`y` writes through factbank_append with a backup, the original is never changed), the plain bank of
tailoring (brackets only for keywords the original says), the strict ATS of both styles, the jargon rule of the letter, and the layout options
(additional experience, graduate order, one-line achievements). Fixtures and MockLLM only."""
import copy
import shutil
from pathlib import Path

import pytest
import yaml

from careerops import factbank_append as F
from careerops.llm import MockLLM
from careerops.review import plaindraft
from careerops.tailoring import plain as P
from careerops.tailoring import render
from careerops.tailoring import tailor as T
from careerops.tailoring.scorer import Keyword

FIX = Path(__file__).resolve().parents[1] / "fixtures"
BANK = FIX / "profile" / "fact_bank.yaml"
GOOD_AZ4 = "Researchers needed software they could trust, so I wrote ~65 automated tests across 8 files to check it."


def bank() -> dict:
    return yaml.safe_load(BANK.read_text(encoding="utf-8"))


def item(fb, bid):
    return next(i for i in P.items(fb) if i.owner_id == bid)


def check(text, bid="az4", fb=None):
    fb = fb or bank()
    it = item(fb, bid)
    return P.problems(text, it.bullet, it.owner, fb, None, it.kind, None if it.kind == "bullet" else it.source)


# ------------------------------------------------------------------ the checks on a candidate
def test_a_candidate_that_passes_every_check():
    assert check(GOOD_AZ4) == []


def test_a_candidate_that_adds_a_number_is_rejected():
    assert any("adds number" in p and "90" in p for p in check(GOOD_AZ4.replace("~65", "90")))


def test_a_candidate_that_adds_a_tool_or_an_organisation_is_rejected():
    probs = check("Researchers needed software they could trust, so I wrote ~65 automated tests in Docker for Globex across 8 files.")
    assert any("'Docker'" in p for p in probs) and any("'Globex'" in p for p in probs)


def test_a_candidate_that_adds_an_outcome_is_rejected():
    assert any("outcome" in p for p in check(GOOD_AZ4.rstrip(".") + ", now in daily use."))


@pytest.mark.parametrize("term", ["HGNC", "parquet", "circuit breaker", "Leiden", "Pydantic"])
def test_jargon_not_on_the_allow_list_is_rejected(term):
    text = f"Researchers needed software they could trust, so I used {term} and wrote ~65 automated tests across 8 files."
    assert any("jargon" in p and term.lower() in p.lower() for p in check(text)), check(text)


def test_allow_listed_names_and_the_roles_own_organisation_are_not_jargon():
    assert P.jargon("At Northwind Research Lab I built an AI assistant in Python on AWS with LangChain.", bank(),
                    {"org": "Northwind Research Lab, University of Northfield"}) == []


def test_over_30_words_is_rejected():
    long = ("Researchers needed software they could trust so I wrote ~65 automated tests across 8 files and then I checked every one of them "
            "again and again with great care for the whole team.")
    assert len(long.split()) > 30 and any("words (at most 30)" in p for p in check(long))


def test_a_flesch_score_under_50_is_rejected():
    hard = "Researchers necessitated comprehensive verification infrastructure, so I engineered ~65 automated evaluations across 8 repositories."
    assert P.flesch(hard) < 50 and any("Flesch" in p for p in check(hard))


def test_the_built_in_flesch_formula():
    assert P.flesch("The cat sat on the mat.") > 100
    assert P.syllables("AWS") == 5 and P.syllables("~65") == 1 and P.syllables("combined") == 2


def test_a_candidate_with_no_result_clause_is_rejected():
    assert any("no result clause" in p for p in check("Researchers needed software they could trust, so I wrote automated tests."))


def test_a_bullet_that_states_no_result_is_needs_result_and_is_not_drafted():
    fb = bank()
    it = item(fb, "dc3")                                       # "Documented every data source ... without a handover meeting."
    assert it.needs_result
    llm = MockLLM({"plain_bullet": lambda ctx: pytest.fail("a NEEDS RESULT bullet is never drafted")})
    text, probs, _ = P.draft(llm, it, fb)
    assert text == "" and probs[0].startswith("NEEDS RESULT")


def test_a_hook_headline_candidate_must_keep_its_number():
    fb = bank()
    it = item(fb, "h_fixture")
    assert it.kind == "headline" and it.field == "plain_headline"
    assert P.problems("AI assistant running on AWS, checked by ~65 automated tests", it.bullet, it.owner, fb, None, "headline", it.source) == []
    assert any("drops the number" in p for p in P.problems("AI assistant running on AWS, checked by automated tests", it.bullet, it.owner, fb,
                                                           None, "headline", it.source))


def test_the_draft_retries_once_with_the_problems_then_gives_nothing():
    fb, seen = bank(), []

    def handler(ctx):
        seen.append(ctx)
        return {"plain": GOOD_AZ4.replace("~65", "90")}
    text, probs, _ = P.draft(MockLLM({"plain_bullet": handler}), item(fb, "az4"), fb)
    assert text == "" and len(seen) == P.TRIES and any("adds number" in p for p in probs)


# ------------------------------------------------------------------ the review flow and the one write
@pytest.fixture
def bank_copy(tmp_path):
    p = tmp_path / "profile" / "fact_bank.yaml"
    p.parent.mkdir()
    shutil.copy(BANK, p)
    return p


def answers(*replies):
    q = list(replies)
    return lambda prompt: q.pop(0) if q else ""


def good_model():
    return MockLLM({"plain_bullet": lambda ctx: {"plain": GOOD_AZ4 if ctx["id"] == "az4" else ""}})


def test_y_saves_plain_through_factbank_append_with_a_backup_and_keeps_the_original(bank_copy):
    before = bank_copy.read_bytes()
    out = []
    stats = plaindraft.run(bank_copy, good_model(), None, ask=answers("y"), out=out.append, ids={"az4"})
    assert stats["saved"] == [("az4", "plain")]
    after = yaml.safe_load(bank_copy.read_text(encoding="utf-8"))
    az4 = next(b for e in after["experience"] for b in e["bullets"] if b["id"] == "az4")
    orig = next(b for e in yaml.safe_load(before.decode("utf-8"))["experience"] for b in e["bullets"] if b["id"] == "az4")
    assert az4["plain"] == GOOD_AZ4 and az4["text"] == orig["text"]
    backups = list((bank_copy.parent / "backups").glob("fact_bank.*.yaml"))
    assert len(backups) == 1 and backups[0].read_bytes() == before
    new_lines = [l for l in bank_copy.read_text(encoding="utf-8").splitlines() if l not in before.decode("utf-8").splitlines()]
    assert len(new_lines) == 1 and new_lines[0].strip().startswith("plain: ")          # one line added, nothing else touched


@pytest.mark.parametrize("reply", ["n", "", "yes please", "q", "e"])
def test_anything_but_y_saves_nothing(bank_copy, reply):
    before = bank_copy.read_bytes()
    plaindraft.run(bank_copy, good_model(), None, ask=answers(reply), out=lambda _: None, ids={"az4"})
    assert bank_copy.read_bytes() == before and not (bank_copy.parent / "backups").exists()


def test_e_shows_the_checks_of_your_edit_and_saves_it_after_y(bank_copy):
    out = []
    mine = "Researchers needed tested software, so I wrote ~65 automated tests across 8 files."
    plaindraft.run(bank_copy, good_model(), None, ask=answers("e", mine, "y"), out=out.append, ids={"az4"})
    az4 = next(b for e in yaml.safe_load(bank_copy.read_text(encoding="utf-8"))["experience"] for b in e["bullets"] if b["id"] == "az4")
    assert az4["plain"] == mine and any("checks:   all pass" in line for line in out)


def test_a_candidate_that_fails_is_never_offered(bank_copy):
    asked = []
    llm = MockLLM({"plain_bullet": lambda ctx: {"plain": "I used parquet and HGNC to write ~65 tests across 8 files."}})
    stats = plaindraft.run(bank_copy, llm, None, ask=lambda p: asked.append(p) or "y", out=lambda _: None, ids={"az4"})
    assert not asked and stats["rejected"].get("az4") and not stats["saved"]


def test_dry_run_asks_nothing_and_saves_nothing(bank_copy):
    before = bank_copy.read_bytes()
    plaindraft.run(bank_copy, good_model(), None, ask=lambda p: pytest.fail("asked"), out=lambda _: None, ids={"az4"}, dry_run=True)
    assert bank_copy.read_bytes() == before


def test_add_field_needs_y_never_replaces_and_restores_on_a_deny_listed_text(bank_copy):
    before = bank_copy.read_bytes()
    with pytest.raises(F.NotConfirmed):
        F.add_field(bank_copy, "az4", "plain", GOOD_AZ4, confirmed="n")
    with pytest.raises(F.AppendError, match="no bullet or hook entry"):
        F.add_field(bank_copy, "zz9", "plain", GOOD_AZ4, confirmed="y")
    with pytest.raises(F.AppendError, match="not a field"):
        F.add_field(bank_copy, "az4", "text", GOOD_AZ4, confirmed="y")
    deny = yaml.safe_load(before.decode("utf-8"))["deny_terms"][0]
    with pytest.raises(F.AppendError, match="would not validate"):
        F.add_field(bank_copy, "az4", "plain", f"I wrote ~65 tests with {deny}.", confirmed="y")
    assert bank_copy.read_bytes() == before
    F.add_field(bank_copy, "az4", "plain", GOOD_AZ4, confirmed="y")
    with pytest.raises(F.AppendError, match="already has"):
        F.add_field(bank_copy, "az4", "plain", "something else, 65 tests.", confirmed="y")


# ------------------------------------------------------------------ the plain bank of tailoring
KWS = [Keyword("python", 3.0, True), Keyword("aws", 3.0, True), Keyword("langchain", 2.0, True), Keyword("docker", 1.0, False),
       Keyword("kubernetes", 1.0, False)]


def with_plain(fb: dict, texts: dict[str, str]) -> dict:
    fb = copy.deepcopy(fb)
    for e in fb["experience"] + fb["projects"]:
        for b in e["bullets"]:
            if b["id"] in texts:
                b["plain"] = texts[b["id"]]
    return fb


def test_a_missing_jd_keyword_is_added_in_brackets_only_when_the_original_has_it():
    fb = with_plain(bank(), {"az1": "Researchers needed answers, so I built an AI assistant used by the whole research team."})
    out, info = P.plain_bank(fb, KWS)
    az1 = next(b for e in out["experience"] for b in e["bullets"] if b["id"] == "az1")
    assert az1["text"] == "Researchers needed answers, so I built an AI assistant used by the whole research team (AWS, LangChain)."
    assert "kubernetes" not in az1["text"].lower() and "python" not in az1["text"].lower()     # not in the original: never added
    assert info["plain"] == ["az1"] and "az2" in info["missing"] and info["technical"]["az1"].startswith("Built a tool-calling LLM agent")
    assert next(b for e in fb["experience"] for b in e["bullets"] if b["id"] == "az1")["text"].startswith("Built")   # the bank is untouched


def test_at_most_three_keywords_go_in_brackets():
    kws = [Keyword(t, 1.0, True) for t in ("aws", "langchain", "bedrock", "llm", "agent")]
    fb = with_plain(bank(), {"az1": "Researchers needed answers, so I built an assistant used by the whole research team."})
    _, info = P.plain_bank(fb, kws)
    assert len(info["added"]["az1"]) == 3


def test_the_plain_bank_uses_the_hooks_plain_fields():
    fb = bank()
    fb["hook_library"][0]["plain_headline"] = "AI assistant running on AWS, checked by ~65 automated tests"
    out, _ = P.plain_bank(fb, KWS)
    assert out["hook_library"][0]["headline"] == "AI assistant running on AWS, checked by ~65 automated tests"


def test_strict_of_the_plain_cv_is_not_below_the_technical_one_after_the_top_up():
    fb = with_plain(bank(), {"az1": "Researchers needed answers, so I built an AI assistant used by the whole research team."})
    out, info = P.plain_bank(fb, KWS, cap=0)                   # no brackets: the plain CV lost AWS and LangChain
    az1 = next(b for e in out["experience"] for b in e["bullets"] if b["id"] == "az1")
    ctx = {"profile": "", "skills": {}, "experience": [{"title": "", "org": "", "bullet_ids": ["az1"], "bullets": [az1["text"]]}], "projects": []}
    plain_s, tech_s = P.strict_both(ctx, info, KWS, [])
    assert plain_s < tech_s
    rep = T.plain_style_report(ctx, info, KWS, [])
    assert rep["strict_plain"] >= rep["strict_technical"] and set(rep["topped_up"]) == {"AWS", "LangChain"}
    assert ctx["experience"][0]["bullets"][0].endswith("(AWS, LangChain).")


def test_style_setting():
    assert P.style({}) == "plain" and P.style({"cv": {"style": "technical"}}) == "technical" and P.style({"cv": {"style": "odd"}}) == "plain"


# ------------------------------------------------------------------ the letter: the jargon rule
def test_the_letter_rewrite_is_refused_when_it_keeps_jargon_in_plain_style(settings, fb_fixture, monkeypatch):
    from test_fluency_v34 import model, make
    from test_company_letter import make_world
    from careerops.tailoring import company_letter as CL

    w = make_world(settings, fb_fixture)
    w.settings.raw["letter"]["compose"] = "assembled"
    w.settings.raw["letter"]["fluency"] = True
    w.settings.raw["cv"]["style"] = "plain"
    written = []

    def record(job, kws, parts, team, fb, settings, out_dir, final_name, header=None, gap=None, **extra):
        written.append(dict(parts))
        paragraphs, names = CL.assemble(parts)
        return Path(out_dir) / f"{final_name}.pdf", CL._letter_dict("ok", paragraphs=paragraphs, names=names, parts=dict(parts),
                                                                   words=CL.words(paragraphs), **extra)
    monkeypatch.setattr(CL, "write_letter", record)
    llm = model(lambda d: dict(d))                       # the draft as it is: its bullets have no plain version, so their jargon stays
    assert not any(k.term.lower() == "aws lambda" for k in w.kws)                       # a JD keyword would be allowed (the ATS reads it)
    pdf, letter = make(w, llm)
    flu = letter["retry"]["fluency"]
    assert flu["source"] == "draft" and any("jargon" in p and "AWS Lambda" in p for p in flu["problems"][0]), flu["problems"]


# ------------------------------------------------------------------ layout options
def test_low_ranked_roles_become_the_additional_experience_line(settings):
    settings.raw["cv"]["additional_experience"] = True
    fb = bank()
    kws = [Keyword("llm", 3.0, True), Keyword("aws", 2.0, True)]
    ctx = {"experience": [{**e, "bullet_ids": [e["bullets"][0]["id"]], "bullets": [e["bullets"][0]["text"]]} for e in fb["experience"]]}
    first = ctx["experience"][0]["id"]
    lines = T.additional_experience(ctx, fb, kws, settings)
    assert lines and ctx["experience"][0]["id"] == first and len(ctx["experience"]) + len(lines) == len(fb["experience"])
    assert all("(" in l and ")" in l for l in lines) and ctx["additional_experience"] == lines


def test_the_additional_experience_line_is_off_unless_you_turn_it_on(settings):
    fb = bank()
    ctx = {"experience": [{**e, "bullet_ids": [e["bullets"][0]["id"]], "bullets": [e["bullets"][0]["text"]]} for e in fb["experience"]]}
    assert T.additional_experience(ctx, fb, [Keyword("llm", 3.0, True)], settings) == [] and "additional_experience" not in ctx


GRAD = {"title": "Graduate Data Scientist (2027 start)", "description": "Our early careers programme ..."}
NOT_GRAD = {"title": "Senior Data Scientist", "description": "Five years of experience."}


def test_graduate_order_only_with_the_flag_on_and_a_graduate_ad(settings):
    assert T.section_order(GRAD, settings) is None                                   # off by default
    settings.raw["cv"]["graduate_order"] = True
    assert T.section_order(NOT_GRAD, settings) is None
    order = T.section_order(GRAD, settings)
    assert order[:6] == ["profile", "education", "experience", "projects", "skills", "certifications"] and "languages" in order


def _ctx():
    from test_cv_rules import CERTS, OPEN, ctx_with
    return render.with_public_links(ctx_with([OPEN], CERTS))


def test_the_template_follows_the_section_order_and_prints_the_additional_line():
    tpl = render.env().get_template("cv2.tex.j2")
    default = tpl.render(**_ctx())
    assert default.index(r"\section{Skills}") < default.index(r"\section{Education}")
    grad = tpl.render(**{**_ctx(), "section_order": ["profile", "education", "experience", "projects", "skills", "certifications", "languages",
                                                    "research_focus", "publications"]})
    assert grad.index(r"\section{Education}") < grad.index(r"\section{Skills}") < grad.index(r"\section{Certifications")
    extra = tpl.render(**{**_ctx(), "additional_experience": ["Software Engineer, Harbour Pay (Chennai, Jul 2023 – Aug 2024)"]})
    assert r"\textbf{Additional experience:} Software Engineer, Harbour Pay (Chennai, Jul 2023 – Aug 2024)" in extra


def test_one_line_achievements_put_certifications_first():
    tpl = render.env().get_template("cv2.tex.j2")
    ach = [{"id": "p", "name": "Lloyd's Datathon", "award": "3rd place", "text": "Long text.", "result": "", "dates": "Mar 2026"}]
    off = tpl.render(**{**_ctx(), "achievements": ach})
    on = tpl.render(**{**_ctx(), "achievements": ach, "achievements_one_line": True})
    assert "Long text." in off and "Long text." not in on
    cert_at, ach_at = on.index(r"\item", on.index(r"\section{Certifications")), on.index("Lloyd")
    assert cert_at < ach_at and "Mar 2026" in on[ach_at:ach_at + 120]


def test_doctor_shows_the_allow_list(settings):
    from careerops.setup import plain_allow_checks
    [(name, ok, _)] = plain_allow_checks(settings)
    assert ok is True and "allow-list" in name and "LangChain" in name and "evidences" in name
