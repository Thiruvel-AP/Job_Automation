"""A letter never reuses another letter's body (Document rules, B5): the body (P2 to P4) is compared with every earlier letter in the outputs and
archive folders by 5-word shingles (Jaccard). At 0.30 or more the model redrafts ONCE, told `do not reuse these sentences: ...`; still 0.30
or more means no letter, and the reason is shown. The greeting, sign-off and closing question are not counted. No network, no model."""
import json
import random
from pathlib import Path

import pytest

from careerops.llm import MockLLM
from careerops.tailoring import company_letter as CL
from careerops.tailoring import letter_reuse as R
from test_company_letter import JOB, TEAM, P2, body, pad, run, world, writer  # noqa: F401  (world is a fixture)

WORDS = ("beyond this example enjoy talking through decisions colleagues explaining each step plain language others follow reasoning separately try "
         "keep explanation short name trade offs openly reviewers challenge early team agree build next quiet careful patient listening asking clear "
         "questions before writing anything down because small habits matter over time people notice when work handled with care everyone rely").split()


def other_body(total_words: int = 400, seed: int = 7) -> list[str]:
    """Three paragraphs of other words entirely: the draft a model writes when it is told not to reuse the first one."""
    rng = random.Random(seed)
    return [" ".join(rng.choice(WORDS) for _ in range(total_words // 3)).capitalize() + "." for _ in range(3)]


def plant(settings, name: str, paragraphs: list[str]) -> None:
    folder = settings.path("outputs") / name
    folder.mkdir(parents=True)
    (folder / "report.json").write_text(json.dumps({"letter": {"status": "ok", "paragraphs": paragraphs}}), encoding="utf-8")


def five(text_body: list[str]) -> list[str]:
    return ["An opening that is not counted.", *text_body, *([""] * (3 - len(text_body))), "Could we take 20 minutes to talk about it?"]


# ------------------------------------------------------------------ the measure
def test_shingles_are_five_word_windows_and_a_short_text_is_one_shingle():
    assert R.shingles("a b c d e f") == {("a", "b", "c", "d", "e"), ("b", "c", "d", "e", "f")}
    assert R.shingles("One, TWO; three!") == {("one", "two", "three")}
    assert R.shingles("") == set()


def test_jaccard():
    assert R.jaccard({1, 2, 3}, {2, 3, 4}) == pytest.approx(0.5) and R.jaccard(set(), {1}) == 0.0 and R.jaccard({1}, {1}) == 1.0


def test_the_limit_is_point_three():
    assert R.LIMIT == 0.30


# ------------------------------------------------------------------ where the earlier letters are
def test_earlier_letters_are_read_from_outputs_and_both_archive_folders(settings):
    plant(settings, "acme_ds_1", five(["Body of the first earlier letter written for Acme about forecasting demand."]))
    for key, company, name in (("applications_auto", "Beta", "Cover Letter.txt"), ("applications", "Gamma", "letter.txt")):
        folder = settings.path(key) / company / "Data Scientist"
        folder.mkdir(parents=True)
        (folder / name).write_text(f"Dear {company} team,\nA letter body from the {key} archive about churn models.\nWith regards", encoding="utf-8")
        (folder / "CV.txt").write_text("this is a cv and not a letter", encoding="utf-8")
    found = {e.name: e for e in R.earlier_letters(settings)}
    assert set(found) == {"acme_ds_1", "Beta/Data Scientist", "Gamma/Data Scientist"}
    assert "forecasting demand" in found["acme_ds_1"].text and "churn models" in found["Beta/Data Scientist"].text


def test_the_outputs_body_leaves_out_the_opening_and_the_closing_question(settings):
    plant(settings, "acme_ds_1", five(["Only this body counts."]))
    [only] = R.earlier_letters(settings)
    assert only.text == "Only this body counts."


def test_the_jobs_own_folder_and_a_letter_that_failed_are_not_earlier_letters(settings):
    plant(settings, "acme_ds_1", five(["Body one."]))
    bad = settings.path("outputs") / "beta_ds_2"
    bad.mkdir()
    (bad / "report.json").write_text(json.dumps({"letter": {"status": "none", "paragraphs": []}}), encoding="utf-8")
    (settings.path("outputs") / "broken").mkdir()
    (settings.path("outputs") / "broken" / "report.json").write_text("{not json", encoding="utf-8")
    assert [e.name for e in R.earlier_letters(settings)] == ["acme_ds_1"]
    assert R.earlier_letters(settings, exclude=["acme_ds_1.partial"]) == []              # the staging folder of the same job


def test_reading_the_archive_changes_nothing_there(settings):
    folder = settings.path("applications") / "Gamma" / "Data Scientist"
    folder.mkdir(parents=True)
    f = folder / "Cover Letter.txt"
    f.write_text("Dear team, a body.", encoding="utf-8")
    before = (f.read_bytes(), f.stat().st_mtime_ns, sorted(p.name for p in folder.iterdir()))
    R.earlier_letters(settings)
    assert before == (f.read_bytes(), f.stat().st_mtime_ns, sorted(p.name for p in folder.iterdir()))


# ------------------------------------------------------------------ the verdict
def test_a_near_copy_scores_over_the_limit_and_names_the_shared_sentences(world):
    first = body(world, 400)
    copy = [first[0], first[1].replace(world.own[0].split()[0], "Another", 1), first[2]]
    score, who = R.worst(copy, [R.Earlier("acme_ds_1", " ".join(first))])
    assert score >= 0.9 and who.name == "acme_ds_1"
    shared = R.shared_sentences(copy, who)
    assert shared and all(s.removesuffix("...")[:100] in " ".join(copy) for s in shared)
    msg = R.problem(score, who, shared)
    assert msg.startswith("reuse: the body is") and "acme_ds_1" in msg and "do not reuse these sentences:" in msg


def test_two_letters_for_different_jobs_that_share_one_bullet_pass(world):
    ours = body(world, 400)
    theirs = [f"{sent} {other_body(450)[0]}" for sent in ("The other company builds routing software for delivery fleets.",
                                                 world.own[0], "On a different requirement I did unrelated work.")]       # one bullet in common
    score, _ = R.worst(ours, [R.Earlier("other", " ".join(theirs))])
    assert score < R.LIMIT


def test_the_worst_of_several_earlier_letters_is_the_one_named(world):
    ours = body(world, 400)
    score, who = R.worst(ours, [R.Earlier("far", "nothing alike here at all in these words"), R.Earlier("near", " ".join(ours))])
    assert who.name == "near" and score == pytest.approx(1.0)
    assert R.worst(ours, []) == (0.0, None)


# ------------------------------------------------------------------ in the letter: redraft once, then no letter
@pytest.mark.slow
def test_a_draft_that_copies_an_earlier_letter_is_redrafted_once_told_what_not_to_reuse(world, monkeypatch):
    monkeypatch.setattr(CL, "check", lambda *a, **k: ([], []))                    # the guards have their own tests; this one is about the copy
    first, second = body(world, 400), other_body()
    plant(world.settings, "acme_ds_1", five(first))
    sent = []

    class Spy(MockLLM):
        def json(self, task, system, user, context=None):
            sent.append(user)
            return super().json(task, system, user, context)
    handler, seen = writer(world, [first, second])
    pdf, letter = run(world, Spy({"cover_letter": handler}))
    assert len(seen) == 2 and letter["status"] == "ok" and letter["paragraphs"][1:4] == second, letter.get("reason")
    assert "do not reuse these sentences:" in sent[1] and "do not reuse these sentences:" not in sent[0]
    assert letter["reuse"]["score"] < R.LIMIT and letter["reuse"]["redrafted"] is True


@pytest.mark.slow
def test_a_second_copy_means_no_letter_and_the_reason_is_shown(world, monkeypatch):
    monkeypatch.setattr(CL, "check", lambda *a, **k: ([], []))
    first = body(world, 400)
    plant(world.settings, "acme_ds_1", five(first))
    handler, seen = writer(world, [first, first, first])
    pdf, letter = run(world, MockLLM({"cover_letter": handler}))
    assert pdf is None and letter["status"] == "none" and len(seen) == 2                    # one redraft, not a third try
    assert "acme_ds_1" in letter["reason"] and "reuse" in letter["reason"]


@pytest.mark.slow
def test_a_letter_that_shares_nothing_is_not_redrafted_and_records_its_score(world):
    plant(world.settings, "acme_ds_1", five(["Nothing in common with the new draft whatsoever, just other words entirely about other things."]))
    handler, seen = writer(world, [body(world, 400)])
    pdf, letter = run(world, MockLLM({"cover_letter": handler}))
    assert len(seen) == 1 and letter["status"] == "ok" and letter["reuse"] == {"score": 0.0, "against": "", "redrafted": False}
