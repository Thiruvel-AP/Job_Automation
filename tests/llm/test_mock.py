"""MockLLM determinism."""
from careerops.llm import MockLLM


FACTS = [{"id": "F1", "text": "MSc Data Science, University of Northfield (completed 2026)"},
         {"id": "F2", "text": "Production LLM agent work on a clinical research platform"},
         {"id": "F3", "text": "Background in mobile FinTech engineering before data science"}]


def test_the_mock_never_selects_and_never_rewords():
    mock = MockLLM()
    assert mock.json("rephrase_bullets", "", "", {"bullets": [{"id": "a1", "text": "x", "terms": [], "introduce": ["y"]}]}) == {}
    try:
        mock.json("tailor_cv", "", "", {})
    except Exception as e:
        assert "no handler for task tailor_cv" in str(e)
    else:
        raise AssertionError("the model no longer selects bullets: tailor_cv has no mock answer")


def test_the_mock_profile_picks_the_first_two_facts_and_stays_within_45_words():
    out = MockLLM().json("tailor_profile", "", "", {"facts": FACTS, "headline": "Data Scientist"})
    assert out["facts"] == ["F1", "F2"]
    assert out["profile"].startswith("Data Scientist with MSc Data Science") and len(out["profile"].split()) <= 45
    long = [{"id": "F1", "text": "word " * 80}, FACTS[1]]
    assert len(MockLLM().json("tailor_profile", "", "", {"facts": long, "headline": "AI Engineer"})["profile"].split()) <= 41


def test_the_mock_profile_retry_returns_just_a_profile():
    out = MockLLM().json("tailor_profile", "", "", {"profile_facts": [f["text"] for f in FACTS], "previous": "x",
                                                    "problems": ["p"], "headline": "Data Scientist"})
    assert set(out) == {"profile"} and out["profile"].startswith("Data Scientist with ")


def test_keywords_come_back_with_an_empty_team_by_default():
    assert MockLLM().json("extract_keywords", "", "", {})["team"] == ""


LETTER_CTX = {"company": "Acme", "title": "Data Scientist", "evidence": ["Built a thing."]}


def test_mock_letter_has_no_gap_sentence_when_gaps_is_empty():
    for ctx in (LETTER_CTX, {**LETTER_CTX, "gaps": []}):
        paras = MockLLM().json("cover_letter", "", "", ctx)["paragraphs"]
        assert len(paras) == 3


def test_mock_letter_names_one_gap_in_one_short_plain_sentence():
    paras = MockLLM().json("cover_letter", "", "", {**LETTER_CTX, "gaps": ["kubernetes", "terraform"]})["paragraphs"]
    assert len(paras) == 4
    hits = [p for p in paras if "kubernetes" in p.lower()]
    assert len(hits) == 1 and len(hits[0].split()) <= 25 and "terraform" not in " ".join(paras).lower()
    assert not any(w in hits[0].lower() for w in ("sorry", "apolog", "unfortunately"))
