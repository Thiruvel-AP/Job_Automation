"""folder_key, NTFS-safe names, recruiter-visible file names, id6."""
import hashlib
from difflib import SequenceMatcher

import pytest

from careerops.apply.naming import file_stem, folder_key, id6, safe_name

EQUAL_PAIRS = [("COOP", "Co-op"), ("Adria Solution LTD", "Adria Solution Ltd."), ("BrainLabs", "Brainlabs"),
               ("83zero Ltd", "83zero"), ("Arvora ltd", "Arvora Limited"), ("Zone & Co", "Zone and Co"),
               ("Checkout.com", "Checkout.com Ltd")]


@pytest.mark.parametrize("a,b", EQUAL_PAIRS)
def test_verified_pairs_from_the_users_archive_have_equal_keys(a, b):
    assert folder_key(a) == folder_key(b) != ""


@pytest.mark.parametrize("a,b,near", [("BCG X", "BCG", True), ("Blending Spoons", "Bending Spoons", True),
                                      ("CGI", "CGI Group", False)])
def test_not_equal_pairs_and_their_near_miss_ratio(a, b, near):
    ka, kb = folder_key(a), folder_key(b)
    assert ka != kb
    assert (SequenceMatcher(None, ka, kb).ratio() >= 0.85) is near


@pytest.mark.parametrize("name,key", [("Foo (UK) Ltd", "foo"), ("Foo Group Ltd", "foogroup"), ("Foo Ltd Ltd.", "foo"),
                                      ("Ltd", "ltd"), ("Smith & Sons plc", "smithandsons"), ("Visa", "visa"),
                                      ("Foo, Inc.", "foo"), ("Acme Co", "acmeco")])
def test_folder_key_strips_trailing_legal_suffixes_only(name, key):
    assert folder_key(name) == key


@pytest.mark.parametrize("raw,expect", [
    ("Data Scientist / ML Engineer", "Data Scientist - ML Engineer"),
    ("AI: R&D?", "AI- R&D-"),
    ('a<b>c"d|e*f\\g', "a-b-c-d-e-f-g"),
    ("  spaced    out  ", "spaced out"),
    ("trailing dots...", "trailing dots"),
    ("tab\there\x00x", "tab-here-x"),
])
def test_safe_name_replaces_ntfs_illegal_characters(raw, expect):
    assert safe_name(raw) == expect


@pytest.mark.parametrize("raw,expect", [("CON", "CON_"), ("nul", "nul_"), ("COM1", "COM1_"), ("LPT9", "LPT9_"),
                                        ("Aux.txt", "Aux_.txt"), ("CONSULTANT", "CONSULTANT")])
def test_safe_name_reserved_windows_names(raw, expect):
    assert safe_name(raw) == expect


def test_safe_name_caps_length_and_never_ends_in_dot_or_space():
    assert len(safe_name("x" * 200)) == 80
    wordy = safe_name("word " * 40)
    assert len(wordy) <= 80 and not wordy.endswith((" ", "."))
    assert safe_name("///") == "---" and safe_name("   ") != ""


def test_id6_is_six_hex_chars_of_sha1_and_has_no_colon():
    job_id = "greenhouse:acme:7"
    assert id6(job_id) == hashlib.sha1(job_id.encode()).hexdigest()[:6]
    assert len(id6(job_id)) == 6 and ":" not in id6(job_id)
    assert id6("greenhouse:acme:7") != id6("greenhouse:acme:8")


def test_file_stem_styles_use_the_identity_given_never_a_hardcoded_name():
    assert file_stem("Ada Quincy Lovelace", "CV", "full") == "Ada Quincy Lovelace CV"
    assert file_stem("Ada Quincy Lovelace", "CV", "short") == "Ada CV"
    assert file_stem("Ada Quincy Lovelace", "Cover Letter", "full") == "Ada Quincy Lovelace Cover Letter"
    assert file_stem("Ada Quincy Lovelace", "Cover Letter", "short") == "Ada Cover Letter"


def test_file_stem_has_no_company_role_date_or_underscores():
    s = file_stem("Ada Lovelace", "CV")
    assert "_" not in s and s == "Ada Lovelace CV"           # default style is full


def test_file_stem_sanitises_and_keeps_total_name_within_80_chars():
    assert file_stem("A/B: C", "CV") == "A-B- C CV"
    long = file_stem("Z" * 200, "Cover Letter", "full")
    assert long.endswith(" Cover Letter") and len(long + ".pdf") <= 80
    assert len(file_stem("Z" * 200, "CV", "full") + ".tex") <= 80


def test_file_stem_rejects_unknown_style():
    with pytest.raises(ValueError):
        file_stem("Ada Lovelace", "CV", "nickname")
