"""The CV template round for config/reference/cv_style_reference.tex v2: four fixed Skills groups (label on its own line), award
projects under Certifications & Achievements, the STAR opener from `situation:`, and the length rules measured on the PDF (bullets
at most 3 lines, profile at most 7, the headline on one line). The unit tests need no xelatex; the snapshot tests render the
fictional fixture profile and read the PDF back (a missing tool makes them fail, not skip). No network, no model (MockLLM)."""
import copy
import re
from pathlib import Path

import pytest
from conftest import fixture_profile_settings

from careerops import hook as hooks
from careerops.config import load_yaml
from careerops.llm import MockLLM
from careerops.tailoring import layout, tailor, typography
from careerops.tailoring.render import achievement_head, env, page_count, pdf_text, render_cv
from careerops.tailoring.scorer import Keyword
from careerops.tailoring.skills import cv_skill_groups, skill_group_config

FIX = Path(__file__).parent.parent / "fixtures"
CFG = skill_group_config(fixture_profile_settings())
LABELS = ["Machine Learning, Deep Learning & Statistics", "Python, Data Quality & Cloud", "NLP, LLMs & Evaluation",
          "Client Delivery & Communication"]


def _bank() -> dict:
    fb = load_yaml(FIX / "profile" / "fact_bank.yaml")
    fb["education"][0]["dates"] = "Sep 2025 \u2013 Sep 2026"
    return fb


def _job() -> dict:
    return {"id": "manual:v2", "source": "manual", "company": "Northwind Health AI", "title": "AI Engineer",
            "location": "Bristol, UK", "url": "u", "apply_url": "u",
            "description": (FIX / "tailoring" / "jd_northwind.txt").read_text(encoding="utf-8")}


def _kw(*terms, weight=1.0):
    return [Keyword(t, weight, True) for t in terms]


# ------------------------------------------------------------------------------------------------ the template (no xelatex)
MINIMAL = dict(id={"name": "A B", "location": "X", "email": "a@b.c", "phone": "+44 7700 900123",
                   "links": {"linkedin": "https://www.linkedin.com/in/a-b/", "github": "https://github.com/a-b",
                             "portfolio": "https://a-b.example.org/portfolio/"}},
               profile="p", skills={"Machine Learning, Deep Learning & Statistics": ["Python"]},
               experience=[{"org": "O", "dates": "Jan 2025 - Feb 2025", "title": "t", "bullets": ["Did a thing."]}],
               projects=[{"name": "P", "stack": "R", "github": "https://github.com/a-b/p", "bullets": ["Built it."]}],
               education=[], certifications=[])


def test_the_header_has_a_portfolio_label_short_link_forms_and_a_headline_line():
    body = env().get_template("cv2.tex.j2").render(**{**MINIMAL, "hook_headline": "Data Scientist \u00b7 ~65 tests", "hook_number": "~65"}
                                                  ).split(r"\begin{document}")[1]
    assert r"{linkedin.com/in/a-b}" in body and r"{github.com/a-b}" in body                    # short display forms
    assert r"\href{https://a-b.example.org/portfolio/}{Portfolio}" in body and "example.org/portfolio/}" not in body.replace(
        r"\href{https://a-b.example.org/portfolio/}", "")                                       # the link text is only "Portfolio"
    assert r"{\fontsize{12}{14}\selectfont\color{navy} Data Scientist \u00b7 " .replace(r"\u00b7", "\u00b7") in body
    assert r"\url" not in body and r"\gh{https://github.com/a-b/p}" in body                     # project links are \gh, never \url


def test_a_skill_group_puts_its_label_on_a_line_of_its_own():
    tex = env().get_template("cv2.tex.j2").render(**MINIMAL)
    assert r"\newcommand{\skillgroup}[2]{{\color{navy}\textbf{#1}}\\ #2\par\vspace{3pt}}" in tex
    assert r"\skillgroup{Machine Learning, Deep Learning \& Statistics}{Python}" in tex and r"\skillrow" not in tex


def test_an_achievement_is_a_bold_lead_then_the_bullet_with_its_result_in_num():
    ctx = {**MINIMAL, "certifications": ["AWS Academy Cloud Foundations, Nov 2025"],
           "achievements": [{"id": "l", "name": "Lloyd's Banking Group Datathon", "award": "3rd place", "bullet_id": "l1",
                             "text": "Detected outliers under 200:1 class imbalance in MLflow.", "result": "200:1 class imbalance"}]}
    body = env().get_template("cv2.tex.j2").render(**ctx).split(r"\begin{document}")[1]
    tail = body.split(r"\section{Certifications \& Achievements}")[1]
    items = re.findall(r"\\item (.*)", tail)
    assert items[0] == (r"\textbf{Lloyd\textquotesingle{}s Banking Group Datathon, 3rd place}: Detected outliers under "
                        r"\num{200:1 class imbalance} in MLflow.")
    assert items[1].startswith("AWS Academy Cloud Foundations") and len(items) == 2


def test_the_award_is_not_said_twice_in_an_achievement_lead():
    assert achievement_head({"name": "Lloyd's Datathon, 3rd place", "award": "3rd place"}) == r"Lloyd\textquotesingle{}s Datathon, 3rd place"
    assert achievement_head({"name": "Datathon", "award": "3rd place"}) == "Datathon, 3rd place"
    assert achievement_head({"name": "Datathon"}) == "Datathon"


# --------------------------------------------------------------------------------------------------------- the STAR opener
def _entry(**kw):
    return {"id": "e", "bullet_ids": ["b1", "b2"], "bullets": ["Built a tool calling agent.", "Deployed it on AWS."], **kw}


def test_an_entry_with_a_situation_opens_its_first_bullet_with_it_exactly_once():
    ctx = {"experience": [_entry(situation="Needed an agent a team could trust")], "projects": []}
    out = layout.with_openers(ctx)
    assert out["experience"][0]["bullets"] == ["Needed an agent a team could trust: built a tool calling agent.", "Deployed it on AWS."]
    again = layout.with_openers(out)                                            # a second pass does not repeat it
    assert again["experience"][0]["bullets"][0].count("Needed an agent") == 1
    assert ctx["experience"][0]["bullets"][0] == "Built a tool calling agent."  # the context the checks saw is untouched


def test_an_entry_without_a_situation_gets_no_opener():
    ctx = {"experience": [_entry(), _entry(situation="  ")], "projects": [_entry(id="p")]}
    out = layout.with_openers(ctx)
    assert [e["bullets"] for e in out["experience"] + out["projects"]] == [_entry()["bullets"]] * 3


def test_the_opener_keeps_an_acronym_and_a_project_gets_one_too():
    ctx = {"experience": [], "projects": [_entry(situation="Needed forecasts validated on time ordered data.", bullets=["LSTM trained on prices."])]}
    assert layout.with_openers(ctx)["projects"][0]["bullets"] == ["Needed forecasts validated on time ordered data: LSTM trained on prices."]


def test_the_hook_bullet_moved_to_the_front_carries_the_opener():
    ctx = {"experience": [_entry(id="az", situation="Needed trust", bullet_ids=["x1", "proof"], bullets=["Wrote docs.", "Wrote ~65 tests."])],
           "projects": []}
    hk = hooks.Hook("p", "proof", "~65", "h", "l", "s", entry_id="az")
    assert hooks.proof_first(ctx, hk)
    assert layout.with_openers(ctx)["experience"][0]["bullets"] == ["Needed trust: wrote ~65 tests.", "Wrote docs."]


def test_a_split_bullet_is_two_bullets_and_only_the_first_part_takes_the_opener():
    ctx = {"experience": [_entry(situation="Needed trust", bullets=["One sentence here today. Another sentence follows after.", "B."])],
           "projects": [], "splits": {"b1": {"text": "One sentence here today. Another sentence follows after.",
                                              "parts": ["One sentence here today.", "Another sentence follows after."]}}}
    out = layout.with_openers(layout.with_splits(ctx))["experience"][0]
    assert out["bullets"] == ["Needed trust: one sentence here today.", "Another sentence follows after.", "B."]
    assert out["bullet_ids"] == ["b1", "b1", "b2"]
    ctx["experience"][0]["bullets"][0] = "Reworded since."                       # a split of text that is no longer there is ignored
    assert layout.with_splits(ctx)["experience"][0]["bullets"][0] == "Reworded since."


# ----------------------------------------------------------------------------------------------------- split / shorten options
def test_a_bullet_is_cut_only_at_a_sentence_end_or_a_semicolon_best_balanced_first():
    text = "Built a pipeline for the tickets. Traced the low scores to repeated vocabulary; reported it to the team lead."
    opts = layout.split_options(text)
    assert len(opts) == 2 and all(a.endswith(".") and b[0].isupper() for a, b in opts)
    assert ["Built a pipeline for the tickets.", "Traced the low scores to repeated vocabulary; reported it to the team lead."] in opts
    assert ["Built a pipeline for the tickets. Traced the low scores to repeated vocabulary.", "Reported it to the team lead."] in opts
    for a, b in opts:                                                            # nothing is lost and nothing cut mid-clause
        assert re.sub(r"[.;]", "", a + " " + b).lower().split() == re.sub(r"[.;]", "", text).lower().split()
    sizes = [abs(len(a) - len(b)) for a, b in opts]
    assert sizes == sorted(sizes)                                                # the better balanced cut comes first


def test_a_bullet_with_only_commas_has_no_split_point():
    assert layout.split_options("Trained per gene models with OLS, a random forest, resumable chunks, and a GPU, then validated them.") == []
    assert layout.split_options("Cut RAM from 8.2 to 5.5 GB; ok.") == []              # a part under four words is not a bullet
    assert layout.split_options("Reached 0.807 Pearson on train and 0.545 on held out test data across the genes.") == []     # a decimal point is no sentence end


# --------------------------------------------------------------------------------------------------- headline and profile
def test_the_headline_loses_its_trailing_stack_list_first_then_clauses_and_never_the_number_before_it_must():
    h = "Data Scientist \u00b7 LLM agent evaluated by ~65 tests deployed on AWS Lambda using LangChain, Claude Haiku and AWS Bedrock"
    opts = layout.headline_options(h, "~65")
    assert opts[0] == "Data Scientist \u00b7 LLM agent evaluated by ~65 tests deployed on AWS Lambda"        # the stack list goes first
    assert all(o.startswith("Data Scientist") for o in opts) and opts[-1] == "Data Scientist"               # the role alone is the last resort
    with_number = [o for o in opts if "~65" in o]
    assert opts[:len(with_number)] == with_number                                                           # versions with the number come first
    assert len(opts) == len(set(opts)) and h not in opts


def test_a_headline_with_a_problem_label_drops_the_label_and_a_short_one_has_no_options_but_the_role():
    opts = layout.headline_options("AI Engineer \u00b7 APIs and data quality: 19,213 gene parquet fallback", "19,213")
    assert opts[0] == "AI Engineer \u00b7 19,213 gene parquet fallback" and opts[-1] == "AI Engineer"
    assert layout.headline_options("AI Engineer", "") == []


def test_the_profile_loses_whole_sentences_from_the_end_and_keeps_the_hook_sentence():
    hook = "Data scientist focused on APIs, including a 19,213 gene fallback."
    profile = hook + " Second sentence about models. Third sentence about data. Fourth one."
    assert layout.profile_options(profile, hook) == [hook + " Second sentence about models. Third sentence about data.",
                                                     hook + " Second sentence about models.", hook]
    assert layout.profile_options(hook, hook) == []


# ------------------------------------------------------------------------------------------------------------- skill groups
def _groups(fb=None, kws=None):
    return cv_skill_groups(fb or _bank(), kws or [], CFG)


def test_four_groups_in_the_reference_order_each_filled_from_the_fact_bank():
    groups, left_out, empty = _groups()
    assert list(groups) == LABELS and left_out == [] and empty == []
    assert groups["Client Delivery & Communication"] == ["stakeholder demos", "scoping with clients", "code review", "technical documentation"]
    ml, cloud, nlp = (groups[k] for k in LABELS[:3])
    assert {"scikit-learn", "XGBoost", "Isolation Forest", "bootstrap"} <= set(ml) and {"PyTorch", "Machine Learning"} <= set(ml)
    assert {"Python", "pandas", "NumPy", "Docker", "PostgreSQL", "AWS Lambda", "FastAPI"} <= set(cloud)
    assert {"LangChain", "RAG", "LLMs", "MCP", "tool calling"} <= set(nlp)


def test_the_standing_rule_skills_are_always_on_the_cv_when_the_bank_has_them():
    fb = _bank()
    fb["skills"]["core"] = ["Python", "pandas", "NumPy", "SciPy", "Matplotlib", "Seaborn", "Plotly", "TensorFlow", "Docker", "Kubernetes"]
    fb["skills"]["Languages"] += ["SciPy", "Matplotlib", "Seaborn", "Plotly", "TensorFlow", "Kubernetes"]
    fb["deny_terms"] = [d for d in fb["deny_terms"] if d != "kubernetes"]
    groups, _, _ = cv_skill_groups(fb, [], {**CFG, "skill_group_max": 2})          # a cap of two cuts nothing that is pinned
    shown = {s for items in groups.values() for s in items}
    assert {"Python", "pandas", "NumPy", "SciPy", "Matplotlib", "Seaborn", "Plotly", "TensorFlow", "Docker", "Kubernetes"} <= shown
    assert "TensorFlow" in groups[LABELS[0]] and "Kubernetes" in groups[LABELS[1]]


def test_skills_inside_a_group_are_ordered_by_the_keyword_weight_they_carry():
    kws = [Keyword("mlflow", 3.0, True), Keyword("fastapi", 2.0, True), Keyword("pytest", 1.0, True)]
    cloud = _groups(kws=kws)[0][LABELS[1]]
    assert cloud[:3] == ["MLflow", "FastAPI", "pytest"]
    assert cloud[3:] == [s for s in _groups()[0][LABELS[1]] if s not in cloud[:3]]       # the rest keep fact-bank order (core first)


def test_the_per_group_cap_cuts_the_lowest_weight_unpinned_skills_only():
    fb = _bank()
    cap = {**CFG, "skill_group_max": 10}
    cloud = cv_skill_groups(fb, [Keyword("react", 5.0, True)], cap)[0][LABELS[1]]
    full = cv_skill_groups(fb, [Keyword("react", 5.0, True)], CFG)[0][LABELS[1]]
    pinned = [s for s in full if s in fb["skills"]["core"]]
    assert len(cloud) == max(10, len(pinned)) and set(pinned) <= set(cloud) and "React" in cloud and cloud[0] == "React"


def test_a_bank_group_nobody_claims_is_left_out_and_a_group_with_no_skills_is_reported_empty():
    fb = _bank()
    fb["skills"]["Mobile & Web"] = ["SwiftUI", "Flutter"]
    del fb["skills"]["Client Delivery & Communication"]
    bare = {**CFG, "skill_groups": [{k: v for k, v in g.items() if k != "from_terms"} for g in CFG["skill_groups"]]}     # no terms fallback
    groups, left_out, empty = cv_skill_groups(fb, [], bare)
    assert left_out == ["Mobile & Web"] and empty == ["Client Delivery & Communication"] and list(groups) == LABELS[:3]
    assert "SwiftUI" not in {s for items in groups.values() for s in items}


def test_a_skill_the_bank_repeats_is_listed_once_in_the_first_group_that_takes_it():
    fb = _bank()
    groups, _, _ = _groups(fb)
    names = [s.casefold() for items in groups.values() for s in items]
    assert len(names) == len(set(names))
    assert "Python" in groups[LABELS[1]] and "Python" not in groups[LABELS[0]]


def test_a_denied_core_skill_still_stops_the_groups():
    fb = _bank()
    fb["skills"]["core"] = [*fb["skills"]["core"], "Kubernetes"]
    with pytest.raises(ValueError, match="kubernetes"):
        cv_skill_groups(fb, [], CFG)


# ---------------------------------------------------------------------------------------------------- achievements (no xelatex)
def test_an_award_project_moves_to_achievements_unless_it_is_in_the_jds_top_three_or_holds_the_hook():
    fb = _bank()
    jd = "Anomaly detection with Isolation Forest and MLflow."
    low = _kw("kubernetes-free", "graphql")
    assert tailor.achievement_projects(fb, low, jd) == ["lloyds"]                       # not in the top three: it moves
    top = _kw("anomaly detection", "isolation forest", "mlflow", weight=3.0)
    assert tailor.achievement_projects(fb, top, jd) == []                              # its keyword score puts it first: it stays a project
    hk = hooks.Hook("p", "lloyds1", "200:1", "h", "l", "s", entry_id="lloyds", entry_kind="project")
    assert tailor.achievement_projects(fb, low, jd, hk) == []                         # the hook's proof bullet is first in its project
    next(p for p in fb["projects"] if p["id"] == "coverfit")["award"] = "Winner"       # only a project with an award is ever moved
    assert tailor.achievement_projects(fb, low, jd) == ["lloyds", "coverfit"]


def test_the_achievement_line_is_the_banks_own_bullet_with_the_award_taken_out():
    fb = _bank()
    fb["projects"][2]["bullets"][0]["text"] = "Won 3rd place by building an anomaly detector under 200:1 imbalance in MLflow."
    fb["projects"][2]["bullets"][0]["result"] = "3rd place; 200:1 imbalance"
    fb["projects"][2]["award"] = "3rd place"
    [a] = tailor.achievement_entries(fb, ["lloyds"], _kw("mlflow", "anomaly detection"))
    assert a["name"] == "Banking datathon" and a["award"] == "3rd place" and a["bullet_id"] == "lloyds1"
    assert "3rd place" not in a["text"] and a["result"] == "200:1 imbalance" and "200:1 imbalance" in a["text"]


# ------------------------------------------------------------------------------------------------ length rules on real PDFs
@pytest.fixture(scope="module")
def base(tmp_path_factory):
    """A tailored fixture CV: its context, hook and keywords are the starting point of the length tests."""
    fb = _bank()
    out = tmp_path_factory.mktemp("v2")
    rep = tailor.tailor_job(_job(), fb, MockLLM(), fixture_profile_settings(), out)
    return rep, fb


def _fit(tmp_path, rep, fb, ctx, llm=None, kws=None, notes=None, hk=None):
    def render():
        pdf = render_cv(ctx, tmp_path, "cv")
        return pdf, page_count(pdf)
    kws = [Keyword(**k) for k in rep["keywords"]] if kws is None else kws
    pdf, _ = tailor.fit_lengths(ctx, render, fb, kws, _job(), llm or MockLLM(), {}, notes if notes is not None else [], hk,
                                {})
    return pdf, layout.measure(pdf)


LONG_TWO = ("Built a graph of 1,399 cell lines and 16,385 genes from fourteen public omics files with a crosswalk, median imputation and "
            "robust scoring, and explored its structure with community detection across many runs. Trained per gene regression models for "
            "eleven thousand genes with a random forest ensemble on a free GPU, in resumable chunks, and validated them on held out data.")


@pytest.mark.slow
def test_a_bullet_over_three_lines_is_split_at_a_sentence_boundary(base, tmp_path):
    rep, fb = base
    fb, ctx = copy.deepcopy(fb), copy.deepcopy(rep["context"])
    entry = ctx["experience"][1]
    bid = entry["bullet_ids"][0]
    next(b for e in fb["experience"] for b in e["bullets"] if b["id"] == bid)["text"] = LONG_TWO
    entry["bullets"][0] = LONG_TWO
    _, m = _fit(tmp_path, rep, fb, ctx, kws=[])
    assert ctx["splits"][bid]["parts"][0].endswith(".") and ctx["splits"][bid]["parts"][1][0].isupper()
    assert " ".join(ctx["splits"][bid]["parts"]).split() == LONG_TWO.split()                  # nothing dropped, nothing cut
    assert max(m.bullets) <= layout.MAX_BULLET_LINES


@pytest.mark.slow
def test_a_long_bullet_with_no_boundary_is_reworded_shorter_by_the_model_not_cut(base, tmp_path):
    rep, fb = base
    fb, ctx = copy.deepcopy(fb), copy.deepcopy(rep["context"])
    entry = ctx["experience"][1]
    bid = entry["bullet_ids"][0]
    long = ("Cleaned and joined donor, event and survey exports with pandas and SQL, documented every source and assumption in a shared "
            "handbook, reported the results to the trustees every quarter, trained the next student team, and delivered a reporting "
            "notebook that the charity reused for their quarterly review, their board papers and their yearly funding applications")
    short = ("Cleaned and joined donor, event and survey exports with pandas and SQL, trained the next student team, and delivered "
             "a reporting notebook that the charity reused.")
    next(b for e in fb["experience"] for b in e["bullets"] if b["id"] == bid)["text"] = long
    entry["bullets"][0] = long
    asked = []

    def handler(c):
        asked.append(c["bullets"])
        return {i["id"]: short for i in c["bullets"]}

    _, m = _fit(tmp_path, rep, fb, ctx, llm=MockLLM({"rephrase_bullets": handler}), kws=[])
    assert asked and asked[0][0]["shorten"] < len(long.split()) and entry["bullets"][0] == short
    assert max(m.bullets) <= layout.MAX_BULLET_LINES


@pytest.mark.slow
def test_a_bullet_that_cannot_be_split_or_shortened_is_left_alone_and_reported(base, tmp_path):
    rep, fb = base
    fb, ctx = copy.deepcopy(fb), copy.deepcopy(rep["context"])
    entry = ctx["experience"][1]
    bid = entry["bullet_ids"][0]
    long = ("Cleaned and joined donor, event and survey exports with pandas and SQL, documented every source and assumption in a shared "
            "handbook, reported the results to the trustees every quarter, trained the next student team, and delivered a reporting "
            "notebook that the charity reused for their quarterly review, their board papers and their yearly funding applications")
    next(b for e in fb["experience"] for b in e["bullets"] if b["id"] == bid)["text"] = long
    entry["bullets"][0] = long
    notes: list[str] = []
    _, m = _fit(tmp_path, rep, fb, ctx, kws=[], notes=notes)                               # MockLLM never rewords
    assert entry["bullets"][0] == long and not ctx.get("splits")                           # never truncated
    assert any(bid in n and "limit 3" in n for n in notes)


@pytest.mark.slow
def test_a_reworded_bullet_that_prints_too_long_goes_back_to_its_fact_bank_wording(base, tmp_path):
    rep, fb = base
    ctx = copy.deepcopy(rep["context"])
    entry = ctx["experience"][1]
    bid = entry["bullet_ids"][0]
    canonical = next(b for e in fb["experience"] for b in e["bullets"] if b["id"] == bid)["text"]
    entry["bullets"][0] = canonical + " " + canonical.replace("Prototyped", "Also prototyped") + " " + canonical
    _, m = _fit(tmp_path, rep, fb, ctx, kws=[])
    assert entry["bullets"][0] == canonical and max(m.bullets) <= layout.MAX_BULLET_LINES


@pytest.mark.slow
def test_a_headline_that_would_wrap_is_shortened_to_one_line_and_the_hook_follows(base, tmp_path):
    rep, fb = base
    ctx, hk = copy.deepcopy(rep["context"]), hooks.Hook.from_dict(rep["hook"])
    long = ("AI Engineer \u00b7 production LLM agent with a 19,213 gene parquet fallback behind circuit breakers on AWS Lambda "
            "using LangChain, AWS Bedrock, Docker and GitHub Actions")
    ctx["hook_headline"], ctx["hook_number"], hk.headline, hk.number = long, "19,213", long, "19,213"
    assert layout.measure(render_cv(ctx, tmp_path, "cv")).headline_lines == 2                  # it does wrap before the fit
    _, m = _fit(tmp_path, rep, fb, ctx, hk=hk)
    assert m.headline_lines == 1 and ctx["hook_headline"] == hk.headline and ctx["hook_headline"].startswith("AI Engineer")
    assert "19,213" in ctx["hook_headline"] and "LangChain" not in ctx["hook_headline"]     # the stack list went first, the number stayed


@pytest.mark.slow
def test_a_profile_over_seven_lines_loses_whole_sentences_after_the_hook_sentence(base, tmp_path):
    rep, fb = base
    ctx, hk = copy.deepcopy(rep["context"]), hooks.Hook.from_dict(rep["hook"])
    filler = " ".join(f"Sentence number {i} is about modelling, evaluation and delivering work for clients on schedule." for i in range(12))
    ctx["profile"] = hk.profile_line + " " + filler
    assert layout.measure(render_cv(ctx, tmp_path, "cv")).profile_lines > layout.MAX_PROFILE_LINES
    _, m = _fit(tmp_path, rep, fb, ctx, hk=hk)
    assert m.profile_lines <= layout.MAX_PROFILE_LINES and ctx["profile"].startswith(hk.profile_line)
    assert ctx["profile"].endswith(".") and ctx["profile"][len(hk.profile_line):].strip().split(". ")[-1].endswith("schedule.")   # whole sentences


@pytest.mark.slow
def test_the_pipeline_report_says_what_the_length_rules_did(base):
    rep, _ = base
    final = rep["lengths"]["final"]
    assert final["headline_lines"] == 1 and final["profile_lines"] <= layout.MAX_PROFILE_LINES
    assert final["longest_bullet"] <= layout.MAX_BULLET_LINES and rep["lengths"]["unfit"] == []
    assert page_count(Path(rep["cv_pdf"])) == 2


# ------------------------------------------------------------------- the CV built from tests/fixtures/profile against the reference
EXCEPTIONS = {"scikit-learn", "k-means", "tf-idf", "skip-gram", "cifar-10", "gpt-oss"}
SECTIONS = ["PROFILE", "SKILLS", "WORK EXPERIENCE", "PROJECTS", "EDUCATION", "CERTIFICATIONS & ACHIEVEMENTS"]


@pytest.mark.slow
def test_the_fixture_cv_follows_the_v2_reference(base):
    rep, fb = base
    pdf = Path(rep["cv_pdf"])
    text = pdf_text(pdf)
    tex = next(pdf.parent.glob("*CV.tex")).read_text(encoding="utf-8")
    # section order
    heads = [ln.strip() for ln in text.splitlines() if ln.strip() in SECTIONS]
    assert heads == SECTIONS
    # four skill groups, each label alone on its line with the items on the next
    assert re.findall(r"\\skillgroup\{([^{}]*)\}", tex) == [label.replace("&", r"\&") for label in LABELS]
    lines = [ln.strip() for ln in text.splitlines()]
    for label in LABELS:
        i = lines.index(label)
        assert lines[i + 1] and lines[i + 1] not in LABELS and not lines[i + 1].startswith(label)
    # no \url (and so no monospace full URL in the PDF); project links read "GitHub"
    assert r"\url" not in tex and "https://" not in text and re.search(r"^Customer churn prediction\s+GitHub$", text, re.M)
    # a one-line headline, and the Portfolio label
    assert rep["hook"]["headline"] and layout.measure(pdf).headline_lines == 1 and "Portfolio" in text and "portfolio#top" not in text
    # no hyphenated compound outside the exception list (and the URL / e-mail handles)
    prose = re.sub(r"\S*[/@]\S*", " ", text)
    allowed = EXCEPTIONS | {p.casefold() for p in typography.load_rules().protected}
    compounds = {w for w in re.findall(r"[A-Za-z0-9]+(?:-[A-Za-z0-9]+)+", prose) if w.casefold() not in allowed}
    assert compounds == set(), compounds
    # two pages, the award project under Achievements, the STAR openers once each
    assert page_count(pdf) == 2
    assert rep["achievements"] == ["lloyds"] and "Banking datathon, 3rd place, national datathon" in text.split("CERTIFICATIONS")[1]
    assert "Banking datathon" not in text.split("PROJECTS")[1].split("EDUCATION")[0]
    flat = " ".join(text.split())
    assert flat.count("Needed an LLM agent a research team could trust before it shipped: ") == 1
    assert flat.count("Needed to know which customers would leave: ") == 1
    assert "Needed" not in " ".join(text.split("Bristol Pro Bono Consulting")[1].split("PROJECTS")[0].split())      # no situation, no opener
    m = layout.measure(pdf)
    assert m.profile_lines <= layout.MAX_PROFILE_LINES and max(m.bullets) <= layout.MAX_BULLET_LINES
