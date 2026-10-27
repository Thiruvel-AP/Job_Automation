"""Agency or direct, same role (discovery/channel.py): before tailoring, the tracker and the archive are checked for the same company and role under another
channel (an agency vs direct, or two agencies). If found: stop and ask which channel owns it. If an agency hides the employer: ask before going on. Never on
silence. Pure functions over job rows and archive (company, role) pairs: no database, no network."""
import pytest

from careerops.discovery import channel


def job(company="Acme", title="Data Scientist", agency=0, description="", location="London", status="triaged", id="x:1", url=""):
    return {"id": id, "company": company, "title": title, "agency": agency, "description": description, "location": location, "status": status, "url": url or f"https://x/{id}"}


HAYS = "Our client, Acme Ltd, is a leading retailer of forecasting tools. You will build models."


# ------------------------------------------------------------------ who the employer is
@pytest.mark.parametrize("text,employer", [
    ("Our client, Acme Ltd, is hiring a data scientist.", "Acme Ltd"),
    ("We are recruiting on behalf of Northwind Health AI for a senior role.", "Northwind Health AI"),
    ("Client: Globex Corporation\nLocation: London", "Globex Corporation"),
    ("Our client is Initech and they are growing fast.", "Initech"),
    ("Our client, The Acme Group, needs a data scientist", "Acme Group"),
    ("Our client is a leading fintech scale-up in London.", None),
    ("Our client is an award-winning retailer.", None),
    ("A great opportunity with a confidential client.", None),
    ("", None),
])
def test_the_employer_an_agency_ad_names(text, employer):
    assert channel.employer_of(text) == employer


def test_channel_and_hidden_employer():
    assert channel.channel_of(job(agency=1)) == "agency" and channel.channel_of(job(agency=0)) == "direct" and channel.channel_of({"company": "x"}) == "direct"
    assert channel.hidden_employer(job("Hays", agency=1, description="Our client is a leading retailer."))
    assert not channel.hidden_employer(job("Hays", agency=1, description=HAYS))
    assert not channel.hidden_employer(job("Acme", agency=0, description="Our client is a leading retailer."))


# ------------------------------------------------------------------ the same role under another channel
def test_direct_job_and_an_agency_ad_that_names_the_same_employer():
    this = job("Acme", agency=0, id="x:1")
    other = job("Hays", agency=1, description=HAYS, id="x:2")
    (m,) = channel.find(this, [other], [])
    assert m.where == "tracker" and "Hays" in m.label and m.channel == "agency" and m.other_id == "x:2"


def test_an_agency_ad_and_the_same_role_applied_direct():
    this = job("Hays", agency=1, description=HAYS, id="x:1")
    (m,) = channel.find(this, [job("Acme", agency=0, status="applied", id="x:2")], [])
    assert m.channel == "direct" and "applied" in m.label


def test_two_agencies_naming_the_same_employer():
    a = job("Hays", agency=1, description=HAYS, id="x:1")
    b = job("Reed Specialist", agency=1, description="On behalf of Acme Ltd we are hiring", id="x:2")
    assert len(channel.find(a, [b], [])) == 1


def test_two_agencies_that_name_nobody_match_on_title_and_place_only():
    a = job("Hays", agency=1, description="Our client is a leading retailer.", location="London, UK", id="x:1")
    same_place = job("Reed", agency=1, description="Our client is an award-winning retailer.", location="London", id="x:2")
    elsewhere = job("Reed", agency=1, description="Our client is an award-winning retailer.", location="Leeds", id="x:3")
    assert len(channel.find(a, [same_place], [])) == 1 and channel.find(a, [elsewhere], []) == []


def test_the_same_company_on_the_same_channel_is_a_duplicate_not_another_channel():
    assert channel.find(job("Acme", id="x:1"), [job("Acme Ltd", id="x:2")], []) == []
    assert channel.find(job("Hays", agency=1, description=HAYS, id="x:1"), [job("Hays", agency=1, description=HAYS, id="x:2")], []) == []


def test_a_different_role_is_not_the_same_role():
    assert channel.find(job("Acme", id="x:1"), [job("Hays", agency=1, description=HAYS, title="ML Engineer", id="x:2")], []) == []
    assert channel.find(job("Acme", id="x:1"), [job("Hays", agency=1, description=HAYS, title="Senior Data Scientist", id="x:2")], []) == []


def test_title_words_in_a_different_order_are_the_same_role():
    assert len(channel.find(job("Acme", title="Data Scientist (m/f/d)", id="x:1"), [job("Hays", title="data scientist", agency=1, description=HAYS, id="x:2")], [])) == 1


@pytest.mark.parametrize("status", ["skipped", "skipped (rules)", "skipped (user)", "rejected", "closed", "error"])
def test_jobs_you_already_dropped_do_not_count(status):
    assert channel.find(job("Acme", id="x:1"), [job("Hays", agency=1, description=HAYS, status=status, id="x:2")], []) == []


def test_the_job_itself_is_never_its_own_other_channel():
    me = job("Acme", id="x:1")
    assert channel.find(me, [me], []) == []


def test_the_archive_is_checked_by_company_folder_and_role_folder():
    this = job("Acme", id="x:1")
    (m,) = channel.find(this, [], [("Acme Ltd", "Data Scientist"), ("Acme", "ML Engineer"), ("Other", "Data Scientist")])
    assert m.where == "archive" and "Acme Ltd/Data Scientist" in m.label and m.channel == "unknown"
    ag = job("Hays", agency=1, description=HAYS, id="x:1")
    assert len(channel.find(ag, [], [("Acme", "Data Scientist")])) == 1                  # the employer an agency names is checked as well


def test_a_version_folder_below_the_role_folder_does_not_matter():
    assert len(channel.find(job("Acme"), [], [("Acme", "Data Scientist")])) == 1


# ------------------------------------------------------------------ what is asked
def test_the_questions_name_both_sides_and_default_to_not_going_on():
    this = job("Acme", id="x:1")
    (m,) = channel.find(this, [job("Hays", agency=1, description=HAYS, id="x:2")], [])
    q = channel.channel_question(this, [m])
    assert "Data Scientist" in q and "Hays" in q and "direct" in q and "agency" in q and q.endswith("[c]ontinue / [s]kip > ")
    assert channel.proceeds("c") and channel.proceeds("Continue") and channel.proceeds(" C ")
    for silence in ("", "  ", "s", "x", "yes please", None):
        assert not channel.proceeds(silence)
    h = channel.hidden_question(job("Hays", agency=1, description="Our client is a leading retailer."))
    assert "Hays" in h and "does not name" in h and h.endswith("[y/N] ")
    assert channel.agrees("y") and channel.agrees("YES") and not channel.agrees("") and not channel.agrees(None) and not channel.agrees("n")
