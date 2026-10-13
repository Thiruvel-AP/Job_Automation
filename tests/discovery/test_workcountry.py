"""W(job): where the work is done (discovery/workcountry.py), generic around visa.home. On-site / hybrid = the office country; remote with no
restriction = home; remote restricted to places that exclude home = that place; else None. A remote role at home is only ever called
`visa.remote.label` on anything you read. Plain rules over the Location header and the ad text: no model, no network."""
import pytest

from careerops.apply import visa
from careerops.discovery import countries, gates, priority, workcountry
from careerops.discovery.workcountry import Rules, resolve

HOME = Rules(home="GB", label="Remote")
ADS = "Acme builds forecasting tools for retailers. You will join a small team of data scientists and ship models. " * 6
APPLICANT = {"visa": {"home": "GB", "remote": {"label": "Remote", "unrestricted_uses": "home", "summary": "I have the right to work here."},
                      "by_country": {
    "GB": {"right_to_work": True, "sponsorship_now": False, "sponsorship_future": True, "summary": "I am on the UK Graduate visa."},
    "default": {"right_to_work": False, "sponsorship_now": True, "sponsorship_future": True,
                "summary": "I would need a work visa for {country} and am open to sponsorship."}}}}
HOME_WORDS = ("united kingdom", "uk", "u.k.", "gb", "great britain", "britain", "england", "scotland", "wales")


def w(location="", ad="", title="Data Scientist", rules=HOME, **kw):
    return resolve(location=location, description=ad, title=title, rules=rules, **kw)


def mentions_home(text: str) -> bool:
    import re
    return bool(re.search(r"(?<![\w])(?:%s)(?![\w])" % "|".join(re.escape(x) for x in HOME_WORDS), text, re.I))


# ------------------------------------------------------------------ the five cases the task names
def test_home_office_is_home_with_the_home_answers():
    work = w("London, UK", ADS + "Hybrid: two days a week in our London office.")
    assert (work.country, work.mode, work.home_remote) == ("GB", "onsite", False)       # an office is not a remote role: its country may be named
    assert work.evidence == "Location: London, UK" and work.shown == "United Kingdom"
    plan = visa.plan_for(APPLICANT, work)
    assert plan.source == "by_country.GB" and [plan.answer(k) for k in visa.KEYS] == ["Yes", "No", "Yes"]


def test_unrestricted_remote_is_home_with_home_answers_and_only_the_remote_label_is_shown():
    work = w("Remote", ADS + "This is a fully remote role. Work from anywhere you like.")
    assert (work.country, work.mode, work.home_remote) == ("GB", "remote", True)
    assert work.shown == "Remote"
    plan = visa.plan_for(APPLICANT, work)
    assert [plan.answer(k) for k in visa.KEYS] == ["Yes", "No", "Yes"]                  # the home block's booleans
    shown = [work.line(), work.shown, work.evidence, plan.describe(), plan.source, *visa.review_lines({"location": "Remote", "description": work.evidence,
                                                                                                         "title": "DS"}, APPLICANT)]
    assert not [s for s in shown if mentions_home(s) or "GB" in s], shown               # the home country is never printed
    assert work.line().startswith("Work location: Remote") and "evidence" in work.line()


def test_unrestricted_remote_with_no_word_about_places_at_all_is_home_too():
    work = w("Remote", ADS)
    assert (work.country, work.shown, work.evidence) == ("GB", "Remote", "Location: Remote")
    assert w("", ADS + "This role is fully remote.").country == "GB" and w("", ADS, title="Remote Data Scientist").country == "GB"


def test_a_remote_role_restricted_to_the_us_is_the_us_and_needs_sponsorship():
    work = w("Remote - US", ADS + "Candidates must be located in the United States.")
    assert (work.country, work.mode, work.home_remote, work.shown) == ("US", "remote", False, "United States")
    plan = visa.plan_for(APPLICANT, work)
    assert plan.source == "by_country.default" and plan.answer("sponsorship_now") == "Yes" and plan.answer("right_to_work") == "No"
    assert plan.summary == "I would need a work visa for United States and am open to sponsorship."


def test_dublin_office_is_ie_and_needs_sponsorship():
    work = w("Dublin, Ireland", ADS + "Hybrid, three days a week in our Dublin office.")
    assert (work.country, work.mode) == ("IE", "onsite")
    plan = visa.plan_for(APPLICANT, work)
    assert plan.answer("sponsorship_now") == "Yes" and "Ireland" in plan.summary


def test_an_ambiguous_ad_is_none_and_the_job_needs_country(settings):
    work = w("", ADS + "We are looking for a data scientist with strong Python. Great culture and benefits.")
    assert work.country is None and work.reason and visa.plan_for(APPLICANT, work) is None
    v = gates.triage({"title": "Data Scientist", "location": "", "description": ADS * 3, "company": "Acme"}, settings, None)
    assert v.passed and v.needs_country and gates.verdict_status(v) == "needs_country"
    assert gates.verdict_fields(v)["reason"].startswith("work country unknown: ") and gates.verdict_fields(v)["work_country"] is None


# ------------------------------------------------------------------ nothing says the home country for a remote role
@pytest.mark.parametrize("location, ad", [
    ("Remote", ""), ("Remote (UK)", ""), ("Remote - United Kingdom", ""), ("UK Remote", ADS), ("Remote", "Open to candidates in the UK."),
    ("Remote", "We hire across EMEA and the UK."), ("Remote - Europe", ""), ("Remote - EMEA", ""), ("Remote - Worldwide", ""),
    ("Remote", "Candidates in the UK, Germany or France are welcome to apply."), ("Remote", "You can work from anywhere in the world.")])
def test_remote_that_includes_home_is_home_and_nothing_printed_names_it(location, ad):
    work = w(location, ADS + ad)
    assert (work.country, work.mode, work.home_remote) == ("GB", "remote", True), work
    plan = visa.plan_for(APPLICANT, work)
    for text in (work.line(), work.shown, work.evidence, plan.describe(), plan.summary):
        assert not mentions_home(text), text
    assert not mentions_home(visa.displayed_location({"location": location, "description": ADS + ad, "title": "Data Scientist"}, APPLICANT))


def test_the_evidence_hides_the_home_country_but_keeps_the_rest_of_the_sentence():
    work = w("Remote", ADS + "This role is fully remote and open to candidates in the UK.")
    assert work.evidence == "This role is fully remote and open to candidates in the [home]."


def test_a_restriction_to_somewhere_else_prints_that_place_and_keeps_the_ads_words():
    work = w("Remote", ADS + "This role is fully remote and open to candidates in the US.")
    assert work.evidence == "This role is fully remote and open to candidates in the US." and not work.home_remote


def test_the_remote_label_is_whatever_applicant_yaml_says():
    work = w("Remote", ADS, rules=Rules(home="GB", label="Anywhere (remote)"))
    assert work.shown == "Anywhere (remote)" and visa.plan_for(APPLICANT, work).summary == "I have the right to work here."
    assert workcountry.rules_from({"visa": {"home": "ie", "remote": {"label": "  Fully remote "}}}) == Rules("IE", "Fully remote")
    assert workcountry.rules_from({"visa": {"home": "GB"}}) == Rules("GB", "Remote") and workcountry.rules_from({}) == Rules("", "Remote")


def test_a_remote_role_at_home_uses_the_remote_summary_if_there_is_one_else_the_home_blocks_own():
    remote = visa.plan_for(APPLICANT, w("Remote", ADS))
    assert remote.summary == "I have the right to work here." and remote.source == "the home block"
    onsite = visa.plan_for(APPLICANT, w("London, UK", ADS))
    assert onsite.summary == "I am on the UK Graduate visa." and onsite.source == "by_country.GB"        # an office is not remote
    plain = {"visa": {**APPLICANT["visa"], "remote": {"label": "Remote"}}}
    assert visa.plan_for(plain, w("Remote", ADS)).summary == "I am on the UK Graduate visa."                # your own text, verbatim


def test_the_summary_says_the_label_for_country_when_the_default_serves_a_remote_role_at_home():
    applicant = {"visa": {"home": "GB", "by_country": {"default": {"summary": "I would need a visa for {country}.", "sponsorship_now": True}}}}
    plan = visa.plan_for(applicant, w("Remote", ADS))
    assert plan.summary == "I would need a visa for Remote." and plan.source == "by_country.default"


# ------------------------------------------------------------------ regions
@pytest.mark.parametrize("location, ad, code, shown", [
    ("Remote - EU", "", "EU", "EU"), ("Remote", "Open to candidates within the EU.", "EU", "EU"), ("Remote - APAC", "", "APAC", "APAC"),
    ("Remote - North America", "", "North America", "North America"), ("Remote", "Remote, Canada only.", "CA", "Canada")])
def test_a_restriction_to_a_region_that_excludes_home_is_that_region(location, ad, code, shown):
    work = w(location, ADS + ad)
    assert (work.country, work.shown, work.home_remote) == (code, shown, False)
    plan = visa.plan_for(APPLICANT, work)
    assert plan.source == "by_country.default"


def test_a_region_has_its_own_block_when_you_write_one():
    applicant = {"visa": {**APPLICANT["visa"], "by_country": {**APPLICANT["visa"]["by_country"], "EU": {"sponsorship_now": True, "summary": "EU."}}}}
    plan = visa.plan_for(applicant, w("Remote - EU", ADS))
    assert plan.source == "by_country.EU" and plan.summary == "EU."


def test_europe_and_emea_include_a_home_in_them_and_eu_does_not_include_the_uk():
    assert w("Remote - Europe", ADS).home_remote and w("Remote - EMEA", ADS).home_remote
    assert w("Remote - EU", ADS).country == "EU"
    assert w("Remote - EU", ADS, rules=Rules(home="DE", label="Remote")).home_remote                    # but it includes Germany


def test_open_to_several_places_none_of_them_home_is_ambiguous():
    work = w("Remote", ADS + "We are hiring remotely in Germany or the Netherlands.")
    assert work.country is None and "several places" in work.reason


def test_a_remote_role_with_a_sentence_that_says_not_decides_nothing_from_it():
    assert w("Remote", ADS + "This role is fully remote for candidates in the US. We cannot hire in the UK.").country == "US"
    assert w("Remote", ADS + "Unfortunately this remote role is not open to candidates in the UK.").country == "GB"       # nothing restricts it


def test_join_us_is_not_the_united_states_and_a_london_office_is_not_remote_scope():
    work = w("Remote", ADS + "Join us. We are remote-first, with an office in London. Open to candidates in Germany.")
    assert work.country == "DE"                                                          # only the places a scope sentence names count


# ------------------------------------------------------------------ the home is whatever visa.home says
def test_home_is_not_hard_wired_to_the_uk():
    ie = Rules(home="IE", label="Remote")
    assert w("Remote", ADS, rules=ie).country == "IE" and w("Remote", ADS, rules=ie).home_remote
    assert w("Dublin", ADS, rules=ie).home_remote is False and w("Dublin", ADS, rules=ie).country == "IE"
    assert w("Remote - UK only", ADS + "Must be UK based.", rules=ie).country == "GB"              # now the UK is somewhere else
    work = w("Remote", ADS + "Open to candidates in Ireland.", rules=ie)
    assert work.evidence == "Open to candidates in [home]." or work.evidence.endswith("[home].")


def test_without_visa_home_a_remote_role_cannot_be_placed_and_an_office_still_can():
    assert w("Remote", ADS, rules=Rules()).country is None and "visa.home" in w("Remote", ADS, rules=Rules()).reason
    assert w("Dublin", ADS, rules=Rules()).country == "IE"


# ------------------------------------------------------------------ on-site and hybrid
@pytest.mark.parametrize("location, code", [
    ("London, UK", "GB"), ("Bristol", "GB"), ("Manchester, England", "GB"), ("United Kingdom", "GB"), ("Edinburgh (Hybrid)", "GB"),
    ("Dublin", "IE"), ("Berlin, Germany", "DE"), ("Amsterdam, NL", "NL"), ("Toronto, ON", "CA"), ("San Francisco, CA", "US"),
    ("Birmingham, AL", "US"), ("London, Ontario", "CA"), ("Sydney NSW", "AU"), ("Singapore", "SG")])
def test_a_place_in_the_location_header_is_the_office_country(location, code):
    work = w(location, ADS)
    assert (work.country, work.mode) == (code, "onsite")


def test_hybrid_in_the_location_beats_a_remote_mention_in_the_ad():
    work = w("Dublin (Hybrid)", ADS + "Some of our team work remotely from home.")
    assert (work.country, work.mode) == ("IE", "onsite")


def test_an_ad_that_says_hybrid_is_not_remote_whatever_else_it_says():
    work = w("Cambridge, UK", ADS + "This is a hybrid role. Remote work is possible on Fridays.")
    assert (work.country, work.mode) == ("GB", "onsite")


def test_no_location_header_reads_the_one_office_the_ad_names():
    assert w("", ADS + "Our office is in Dublin and you will be based in the office three days a week.").country == "IE"
    assert w("", ADS + "You will be based in Leeds.").country == "GB"


def test_two_offices_in_two_countries_are_ambiguous():
    work = w("", ADS + "We have an office in Berlin. We also have a hybrid team based in Paris.")
    assert work.country is None and "several countries" in work.reason


# ------------------------------------------------------------------ the country you gave
def test_a_country_you_give_wins_over_the_ad_and_may_be_a_region():
    work = w("Remote - Germany", ADS + "Based in Germany.", override="Ireland")
    assert (work.country, work.mode) == ("IE", "explicit") and work.evidence.startswith("set by you")
    assert w("", ADS, override="de").country == "DE" and w("", ADS, override="eu").country == "EU"
    bad = w("London", ADS, override="Narnia")
    assert bad.country is None and "Narnia" in bad.reason


def test_for_job_uses_the_stored_country_only_when_you_set_it():
    job = {"location": "London, UK", "description": ADS, "title": "Data Scientist", "work_country": "DE", "work_evidence": "Location: Berlin"}
    assert workcountry.for_job(job, APPLICANT).country == "GB"                           # a stored guess is recomputed
    job["work_evidence"] = "set by you: set-country DE"
    assert workcountry.for_job(job, APPLICANT).country == "DE"


# ------------------------------------------------------------------ triage, the label column and priority
def test_triage_stores_w_its_evidence_and_the_label_for_a_remote_role_at_home(settings):
    f = gates.verdict_fields(gates.triage({"title": "Data Scientist", "location": "Remote", "description": ADS * 3, "company": "Acme"}, settings, None))
    assert f["status"] == "triaged" and f["work_country"] == "GB" and f["work_evidence"] == "Location: Remote" and f["work_location"] == "Remote"
    f = gates.verdict_fields(gates.triage({"title": "Data Scientist", "location": "Bristol, UK", "description": ADS * 3, "company": "Acme"}, settings, None))
    assert f["status"] == "triaged" and f["work_country"] == "GB" and f["work_evidence"] == "Location: Bristol, UK" and f["work_location"] is None


def test_a_truncated_jd_is_needs_jd_before_it_is_needs_country(settings):
    v = gates.triage({"title": "Data Scientist", "location": "", "description": "short", "company": "Acme"}, settings, None)
    assert gates.verdict_status(v) == "needs_jd"


def test_a_skipped_job_is_skipped_not_needs_country(settings):
    v = gates.triage({"title": "Head of Marketing", "location": "", "description": ADS * 3, "company": "Acme"}, settings, None)
    assert gates.verdict_status(v) == "skipped"


def test_a_country_you_set_survives_a_new_triage(settings):
    job = {"title": "Data Scientist", "location": "", "description": ADS * 3, "company": "Acme", "work_country": "IE",
           "work_evidence": "set by you: set-country IE"}
    v = gates.triage(job, settings, None)
    assert v.work_country == "IE" and not v.needs_country


def test_priority_s_is_normal_at_home_and_non_home_anywhere_else(settings):
    job = {"company": "X", "description": "a job", "sponsor_match": "exact"}
    assert priority.sponsor_factor({**job, "work_country": "GB"}, settings)[0] == 1.0                  # W == home
    assert priority.sponsor_factor({**job, "work_country": "IE"}, settings) == (0.5, "work location Ireland is not home")
    assert priority.sponsor_factor({**job, "work_country": "EU"}, settings) == (0.5, "work location EU is not home")
    assert priority.sponsor_factor({**job, "work_country": None}, settings)[0] == 1.0                  # unknown W: no penalty
    assert priority.sponsor_factor({**job, "work_country": "GB", "country": "Canada"}, settings)[0] == 1.0   # W wins over a listing's country
    assert priority.sponsor_factor({**job, "country": "Canada"}, settings)[0] == 0.5                  # no W yet: the listing's country
    settings.raw["priority"]["sponsor_weights"]["non_home"] = 0.3
    assert priority.sponsor_factor({**job, "work_country": "DE"}, settings)[0] == 0.3


def test_the_weight_is_non_home_with_the_old_name_still_read(settings):
    assert priority.weights(settings)["non_home"] == 0.5 and "non_uk" not in priority.weights(settings)
    del settings.raw["priority"]["sponsor_weights"]["non_home"]
    assert priority.weights(settings)["non_home"] == 0.5                                               # the built-in default
    settings.raw["priority"]["sponsor_weights"]["non_uk"] = 0.4
    assert priority.weights(settings)["non_home"] == 0.4


def test_priority_home_is_visa_home_not_the_uk(settings, tmp_path):
    import yaml
    from pathlib import Path
    a = yaml.safe_load(Path(settings.raw["paths"]["applicant"]).read_text(encoding="utf-8"))
    a["visa"]["home"] = "IE"
    a["visa"]["by_country"]["IE"] = a["visa"]["by_country"]["GB"]
    p = tmp_path / "applicant.yaml"
    p.write_text(yaml.safe_dump(a), encoding="utf-8")
    settings.raw["paths"]["applicant"] = str(p)
    job = {"company": "X", "description": "a job", "sponsor_match": "exact"}
    assert priority.sponsor_factor({**job, "work_country": "IE"}, settings)[0] == 1.0
    assert priority.sponsor_factor({**job, "work_country": "GB"}, settings)[0] == 0.5


def test_an_inbox_file_is_ranked_by_the_w_its_location_and_ad_give(settings, tmp_path):
    def rank(location):
        p = tmp_path / "x.txt"
        p.write_text(f"Company: Acme\nTitle: Data Scientist\nURL: https://x.example/1\nLocation: {location}\n\n{ADS * 3}", encoding="utf-8")
        job = priority.file_job(p, settings, {}, None)
        return job["work_country"], priority.priority(job, settings, tailored=False).s
    assert rank("Dublin") == ("IE", 0.5)
    assert rank("Remote")[0] == "GB" and rank("Remote")[1] > 0.5
    assert rank("Remote - US") == ("US", 0.5)
    assert rank("Bristol, UK")[0] == "GB" and rank("Bristol, UK")[1] > 0.5
    assert rank("")[0] is None


# ------------------------------------------------------------------ countries helpers
def test_regions_cover_the_countries_they_name():
    assert countries.covers("EMEA", "GB") and countries.covers("Europe", "gb") and not countries.covers("EU", "GB") and countries.covers("EU", "DE")
    assert countries.covers("GB", "United Kingdom") and not countries.covers("US", "GB") and not countries.covers("", "GB")
    assert countries.place_code("emea") == "EMEA" and countries.place_code("Ireland") == "IE" and countries.place_code("x") == ""
    assert countries.mentions("Remote across EMEA and the UK", regions=True) == ["EMEA", "United Kingdom"] or \
        set(countries.mentions("Remote across EMEA and the UK", regions=True)) == {"EMEA", "United Kingdom"}
    assert countries.mentions("Join us in the European Union", regions=True) == ["EU"]
