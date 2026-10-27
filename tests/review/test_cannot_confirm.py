"""E1: `user-cannot-confirm` next to `declined` in data/facts/proposed.yaml. It is permanent and about the KEYWORD: such a fact is never asked again, by any later
job, and never used."""
import pytest
import yaml

from careerops import bands
from careerops.config import load_yaml
from careerops.review import addfacts, factsave, gaps
from careerops.tailoring import provisional as prov

from conftest import PROFILE_FIX
from test_gap_report import report

FACT = "Scheduled a nightly Dagster pipeline that retrained a churn model, which cut manual reruns to zero."
JOB_A, JOB_B = "manual:a", "manual:b"


@pytest.fixture
def fb():
    return load_yaml(PROFILE_FIX / "fact_bank.yaml")


@pytest.fixture
def store(tmp_path):
    return prov.Store(tmp_path / "facts" / "proposed.yaml")


def gap_for(fb):
    return gaps.build(report(fb), fb, bands.Bands())


def collect(store, fb, job, *answers, said=None):
    it = iter(answers)
    asked = []

    def ask(prompt):
        asked.append(prompt)
        return next(it)
    out = addfacts.collect(ask, gap_for(fb), fb, store, job, out=(said if said is not None else []).append)
    return out, asked


def keywords_asked(asked):
    return [p.split(" (+")[0] for p in asked if "have you used it?" in p]


def test_the_state_is_named_user_cannot_confirm_and_sits_next_to_declined():
    assert (prov.DECLINED, prov.CANNOT_CONFIRM) == ("declined", "user-cannot-confirm")


def test_n_at_the_question_stores_the_keyword_as_cannot_confirm_with_no_text(store, fb):
    out, asked = collect(store, fb, JOB_A, "n", "", "", "")          # Dagster: n; enter to skip the others
    assert out == []
    [f] = [f for f in store.facts() if f.status == prov.CANNOT_CONFIRM]
    assert (f.keyword, f.text, f.job_id) == ("Dagster", "", JOB_A)
    assert yaml.safe_load(store.path.read_text(encoding="utf-8"))["facts"][0]["status"] == "user-cannot-confirm"


def test_a_fact_marked_cannot_confirm_is_never_offered_again_by_a_later_job(store, fb):
    _, first = collect(store, fb, JOB_A, "n", "", "", "")
    assert "Dagster" in keywords_asked(first)
    _, later = collect(store, fb, JOB_B, "", "", "", "")
    assert "Dagster" not in keywords_asked(later)
    assert "Dagster" not in [r.term for r in addfacts.asked_rows(gap_for(fb), store, JOB_B)]


def test_it_is_not_asked_on_the_same_job_again_either(store, fb):
    collect(store, fb, JOB_A, "n", "", "", "")
    _, again = collect(store, fb, JOB_A, "", "", "")
    assert "Dagster" not in keywords_asked(again)


def test_the_match_ignores_case_and_spacing(store, fb):
    store.mark_cannot_confirm("  DAGSTER ", JOB_A)
    assert store.cannot_confirm("dagster") and store.cannot_confirm("Dagster")
    assert "Dagster" not in [r.term for r in addfacts.asked_rows(gap_for(fb), store, JOB_B)]


def test_no_fact_can_be_stored_for_the_keyword_afterwards(store):
    store.mark_cannot_confirm("Dagster", JOB_A)
    with pytest.raises(ValueError, match="cannot confirm"):
        store.add("Dagster", FACT, "az", JOB_B)
    assert [f.status for f in store.facts()] == [prov.CANNOT_CONFIRM]


def test_a_fact_typed_earlier_for_the_keyword_is_not_used_any_more(store, fb):
    f = store.add("Dagster", FACT, "az", JOB_A)
    assert store.for_job(JOB_A) == [f] or [x.text for x in store.for_job(JOB_A)] == [FACT]
    store.mark_cannot_confirm("Dagster", JOB_B)                         # said on another job
    assert store.for_job(JOB_A) == []
    bank, _ = prov.augment(fb, store.for_job(JOB_A))
    assert not prov.provisional_bullets(bank)


def test_other_keywords_and_the_saved_fact_are_not_touched(store):
    kept = store.add("Redshift", "Loaded a Redshift warehouse nightly with a COPY job that ran in minutes.", "az", JOB_A)
    saved = store.add("Spark", "Ran Spark jobs on a laptop cluster for a coursework project on taxi data.", "az", JOB_A)
    store.mark(saved, prov.SAVED, saved_as="az9")
    store.mark_cannot_confirm("Spark", JOB_B)
    by_keyword = {k: [f.status for f in store.facts() if f.keyword == k] for k in ("Redshift", "Spark")}
    assert by_keyword["Redshift"] == [prov.PROVISIONAL]
    assert by_keyword["Spark"] == [prov.SAVED, prov.CANNOT_CONFIRM]          # the saved bullet stays saved (it is in the fact bank); the keyword is closed
    assert [x.keyword for x in store.for_job(JOB_A)] == ["Redshift"]


def test_declined_is_per_job_and_cannot_confirm_is_not(store, fb):
    f = store.add("Dagster", FACT, "az", JOB_A)
    store.mark(f, prov.DECLINED)
    assert "Dagster" in [r.term for r in addfacts.asked_rows(gap_for(fb), store, JOB_B)]          # declined: another job may ask again
    store.mark_cannot_confirm("Dagster", JOB_A)
    assert "Dagster" not in [r.term for r in addfacts.asked_rows(gap_for(fb), store, JOB_B)]


def test_it_survives_a_reload_of_the_file(store, tmp_path):
    store.mark_cannot_confirm("Dagster", JOB_A)
    assert prov.Store(store.path).cannot_confirm("Dagster")


def test_the_save_prompt_has_a_c_that_marks_the_keyword_for_good_and_writes_no_fact(settings, tmp_path, fb):
    bank = tmp_path / "profile" / "fact_bank.yaml"
    bank.parent.mkdir()
    bank.write_bytes((PROFILE_FIX / "fact_bank.yaml").read_bytes())
    settings.raw["paths"]["fact_bank"] = str(bank)
    before = bank.read_bytes()
    st = prov.store(settings)
    fact = st.add("Dagster", FACT, "az", JOB_A)
    rep = {"provisional": [{"id": fact.bullet_id, "keyword": "Dagster", "text": FACT, "on_cv": True}]}
    said = []
    done = factsave.offer(settings, load_yaml(bank), JOB_A, rep, lambda p: "c", sent=True, out=said.append)
    assert done == [] and bank.read_bytes() == before
    assert st.cannot_confirm("Dagster") and st.for_job(JOB_A) == []
    assert not [r for r in addfacts.asked_rows(gap_for(fb), st, JOB_B) if r.term == "Dagster"]
    assert any("cannot confirm" in line for line in said)


def test_the_question_tells_the_user_about_n():
    assert "n if you cannot confirm it (never asked again)" in addfacts.PROMPT
