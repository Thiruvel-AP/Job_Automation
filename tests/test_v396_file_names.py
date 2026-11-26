"""v3.9.6: '<Name> CV - <Role> - <Company>.pdf' and one sponsor lookup for the estimate and the final P."""
import pytest

from careerops.apply import naming
from careerops.discovery import manual, priority
from careerops.discovery import register as R
from careerops.tailoring.tailor import _names

NAME = "Thiruvel Andagurunathan Pandian"


def test_lseg_data_scientist():
    assert naming.output_stem(NAME, "CV", "Data Scientist", "LSEG") == "Thiruvel Andagurunathan Pandian CV - Data Scientist - LSEG"


def test_kong_ai_engineer_letter():
    assert naming.output_stem(NAME, "CoverLetter", "AI Engineer", "Kong") == "Thiruvel Andagurunathan Pandian CoverLetter - AI Engineer - Kong"


def test_role_labels_and_unknown_family_warns():
    seen = []
    assert naming.role_label("data_scientist") == "Data Scientist"
    assert naming.role_label("forward_deployed_engineer") == "AI Engineer"
    assert naming.role_label("brand_new_family", warn=seen.append) == "AI Engineer" and "brand_new_family" in seen[0]


def test_illegal_characters_removed():
    s = naming.output_stem(NAME, "CV", "AI Engineer", 'AT/T: "Labs"? <x>*|\\ Ltd.')
    assert not set('\\/:*?"<>|') & set(s) and s == s.strip() and "  " not in s and not s.endswith(".")


def test_very_long_company_is_cut_to_the_limit_company_first():
    s = naming.output_stem(NAME, "CoverLetter", "Data Scientist", "Acme " * 80)
    assert len(s + ".pdf") <= 150 and " - Data Scientist - Acme" in s


def test_names_use_applicant_yaml_family_label_and_the_agency_name(settings):
    fb = {"identity": {"name": "Someone Else"}}
    cv, letter = _names(fb, settings, {"company": "Hays", "agency": 1}, "genai_engineer")        # an agency ad: the row's company is the agency
    first = settings.path("applicant")
    assert cv.endswith("CV - AI Engineer - Hays") and letter.endswith("CoverLetter - AI Engineer - Hays")
    assert first.exists() and cv.startswith("Ada Lovelace ")                 # the fixture applicant.yaml, not the fact bank "Someone Else"


def test_company_with_slash_and_colon_in_names(settings):
    cv, _ = _names({"identity": {"name": "x"}}, settings, {"company": "A/B: Co"}, "data_scientist")
    assert cv.endswith("CV - Data Scientist - A B Co")


def test_estimate_and_final_use_the_same_sponsor_lookup(db, settings, tmp_path, monkeypatch):
    f = tmp_path / "a.yaml"
    f.write_text("LSEG:\n  - London Stock Exchange Group\n", encoding="utf-8")
    monkeypatch.setattr(R, "ALIASES_PATH", f)
    db.replace_sponsors([(R.normalise("London Stock Exchange Group plc"), "London Stock Exchange Group plc", "London", "", "A", "Skilled Worker")])
    est_s = priority.sponsor_factor({"sponsor_match": R.SponsorIndex(db).lookup("Lseg").label}, settings)[0]
    jid = manual.add_jd(db, "x" * 900, company="Lseg", title="Data Scientist", url="https://e.com/1")
    db.update_job(jid, sponsor_match="none", sponsor_name=None, status="needs_review")           # triaged before the alias map existed
    assert manual.refresh_sponsor(db, settings, jid) is True
    assert priority.sponsor_factor(db.job(jid), settings)[0] == est_s == 0.9
    assert manual.refresh_sponsor(db, settings, jid) is False
