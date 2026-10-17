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


def test_the_mock_has_no_profile_and_no_hook_task_because_both_are_assembled_in_code():
    for task in ("tailor_profile", "hook"):
        try:
            MockLLM().json(task, "", "", {})
        except Exception as e:
            assert f"no handler for task {task}" in str(e)
        else:
            raise AssertionError(f"{task} is no model task any more")


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
