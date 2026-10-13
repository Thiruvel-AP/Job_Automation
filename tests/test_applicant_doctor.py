"""`careerops doctor` checks applicant.yaml: placeholder email domain, "+44 0" phone, postcode in `location`, and the per-country visa
answers (visa.home and visa.by_country[visa.home] must exist; default only warns).
Files are written under tmp_path; the real applicant.yaml is never read here."""
import pytest
import yaml

from careerops.setup import applicant_checks, doctor

BLOCK = {"right_to_work": True, "sponsorship_now": False, "sponsorship_future": True, "summary": "x"}
VISA = {"home": "GB", "remote": {"label": "Remote"}, "by_country": {"GB": BLOCK, "default": {**BLOCK, "sponsorship_now": True}}}
GOOD = {"email": "ada@gmail.com", "phone": "+44 7700 900123", "location": "Bristol, United Kingdom", "visa": VISA}


def _checks(tmp_path, **over):
    p = tmp_path / "applicant.yaml"
    p.write_text(yaml.safe_dump({**GOOD, **over}), encoding="utf-8")
    return applicant_checks(p)


def _failed(checks):
    return [name for name, ok, _ in checks if not ok]


def _warned(checks):
    return [name for name, ok, _ in checks if ok == "warn"]


def test_a_real_email_a_trimmed_uk_phone_and_a_city_pass_every_check(tmp_path):
    assert _failed(_checks(tmp_path)) == [] and _warned(_checks(tmp_path)) == [] and len(_checks(tmp_path)) == 6


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


# ------------------------------------------------------------------ the per-country visa block
def test_without_visa_home_or_its_block_doctor_fails_and_says_what_to_add(tmp_path):
    no_home = {k: v for k, v in VISA.items() if k != "home"}
    for visa, name in ((no_home, "applicant visa.home is set to a country"),
                       ({**VISA, "home": "Narnia"}, "applicant visa.home is set to a country"),
                       ({**VISA, "by_country": {"default": BLOCK}}, "applicant visa.by_country[visa.home] is present"),
                       ({**VISA, "by_country": {"GB": {}, "default": BLOCK}}, "applicant visa.by_country[visa.home] is present"),
                       ({**VISA, "by_country": {"IE": BLOCK, "default": BLOCK}}, "applicant visa.by_country[visa.home] is present")):
        checks = _checks(tmp_path, visa=visa)
        assert name in _failed(checks), visa
        assert all(h for n, ok, h in checks if not ok and n.startswith("applicant visa"))
    assert _failed(_checks(tmp_path, visa={})) == ["applicant visa.home is set to a country", "applicant visa.by_country[visa.home] is present"]


def test_home_is_whatever_visa_home_says_and_its_block_may_be_keyed_by_name_or_code(tmp_path):
    assert _failed(_checks(tmp_path, visa={"home": "Ireland", "by_country": {"IE": BLOCK, "default": BLOCK}})) == []
    assert _failed(_checks(tmp_path, visa={"home": "IE", "by_country": {"Ireland": BLOCK}})) == []
    assert _failed(_checks(tmp_path, visa={"home": "IE", "by_country": {"GB": BLOCK, "default": BLOCK}})) == ["applicant visa.by_country[visa.home] is present"]
    assert _failed(_checks(tmp_path, visa={"home": "GB", "by_country": {"GB": "yes"}})) == ["applicant visa.by_country[visa.home] is present"]


def test_a_missing_default_is_a_warning_not_a_failure(tmp_path):
    checks = _checks(tmp_path, visa={**VISA, "by_country": {"GB": BLOCK}})
    assert _failed(checks) == [] and _warned(checks) == ["applicant visa.by_country.default is present"]


def test_doctor_exits_zero_on_warnings_only_and_prints_them_as_warn(settings, tmp_path, monkeypatch, capsys):
    from careerops import cli
    monkeypatch.setattr(cli, "load_settings", lambda path=None: settings)
    monkeypatch.setattr(cli, "load_dotenv", lambda p: None)
    monkeypatch.setattr(cli, "doctor", lambda s, ping=True: [("a fine check", True, ""), ("only a warning", "warn", "do this when you can")])
    with pytest.raises(SystemExit) as e:
        cli.main(["doctor", "--no-ping"])
    out = capsys.readouterr().out
    assert e.value.code == 0 and "[ok] a fine check" in out and "[warn] only a warning" in out and "do this when you can" in out
    monkeypatch.setattr(cli, "doctor", lambda s, ping=True: [("broken", False, "fix it"), ("only a warning", "warn", "later")])
    with pytest.raises(SystemExit) as e:
        cli.main(["doctor", "--no-ping"])
    assert e.value.code == 1 and "[!!] broken" in capsys.readouterr().out


@pytest.mark.slow
def test_a_missing_applicant_file_fails_and_doctor_includes_the_checks(settings, tmp_path):
    assert _failed(applicant_checks(tmp_path / "nope.yaml")) == ["applicant.yaml"]
    p = tmp_path / "applicant.yaml"
    p.write_text(yaml.safe_dump({**GOOD, "email": "a@example.org"}), encoding="utf-8")
    settings.raw["paths"]["applicant"] = str(p)
    names = [(n, ok) for n, ok, _ in doctor(settings) if n.startswith("applicant")]
    assert len(names) == 6 and [ok for _, ok in names] == [True, True, True, False, True, True]       # 3 visa checks, then email, phone, location
