"""After the job (review/factsave.py): each typed fact the CV used is offered for the fact bank, `Add to fact_bank.yaml under <role>? "<text>"
[y/N/e(dit)]`. A `y` goes through factbank_append (backup, append, validate); N declines it for good; nothing else writes the bank."""
import hashlib
from datetime import date

import pytest
import yaml

from careerops import mockroot
from careerops.config import load_yaml
from careerops.review import factsave
from careerops.tailoring import provisional as prov

from conftest import PROFILE_FIX

FACT = "Scheduled a nightly Dagster pipeline that retrained a churn model, which cut manual reruns to zero."
JOB = "manual:gap1"


@pytest.fixture
def kit(settings, tmp_path):
    bank = tmp_path / "profile" / "fact_bank.yaml"
    bank.parent.mkdir()
    bank.write_bytes((PROFILE_FIX / "fact_bank.yaml").read_bytes())
    settings.raw["paths"]["fact_bank"] = str(bank)
    fb = load_yaml(bank)
    store = prov.store(settings)
    fact = store.add("Dagster", FACT, "az", JOB)
    rep = {"provisional": [{"id": fact.bullet_id, "keyword": "Dagster", "text": FACT, "on_cv": True}]}

    class Kit:
        pass
    k = Kit()
    k.settings, k.bank, k.fb, k.store, k.fact, k.rep, k.said, k.asked = settings, bank, fb, store, fact, rep, [], []
    k.sha = lambda: hashlib.sha256(bank.read_bytes()).hexdigest()

    def run(*answers, sent=True, why="", job=JOB, rep_=None):
        it = iter(answers)

        def ask(prompt):
            k.asked.append(prompt)
            return next(it)

        return factsave.offer(settings, fb, job, rep if rep_ is None else rep_, ask, sent=sent, why=why, out=k.said.append)
    k.run = run
    return k


def test_a_y_adds_the_fact_under_its_role_with_a_backup(kit):
    old = kit.bank.read_bytes()
    [done] = kit.run("y")
    assert done.bullet_id == "az6" and done.backup.read_bytes() == old
    assert done.backup.parent == kit.bank.parent / "backups" and done.backup.name.startswith("fact_bank.")
    new = yaml.safe_load(kit.bank.read_text(encoding="utf-8"))["experience"][0]["bullets"][-1]
    assert new == {"id": "az6", "text": FACT, "terms": ["Dagster"], "source": "user_" + date.today().strftime("%Y%m%d")}


def test_the_question_names_the_role_and_quotes_the_text(kit):
    kit.run("n")
    assert len(kit.asked) == 1
    q = kit.asked[0]
    assert q.startswith("Add to fact_bank.yaml under az: Data Scientist") and q.endswith(f'"{FACT}" [y/N/e(dit)] ')


def test_a_y_marks_the_fact_saved_so_it_is_not_provisional_any_more(kit):
    kit.run("y")
    [f] = kit.store.facts()
    assert f.status == "saved" and f.saved_as == "az6" and kit.store.for_job(JOB) == []


def test_the_summary_says_what_was_added_and_where_the_backup_is(kit):
    [done] = kit.run("y")
    text = "\n".join(kit.said)
    assert "Added to fact_bank.yaml:" in text and "az6" in text and FACT in text and str(done.backup) in text


def test_the_fact_bank_held_in_memory_sees_the_new_bullet(kit):
    kit.run("y")
    assert [b["id"] for b in kit.fb["experience"][0]["bullets"]][-1] == "az6"


@pytest.mark.parametrize("answer", ["n", "N", "", "yes", "later", " "])
def test_anything_but_y_declines_it_and_writes_nothing(kit, answer):
    before = kit.sha()
    assert kit.run(answer) == []
    assert kit.sha() == before and not (kit.bank.parent / "backups").exists()
    assert kit.store.facts()[0].status == "declined"
    kit.asked.clear()
    assert kit.run() == [] and kit.asked == []                                               # a declined fact is never offered or used again
    assert kit.store.for_job(JOB) == []


def test_e_lets_the_user_retype_it_and_only_a_y_then_writes_it(kit):
    own = "Ran the nightly Dagster retraining of a churn model for a coursework project."
    [done] = kit.run("e", own, "y")
    assert done.text == own
    assert yaml.safe_load(kit.bank.read_text(encoding="utf-8"))["experience"][0]["bullets"][-1]["text"] == own
    assert kit.store.facts()[0].text == FACT                                                 # what was first typed stays on record


def test_e_then_n_writes_nothing(kit):
    before = kit.sha()
    assert kit.run("e", "Ran Dagster nightly.", "n") == [] and kit.sha() == before
    assert kit.store.facts()[0].status == "declined"


def test_e_with_an_empty_line_keeps_the_text_as_typed(kit):
    [done] = kit.run("e", "", "y")
    assert done.text == FACT


def test_a_job_not_sent_says_so_in_so_many_words(kit):
    kit.run("n", sent=False, why="you did not submit it")
    text = "\n".join(kit.said)
    assert "NOT SENT" in text and "this CV was not sent" in text and "you did not submit it" in text


def test_a_job_that_was_sent_does_not_say_it_was_not(kit):
    kit.run("n", sent=True)
    assert "not sent" not in "\n".join(kit.said).lower()


def test_a_fact_the_cv_does_not_use_is_not_offered(kit):
    rep = {"provisional": [{"id": kit.fact.bullet_id, "keyword": "Dagster", "text": FACT, "on_cv": False}]}
    assert kit.run(rep_=rep) == [] and kit.asked == [] and kit.store.for_job(JOB)           # still provisional, still its own job's


def test_a_job_with_no_typed_facts_asks_nothing(kit):
    assert kit.run(rep_={"provisional": []}) == [] and kit.run(rep_={}) == [] and kit.asked == []


def test_another_jobs_fact_is_not_offered(kit):
    assert kit.run(job="manual:other") == [] and kit.asked == []


def test_a_mock_run_never_writes_the_real_fact_bank(kit, tmp_path):
    kit.settings.raw["paths"]["mock_root"] = str(tmp_path / "mock")                     # the mock root of this test, not the repo's data/mock
    mock = mockroot.mock_settings(kit.settings)
    mock_store = prov.store(mock)
    f = mock_store.add("Dagster", FACT, "az", JOB)
    rep = {"provisional": [{"id": f.bullet_id, "keyword": "Dagster", "text": FACT, "on_cv": True}]}
    before = kit.sha()
    said = []
    assert factsave.offer(mock, kit.fb, JOB, rep, lambda p: pytest.fail("a mock run asks nothing"), sent=True, out=said.append) == []
    assert kit.sha() == before and "--mock" in "\n".join(said)


def test_a_fact_that_would_break_the_deny_list_is_not_added_and_stays_provisional(kit):
    bad = kit.store.add("Terraform", "Provisioned the cluster with Terraform.", "az", JOB)
    rep = {"provisional": [{"id": bad.bullet_id, "keyword": "Terraform", "text": bad.text, "on_cv": True}]}
    before = kit.sha()
    assert kit.run("y", rep_=rep) == [] and kit.sha() == before
    assert "NOT added" in "\n".join(kit.said)
    assert bad.status == "provisional" and bad.bullet_id in [f.bullet_id for f in kit.store.for_job(JOB)]


def test_two_facts_are_asked_one_by_one(kit):
    second = kit.store.add("Spark", "Ran Spark jobs on a university cluster for a coursework pipeline.", "dc", JOB)
    rep = {"provisional": [{"id": kit.fact.bullet_id, "keyword": "Dagster", "text": FACT, "on_cv": True},
                           {"id": second.bullet_id, "keyword": "Spark", "text": second.text, "on_cv": True}]}
    done = kit.run("y", "n", rep_=rep)
    assert [d.bullet_id for d in done] == ["az6"] and len(kit.asked) == 2
    assert {f.keyword: f.status for f in kit.store.facts()} == {"Dagster": "saved", "Spark": "declined"}
