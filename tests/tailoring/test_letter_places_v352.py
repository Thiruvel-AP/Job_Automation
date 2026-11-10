"""v3.5.2 (10 Nov 2026, the 9 Nov 17:31 RightShip letter): one paragraph per employer or project, every sentence names where the work was done (with
the dissertation / hackathon context the fact bank gives it), at most two claims a paragraph, the plain-language jargon rule (no Ensembl, HGNC,
parquet unless the ad names them), P5 ties the ad's own sentence about its customers to one fact of this CV, and "Hi RightShip team" unless the ad
names a team. MockLLM and fixtures only."""
import pytest

from careerops.tailoring import assembled_letter as AL
from careerops.tailoring import letters as L
from careerops.tailoring.grounding import sentences

FB = {"experience": [
    {"id": "az", "org": "AstraZeneca", "title": "Data Scientist, MSc Dissertation Project", "employment_type": "indirect contract (MSc Dissertation)",
     "bullets": [{"id": "az2", "text": "Designed fail-safe data resolution via live Ensembl/HGNC APIs with a local 19,213-gene parquet fallback and circuit breakers."},
                 {"id": "az4", "text": "Wrote a three-tier test framework (tool-level, API/integration, live agent) of ~65 tests across 8 files, validated across "
                                       "the LLM provider migration."}]},
    {"id": "dc180", "org": "180 Degrees Consulting Bristol", "title": "Data Consultant", "employment_type": "internship",
     "bullets": [{"id": "dc8", "text": "Contributed to the client's production MCP server, now in live use."}]}],
    "projects": [{"id": "coverfit", "name": "CoverFit — insurance benchmarking API (Bristol × UWE AI Hackathon)",
                  "bullets": [{"id": "p_cf1", "text": "Paired a deterministic matching engine with one LLM call, answering in under 3 seconds."}]}],
    "skills": {"core": ["Python", "AWS"]}}
RIGHTSHIP_ABOUT = ["RightShip is the maritime industry's leading safety and risk intelligence platform.",
                   "By combining trusted standards, AI-native technology, rich maritime data and industry expertise, RightShip transforms complex "
                   "safety and operational risk into intelligence our customers can act on with confidence."]


def test_the_place_phrase_keeps_the_context_the_fact_bank_gives():
    az, dc, cf = (AL.source_of(FB, i) for i in ("az4", "dc8", "p_cf1"))
    assert (az.phrase, az.mine) == ("On my AstraZeneca dissertation project", "my AstraZeneca dissertation project")
    assert (dc.phrase, dc.mine) == ("At 180 Degrees Consulting Bristol", "my work at 180 Degrees Consulting Bristol")
    assert (cf.phrase, cf.name) == ("On my CoverFit hackathon project", "CoverFit")


def test_every_sentence_opens_with_its_place():
    az = AL.source_of(FB, "az4")
    one = AL.told("Wrote a three-tier test framework of ~65 tests across 8 files.", az)
    two = AL.told("Built the Python data pipeline.", az, second=True)
    assert one == "On my AstraZeneca dissertation project, I wrote a three-tier test framework of ~65 tests across 8 files."
    assert two == "On my AstraZeneca dissertation project, I also built the Python data pipeline."
    assert AL.told("Researchers had 14 datasets, so I joined them.", az).startswith("On my AstraZeneca dissertation project, researchers had 14")


@pytest.mark.parametrize("allow, out", [([], True), (["Ensembl", "HGNC", "parquet", "circuit breakers", "API"], False)])
def test_ensembl_hgnc_and_parquet_keep_a_bullet_out_of_the_letter_unless_the_ad_names_them(allow, out):
    bullet = FB["experience"][0]["bullets"][0]
    text, jargon = AL.letter_text(bullet, FB, True, allow)
    assert bool(jargon) is out
    if out:
        assert {"Ensembl", "HGNC", "parquet"} & {j for j in jargon} or any(j.lower() in AL.LETTER_NEVER for j in jargon)


def test_plain_style_tells_a_bullet_without_its_bracketed_asides_or_with_its_approved_plain_version():
    bullet = FB["experience"][0]["bullets"][1]
    text, jargon = AL.letter_text(bullet, FB, True, ["LLM"])
    assert text == "Wrote a three-tier test framework of ~65 tests across 8 files, validated across the LLM provider migration." and not jargon
    plain = {**bullet, "plain": "I checked the AI assistant with ~65 automated tests across 8 files, so it stayed reliable."}
    assert AL.letter_text(plain, FB, True, [])[0] == plain["plain"]
    assert AL.letter_text(bullet, FB, False, [])[0] == bullet["text"]                    # technical style: word for word


def test_p5_quotes_the_ads_customer_sentence_and_ties_it_to_one_fact():
    clauses = AL.ad_clauses(RIGHTSHIP_ABOUT, "RightShip")
    assert clauses[0] == "RightShip transforms complex safety and operational risk into intelligence its customers can act on with confidence"
    src = AL.source_of(FB, "az4")
    fact = AL.fact_clause("Wrote a three-tier test framework of ~65 tests across 8 files, validated across the LLM provider migration.")
    assert fact == "I wrote a three-tier test framework of ~65 tests across 8 files"
    p5 = AL.tie_p5(clauses[0], src, fact)
    assert p5 == ("RightShip transforms complex safety and operational risk into intelligence its customers can act on with confidence — the same "
                  "aim as my AstraZeneca dissertation project, where I wrote a three-tier test framework of ~65 tests across 8 files.")
    assert len(sentences(p5)) == 1 and AL.p5_split(p5) == (clauses[0], "my AstraZeneca dissertation project, where " + fact)


def test_the_fact_half_of_p5_may_not_add_a_number_or_a_tool():
    fact = {"id": "az4", "text": "Wrote a test framework of ~65 tests across 8 files.", "source": AL.source_of(FB, "az4")}
    assert AL.fact_problems("my AstraZeneca dissertation project, where I wrote ~65 tests", fact, FB) == []
    assert any("93" in p for p in AL.fact_problems("my AstraZeneca dissertation project, where I cut costs by 93%", fact, FB))
    assert any("aws" in p.lower() for p in AL.fact_problems("my AstraZeneca dissertation project, where I used AWS", fact, FB))


def test_a_rewrite_that_drops_the_place_from_a_sentence_is_refused():
    g3 = [{"id": "az4", "source": AL.source_of(FB, "az4")}]
    g4 = [{"id": "dc8", "source": AL.source_of(FB, "dc8")}]
    ok = {"P3": "On my AstraZeneca dissertation project, I wrote ~65 tests.", "P4": "At 180 Degrees Consulting Bristol, I contributed to it."}
    assert AL.place_problems(ok, g3, g4) == []
    bad = {**ok, "P3": ok["P3"] + " I also built a pipeline."}
    assert AL.place_problems(bad, g3, g4) and "AstraZeneca" in AL.place_problems(bad, g3, g4)[0]


@pytest.mark.parametrize("team, shown", [("Product", ""), ("Product team", ""), ("Data Science", ""), ("Engineering", ""),
                                         ("AI Accelerator", "AI Accelerator"), ("Functional Analytics", "Functional Analytics")])
def test_the_greeting_names_a_team_only_when_the_ad_names_one(settings, team, shown):
    assert L.named_team(team) == shown
    want = f"Hi RightShip {shown} team" if shown else "Hi RightShip team"
    assert L.greeting({"company": "RightShip"}, team, settings) == want


def test_freelance_work_is_my_freelance_work_never_at_freelance():
    fb = {"experience": [{"id": "freelance", "org": "Freelance", "employment_type": "freelance", "bullets": [{"id": "fl5", "text": "Designed pipelines."}]}]}
    src = AL.source_of(fb, "fl5")
    assert (src.phrase, src.mine) == ("In my freelance work", "my freelance work")
    assert AL.told("Designed reproducible pipelines.", src) == "In my freelance work, I designed reproducible pipelines."


def test_an_ad_clause_needs_the_company_as_its_subject():
    about = ["More than 1,000 organisations worldwide rely on RightShip to strengthen safety, improve resilience and raise standards."]
    assert AL.ad_clauses(about, "RightShip") == []


def test_an_adjective_is_not_a_thing_work_used():
    group = [{"text": "Proposed an MCP-based agentic tool in Python.", "terms": ["agentic", "python"]}]
    assert AL._terms_sentence(group, ["agentic", "python"], "") == "That work used Python."


def test_p5_tries_every_fact_before_it_gives_up_the_tie():
    from test_company_letter import make_world
    from careerops.llm import MockLLM
    from careerops.tailoring import company_letter as CL

    calls = []

    def check(p5):
        calls.append(p5)
        return [] if "second fact" in p5 else ["refused"]
    src = AL.source_of(FB, "az4")
    facts = [{"id": "a", "text": "Wrote the first fact of the work.", "source": src}, {"id": "b", "text": "Wrote the second fact of the work.", "source": src}]
    got = next((t for f in facts for ad in AL.ad_clauses(RIGHTSHIP_ABOUT, "RightShip")
                for t in [AL.tie_p5(ad, f["source"], AL.fact_clause(f["text"]))] if not check(t)), "")
    assert "where I wrote the second fact of the work" in got and len(calls) > 1


@pytest.fixture
def p2_world(settings, fb_fixture, monkeypatch):
    from pathlib import Path
    from test_company_letter import make_world
    from careerops.tailoring import company_letter as CL

    w = make_world(settings, fb_fixture, result="Live HGNC lookups and a 19,213-gene parquet fallback kept the answers stable.")
    w.settings.raw["letter"]["compose"] = "assembled"
    w.written = []

    def record(job, kws, parts, team, fb, settings, out_dir, final_name, header=None, gap=None, **extra):
        w.written.append(dict(parts))
        paragraphs, names = CL.assemble(parts)
        return Path(out_dir) / f"{final_name}.pdf", CL._letter_dict("ok", paragraphs=paragraphs, names=names, parts=dict(parts),
                                                                   words=CL.words(paragraphs), **extra)
    monkeypatch.setattr(CL, "write_letter", record)
    return w


@pytest.mark.parametrize("style", ["plain", "technical"])
def test_p2_that_says_hgnc_is_left_out_in_plain_style_and_parquet_alone_would_stay_because_the_ad_names_it(p2_world, style):
    from test_letter_assembled_v25 import make, p5_model
    from test_company_letter import P5

    w = p2_world
    w.settings.raw["cv"]["style"] = style
    assert "HGNC" in (w.plan.p2 or "") and "parquet" in w.job["description"].lower()          # the Anaplan ad names Parquet, not HGNC
    pdf, letter = make(w, p5_model(P5))
    assert letter["status"] == "ok", letter.get("reason")
    parts = w.written[0]
    if style == "plain":
        notes = [n for n in letter["notes"] if "P2 left out: it says" in n]
        assert "P2" not in parts and notes and "hgnc" in notes[0].lower() and "parquet" not in notes[0].lower()
    else:
        assert "HGNC" in parts.get("P2", "") or any("P2 left out" in n for n in letter["notes"])
