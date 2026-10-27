"""D3 interview prep pack (careerops/review/prep.py): at `mark-applied`, `prep.md` goes into the job's archive folder. Questions come from the ad's
keywords; each is answered with 1-2 fact-bank entries in situation / task + action / result order, quoting bullet ids; the gaps come from the gap report with
a one-line learn task; three questions to ask them. TRUTH RULE: facts about you come only from the fact bank; a question with no supporting bullet says
`no evidence: don't claim`. No model, no network."""
import json
import re
from pathlib import Path

import pytest

from careerops import bands, cli
from careerops.config import load_yaml
from careerops.review import prep
from conftest import PROFILE_FIX
from outreach_fakes import make_applied

NO_EVIDENCE = "no evidence: don't claim"
FB = load_yaml(PROFILE_FIX / "fact_bank.yaml")


def kw(term, weight=2, required=True):
    return {"term": term, "weight": weight, "required": required}


def report(keywords=None, claimable=None, **extra):
    kws = keywords if keywords is not None else [
        kw("Python", 3), kw("LangChain", 3), kw("Docker", 2), kw("pytest", 2), kw("SQL", 2), kw("MLflow", 1, False), kw("FastAPI", 1, False),
        kw("PyTorch", 2), kw("Kubernetes", 5), kw("Terraform", 2, False)]
    rep = {"keywords": kws, "claimable": claimable if claimable is not None else ["Python", "LangChain", "Docker", "pytest", "SQL", "MLflow", "FastAPI",
                                                                              "PyTorch"],
           "ats": {"strict": 58.0, "ceiling": 80.0, "hits": ["Python"]}, "team": "",
           "context": {"experience": [{"bullet_ids": ["az1", "az2"]}], "projects": [{"name": "Customer churn prediction", "bullet_ids": ["churn1"]}]},
           "hook": {"proof_bullet_id": "az2", "org": "Northwind Research Lab"}, "company_hook": "Northwind builds clinical decision-support tools."}
    rep.update(extra)
    return rep


JOB = {"id": "manual:northwind1", "company": "Northwind Health AI", "title": "AI Engineer", "description": "Python and LangChain."}


def build(rep=None, fb=None, job=None):
    return prep.build(job or JOB, rep or report(), fb or FB, bands.Bands(auto=70.0, review=50.0, rescue_ceiling=60.0))


def bullet_texts(fb=FB):
    return {b["id"]: b["text"] for sec in ("experience", "projects") for e in fb[sec] for b in e["bullets"]}


def sections(md):
    out, name = {}, None
    for line in md.splitlines():
        if line.startswith("## "):
            name = line[3:].strip()
            out[name] = []
        elif name:
            out[name].append(line)
    return {k: "\n".join(v) for k, v in out.items()}


def questions(md):
    return [l for l in sections(md)["Questions"].splitlines() if re.match(r"\d+\. ", l)]


# ------------------------------------------------------------------ the questions
def test_there_are_eight_to_ten_questions_from_the_ads_requirements():
    qs = questions(build())
    assert 8 <= len(qs) <= 10
    text = "\n".join(qs)
    for term in ("Python", "LangChain", "Docker", "pytest", "SQL"):
        assert term in text


def test_the_heaviest_requirements_are_asked_first_required_before_preferred():
    rep = report(keywords=[kw("Terraform", 9, False), kw("SQL", 1), kw("Docker", 4), kw("Python", 4)])
    qs = questions(build(rep))
    order = [next(t for t in ("Terraform", "SQL", "Docker", "Python") if t in q) for q in qs[:4]]
    assert order == ["Docker", "Python", "SQL", "Terraform"]                     # required by weight (the ad's order within a tie), then preferred


def test_a_thin_ad_still_gets_the_standard_questions_and_never_more_than_ten():
    assert len(questions(build(report(keywords=[kw("Python")])))) <= 10
    assert len(questions(build(report(keywords=[kw(f"Term{i}") for i in range(30)])))) == 10


def test_every_question_has_either_a_story_or_the_no_evidence_mark_or_says_no_story_is_needed():
    md = build()
    blocks = [b for b in re.split(r"\n(?=\d+\. )", sections(md)["Questions"]) if re.match(r"\d+\. ", b)]
    assert len(blocks) >= 8
    for b in blocks:
        assert "[" in b or NO_EVIDENCE in b or "no story needed" in b, b


# ------------------------------------------------------------------ the stories: fact bank only, STAR order, bullet ids
def test_a_story_quotes_the_bullet_word_for_word_with_its_id_and_the_entrys_situation():
    md = build(report(keywords=[kw("LangChain", 3)]))
    texts = bullet_texts()
    assert "[az1]" in md and texts["az1"] in md
    assert "Needed an LLM agent a research team could trust before it shipped" in md          # the entry's own `situation:`


def test_a_story_goes_situation_then_task_and_action_then_result():
    md = build(report(keywords=[kw("LangChain", 3)]))
    block = sections(md)["Questions"]
    s, a, r = block.index("Situation"), block.index("Task and action"), block.index("Result")
    assert s < a < r


def test_a_result_is_the_bullets_own_result_field_and_never_an_invented_one():
    fb = json.loads(json.dumps(FB))
    for e in fb["experience"]:
        for b in e["bullets"]:
            if b["id"] == "az1":
                b["result"] = "the whole research team used it daily"
    md = build(report(keywords=[kw("LangChain", 3)]), fb=fb)
    assert "the whole research team used it daily" in md
    plain = build(report(keywords=[kw("Docker", 3)]))
    assert re.search(r"Result:.*(none recorded|numbers in the bullet)", plain)


def test_a_question_gets_at_most_two_entries_and_the_cv_bullets_come_first():
    md = build(report(keywords=[kw("Python", 3)]))
    block = sections(md)["Questions"]
    first = block.split("\n2. ")[0]
    assert first.count("Story ") <= 2 and "Story A" in first
    assert "az1" in first or "churn1" in first                                 # on this CV (context.bullet_ids)


def test_a_keyword_the_fact_bank_cannot_back_says_no_evidence_dont_claim():
    md = build(report(keywords=[kw("Python", 3), kw("Rust", 3)], claimable=["Python"]))
    block = [q for q in re.split(r"\n(?=\d+\. )", sections(md)["Questions"]) if "Rust" in q][0]
    assert NO_EVIDENCE in block and "[" not in block.split("\n", 1)[1]


def test_a_deny_listed_term_is_never_given_a_story_even_if_a_bullet_mentions_the_word():
    fb = json.loads(json.dumps(FB))
    fb["experience"][0]["bullets"][0]["terms"].append("kubernetes")           # a stray tag on a bullet
    md = build(report(keywords=[kw("Kubernetes", 5)]), fb=fb)
    block = [q for q in re.split(r"\n(?=\d+\. )", sections(md)["Questions"]) if "Kubernetes" in q][0]
    assert NO_EVIDENCE in block and "az1" not in block


def test_no_bullet_text_in_the_pack_is_anything_but_a_fact_bank_bullet():
    md = build()
    texts = set(bullet_texts().values())
    quoted = re.findall(r"Task and action \[[\w-]+\](?: \(provisional\))?: (.+)", md)
    assert quoted and all(q.strip() in texts for q in quoted)


def test_ids_quoted_in_the_pack_all_exist_in_the_fact_bank():
    md = build()
    ids = set(re.findall(r"\[([a-z]+\d+)\]", md))
    assert ids and ids <= set(bullet_texts())


def test_a_provisional_bullet_is_marked_as_such():
    fb = json.loads(json.dumps(FB))
    fb["experience"][0]["bullets"][0]["provisional"] = True
    md = build(report(keywords=[kw("LangChain", 3)]), fb=fb)
    assert "[az1]" in md and "provisional" in md


def test_the_pack_has_no_number_that_is_in_neither_the_fact_bank_nor_the_ad():
    md = build()
    body = re.sub(r"^\d+\. ", "", md, flags=re.M)                              # list numbering
    body = re.sub(r"\[[\w-]+\]", " ", body)                                     # ids
    known = set(re.findall(r"\d[\d,]*(?:\.\d+)?", json.dumps(FB) + " " + JOB["description"] + " " + json.dumps(report())))
    ours = set(re.findall(r"\d[\d,]*(?:\.\d+)?", body))
    assert ours - {n.replace(",", "") for n in known} - known == set()


# ------------------------------------------------------------------ the gaps
def test_the_gaps_are_the_gap_reports_gap_rows_with_a_one_line_learn_task_each():
    md = build()
    gaps = sections(md)["Gaps"]
    assert "Terraform" in gaps and "Kubernetes" in gaps
    for term in ("Terraform", "Kubernetes"):
        line = next(l for l in gaps.splitlines() if term in l)
        assert "learn" in line.lower() and "\n" not in line
    assert "Python" not in gaps and "LangChain" not in gaps                    # those the fact bank backs


def test_a_deny_listed_gap_is_marked_do_not_claim():
    gaps = sections(build())["Gaps"]
    assert "do not claim" in next(l for l in gaps.splitlines() if "Kubernetes" in l).lower()


def test_a_gap_the_user_has_not_denied_gets_a_learn_task_and_no_do_not_claim():
    rep = report(keywords=[kw("Python", 3), kw("Rust", 3)], claimable=["Python"])
    line = next(l for l in sections(build(rep))["Gaps"].splitlines() if "Rust" in l)
    assert "learn" in line.lower() and "do not claim" not in line.lower()


def test_no_gaps_says_the_fact_bank_backs_every_term():
    rep = report(keywords=[kw("Python"), kw("SQL")], claimable=["Python", "SQL"])
    assert "backs every term" in sections(build(rep))["Gaps"]


# ------------------------------------------------------------------ the questions to ask them
def test_exactly_three_questions_to_ask_and_they_claim_nothing_about_you():
    asks = [l for l in sections(build())["Questions to ask them"].splitlines() if l.startswith("- ")]
    assert len(asks) == 3 and all(a.rstrip().endswith("?") for a in asks)
    assert not re.search(r"\b(I|my|I've)\b", "\n".join(asks))


def test_a_question_to_ask_names_a_term_of_the_ad():
    asks = sections(build())["Questions to ask them"]
    assert any(t in asks for t in ("Python", "LangChain", "Docker", "pytest", "SQL"))


# ------------------------------------------------------------------ the file
def test_the_pack_says_where_its_facts_come_from_and_who_it_is_for():
    md = build()
    assert md.startswith("# Interview prep: Northwind Health AI | AI Engineer")
    assert "fact bank" in md.lower() and NO_EVIDENCE in md.split("## Questions")[0]


def test_the_pack_is_written_into_the_archive_folder_and_never_over_an_existing_file(settings, db, job, tmp_path):
    row = make_applied(settings, db, job, tmp_path)
    folder = tmp_path / "Auto" / "Northwind Health AI" / "AI Engineer"
    folder.mkdir(parents=True)
    db.update_job(row["id"], application_dir=str(folder))
    path = prep.write_pack(db, settings, row["id"])
    assert path == folder / "prep.md" and path.read_text(encoding="utf-8").startswith("# Interview prep:")
    path.write_text("MY EDITS", encoding="utf-8")
    with pytest.raises(prep.PrepError, match="already"):
        prep.write_pack(db, settings, row["id"])
    assert path.read_text(encoding="utf-8") == "MY EDITS"


def test_no_archive_folder_or_no_report_is_a_clear_error_and_writes_nothing(settings, db, job, tmp_path):
    row = make_applied(settings, db, job, tmp_path)
    with pytest.raises(prep.PrepError, match="archive folder"):
        prep.write_pack(db, settings, row["id"])
    folder = tmp_path / "Auto" / "X"
    folder.mkdir(parents=True)
    db.update_job(row["id"], application_dir=str(folder), out_dir=str(tmp_path / "gone"))
    with pytest.raises(prep.PrepError, match="report"):
        prep.write_pack(db, settings, row["id"])
    assert list(folder.iterdir()) == []


def test_the_pack_is_written_after_mark_applied_and_a_failure_does_not_undo_the_application(settings, db, job, tmp_path, monkeypatch, capsys):
    row = make_applied(settings, db, job, tmp_path)
    folder = tmp_path / "Auto" / "Northwind Health AI" / "AI Engineer"
    folder.mkdir(parents=True)
    db.update_job(row["id"], application_dir=str(folder))
    monkeypatch.setattr(cli, "_archive_files", lambda *a, **k: True)
    monkeypatch.setattr(cli, "_after_applied", lambda *a, **k: None)
    assert cli._archive(db, settings, db.job(row["id"]), {}, None, None, False) is True
    assert (folder / "prep.md").is_file() and "interview prep" in capsys.readouterr().out
    (folder / "prep.md").write_text("x", encoding="utf-8")
    assert cli._archive(db, settings, db.job(row["id"]), {}, None, None, False) is True            # the second one: refused, printed, harmless
    assert "interview prep not written" in capsys.readouterr().err and db.job(row["id"])["status"] == "applied"


def test_the_users_own_archive_folder_is_never_written_to(settings, db, job, tmp_path):
    row = make_applied(settings, db, job, tmp_path)
    mine = settings.path("applications") / "Northwind Health AI" / "AI Engineer"
    mine.mkdir(parents=True)
    db.update_job(row["id"], application_dir=str(mine))
    with pytest.raises(prep.PrepError, match="your own"):
        prep.write_pack(db, settings, row["id"])
    assert list(mine.iterdir()) == []
