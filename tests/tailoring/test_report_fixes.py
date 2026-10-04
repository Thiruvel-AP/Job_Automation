"""Fixes from the first real run (Chattermill): keyword quality, bullet ranking, rephrase coverage, profile retry,
letter rules, text normalisation and honest gaps. One section per fix."""
import copy
import re
from pathlib import Path

import pytest

from careerops.llm import MockLLM
from careerops.tailoring import budget, prompts as P, tailor
from careerops.tailoring.selection import select_cv
from careerops.tailoring.faithfulness import check
from careerops.tailoring.render import pdf_text
from careerops.tailoring.scorer import (Keyword, cap_keywords, ceiling, drop_contained, merge_keywords, score,
                                        taxonomy_keywords)
from careerops.tailoring.textnorm import normalise_json, normalise_text

FIX = Path(__file__).resolve().parents[1] / "fixtures" / "tailoring"
FEEDBACK_JD = (FIX / "jd_feedback_nlp.txt").read_text(encoding="utf-8")


def _job(job, jd):
    return {**job, "title": "Applied NLP Engineer", "company": "Reviewly", "description": jd}


def _kw_llm(required, preferred=(), hook="", team=""):
    return MockLLM({"extract_keywords": lambda ctx: {"required": list(required), "preferred": list(preferred),
                                                     "company_hook": hook, "team": team}})


# ================================================================== 1. keywords
def test_keywords_prompt_states_the_new_rules():
    s = P.KEYWORDS_SYSTEM
    for must in ("1-3 word", "NEVER a phrase containing a generic word",
                 "tasks, workflows, features, approaches, systems, frameworks, patterns,\n  skills, sets, quality, generation",
                 "Merge plurals", "Degree", "NOT keywords", '"A or B or C"', "At most 25"):
        assert must in s, must


NJD = ("we use data, tasks, skills, workflows, techniques and patterns. bachelor's degree in computer science. msc, "
       "phd in statistics, alpha beta gamma delta. x. ml systems, data quality, coding tasks, product features, "
       "text generation, feature sets, new approaches, deep learning frameworks, quality assurance.")


@pytest.mark.parametrize("raw", ["data", "tasks", "skills", "data skills", "workflows", "techniques", "patterns",
                                 "Bachelor's degree in Computer Science", "MSc", "PhD in statistics",
                                 "alpha beta gamma delta", "not in the jd at all", "x",
                                 # a generic word anywhere in the phrase disqualifies it
                                 "ML systems", "data quality", "coding tasks", "product features", "text generation",
                                 "feature sets", "new approaches", "deep learning frameworks", "quality assurance"])
def test_generic_degree_long_and_invented_keywords_are_dropped(raw):
    assert tailor.llm_keyword(raw, NJD, 2.0, True) is None


def test_generic_words_only_disqualify_the_words_the_rules_name():
    njd = "feature engineering, system design, data pipelines, framework migration, setting up, qualitative research"
    for ok in ("feature engineering", "system design", "data pipelines", "qualitative research"):
        assert tailor.llm_keyword(ok, njd, 1.0, False).term == ok


def test_a_keyword_must_be_whole_words_verbatim_in_the_jd():
    assert tailor.llm_keyword("Python", "we like pythonic code", 2.0, True) is None
    assert tailor.llm_keyword("Python", "we like python code", 2.0, True).term == "Python"


def test_a_specific_keyword_that_contains_a_generic_word_is_kept():
    assert tailor.llm_keyword("data analysis", "strong data analysis", 2.0, True).term == "data analysis"


def test_alternatives_become_one_any_of_keyword():
    njd = "proficiency in pytorch or tensorflow, or jax."
    k = tailor.llm_keyword("PyTorch or TensorFlow or JAX", njd, 2.0, True)
    assert (k.term, k.alts, k.required, k.weight) == ("PyTorch", ["TensorFlow", "JAX"], True, 2.0)
    k = tailor.llm_keyword("PyTorch, TensorFlow or JAX", njd, 1.0, False)
    assert k.members() == ["PyTorch", "TensorFlow", "JAX"]
    k = tailor.llm_keyword("PyTorch or Keras", njd, 2.0, True)                # Keras is not in the JD: dropped
    assert k.members() == ["PyTorch"]


def test_ci_cd_is_not_split_into_alternatives():
    assert tailor.llm_keyword("CI/CD", "we use ci/cd daily", 2.0, True).members() == ["CI/CD"]


def test_any_of_keyword_hits_when_any_member_appears_and_counts_once():
    k = Keyword("PyTorch", 2.0, True, ["TensorFlow"])
    assert score("Trained models in TensorFlow.", [k]).literal == 1.0
    assert score("Trained models in Keras.", [k]).literal == 0.0
    assert score("PyTorch and TensorFlow.", [k, Keyword("sql", 2.0, True)]).literal == 0.5      # one keyword, weight 2


def test_plurals_and_near_duplicates_merge_into_one_keyword():
    kws = merge_keywords([Keyword("llm", 2.0, True), Keyword("python", 2.0, True)],
                         [Keyword("LLMs", 1.0, False), Keyword("communication skills", 1.0, False),
                          Keyword("communication", 2.0, True), Keyword("large language models", 1.0, False)])
    terms = [k.term for k in kws]
    assert terms == ["llm", "python", "communication skills"] or terms == ["llm", "python", "communication"]
    llm = next(k for k in kws if k.term == "llm")
    assert set(llm.alts) == {"LLMs", "large language models"} and llm.weight == 2.0 and llm.required
    comm = kws[-1]
    assert comm.weight == 2.0 and comm.required and len(kws) == 3


def test_taxonomy_keywords_merge_wording_variants():
    kws = taxonomy_keywords("Requirements\nExperience with LLMs and large language models, plus NLP and natural "
                            "language processing.\n")
    terms = {k.term for k in kws}
    assert len([t for t in terms if t in ("llm", "llms", "large language models")]) == 1
    assert len([t for t in terms if t in ("nlp", "natural language processing")]) == 1


def test_an_any_of_group_absorbs_the_taxonomy_keywords_it_names():
    tax = taxonomy_keywords("Requirements\nProficiency in PyTorch or TensorFlow.\n")
    assert {k.term for k in tax} == {"pytorch", "tensorflow"}
    merged = merge_keywords(tax, [Keyword("PyTorch", 2.0, True, ["TensorFlow"])])
    assert len(merged) == 1 and merged[0].members()[0].lower() == "pytorch" and merged[0].required


def test_analyse_keeps_at_most_25_keywords_and_none_of_the_junk(settings, job):
    jd = job["description"] + "\nWe value tasks and data.\nBachelor's degree in Computer Science.\n"
    verbatim = [f"alpha{i}" for i in range(40)]
    j = {**job, "description": jd + " ".join(verbatim)}
    llm = _kw_llm(["data", "tasks", "Bachelor's degree in Computer Science"] + verbatim[:30], ["workflows"])
    notes: list[str] = []
    kws, _, _ = tailor.analyse(j, llm, settings, notes)
    assert len(kws) == settings["scoring"]["max_keywords"] == 25            # the total, taxonomy and model together
    assert not {"data", "tasks", "workflows"} & {k.term.lower() for k in kws}
    assert not any("degree" in k.term.lower() for k in kws)
    assert any(n.startswith("keywords: capped at 25; dropped ") for n in notes)


def _kw(term, weight=1.0, required=False, alts=()):
    return Keyword(term, weight, required, list(alts))


def test_a_longer_keyword_is_merged_into_the_shorter_only_when_the_extra_words_are_generic_heads():
    kws, notes = drop_contained([_kw("customer feedback systems", 1.0), _kw("customer feedback", 2.0, True),
                                 _kw("python"), _kw("llm agents"), _kw("LLM", 1.0), _kw("pipelines")])
    assert [k.term for k in kws] == ["customer feedback", "python", "llm agents", "LLM", "pipelines"]   # input order kept
    cf = kws[0]
    assert cf.weight == 2.0 and cf.required                                  # the heavier weight survives the merge
    assert notes == ["dropped 'customer feedback systems' (contains 'customer feedback' plus only generic head words)"]


def test_hypothesis_testing_and_statistical_modelling_survive_next_to_the_shorter_terms():
    kws, notes = drop_contained([_kw("hypothesis testing", 2.0), _kw("testing", 1.0), _kw("statistical modelling", 2.0),
                                 _kw("modelling", 1.0), _kw("customer feedback analysis"), _kw("customer feedback")])
    assert {k.term for k in kws} == {"hypothesis testing", "testing", "statistical modelling", "modelling",
                                     "customer feedback analysis", "customer feedback"} and notes == []


def test_containment_is_whole_words_after_stemming_and_leaves_any_of_keywords_alone():
    kws, _ = drop_contained([_kw("learning"), _kw("learning systems"), _kw("sql"), _kw("sqlalchemy"), _kw("model"),
                             _kw("models workflows"), _kw("PyTorch", alts=["TensorFlow"]), _kw("PyTorch lightning"),
                             _kw("PyTorch frameworks")])
    # "sqlalchemy" is not "sql" (whole words); "models workflows" folds into "model" after stemming; "PyTorch
    # frameworks" folds into the any-of keyword, but "PyTorch lightning" (a real extra word) stays
    assert [k.term for k in kws] == ["learning", "sql", "sqlalchemy", "model", "PyTorch", "PyTorch lightning"]


def test_the_cap_keeps_the_heaviest_then_the_most_frequently_said():
    jd = "sql sql sql python python rust\nairflow go"
    kws = [_kw("rust", 1.0), _kw("python", 1.0), _kw("sql", 1.0), _kw("airflow", 2.0, True), _kw("go", 1.0)]
    out, notes = cap_keywords(kws, jd, 3)
    assert [k.term for k in out] == ["airflow", "sql", "python"]             # weight 2 first, then 3 mentions, then 2
    assert notes == ["capped at 3; dropped go, rust"]                        # a full tie: the later in the alphabet goes
    assert cap_keywords(kws, jd, 10)[1] == []                                # nothing cut, nothing to say
    tie, _ = cap_keywords([_kw("b1"), _kw("a1")], "a1 b1", 2)
    assert [k.term for k in tie] == ["a1", "b1"]                             # full ties are ordered by term, not by input


def test_analyse_merges_containment_duplicates_across_model_and_taxonomy(settings, job):
    j = _job(job, "Requirements\nExperience with customer feedback pipelines and customer feedback analysis. LLM agents "
                  "in Python; LLM.\n")
    kws, _, notes = tailor.analyse(j, _kw_llm(["customer feedback", "customer feedback pipelines",
                                               "customer feedback analysis", "LLM agents"]), settings)
    terms = [k.term for k in kws]
    assert "customer feedback" in terms and "customer feedback pipelines" not in terms     # a generic head: folded
    assert "customer feedback analysis" in terms                                            # a real extra word: kept
    assert any("LLM agents" in k.members() for k in kws)                                    # (merged with "agents")
    assert any(k.term.lower() == "llm" for k in kws)


def test_analyse_normalises_hyphens_before_the_verbatim_check(settings, job):
    j = _job(job, "Requirements\nWe build things end-to-end.\n")
    kws, _, _ = tailor.analyse(j, _kw_llm(["end‑to‑end"]), settings)
    assert "end-to-end" in [k.term for k in kws]


def test_a_keyword_group_member_must_still_be_evidenced_to_be_used(fb_fixture):
    kw = [Keyword("PyTorch", 2.0, True, ["TensorFlow"])]
    ok = check("Trained a PyTorch model.", "Trained a PyTorch model.", ["pytorch"], [], kw)
    bad = check("Trained a TensorFlow model.", "Trained a PyTorch model.", ["pytorch"], [], kw)
    assert ok.ok and not bad.ok and "TensorFlow" in bad.violations[0]


# ================================================================== 2. bullet ranking
def _with_iata(fb):
    fb = copy.deepcopy(fb)
    fb["projects"].append({"id": "iata", "name": "Customer-feedback text analytics (MSc team project)",
                           "stack": "Python, scikit-learn, SBERT, K-Means, DBSCAN", "github": None, "bullets": [
        {"id": "p_ia1", "text": "Contributed to a team project clustering airline customer feedback with SBERT "
                                "embeddings and K-Means, DBSCAN and HAC, comparing cluster quality.",
         "terms": ["nlp", "text analytics", "customer feedback", "clustering", "sbert", "embeddings", "k-means",
                   "dbscan", "scikit-learn", "python"]}]})
    return fb


FEEDBACK_KWS = ["customer feedback", "text analytics", "topic clustering", "sentiment", "SBERT", "K-Means", "DBSCAN",
                "NLP", "PyTorch or TensorFlow", "scikit-learn"]


def _feedback_kws(settings, job, fb=None):
    kws, hook, team = tailor.analyse(_job(job, FEEDBACK_JD), _kw_llm(FEEDBACK_KWS[:8], FEEDBACK_KWS[8:]), settings)
    return kws


def test_iata_ranks_in_the_top_3_projects_for_a_customer_feedback_nlp_jd(settings, job, fb_fixture):
    fb = _with_iata(fb_fixture)
    kws = _feedback_kws(settings, job)
    ranking = budget.rank_projects(fb, kws, preferred=["churn", "p1", "p2", "p3"])     # the family prior favours others
    assert "iata" in ranking[:3] and ranking[0] == "iata"


def test_the_iata_project_is_chosen_first_in_the_code_selection_despite_the_family_prior(settings, job, fb_fixture):
    fb = _with_iata(fb_fixture)
    kws = _feedback_kws(settings, job)
    sel = select_cv(fb, kws, ["p1", "p2", "p3"])
    assert sel["projects"][0] == "iata" and len(sel["projects"]) == 2


# (3. rephrase coverage -> test_rephrase.py, 4. profile -> test_profile_source.py)

# ================================================================== 5. letter
EV_TEAM = "Contributed to a team of five clustering airline customer feedback with SBERT embeddings and K-Means."
EV_TESTS = "Wrote a three-tier test framework of ~65 tests across 8 files validated across the provider migration."


def test_letter_prompt_states_the_new_rules():
    s = P.LETTER_SYSTEM
    for must in ("Write NO closing call to action", "NEVER upgrade the candidate's role", "contributed, shipped, supported or team",
                 "~65 tests", "scalable, robust, seamless or cutting-edge"):
        assert must in s, must


def test_the_last_paragraph_must_be_a_question():
    assert _style(["Hook?", "Example.", "I look forward to a call."]) == ["last paragraph is not a call-to-action question"]
    assert _style(["Hook.", "Example.", "Could we talk for twenty minutes?"]) == []


def _style(paras, evidence=("Built a pipeline.",)):
    return tailor._style_problems(paras, list(evidence))


def test_a_letter_may_not_promote_a_contributed_team_bullet_to_built_or_led():
    q = "Could we talk?"
    bad = tailor._style_problems(["I built the customer feedback clustering with SBERT embeddings and K-Means.", q],
                                 [EV_TEAM])
    assert bad and "upgrades the role: 'built'" in bad[0] and "contributed" in bad[0].lower()
    assert tailor._style_problems(["I led the SBERT embeddings and K-Means customer feedback clustering.", q],
                                  [EV_TEAM])
    ok = tailor._style_problems(["I contributed to a team of five clustering customer feedback with SBERT embeddings "
                                 "and K-Means.", q], [EV_TEAM])
    assert ok == []


def test_the_role_check_ignores_sentences_about_other_evidence_and_verbs_the_evidence_uses():
    q = "Could we talk?"
    other = ["I built a test framework with about 65 tests across 8 files.", q]
    assert tailor._style_problems(other, [EV_TEAM, EV_TESTS]) == []
    ev = "Built the SBERT embeddings clustering of customer feedback as part of a team of five."
    assert tailor._style_problems(["I built the SBERT embeddings clustering of customer feedback.", q], [ev]) == []


@pytest.mark.parametrize("sentence, flagged", [("I wrote about 65 tests across 8 files.", False),
                                               ("I wrote ~65 tests across 8 files.", False),
                                               ("I wrote roughly 65 tests across 8 files.", False),
                                               ("I wrote 65 tests across 8 files.", True),
                                               ("I wrote exactly 65 tests across 8 files.", True)])
def test_approximations_stay_approximate(sentence, flagged):
    problems = tailor._style_problems([sentence, "Could we talk?"], [EV_TESTS])
    assert bool(problems) is flagged
    if flagged:
        assert problems == ["drops the approximation '~65'"]


@pytest.mark.parametrize("word", ["scalable", "robust", "seamless", "cutting-edge"])
def test_vague_filler_is_banned_unless_the_evidence_uses_it(word):
    para = [f"I built {word} systems.", "Could we talk?"]
    assert tailor._style_problems(para, ["Built a pipeline."]) == [f"vague filler '{word}' is not in the evidence"]
    assert tailor._style_problems(para, [f"Built a {word} pipeline."]) == []


def test_letter_evidence_is_the_two_best_cv_bullets_from_different_entries(fb_fixture):
    kws = [Keyword("langchain", 2.0, True), Keyword("docker", 2.0, True)]
    bidx = tailor.bullet_index(fb_fixture)
    ev = tailor.letter_evidence(fb_fixture, kws, {"a1", "a3", "b1"})     # a1 and a3 tie (same entry); b1 is the other entry
    assert [e["id"] for e in ev] == ["a1", "b1"]
    assert ev[0] == {"id": "a1", "text": bidx["a1"]["text"], "terms": bidx["a1"]["terms"]}   # canonical bank text
    same = tailor.letter_evidence(fb_fixture, kws, {"a1", "a3"})            # nothing from another entry: take the next best
    assert [e["id"] for e in same] == ["a1", "a3"]
    assert [e["id"] for e in tailor.letter_evidence(fb_fixture, kws, {"a3"})] == ["a3"]


def test_letter_evidence_only_comes_from_the_bullets_on_the_cv(fb_fixture):
    kws = [Keyword("langchain", 2.0, True)]                                # a1 is the best bullet in the whole bank
    ids = [e["id"] for e in tailor.letter_evidence(fb_fixture, kws, {"b1", "b2", "c1"})]
    assert "a1" not in ids and len(ids) == 2 and set(ids) <= {"b1", "b2", "c1"}


def test_the_letter_prompt_gets_exactly_those_two_bullets_the_hook_and_nothing_else(settings, fb_fixture, job, spy_llm):
    llm = spy_llm()
    rep = tailor.tailor_job(job, fb_fixture, llm, settings, settings.path("outputs"))
    call = [s for s in llm.sent if s["task"] == "cover_letter"][-1]
    bidx = tailor.bullet_index(fb_fixture)
    ev = rep["letter"]["evidence"]
    cv_ids = {i for e in rep["context"]["experience"] + rep["context"]["projects"] for i in e["bullet_ids"]}
    assert len(ev) == 2 and {e["id"] for e in ev} <= cv_ids
    assert call["context"]["evidence"] == [bidx[e["id"]]["text"] for e in ev]
    assert "JD KEYWORDS" not in call["user"] and "COMPANY HOOK" in call["user"] and "GAPS" in call["user"]
    for e in ev:
        assert e["text"] in call["user"]


def _letter_llm(*drafts):
    drafts = list(drafts)
    return lambda ctx: {"paragraphs": drafts.pop(0) if len(drafts) > 1 else drafts[0]}


GOOD = ["Reviewly turns feedback into themes, which is the work I do.", "I built a tool-calling agent.",
        "Could we talk for twenty minutes about it?"]
NO_QUESTION = ["Reviewly turns feedback into themes, which is the work I do.", "I built a tool-calling agent.",
               "I would welcome a call to discuss it."]


def test_the_closing_question_is_written_by_code_so_a_draft_without_one_is_accepted(settings, fb_fixture, job, spy_llm):
    llm = spy_llm({"cover_letter": _letter_llm(NO_QUESTION)})
    rep = tailor.tailor_job(job, fb_fixture, llm, settings, settings.path("outputs"))
    assert len([s for s in llm.sent if s["task"] == "cover_letter"]) == 1           # nothing to retry
    assert "closing" not in P.LETTER_USER.lower() and "closing question" in P.LETTER_SYSTEM
    assert rep["letter"]["status"] == "ok"
    assert rep["letter"]["paragraphs"] == NO_QUESTION + [tailor.letter_closing(settings, job["company"])]
    assert rep["letter"]["paragraphs"][-1] == ("Could we find twenty minutes to talk about where I could contribute "
                                               "first at Northwind Health AI?")


def test_a_question_the_model_wrote_is_replaced_by_the_template_closing_naming_the_team(settings, fb_fixture, job,
                                                                                        spy_llm):
    jd = job["description"] + "\nYou will join our AI Accelerator group.\n"
    llm = spy_llm({"cover_letter": _letter_llm(GOOD),
                   "extract_keywords": lambda ctx: {"required": [], "preferred": [], "company_hook": "Reviewly",
                                                    "team": "AI Accelerator"}})
    rep = tailor.tailor_job({**job, "description": jd}, fb_fixture, llm, settings, settings.path("outputs"))
    assert rep["letter"]["paragraphs"][:-1] == GOOD[:-1] and GOOD[-1] not in rep["letter"]["paragraphs"]
    assert rep["letter"]["paragraphs"][-1] == ("Could we find twenty minutes to talk about where I could contribute first "
                                               "on the AI Accelerator team at Northwind Health AI?")
    assert "cover_letter: dropped the model's own closing question; the template closing is used" in rep["letter"]["notes"]


def test_the_fallback_letter_ends_on_the_same_template_closing(settings, fb_fixture, job, spy_llm):
    lying = ["Reviewly turns feedback into themes.", "I cut latency by 9876 percent across the platform."]
    rep = tailor.tailor_job(job, fb_fixture, spy_llm({"cover_letter": _letter_llm(lying)}), settings,
                            settings.path("outputs"))
    assert rep["letter"]["status"] == "fallback"
    assert rep["letter"]["paragraphs"][-1] == tailor.letter_closing(settings, job["company"])


# ================================================================== 6. text normalisation
def test_normalise_text_maps_the_typographic_characters_and_keeps_en_dashes():
    assert normalise_text("end‑to‑end and non‐linear") == "end-to-end and non-linear"
    assert normalise_text("‘quote’ “double” it’s") == "'quote' \"double\" it's"
    assert normalise_text("a b c") == "a b c"
    assert normalise_text("Jan 2024 – Mar 2025") == "Jan 2024 – Mar 2025"


def test_normalise_json_reaches_every_string():
    out = normalise_json({"a": ["x‑y", {"b": "“hi”"}], "n": 3, "t": True, "k‑ey": "v"})
    assert out == {"a": ["x-y", {"b": '"hi"'}], "n": 3, "t": True, "k‑ey": "v"}


def test_llm_text_is_normalised_before_scoring_and_rendering(settings, fb_fixture, job):
    dirty = "Data Scientist with LLM agent work, it’s real work."

    llm = MockLLM({"tailor_profile": lambda ctx: {"facts": ["F1", "F2"], "profile": dirty},
                   "extract_keywords": lambda ctx: {"required": [], "preferred": [], "team": "",
                                                    "company_hook": "Northwind’s tools help “oncology” teams."},
                   "cover_letter": lambda ctx: {"paragraphs": ["It’s the work I do.", "I built a non‑linear agent.",
                                                                "Could we talk briefly?"]}})
    rep = tailor.tailor_job(job, fb_fixture, llm, settings, settings.path("outputs"))
    assert rep["context"]["profile"] == "Data Scientist with LLM agent work, it's real work."
    assert rep["company_hook"] == 'Northwind\'s tools help "oncology" teams.'
    assert rep["letter"]["paragraphs"][0] == "It's the work I do." and " " not in rep["letter"]["paragraphs"][2]
    text = pdf_text(Path(rep["cv_pdf"])) + pdf_text(Path(rep["letter_pdf"]))
    assert "non-linear agent" in text and not re.search("[‐‑‘’“” ]", text)


# ================================================================== 7. genuine gaps
def _fact_text_with_terms(fb, terms):
    fb = copy.deepcopy(fb)
    fb["experience"][0]["bullets"][0]["terms"] = list(fb["experience"][0]["bullets"][0]["terms"]) + terms
    return tailor.fact_text(fb)


def test_parameter_efficient_is_not_a_gap_when_a_bullets_terms_say_peft_or_lora(fb_fixture):
    kw = [Keyword("parameter-efficient", 1.0, False)]
    for terms in (["peft"], ["lora"], ["PEFT", "LoRA"]):
        c, gaps = ceiling(_fact_text_with_terms(fb_fixture, terms), kw)
        assert gaps == [] and c == 1.0, terms
    c, gaps = ceiling(_fact_text_with_terms(fb_fixture, []), kw)
    assert gaps == ["parameter-efficient"] and c == 0.0                    # no evidence anywhere: a genuine gap


def test_any_of_alternatives_are_not_gaps_when_one_is_evidenced(fb_fixture):
    kw = [Keyword("TensorFlow", 2.0, True, ["PyTorch"])]                   # the fact bank has PyTorch only
    assert ceiling(tailor.fact_text(fb_fixture), kw) == (1.0, [])
    assert ceiling(tailor.fact_text(fb_fixture), [Keyword("JAX", 2.0, True, ["Keras"])])[1] == ["JAX"]


def test_stemming_still_applies_to_gaps(fb_fixture):
    _, gaps = ceiling(tailor.fact_text(fb_fixture), [Keyword("forecasts", 1.0, False)])
    assert gaps == []                                                       # "forecasting" is in the bank


def test_the_synonym_only_counts_for_gaps_not_for_the_ats_score_of_the_cv():
    kw = [Keyword("parameter-efficient", 1.0, False)]
    assert score("Fine-tuned with LoRA.", kw).literal == 0.0                # an ATS looks for the JD's word
    assert score("Fine-tuned with LoRA.", kw, synonyms=True).literal == 1.0


def test_the_report_leaves_parameter_efficient_out_of_genuine_gaps(settings, fb_fixture, job):
    """The Chattermill case: the JD says "parameter-efficient techniques such as LoRA"; a bullet's terms say peft/lora."""
    jd = job["description"] + "\nExposure to parameter-efficient techniques such as LoRA is a plus.\n"
    llm = _kw_llm(["Python", "parameter-efficient"])      # required, so the 25-keyword cap keeps it
    fb = copy.deepcopy(fb_fixture)
    fb["experience"][0]["bullets"][0]["terms"] += ["peft", "lora"]
    rep = tailor.tailor_job({**job, "description": jd}, fb, llm, settings, settings.path("outputs"))
    kw = next(k for k in rep["keywords"] if "parameter-efficient" in [k["term"], *k["alts"]])   # merged with LoRA
    assert {"lora", "parameter-efficient"} <= {w.lower() for w in [kw["term"], *kw["alts"]]}
    assert not {"lora", "parameter-efficient"} & {g.lower() for g in rep["genuine_gaps"]}
    control = tailor.tailor_job({**job, "id": "manual:control", "description": jd}, fb_fixture, llm, settings,
                                settings.path("outputs"))                   # no peft/lora in this bank: a real gap
    assert {"lora", "parameter-efficient"} & {g.lower() for g in control["genuine_gaps"]}
