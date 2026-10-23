"""The gap report (review/gaps.py): for a job in the review band (50 <= strict S < 70, or the "under-uses" rescue case) the review header
lists every JD keyword missing from the CV with its weight w and gain g = 100 x w / sum(w), largest first, marked `claimable` (the fact
bank evidences it) or `gap` (it does not). All numbers come from the scorer's own: nothing here reads a PDF or calls a model."""
import copy

import pytest

from careerops import bands
from careerops.config import load_yaml
from careerops.review import gaps

from conftest import PROFILE_FIX

# (term, weight, required, on the CV?, claimable?)    sum of weights = 20, so a keyword of weight w gains 5 x w points
KEYWORDS = [
    ("Python", 2, True, True, True), ("LangChain", 2, True, True, True), ("Docker", 2, True, True, True),
    ("scikit-learn", 2, True, True, True), ("pytest", 1, False, True, True), ("pandas", 1, False, True, True),
    ("Git", 1, False, True, True), ("RAG", 1, False, True, True),
    ("SQL", 2, True, False, True),            # in the bank (skills "SQL", bullet dc2), not on the CV
    ("MLflow", 1, False, False, True),
    ("Dagster", 2, True, False, False),       # nothing in the bank
    ("Redshift", 1, False, False, False),
    ("Spark", 1, False, False, False),
    ("Terraform", 1, False, False, False),    # on the deny list: the user said they lack it
]
TOTAL = sum(k[1] for k in KEYWORDS)


@pytest.fixture
def fb() -> dict:
    return load_yaml(PROFILE_FIX / "fact_bank.yaml")


def report(fb, *, overflow=("dc2",), chosen=("az1", "az2", "az3", "fl1", "dc1"), keywords=KEYWORDS, extra=None) -> dict:
    kws = [{"term": t, "weight": float(w), "required": req, "alts": []} for t, w, req, _, _ in keywords]
    hits = [t for t, _, _, on, _ in keywords if on]
    claimable = [t for t, _, _, _, c in keywords if c]
    tot = sum(w for _, w, *_ in keywords)
    strict = 100.0 * sum(w for t, w, *_ in keywords if t in hits) / tot
    ceiling = 100.0 * sum(w for t, w, *_ in keywords if t in claimable) / tot
    rep = {"keywords": kws, "claimable": claimable, "genuine_gaps": [], "possible_evidence": {},
           "ats": {"strict": strict, "ceiling": ceiling, "hits": hits, "misses": [t for t, *_ in keywords if t not in hits], "negated": []},
           "final": {"pages": 2, "wording_misses": []}, "fill": {"rejected_overflow": list(overflow)},
           "context": {"experience": [{"id": "az", "bullet_ids": [b for b in chosen if b.startswith("az")]}], "projects": [],
                       "skills": {"Core": ["Python", "Docker", "Git"]}},
           "ranking": {"bullets": [], "projects": []}, "attempts": [{"violations": [], "warnings": []}], "placement": {"blocked": []}}
    rep.update(extra or {})
    return rep


def test_the_gains_add_up(fb):
    rep = report(fb)
    g = gaps.build(rep, fb, bands.Bands())
    everything = {k[0]: 100.0 * k[1] / TOTAL for k in KEYWORDS}
    assert sum(everything.values()) == pytest.approx(100.0)
    strict, ceiling = rep["ats"]["strict"], rep["ats"]["ceiling"]
    assert strict == pytest.approx(60.0) and ceiling == pytest.approx(75.0)
    assert sum(r.gain for r in g.rows) == pytest.approx(100.0 - strict)                 # all the points still to win
    assert g.gap_total == pytest.approx(100.0 - ceiling)                               # what the bank cannot back is 100 minus the ceiling's covered part
    assert g.claimable_total == pytest.approx(ceiling - strict)                         # what re-tailoring can win
    assert g.claimable_total + g.gap_total == pytest.approx(100.0 - strict)
    assert all(r.gain == pytest.approx(everything[r.term]) for r in g.rows)


def test_the_header_names_s_c_and_the_points_still_needed(fb):
    g = gaps.build(report(fb), fb, bands.Bands())
    assert g.lines()[0] == "ATS strict 60 | ceiling 75 | to reach 70 you need +10 points"
    assert g.needed == pytest.approx(10.0)


def test_missing_keywords_are_listed_largest_gain_first_and_hits_are_not(fb):
    g = gaps.build(report(fb), fb, bands.Bands())
    assert [r.term for r in g.rows] == ["SQL", "Dagster", "MLflow", "Redshift", "Spark", "Terraform"]   # weight order; a tie keeps the JD's order
    assert [r.gain for r in g.rows] == pytest.approx([10, 10, 5, 5, 5, 5])


def test_each_row_is_marked_claimable_or_gap(fb):
    g = gaps.build(report(fb), fb, bands.Bands())
    kind = {r.term: r.kind for r in g.rows}
    assert kind == {"SQL": "claimable", "MLflow": "claimable", "Dagster": "gap", "Redshift": "gap", "Spark": "gap", "Terraform": "gap"}


def test_the_example_lines(fb):
    text = "\n".join(gaps.build(report(fb), fb, bands.Bands()).lines())
    assert "+10.0  Dagster (required, w=2)  gap" in text
    assert '+10.0  SQL (required, w=2)  claimable: skills "SQL", bullet dc2 (left off: page full)' in text
    assert "+5.0  Redshift (preferred, w=1)  gap" in text


def test_a_gain_is_rounded_half_up_like_the_example_line(fb):
    kws = [("Python", 2, True, True, True), ("Git", 1, False, True, True)] + [(f"hit{n}", 2, True, True, True) for n in range(37)]
    kws += [("Luigi", 2, True, False, False), ("Dagster", 1, False, False, False)]          # sum of weights 80: Luigi +2.5, Dagster +1.25
    g = gaps.build(report(fb, keywords=kws), fb, bands.Bands())
    text = "\n".join(g.lines())
    assert "+2.5  Luigi (required, w=2)  gap" in text
    assert "+1.3  Dagster (preferred, w=1)  gap" in text                                    # 1.25 -> 1.3, not 1.2


def test_the_two_sums_are_printed(fb):
    text = "\n".join(gaps.build(report(fb), fb, bands.Bands()).lines())
    assert "claimable total: +15 points (re-tailoring can win them)" in text
    assert "gap total: +25 points (each needs a new fact)" in text


def test_it_says_whether_re_tailoring_alone_can_reach_the_band(fb):
    assert "re-tailoring alone could reach 70" in "\n".join(gaps.build(report(fb), fb, bands.Bands()).lines())
    low = [("Python", 2, True, True, True), ("SQL", 1, False, False, True), ("Dagster", 2, True, False, False), ("Spark", 2, True, False, False),
           ("Kafka", 2, True, False, False), ("Luigi", 1, False, False, False)]
    text = "\n".join(gaps.build(report(fb, keywords=low), fb, bands.Bands()).lines())
    assert "re-tailoring alone could reach" not in text and "the rest needs a new fact" in text


def test_the_reason_a_claimable_bullet_was_left_off(fb):
    # not on the CV, in the rejected-for-space list: page full
    assert "(left off: page full)" in "\n".join(gaps.build(report(fb), fb, bands.Bands()).lines())
    # not on the CV, not tried: rank, with its place in the ranking when the report has it
    rep = report(fb, overflow=(), extra={"ranking": {"bullets": [{"id": "az1", "score": 9.0}, {"id": "dc2", "score": 4.0}], "projects": []}})
    row = next(r for r in gaps.build(rep, fb, bands.Bands()).rows if r.term == "SQL")
    assert "left off: rank" in row.line() and "#2" in row.line()
    # a chosen verbatim bullet replaces it: say which
    fb2 = copy.deepcopy(fb)
    fb2["experience"][1]["bullets"].append({"id": "dc9", "verbatim": True, "replaces": ["dc2"], "text": "Prototyped things.", "terms": ["mcp"]})
    rep = report(fb2, overflow=(), chosen=("az1", "dc9"))
    rep["context"]["experience"].append({"id": "dc", "bullet_ids": ["dc9"]})
    row = next(r for r in gaps.build(rep, fb2, bands.Bands()).rows if r.term == "SQL")
    assert "dc9 replaces it" in row.line()


def test_a_bullet_on_the_cv_that_only_evidences_the_word_through_its_terms_is_a_guard_case(fb):
    kws = [*KEYWORDS, ("MLOps", 1, False, False, True)]               # az3's `terms:` say mlops, its text does not: a rewording may not add the word
    rep = report(fb, overflow=(), chosen=("az1", "az3"), keywords=kws)
    row = next(r for r in gaps.build(rep, fb, bands.Bands()).rows if r.term == "MLOps")
    assert row.kind == "claimable" and "bullet az3" in row.line() and "left off: guard" in row.line()


def test_a_deny_listed_term_is_a_gap_the_user_is_never_asked_about(fb):
    g = gaps.build(report(fb), fb, bands.Bands())
    row = next(r for r in g.rows if r.term == "Terraform")
    assert row.kind == "gap" and row.denied and "deny-listed" in row.line()
    assert "Terraform" not in [r.term for r in g.askable(5)]
    assert [r.term for r in g.askable(5)] == ["Dagster", "Redshift", "Spark"]          # the top gaps by gain, gaps only


def test_possible_evidence_lines_are_kept(fb):
    rep = report(fb, extra={"possible_evidence": {"Dagster": [{"via": "Prefect", "where": ["skills", "az3"]}]}})
    text = "\n".join(gaps.build(rep, fb, bands.Bands()).lines())
    assert "Possible evidence" in text and "Dagster <- Prefect (skills, az3)" in text


def test_it_applies_to_the_review_and_rescue_bands_only(fb):
    rep = report(fb)
    rep["band"] = bands.classify(60, 75, bands.Bands()).to_dict()
    assert gaps.applies(rep, bands.Bands())
    rep["band"] = bands.classify(75, 90, bands.Bands()).to_dict()
    assert not gaps.applies(rep, bands.Bands())                                         # auto band: the plain header
    rep["band"] = bands.classify(40, 65, bands.Bands()).to_dict()
    assert gaps.applies(rep, bands.Bands())                                             # rescue
    del rep["band"]                                                                     # a report from before the bands: judged by its own numbers
    assert gaps.applies(rep, bands.Bands())


def test_a_strict_score_at_or_above_the_target_needs_no_points(fb):
    g = gaps.build(report(fb, keywords=[("Python", 3, True, True, True), ("SQL", 1, False, False, True)]), fb, bands.Bands())
    assert g.needed == 0 and "to reach 70 you need +0 points" in g.lines()[0]
