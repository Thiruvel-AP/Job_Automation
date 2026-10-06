"""`careerops doctor` checks applicant.yaml: placeholder email domain, "+44 0" phone, postcode in `location`.
Files are written under tmp_path; the real applicant.yaml is never read here."""
import pytest
import yaml

from careerops.setup import applicant_checks, doctor

GOOD = {"email": "ada@gmail.com", "phone": "+44 7700 900123", "location": "Bristol, United Kingdom"}


def _checks(tmp_path, **over):
    p = tmp_path / "applicant.yaml"
    p.write_text(yaml.safe_dump({**GOOD, **over}), encoding="utf-8")
    return applicant_checks(p)


def _failed(checks):
    return [name for name, ok, _ in checks if not ok]


def test_a_real_email_a_trimmed_uk_phone_and_a_city_pass_every_check(tmp_path):
    assert _failed(_checks(tmp_path)) == [] and len(_checks(tmp_path)) == 3


@pytest.mark.parametrize("email", ["ada@example.com", "ada@example.org", "ada@EXAMPLE.NET", "ada@mail.example.com", ""])
def test_a_placeholder_or_empty_email_fails(tmp_path, email):
    failed = _failed(_checks(tmp_path, email=email))
    assert len(failed) == 1 and failed[0].startswith("applicant email")


@pytest.mark.parametrize("phone", ["+44 07741016954", "+4407741016954", "+44 (0)7741 016954", "+44-07741-016954"])
def test_a_plus44_followed_by_a_zero_fails(tmp_path, phone):
    failed = _failed(_checks(tmp_path, phone=phone))
    assert len(failed) == 1 and failed[0].startswith("applicant phone")


@pytest.mark.parametrize("phone", ["+44 7741 016954", "07741 016954", "+1 415 555 0100"])
def test_other_phone_forms_are_not_flagged(tmp_path, phone):
    assert _failed(_checks(tmp_path, phone=phone)) == []


@pytest.mark.parametrize("loc", ["BS34 5JW, number 7 Donns, Bristol, United Kingdom", "7 Donns Road, bs8 1ab", "SW1A 1AA",
                                 "London EC1A1BB"])
def test_a_location_with_a_uk_postcode_fails(tmp_path, loc):
    failed = _failed(_checks(tmp_path, location=loc))
    assert len(failed) == 1 and failed[0].startswith("applicant location")


def test_a_city_with_digits_in_a_name_is_not_a_postcode(tmp_path):
    assert _failed(_checks(tmp_path, location="Milton Keynes MK, United Kingdom")) == []


def test_the_failing_placeholder_in_the_users_own_file_is_what_doctor_reports_with_a_fix_hint(tmp_path):
    bad = _checks(tmp_path, email="x@example.com", phone="+44 07741016954", location="BS34 5JW, Bristol")
    assert len(_failed(bad)) == 3 and all(hint for _, ok, hint in bad if not ok)


def test_a_missing_applicant_file_fails_and_doctor_includes_the_checks(settings, tmp_path):
    assert _failed(applicant_checks(tmp_path / "nope.yaml")) == ["applicant.yaml"]
    p = tmp_path / "applicant.yaml"
    p.write_text(yaml.safe_dump({**GOOD, "email": "a@example.org"}), encoding="utf-8")
    settings.raw["paths"]["applicant"] = str(p)
    names = [(n, ok) for n, ok, _ in doctor(settings) if n.startswith("applicant")]
    assert len(names) == 3 and [ok for _, ok in names] == [False, True, True]
