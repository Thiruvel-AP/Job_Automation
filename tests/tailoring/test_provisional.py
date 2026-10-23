"""Provisional facts (tailoring/provisional.py): what the user typed for a gap keyword during review. They go to data/facts/proposed.yaml, never to
the fact bank; a re-tailor sees them as extra bullets of the role the user named, marked provisional, and ONLY for the job they were typed for."""
import copy

import pytest
import yaml

from careerops.config import load_yaml
from careerops.tailoring import provisional as prov
from careerops.tailoring.faithfulness import check
from careerops.tailoring.scorer import Keyword

from conftest import PROFILE_FIX


@pytest.fixture
def fb() -> dict:
    return load_yaml(PROFILE_FIX / "fact_bank.yaml")


@pytest.fixture
def store(tmp_path) -> prov.Store:
    return prov.Store(tmp_path / "facts" / "proposed.yaml")


TEXT = 'Scheduled the nightly retraining of a churn model in Dagster at "Northwind": 40% fewer manual reruns (£0 extra).'


def test_a_fact_is_stored_with_the_users_exact_text_and_what_it_belongs_to(store):
    f = store.add("Dagster", TEXT, "az", "manual:abc", on="2026-10-23")
    assert (f.keyword, f.text, f.target, f.job_id, f.date, f.status) == ("Dagster", TEXT, "az", "manual:abc", "2026-10-23", "provisional")
    raw = yaml.safe_load(store.path.read_text(encoding="utf-8"))["facts"]
    assert raw[0]["text"] == TEXT and raw[0]["keyword"] == "Dagster" and raw[0]["target"] == "az"
    assert raw[0]["job_id"] == "manual:abc" and str(raw[0]["date"]) == "2026-10-23"


def test_nothing_is_written_until_a_fact_is_added(store):
    assert store.facts() == [] and not store.path.exists()


def test_facts_survive_a_reload(store):
    store.add("Dagster", TEXT, "az", "manual:abc")
    again = prov.Store(store.path)
    assert [f.text for f in again.facts()] == [TEXT]


def test_an_empty_or_multiline_answer_is_not_a_fact(store):
    for bad in ("", "   ", "two\nlines"):
        with pytest.raises(ValueError):
            store.add("Dagster", bad, "az", "manual:abc")
    assert store.facts() == []


def test_a_fact_is_only_provisional_for_the_job_it_was_typed_for(store):
    store.add("Dagster", TEXT, "az", "manual:A")
    store.add("Spark", "Ran Spark jobs on a university cluster for a coursework pipeline.", "dc", "manual:B")
    assert [f.keyword for f in store.for_job("manual:A")] == ["Dagster"]
    assert [f.keyword for f in store.for_job("manual:B")] == ["Spark"]
    assert store.for_job("manual:C") == []


def test_a_declined_fact_is_never_used_again(store):
    f = store.add("Dagster", TEXT, "az", "manual:A")
    store.mark(f, prov.DECLINED)
    assert store.for_job("manual:A") == []
    assert prov.Store(store.path).facts()[0].status == "declined"             # kept in the file, marked


def test_a_saved_fact_is_in_the_bank_now_so_it_is_not_provisional_any_more(store):
    f = store.add("Dagster", TEXT, "az", "manual:A")
    store.mark(f, prov.SAVED, saved_as="az7")
    assert store.for_job("manual:A") == [] and prov.Store(store.path).facts()[0].saved_as == "az7"


def test_augment_adds_a_marked_bullet_to_the_named_role_and_leaves_the_bank_alone(fb, store):
    original = copy.deepcopy(fb)
    f = store.add("Dagster", TEXT, "az", "manual:A")
    aug, notes = prov.augment(fb, [f])
    assert fb == original                                                       # the real bank is not touched in memory either
    az = next(e for e in aug["experience"] if e["id"] == "az")
    new = az["bullets"][-1]
    assert new["provisional"] is True and new["text"] == TEXT and new["terms"] == ["Dagster"] and new["verbatim"] is True
    assert new["id"] not in {b["id"] for sec in ("experience", "projects") for e in fb[sec] for b in e["bullets"]}
    assert notes == []


def test_augment_can_add_to_a_project_too(fb, store):
    f = store.add("Dagster", TEXT, "churn", "manual:A")
    aug, _ = prov.augment(fb, [f])
    assert aug["projects"][0]["id"] == "churn" and aug["projects"][0]["bullets"][-1]["provisional"] is True


def test_augment_skips_a_fact_whose_role_is_gone_and_says_so(fb, store):
    f = store.add("Dagster", TEXT, "nope", "manual:A")
    aug, notes = prov.augment(fb, [f])
    assert "nope" in notes[0] and "Dagster" in notes[0] and not any(b.get("provisional") for e in aug["experience"] for b in e["bullets"])


def test_augment_with_no_facts_returns_an_equal_bank(fb):
    aug, notes = prov.augment(fb, [])
    assert aug == fb and notes == []


def test_the_claim_guard_accepts_a_wording_of_the_typed_fact_only_in_the_bank_built_for_its_job(fb, store):
    """faithfulness.check takes the bullet as its source: in the job's own bank the provisional bullet is a source for 'Dagster', in another
    job's bank (which has no such bullet) a text that says Dagster has no source."""
    f = store.add("Dagster", TEXT, "az", "manual:A")
    mine, _ = prov.augment(fb, store.for_job("manual:A"))
    other, _ = prov.augment(fb, store.for_job("manual:B"))
    kws = [Keyword("Dagster", 2, True)]
    bullet = next(b for e in mine["experience"] for b in e["bullets"] if b.get("provisional"))
    ok = check(TEXT, bullet["text"], bullet["terms"], mine.get("deny_terms", []), kws)
    assert ok.ok
    assert not any(b.get("provisional") for e in other["experience"] for b in e["bullets"])
    nope = check(TEXT, other["experience"][0]["bullets"][0]["text"], other["experience"][0]["bullets"][0]["terms"], other.get("deny_terms", []), kws)
    assert not nope.ok and any("Dagster" in v for v in nope.violations)


def test_marked_bullets_of_a_bank_are_listed(fb, store):
    f = store.add("Dagster", TEXT, "az", "manual:A")
    aug, _ = prov.augment(fb, [f])
    assert [b["text"] for b in prov.provisional_bullets(aug)] == [TEXT] and prov.provisional_bullets(fb) == []


def test_the_deny_list_stops_a_fact_before_it_is_stored(fb):
    assert prov.deny_problem(fb, "Provisioned the cluster with Terraform.") == "terraform"
    assert prov.deny_problem(fb, TEXT) is None
