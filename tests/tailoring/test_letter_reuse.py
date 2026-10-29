"""A letter never reuses another letter's body (Document rules, B5): P2 to P5 (what it shows, the skills, the roll-up, why this team) are compared with
every earlier letter in the outputs and archive folders by 5-word shingles (Jaccard). The greeting, P1 (the hook), P6 (the honest gap), P7 (the ask)
and the sign-off are not counted. At 0.30 or more the model redrafts ONCE, told `do not reuse these sentences: ...`; still 0.30 or more means no
letter, and the reason is shown. No network, no model."""
import json
import random
from pathlib import Path

import pytest

from careerops.llm import MockLLM
from careerops.tailoring import company_letter as CL
from careerops.tailoring import letter_reuse as R
from test_company_letter import JOB, P3, TEAM, body, letter_parts, pad, run, world, writer  # noqa: F401  (world is a fixture)

WORDS = ("beyond this example enjoy talking through decisions colleagues explaining each step plain language others follow reasoning separately try "
         "keep explanation short name trade offs openly reviewers challenge early team agree build next quiet careful patient listening asking clear "
         "questions before writing anything down because small habits matter over time people notice when work handled with care everyone rely").split()


def other_body(total_words: int = 240, seed: int = 7) -> list[str]:
    """Three paragraphs (P3, P4, P5) of other words entirely: the draft a model writes when it is told not to reuse the first one."""
    rng = random.Random(seed)
    return [" ".join(rng.choice(WORDS) for _ in range(total_words // 3)).capitalize() + "." for _ in range(3)]


def plant(settings, name: str, text_body: list[str], with_gap: bool = False) -> None:
    """An earlier letter of the outputs folder: an opening (P1), the body paragraphs P3.. , an optional gap (P6) and the ask (P7)."""
    paragraphs = ["An opening that is not counted.", *text_body, *(["One honest gap: I have never used Snowflake."] if with_gap else []), "Could we spend 20 minutes on it?"]
    names = ["P1", *["P3", "P4", "P5"][:len(text_body)], *(["P6"] if with_gap else []), "P7"]
    folder = settings.path("outputs") / name
    folder.mkdir(parents=True)
    (folder / "report.json").write_text(json.dumps({"letter": {"status": "ok", "paragraphs": paragraphs, "names": names}}), encoding="utf-8")


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
    plant(settings, "acme_ds_1", ["Body of the first earlier letter written for Acme about forecasting demand."])
    for key, company, name in (("applications_auto", "Beta", "Cover Letter.txt"), ("applications", "Gamma", "letter.txt")):
        folder = settings.path(key) / company / "Data Scientist"
        folder.mkdir(parents=True)
        (folder / name).write_text(f"Dear {company} team,\nA letter body from the {key} archive about churn models.\nWith regards", encoding="utf-8")
        (folder / "CV.txt").write_text("this is a cv and not a letter", encoding="utf-8")
    found = {e.name: e for e in R.earlier_letters(settings)}
    assert set(found) == {"acme_ds_1", "Beta/Data Scientist", "Gamma/Data Scientist"}
    assert "forecasting demand" in found["acme_ds_1"].text and "churn models" in found["Beta/Data Scientist"].text


def test_the_outputs_body_is_p2_to_p5_and_leaves_out_the_hook_the_gap_and_the_ask(settings):
    plant(settings, "acme_ds_1", ["Only this body counts."], with_gap=True)
    [only] = R.earlier_letters(settings)
    assert only.text == "Only this body counts."


def test_p2_is_part_of_the_body_and_an_older_five_paragraph_letter_still_counts_p2_to_p4(settings):
    folder = settings.path("outputs") / "with_p2"
    folder.mkdir(parents=True)
    (folder / "report.json").write_text(json.dumps({"letter": {"status": "ok", "names": ["P1", "P2", "P3", "P7"],
                                                              "paragraphs": ["hook", "what it shows", "skills", "ask"]}}), encoding="utf-8")
    old = settings.path("outputs") / "old_five"
    old.mkdir()
    (old / "report.json").write_text(json.dumps({"letter": {"status": "ok", "paragraphs": ["open", "why", "three", "four", "close"]}}), encoding="utf-8")
    found = {e.name: e.text for e in R.earlier_letters(settings)}
    assert found == {"old_five": "why three four", "with_p2": "what it shows skills"}


def test_the_jobs_own_folder_and_a_letter_that_failed_are_not_earlier_letters(settings):
    plant(settings, "acme_ds_1", ["Body one."])
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
    first = body(world)
    copy = [first[0], first[1].replace(P3.split()[0], "Another", 1), first[2]]
    score, who = R.worst(copy, [R.Earlier("acme_ds_1", " ".join(first))])
    assert score >= 0.9 and who.name == "acme_ds_1"
    shared = R.shared_sentences(copy, who)
    assert shared and all(s.removesuffix("...")[:100] in " ".join(copy) for s in shared)
    msg = R.problem(score, who, shared)
    assert msg.startswith("reuse: the body is") and "acme_ds_1" in msg and "do not reuse these sentences:" in msg


def test_two_letters_for_different_jobs_that_share_one_bullet_pass(world):
    ours = body(world)
    theirs = [f"{sent} {other_body(450)[0]}" for sent in ("The other company builds routing software for delivery fleets.",
                                                 P3, "On a different requirement I did unrelated work.")]       # one paragraph in common
    score, _ = R.worst(ours, [R.Earlier("other", " ".join(theirs))])
    assert score < R.LIMIT


def test_the_worst_of_several_earlier_letters_is_the_one_named(world):
    ours = body(world)
    score, who = R.worst(ours, [R.Earlier("far", "nothing alike here at all in these words"), R.Earlier("near", " ".join(ours))])
    assert who.name == "near" and score == pytest.approx(1.0)
    assert R.worst(ours, []) == (0.0, None)


# ------------------------------------------------------------------ in the letter: redraft once, then no letter
@pytest.mark.slow
def test_a_draft_that_copies_an_earlier_letter_is_redrafted_once_told_what_not_to_reuse(world, monkeypatch):
    monkeypatch.setattr(CL, "check", lambda *a, **k: ([], []))                    # the guards have their own tests; this one is about the copy
    first, second = body(world), other_body()
    plant(world.settings, "acme_ds_1", first)
    sent = []

    class Spy(MockLLM):
        def json(self, task, system, user, context=None):
            sent.append(user)
            return super().json(task, system, user, context)
    handler, seen = writer([first, second])
    pdf, letter = run(world, Spy({"cover_letter": handler}))
    assert len(seen) == 2 and letter["status"] == "ok" and [letter["parts"][n] for n in ("P3", "P4", "P5")] == second, letter.get("reason")
    assert "do not reuse these sentences:" in sent[1] and "do not reuse these sentences:" not in sent[0]
    assert letter["reuse"]["score"] < R.LIMIT and letter["reuse"]["redrafted"] is True


@pytest.mark.slow
def test_a_second_copy_means_no_letter_and_the_reason_is_shown(world, monkeypatch):
    monkeypatch.setattr(CL, "check", lambda *a, **k: ([], []))
    first = body(world)
    plant(world.settings, "acme_ds_1", first)
    handler, seen = writer([first, first, first])
    pdf, letter = run(world, MockLLM({"cover_letter": handler}))
    assert pdf is None and letter["status"] == "none" and len(seen) == 2                    # one redraft, not a third try
    assert "acme_ds_1" in letter["reason"] and "reuse" in letter["reason"]


@pytest.mark.slow
def test_a_letter_that_shares_nothing_is_not_redrafted_and_records_its_score(world):
    plant(world.settings, "acme_ds_1", ["Nothing in common with the new draft whatsoever, just other words entirely about other things."])
    handler, seen = writer([body(world)])
    pdf, letter = run(world, MockLLM({"cover_letter": handler}))
    assert len(seen) == 1 and letter["status"] == "ok" and letter["reuse"] == {"score": 0.0, "against": "", "redrafted": False}


@pytest.mark.slow
def test_a_shared_hook_ask_or_gap_is_not_a_copy(world):
    """P1, P6 and P7 are the same sentences in many letters of one person (the same hook): they are not compared."""
    parts = letter_parts(world)
    plant(world.settings, "acme_ds_1", other_body(), with_gap=True)
    folder = world.settings.path("outputs") / "acme_ds_1"
    data = json.loads((folder / "report.json").read_text(encoding="utf-8"))
    data["letter"]["paragraphs"][0], data["letter"]["paragraphs"][-1] = parts["P1"], parts["P7"]
    (folder / "report.json").write_text(json.dumps(data), encoding="utf-8")
    handler, seen = writer([[parts["P3"], parts["P4"], parts["P5"]]])
    pdf, letter = run(world, MockLLM({"cover_letter": handler}))
    assert len(seen) == 1 and letter["status"] == "ok" and letter["reuse"]["score"] < R.LIMIT
