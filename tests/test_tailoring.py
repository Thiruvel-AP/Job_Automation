"""Tailoring side: scorer, guard, LLM JSON parsing, the full loop with MockLLM, review, export, apply."""
import json
from pathlib import Path

import pytest
import yaml

from careerops import review
from careerops.config import load_yaml
from careerops.db import DB
from careerops.export import export
from careerops.faithfulness import check
from careerops.llm import MockLLM, extract_json
from careerops.rewards import band_reward, faithfulness_reward
from careerops.scorer import Keyword, ceiling, norm_stem, score, taxonomy_keywords
from careerops.tailor import fact_text, tailor_job

FIX = Path(__file__).parent / "fixtures"


# ---------------------------------------------------------------- scorer
def test_literal_vs_stemmed_and_linewrap():
    kws = [Keyword("communicate", 1, True), Keyword("end-to-end", 1, True), Keyword("scalable", 2, True),
           Keyword("Master's degree", 1, True)]
    text = "Communicating clearly. Built end to end systems that are scal-\nable. Master’s degree."
    s = score(text, kws)
    assert "scalable" in s.hits                     # line-wrap hyphen is an artifact, not a miss
    assert "Master's degree" in s.hits              # curly apostrophe normalised
    assert set(s.wording_misses) == {"communicate", "end-to-end"}
    assert s.literal == pytest.approx(3 / 5) and s.stemmed == 1.0


def test_british_american():
    assert norm_stem("containerised") == norm_stem("containerized")
    assert norm_stem("modelling behaviour") == norm_stem("modeling behavior")


def test_taxonomy_weights_by_section(jd):
    kws = {k.term.lower(): k for k in taxonomy_keywords(jd)}
    assert kws["python"].weight == 2.0 and kws["python"].required
    assert kws["kubernetes"].weight == 1.0 and not kws["kubernetes"].required


def test_ceiling_reports_genuine_gaps(jd):
    fb = load_yaml(Path("config/profile/fact_bank.yaml"))
    c, gaps = ceiling(fact_text(fb), taxonomy_keywords(jd))
    assert "kubernetes" in [g.lower() for g in gaps]     # confirmed-absent → honest gap
    assert 0.7 < c < 1.0


# ---------------------------------------------------------------- guard
KW = [Keyword("Kubernetes", 1, False), Keyword("docker", 2, True), Keyword("mlflow", 1, False)]
SRC = "Deployed the agent as a Docker container on AWS Lambda with ECR image pipelines."


def test_guard_accepts_faithful_rephrase():
    assert check("Containerised and deployed the agent with Docker on AWS Lambda via ECR pipelines.",
                 SRC, ["containerised"], ["kubernetes"], KW).ok


@pytest.mark.parametrize("bad, why", [
    ("Deployed the agent with Docker and Kubernetes on AWS Lambda.", "deny"),
    ("Deployed the agent with Docker on AWS Lambda, cutting latency 40%.", "numbers"),
    ("Deployed the agent with Docker on AWS Lambda, tracked in MLflow.", "keyword"),
])
def test_guard_rejects_fabrication(bad, why):
    c = check(bad, SRC, [], ["kubernetes"], KW)
    assert not c.ok and any(why in v for v in c.violations), c.violations


def test_extract_json_variants():
    assert extract_json('```json\n{"a": 1}\n```') == {"a": 1}
    assert extract_json('<think>hmm {x}</think> Sure: {"b": [1,2]} done') == {"b": [1, 2]}


def test_rewards():
    kws = [{"term": "python", "weight": 1, "required": True}, {"term": "sql", "weight": 1, "required": True},
           {"term": "docker", "weight": 1, "required": True}, {"term": "aws", "weight": 1, "required": True}]
    r = band_reward(["python sql docker", "python sql docker aws", "nothing"], [kws] * 3)
    assert r[0] == 1.0 and r[1] < 1.0 and r[2] < r[1]          # 75% in band; 100% stuffed; 0% worst
    f = faithfulness_reward(["Used Docker on AWS.", "Used Kubernetes."], ["Used Docker on AWS."] * 2,
                            [[], []], [["kubernetes"]] * 2, [kws] * 2)
    assert f[0] > 0 > f[1]


# ---------------------------------------------------------------- full loop
def _fabricating_llm():
    """A model that tries to lie: invents a number, a deny-listed tool and an unevidenced keyword."""
    def tailor(ctx):
        out = MockLLM().json("tailor_cv", "", "", ctx)
        out["experience"]["az"] = ["az1", "az3", "az2"]          # make sure the lies are on the page
        out["rephrase"] = {
            "az3": "Deployed the agent with Docker and Kubernetes on AWS Lambda, cutting cost 40%.",
            "az1": "Built a tool-calling LLM agent (LangChain, AWS Bedrock) with agentic workflows for a "
                   "multi-omics cancer research platform with three tools.",
        }
        out["profile"] = "AI engineer with 5 years of production experience in Kubernetes."
        return out

    def keywords(ctx):
        return {"required": ["Python", "LLM agents", "made-up keyword not in JD"], "preferred": ["Kubernetes"],
                "company_hook": "Northwind builds clinical decision-support tools for oncology researchers."}

    return MockLLM({"tailor_cv": tailor, "extract_keywords": keywords})


@pytest.fixture
def tailored(settings, db, job, tmp_path):
    fb = load_yaml(settings.path("fact_bank"))
    db.upsert_job(job)
    llm = _fabricating_llm()
    rep = tailor_job(job, fb, llm, settings, settings.path("outputs"))
    f = rep["final"]
    db.update_job(job["id"], status="needs_review", keywords=rep["keywords"], ceiling=rep["ceiling"],
                  score=f["score"], score_stemmed=f["score_stemmed"], letter_score=rep["letter"]["score"],
                  attempts=len(rep["attempts"]), out_dir=rep["out_dir"])
    return rep, fb, llm


def test_tailor_loop_end_to_end(tailored):
    rep, fb, llm = tailored
    assert Path(rep["cv_pdf"]).exists() and Path(rep["letter_pdf"]).exists()
    assert rep["final"]["pages"] <= 2
    terms = [k["term"] for k in rep["keywords"]]
    assert "made-up keyword not in JD" not in terms            # verbatim filter
    viol = " ".join(v for a in rep["attempts"] for v in a["violations"])
    assert "kubernetes" in viol.lower() and "40%" in viol      # guard fired
    # the lie never reached the PDF
    from careerops.render import pdf_text
    text = pdf_text(Path(rep["cv_pdf"])).lower()
    assert "kubernetes" not in text and "40%" not in text and "5 years" not in text
    assert "agentic" in text                                   # evidenced rephrase (az1 terms) survived
    assert rep["ceiling"] < 1.0 and any(g.lower() == "kubernetes" for g in rep["genuine_gaps"])
    assert llm.calls.count("tailor_cv") == len(rep["attempts"]) <= 3


def test_letter_rules(tailored):
    rep, _, _ = tailored
    text = " ".join(rep["letter"]["paragraphs"])
    assert len(text.split()) <= 250
    assert not text.lower().startswith("i am writing")
    assert "look forward to hearing" not in text.lower()


# ---------------------------------------------------------------- review + export
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
    from openpyxl import load_workbook
    ws = load_workbook(settings.path("excel"))["Jobs"]
    assert n == 1 and ws["A2"].value == "Northwind Health AI" and ws["D2"].value == "approved"


# ---------------------------------------------------------------- apply assist (headless)
def test_fill_page_never_submits(tmp_path):
    pw = pytest.importorskip("playwright.sync_api")
    from careerops.apply import fill_page
    applicant = yaml.safe_load(Path("config/profile/applicant.yaml").read_text())
    applicant.update(last_name="Tester", email="t@example.com")
    cv = tmp_path / "CV.pdf"; cv.write_bytes(b"%PDF-1.4\n%%EOF")
    cl = tmp_path / "CL.pdf"; cl.write_bytes(b"%PDF-1.4\n%%EOF")
    try:
        with pw.sync_playwright() as p:
            browser = p.chromium.launch()
            page = browser.new_page()
            page.goto((FIX / "form.html").resolve().as_uri())
            r = fill_page(page, applicant, str(cv), str(cl))
            assert page.input_value("#first_name") == "Thiruvel"
            assert page.input_value("#email") == "t@example.com"
            assert page.input_value("#salary") == ""                       # never_fill
            assert any("Cover" in u for u in r["uploaded"]) and any("Resume" in u for u in r["uploaded"])
            assert any("sponsorship" in m for m in r["manual"])
            assert page.evaluate("document.body.dataset.submitted") is None  # not submitted
            browser.close()
    except Exception as e:  # browser binaries missing in this environment
        if "Executable doesn't exist" in str(e):
            pytest.skip("playwright chromium not installed")
        raise
