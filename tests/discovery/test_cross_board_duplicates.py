"""One job posted on two boards of one employer ("graphcore" and "graphcore-early-careers") is one job. The duplicate key is the company FAMILY
(the name without "Early Careers", "Careers", "UK", "Ltd" ...) plus the normalised title. Discovery keeps the newer copy and marks the older
inbox file `Duplicate of: <the newer file>`; `live` does not work on a marked file; the auto-apply duplicate gate uses the same key."""
from datetime import datetime, timezone

import pytest

from careerops.apply import autogate
from careerops.discovery import candidate as cand, discover as disc, manual
from careerops.discovery.candidate import Candidate
from careerops.discovery.discover import Discover, Known
from careerops.discovery.http import Fetcher
from careerops.discovery.robots import RobotsCache
from netfake import Net

TODAY = datetime(2026, 10, 21, 12, tzinfo=timezone.utc)
JD = "Graduate Machine Learning Engineer. You will build and evaluate models in Python. " * 20
TITLE = "Graduate Machine Learning Engineer 2027"


def graphcore(board: str, company: str, posted: str, ext_id: str, title: str = TITLE, location: str = "Bristol, UK") -> Candidate:
    url = f"https://job-boards.greenhouse.io/{board}/jobs/{ext_id}"
    return Candidate(source="greenhouse", company=company, title=title, location=location, url=url, description=JD, posted=posted,
                     posted_source="greenhouse.first_published", ext_id=ext_id, board=f"greenhouse:{board}", apply_url=url, apply_kind="greenhouse")


OLDER = graphcore("graphcore", "Graphcore", "2026-09-30T09:00:00+00:00", "7001")
NEWER = graphcore("graphcore-early-careers", "Graphcore Early Careers", "2026-10-14T09:00:00+00:00", "8002")


@pytest.fixture
def d(settings, db):
    settings.raw["discover"]["alert_probes_per_run"] = 0
    settings.raw["discover"]["sources"] = []
    settings.raw["discover"]["listed_only"] = {"enabled": False}
    fetcher = Fetcher(RobotsCache(), get=Net(strict=False).get, sleep=lambda s: None, clock=lambda: 0.0, gap_s=0.0)
    return Discover(settings, db, fetcher, today=TODAY, say=lambda s: None)


def inbox(settings) -> dict[str, str]:
    box = settings.path("jds") / "inbox"
    return {p.name: p.read_text(encoding="utf-8") for p in sorted(box.glob("*.txt"))} if box.exists() else {}


@pytest.mark.parametrize("a,b", [("Graphcore", "Graphcore Early Careers"), ("Graphcore", "Graphcore Careers"), ("Graphcore UK", "Graphcore Ltd"),
                                 ("Graphcore Limited", "GRAPHCORE (UK) Early Careers"), ("Acme & Co", "Acme and Co Careers")])
def test_the_company_family_strips_early_careers_careers_uk_and_legal_suffixes(a, b):
    assert cand.family_key(a) == cand.family_key(b) != ""


@pytest.mark.parametrize("a,b", [("Graphcore", "Graphite Health"), ("Monzo", "Monzo Bank Careers Ltd Europe"), ("Careers", "Careers Hub")])
def test_different_employers_keep_different_families(a, b):
    assert cand.family_key(a) != cand.family_key(b) or cand.family_key(a) == cand.family_key(b) == ""


def test_a_name_that_is_only_noise_keeps_its_own_key():
    assert cand.family_key("Careers") == "careers" and cand.family_key("UK Ltd") != ""


def test_known_finds_the_same_job_under_the_other_boards_name_whatever_the_place_says():
    k = Known()
    k.add("Graphcore", TITLE, "Bristol, UK", "found earlier")
    assert k.where("Graphcore Early Careers", TITLE, "Bristol, UK") == "found earlier"
    assert k.where("Graphcore Early Careers", TITLE, "Cambridge, UK") == "found earlier"          # another board's spelling of the place
    assert k.where("Graphcore", TITLE, "Cambridge, UK") is None                                    # the same name keeps its place rule
    assert k.where("Graphcore Early Careers", "Graduate Data Scientist 2027", "Bristol, UK") is None


def test_the_two_graphcore_postings_collapse_to_one_job_and_the_newer_copy_is_kept(settings, d):
    assert d.accept(OLDER, "ats") == "written"
    assert d.accept(NEWER, "ats") == "written"
    files = inbox(settings)
    live = {n: t for n, t in files.items() if "Duplicate of:" not in t}
    marked = {n: t for n, t in files.items() if "Duplicate of:" in t}
    assert len(files) == 2 and len(live) == 1 and len(marked) == 1
    (new_name, new_text), (old_name, old_text) = next(iter(live.items())), next(iter(marked.items()))
    assert "Company: Graphcore Early Careers" in new_text and "Company: Graphcore\n" in old_text
    assert f"Duplicate of: {new_name}" in old_text
    assert old_text.split("\n\n", 1)[1] == files[old_name].split("\n\n", 1)[1] and "Build and evaluate" not in old_text      # the ad itself is untouched
    assert d.run.sources["ats"]["superseded"] == 1


def test_the_newer_copy_seen_first_makes_the_older_one_a_plain_duplicate(settings, d):
    assert d.accept(NEWER, "ats") == "written"
    assert d.accept(OLDER, "ats") == "dropped"
    files = inbox(settings)
    assert len(files) == 1 and "Duplicate of:" not in next(iter(files.values())) and d.run.sources["ats"]["duplicate"] == 1


def test_two_copies_without_a_posting_time_keep_the_first(settings, d):
    a, b = graphcore("graphcore", "Graphcore", "", "7001"), graphcore("graphcore-early-careers", "Graphcore Early Careers", "", "8002")
    assert d.accept(a, "ats") == "written" and d.accept(b, "ats") == "dropped"
    assert len(inbox(settings)) == 1


def test_a_dry_run_marks_nothing(settings, d):
    d.dry_run = True
    d.accept(OLDER, "ats")
    d.accept(NEWER, "ats")
    assert inbox(settings) == {} and d.run.sources["ats"]["superseded"] == 1


def test_an_older_copy_already_in_the_inbox_from_an_earlier_run_is_marked_too(settings, d, db):
    d.accept(OLDER, "ats")
    d2 = Discover(settings, db, d.f, today=TODAY, say=lambda s: None)                 # a new run: the inbox file is found by its headers
    assert d2.accept(NEWER, "ats") == "written"
    assert sum("Duplicate of:" in t for t in inbox(settings).values()) == 1


def test_a_marked_file_is_not_a_job_for_live_and_the_header_is_read():
    text = "Company: Graphcore\nTitle: Graduate Machine Learning Engineer 2027\nURL: https://x.example/1\nDuplicate of: graphcore-early-careers-1.txt\n\nbody"
    assert manual.header_fields(text)["duplicate_of"] == "graphcore-early-careers-1.txt"


# ------------------------------------------------------------------ the auto-apply duplicate gate
def test_a_job_applied_to_under_the_other_boards_name_is_a_duplicate_for_the_gate(settings, db):
    applied = {"id": "greenhouse:graphcore:7001", "source": "greenhouse", "company": "Graphcore", "title": TITLE, "location": "Bristol, UK",
               "url": "https://x.example/7001", "apply_url": "https://x.example/7001", "description": JD}
    db.upsert_job(applied)
    db.update_job(applied["id"], status="applied", applied_at="2026-10-01T10:00:00+00:00")
    other = {**applied, "id": "greenhouse:graphcore-early-careers:8002", "company": "Graphcore Early Careers", "status": "needs_review",
             "applied_at": None}
    why = autogate.duplicate_reason(db, settings, other, [], None, True)
    assert why is not None and "greenhouse:graphcore:7001" in why
