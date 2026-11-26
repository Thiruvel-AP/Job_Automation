"""v3.9.7: the company is stored as the ad writes it (not title-cased from a slug); the Sponsor line never contradicts itself."""
import pytest

from careerops.discovery import manual
from careerops.review.workflow import sponsor_tail
from careerops.tailoring.letters import greeting

LSEG_AD = "Company: Lseg\nTitle: Data Scientist\nURL: https://x.example/1\n\nABOUT US:\nLSEG (London Stock Exchange Group) is a markets business.\n"


@pytest.mark.parametrize("company,title,text,want", [
    ("Lseg", "Data Scientist", LSEG_AD, "LSEG"),
    ("Kong Inc", "Data Scientist", "Kong is an API company.", "Kong"),
    ("Deliveroo", "Data Scientist", "Join deliveroo and help riders.", "Deliveroo"),
])
def test_display_name_reads_the_ads_words_not_its_header(company, title, text, want):
    assert manual.display_name(company, title, text) == want


def test_lseg_greeting(settings):
    name = manual.display_name("Lseg", "Data Scientist", LSEG_AD)
    assert greeting({"company": name}, "", settings) == "Hi LSEG team"


def test_add_jd_stores_the_display_name_and_refresh_fixes_an_old_row(db):
    jid = manual.add_jd(db, LSEG_AD * 20, company="Lseg", title="Data Scientist", url="https://x.example/1")
    assert db.job(jid)["company"] == "LSEG"
    db.update_job(jid, company="Lseg")
    assert manual.refresh_company(db, jid) is True and db.job(jid)["company"] == "LSEG"
    assert manual.refresh_company(db, jid) is False


def test_sponsor_tail_matched_says_on_the_register_and_drops_the_stale_note():
    job = {"sponsor_name": "London Stock Exchange Group plc", "sponsor_match": "alias"}
    t = sponsor_tail(job, ["Not on sponsor register", "Sponsor matched via alias 'LSEG': register name 'London Stock Exchange Group plc' — verify"])
    assert 'MATCHED via alias "LSEG" -> on the sponsor register' in t and "Not on sponsor register" not in t


def test_sponsor_tail_not_matched_says_not_on_register():
    t = sponsor_tail({"sponsor_name": None, "sponsor_match": "none"}, ["Not on sponsor register"])
    assert "Not on sponsor register" in t and "MATCHED" not in t
