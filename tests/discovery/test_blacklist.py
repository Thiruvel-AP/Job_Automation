"""The blacklist (discovery/blacklist.py): blacklist.yaml (beside the fact bank) is the user's own, code only reads it. Entries {name, scope: company|domain, since,
reason}. A hit stops before tailoring and asks `<company> is on your blacklist since <date>: <reason>. Apply anyway? [y/N]`. A domain matches the exact host or a
subdomain, never a lookalike."""
import inspect
from datetime import date

import pytest

from careerops.discovery import blacklist as bl


def make(tmp_path, text):
    p = tmp_path / "blacklist.yaml"
    p.write_text(text, encoding="utf-8")
    return bl.load(p)


YAML = """
blacklist:
  - {name: Acme Recruitment, scope: company, since: 2026-03-01, reason: ghosted me twice}
  - {name: ibm.com, scope: domain, since: "2025-11-20", reason: unpaid take-home task}
"""


def test_a_company_entry_matches_the_employer_family_not_a_substring(tmp_path):
    b = make(tmp_path, YAML)
    assert b.hit("Acme Recruitment Ltd") is not None and b.hit("ACME RECRUITMENT") is not None and b.hit("acme recruitment (UK)") is not None
    assert b.hit("Acme") is None and b.hit("Acme Recruitment Services Group") is None and b.hit("") is None


@pytest.mark.parametrize("url,hit", [
    ("https://ibm.com/careers/job/1", True),
    ("https://www.ibm.com/careers/job/1", True),
    ("https://careers.ibm.com/job/1", True),
    ("https://a.b.ibm.com:8443/x", True),
    ("HTTPS://CAREERS.IBM.COM/x", True),
    ("https://notibm.com/careers", False),
    ("https://ibm.com.evil.example/careers", False),
    ("https://myibm.com/", False),
    ("https://ibm.co/", False),
    ("", False),
])
def test_a_domain_entry_matches_the_host_or_a_subdomain_and_never_a_lookalike(tmp_path, url, hit):
    b = make(tmp_path, YAML)
    assert (b.hit("Some Company", url) is not None) is hit


def test_either_address_of_the_job_can_hit(tmp_path):
    b = make(tmp_path, YAML)
    assert b.hit("Some Company", "https://boards.greenhouse.io/x/jobs/1", "https://careers.ibm.com/apply/1")


def test_the_domain_entry_may_be_written_as_an_address(tmp_path):
    b = make(tmp_path, "- {name: 'https://www.ibm.com/uk-en', scope: domain, since: 2025-01-01, reason: x}\n")
    assert b.hit("x", "https://careers.ibm.com/1") and not b.hit("x", "https://notibm.com/1")


def test_the_question_names_the_company_the_date_and_the_reason(tmp_path):
    b = make(tmp_path, YAML)
    e = b.hit("Acme Recruitment Ltd")
    assert e.since == "2026-03-01"
    assert bl.question(e, "Acme Recruitment Ltd") == "Acme Recruitment Ltd is on your blacklist since 2026-03-01: ghosted me twice. Apply anyway? [y/N] "
    d = b.hit("Some Company", "https://careers.ibm.com/1")
    assert bl.question(d, "Some Company") == "Some Company is on your blacklist since 2025-11-20: unpaid take-home task. Apply anyway? [y/N] "


def test_a_missing_file_is_an_empty_blacklist(tmp_path):
    b = bl.load(tmp_path / "nope.yaml")
    assert b.entries == [] and b.hit("Anyone", "https://x.example/") is None


def test_a_list_at_the_top_level_and_an_entry_without_a_reason_or_date_are_accepted(tmp_path):
    b = make(tmp_path, "- {name: Foo Ltd}\n")
    e = b.hit("Foo")
    assert e.scope == "company" and e.since == "" and e.reason == ""
    assert bl.question(e, "Foo").startswith("Foo is on your blacklist: ") or "blacklist" in bl.question(e, "Foo")


def test_a_date_written_as_a_yaml_date_is_read(tmp_path):
    b = make(tmp_path, "blacklist:\n  - {name: Foo, scope: company, since: 2026-01-05, reason: r}\n")
    assert b.hit("Foo").since == date(2026, 1, 5).isoformat()


@pytest.mark.parametrize("text,words", [
    ("blacklist: [", "cannot be read"),
    ("blacklist: 5\n", "list"),
    ("blacklist:\n  - {scope: company}\n", "name"),
    ("blacklist:\n  - {name: Foo, scope: planet}\n", "scope"),
    ("blacklist:\n  - just a string\n", "entry 1"),
])
def test_a_file_that_cannot_be_understood_is_an_error_never_an_empty_blacklist(tmp_path, text, words):
    with pytest.raises(bl.BlacklistError, match=words):
        make(tmp_path, text)


def test_code_only_reads_the_file(tmp_path):
    p = tmp_path / "blacklist.yaml"
    p.write_text(YAML, encoding="utf-8")
    before = p.read_bytes()
    bl.load(p).hit("Acme Recruitment")
    assert p.read_bytes() == before
    src = inspect.getsource(bl)
    assert ".write" not in src and "open(" not in src


def test_the_file_lives_beside_the_fact_bank(settings):
    assert bl.path_for(settings) == settings.path("fact_bank").parent / "blacklist.yaml"
