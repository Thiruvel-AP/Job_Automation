"""v1.2.1: ITOL Recruit and its other names are blacklisted by name; "training course and fees apply" / "course fees" stop an ad before tailoring."""
import pytest

from careerops.discovery import blacklist, shortlist


@pytest.mark.parametrize("company", ["ITOL Recruit", "AI Jobs at ITOL Recruit", "Itonlinelearning", "ITONLINELEARNING Ltd", "itol recruit"])
def test_every_itol_name_is_a_training_scheme_whatever_the_ad_says(company):
    assert blacklist.training_scheme(company, "Junior AI Developer") is not None


def test_a_similar_employer_is_not_blacklisted():
    assert blacklist.training_scheme("Itol Systems", "Data Scientist, salary £60,000") is None


@pytest.mark.parametrize("text", ["Please note this is a training course and fees apply.", "Course fees are payable.", "There are no course fee refunds."])
def test_the_fee_phrases_stop_an_ad_from_any_employer(text):
    assert blacklist.training_scheme("Some Employer", text) is not None


def test_a_blacklisted_ad_is_filed_in_skipped_blacklist_and_never_deleted(settings, tmp_path):
    src = tmp_path / "itol-recruit-trainee-ai-engineer-1.txt"
    src.write_text("Company: ITOL Recruit\nTitle: Trainee AI Engineer\n", encoding="utf-8")
    dst = shortlist.file_away(settings, src, "skipped_blacklist", "ITOL Recruit sells fee-charging training courses as jobs")
    assert dst is not None and dst.parent.name == "skipped_blacklist" and dst.exists() and not src.exists()
    assert dst.with_name(dst.stem + ".reason.txt").exists()
