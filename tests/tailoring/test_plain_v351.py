"""9 Nov 2026 fixes to plain-draft (tailoring/plain.py, review/plaindraft.py): Flesch >= 30, at most 25 words and 1 jargon term; an ownership word
is never upgraded (dc2 "Contributed the MCP implementation" came back as "I built it"); a commendation keeps its own subject, never "giving";
the original's result is kept in meaning or no candidate is shown; two candidates per bullet; a saved line that breaks the ownership or credit
rule is not used on the CV. dc2 is the real bullet's text, copied into a tmp bank (the real fact bank is never read). MockLLM only."""
import copy
import shutil

import pytest
import yaml

from careerops.llm import MockLLM
from careerops.review import plaindraft
from careerops.tailoring import plain as P
from test_plain_v34 import BANK, GOOD_AZ4, KWS, answers, bank, item

DC2 = {"id": "dc2",
       "text": "Contributed the MCP implementation to the client's production repository (TypeScript/Node.js MCP server, OpenAI query generation, "
               "Convex database), now in live day-to-day use; engagement received a 5-star commendation from senior leadership.",
       "result": "5-star commendation from senior leadership",
       "terms": ["typescript", "node.js", "openai", "production", "client", "delivery"]}
DC180 = {"id": "dc180", "title": "Data Consultant", "org": "180 Degrees Consulting Bristol", "dates": "Jan 2026 – Apr 2026",
         "location": "Bristol, UK", "bullets": [DC2]}
USER_DC2 = "Client needed new server, I built it using simple tools, giving 5-star commendation."       # the line saved on 8 Nov
GOOD_DC2 = "I contributed server code for a client team; the engagement received a 5-star commendation from senior leadership."
GOOD2_AZ4 = "I wrote ~65 automated tests across 8 files so the team could trust the agent after the model change."


def dc_bank(plain: str | None = None) -> dict:
    fb = bank()
    for e in fb["experience"]:                                  # the fictional bank has its own dc2: the real one's text takes its id here
        e["bullets"] = [b for b in e["bullets"] if b["id"] != "dc2"]
    entry = copy.deepcopy(DC180)
    if plain:
        entry["bullets"][0]["plain"] = plain
    fb["experience"].append(entry)
    return fb


def dc2_problems(text: str) -> list[str]:
    fb = dc_bank()
    it = item(fb, "dc2")
    return P.problems(text, it.bullet, it.owner, fb, None)


# ------------------------------------------------------------------ the limits
def test_the_new_limits():
    assert P.MAX_WORDS == 25 and P.MIN_FLESCH == 30 and P.MAX_JARGON == 1 and P.CANDIDATES == 2
    assert P.min_flesch(yaml.safe_load(open(BANK.parents[1] / "config" / "settings.yaml", encoding="utf-8"))) == 30


def test_one_jargon_term_is_allowed_and_two_are_not():
    fb = bank()
    it = item(fb, "az4")
    one = "Researchers needed deterministic software, so I wrote ~65 automated tests across 8 files."
    two = "Researchers needed deterministic software, so I wrote ~65 automated tests across 8 files in parquet."
    assert P.jargon(one, fb) == ["deterministic"] and P.problems(one, it.bullet, it.owner, fb) == []
    assert any("jargon" in p and "at most 1" in p for p in P.problems(two, it.bullet, it.owner, fb))


# ------------------------------------------------------------------ ownership and credit (dc2)
def test_the_saved_dc2_line_breaks_every_new_rule():
    probs = dc2_problems(USER_DC2)
    assert any("upgrades the ownership" in p and "'contributed'" in p and "'built'" in p for p in probs), probs
    assert any("drops the ownership word 'contributed'" in p for p in probs)
    assert any("'giving'" in p for p in probs) and any("keeps its subject: 'engagement" in p for p in probs)
    assert any("does not keep the result" in p for p in probs)


def test_a_dc2_line_that_keeps_contributed_and_the_engagements_commendation_passes():
    assert dc2_problems(GOOD_DC2) == []


@pytest.mark.parametrize("weak, strong", [("Contributed", "built"), ("Helped", "led"), ("Supported", "owned"), ("Assisted", "designed")])
def test_a_weak_ownership_word_is_never_upgraded(weak, strong):
    source = f"{weak} the migration of 14 dashboards to the new platform."
    assert any("upgrades the ownership" in p for p in P.ownership_problems(f"I {strong} the move of 14 dashboards to a new platform.", source))
    assert P.ownership_problems(f"I {weak.lower()} to the move of 14 dashboards to a new platform.", source) == []
    assert P.ownership_problems("I built 14 dashboards.", "Built 14 dashboards.") == []          # a strong bullet may stay strong


@pytest.mark.parametrize("bad", ["I contributed server code, giving the team a 5-star commendation from senior leadership.",
                                 "I contributed server code and I received a 5-star commendation from senior leadership.",
                                 "I contributed server code, earning a 5-star commendation from senior leadership."])
def test_a_commendation_keeps_its_subject(bad):
    assert P.credit_problems(bad, DC2["text"])


# ------------------------------------------------------------------ the result is kept
def test_the_result_clause_of_a_bullet():
    fb = bank()
    assert P.result_clause(item(fb, "az4").bullet) == "~65 tests across 8 files"
    assert P.result_clause(item(fb, "az3").bullet).startswith("replacing a manual copy-and-paste release process")
    assert P.result_clause(item(fb, "az2").bullet).startswith("answers stay reproducible")
    assert P.result_clause(DC2) == "5-star commendation from senior leadership"


def test_a_candidate_that_loses_the_result_is_not_shown():
    fb = bank()
    az4, az3 = item(fb, "az4").bullet, item(fb, "az3").bullet
    assert any("drops the result's number" in p for p in P.result_problems("I wrote ~65 automated tests to check the agent.", az4))
    assert any("counts (tests, files)" in p for p in P.result_problems("I wrote ~65 automated checks across 8 folders.", az4))
    assert any("does not keep the result" in p for p in P.result_problems("I put the agent on AWS in a Docker container.", az3))
    assert P.result_problems("I put the agent on AWS, replacing a manual copy-and-paste release process.", az3) == []


def test_unsure_means_no_candidate():
    assert P.result_problems("I did 3 things.", {"text": "Wrote notes for the team."})[0].startswith("cannot find the bullet's result clause")


# ------------------------------------------------------------------ two candidates
def two_model(*replies):
    seen, queue = [], list(replies)

    def handler(ctx):
        seen.append(ctx)
        return {"plain": queue.pop(0) if len(queue) > 1 else queue[0]}
    llm = MockLLM({"plain_bullet": handler})
    llm.seen = seen
    return llm


def test_two_candidates_come_back_from_one_call():
    fb = bank()
    llm = two_model([GOOD_AZ4, GOOD2_AZ4])
    cands, probs, _ = P.draft(llm, item(fb, "az4"), fb)
    assert cands == [GOOD_AZ4, GOOD2_AZ4] and probs == [] and len(llm.seen) == 1


def test_a_failed_candidate_is_asked_for_again_and_the_good_one_is_kept():
    fb = bank()
    llm = two_model([GOOD_AZ4, GOOD_AZ4.replace("~65", "90")], [GOOD2_AZ4])
    cands, _, _ = P.draft(llm, item(fb, "az4"), fb)
    assert cands == [GOOD_AZ4, GOOD2_AZ4] and len(llm.seen) == P.TRIES


def test_the_review_shows_two_and_saves_the_one_you_pick_after_y(tmp_path):
    p = tmp_path / "profile" / "fact_bank.yaml"
    p.parent.mkdir()
    shutil.copy(BANK, p)
    out = []
    stats = plaindraft.run(p, two_model([GOOD_AZ4, GOOD2_AZ4]), None, ask=answers("2", "y"), out=out.append, ids={"az4"})
    assert any(l.strip().startswith("1.") and GOOD_AZ4 in l for l in out) and any(l.strip().startswith("2.") and GOOD2_AZ4 in l for l in out)
    az4 = next(b for e in yaml.safe_load(p.read_text(encoding="utf-8"))["experience"] for b in e["bullets"] if b["id"] == "az4")
    assert stats["saved"] == [("az4", "plain")] and az4["plain"] == GOOD2_AZ4


def test_picking_without_y_saves_nothing(tmp_path):
    p = tmp_path / "profile" / "fact_bank.yaml"
    p.parent.mkdir()
    shutil.copy(BANK, p)
    before = p.read_bytes()
    plaindraft.run(p, two_model([GOOD_AZ4, GOOD2_AZ4]), None, ask=answers("1", "n"), out=lambda _: None, ids={"az4"})
    assert p.read_bytes() == before


# ------------------------------------------------------------------ a saved line that breaks the rules
def test_a_saved_line_that_upgrades_ownership_is_not_used_on_the_cv():
    fb = dc_bank(USER_DC2)
    out, info = P.plain_bank(fb, KWS)
    dc2 = next(b for e in out["experience"] for b in e["bullets"] if b["id"] == "dc2")
    assert dc2["text"] == DC2["text"] and "dc2" in info["refused"] and "dc2" not in info["plain"]
    assert P.usable_plain(dc2 | {"plain": USER_DC2}) == "" and P.usable_plain(dc2 | {"plain": GOOD_DC2}) == GOOD_DC2
    good, info2 = P.plain_bank(dc_bank(GOOD_DC2), KWS)
    assert "dc2" in info2["plain"] and not info2["refused"]


def test_plain_draft_lists_the_saved_line_it_will_not_use_and_never_changes_it(tmp_path):
    p = tmp_path / "profile" / "fact_bank.yaml"
    p.parent.mkdir()
    p.write_text(yaml.safe_dump(dc_bank(USER_DC2), sort_keys=False, allow_unicode=True), encoding="utf-8")
    before = p.read_bytes()
    out = []
    stats = plaindraft.run(p, two_model([GOOD_DC2]), None, ask=lambda q: pytest.fail("asked"), out=out.append, ids={"dc2"}, dry_run=True)
    assert "dc2" in stats["refused"] and any("NOT used on the CV" in l for l in out) and p.read_bytes() == before
