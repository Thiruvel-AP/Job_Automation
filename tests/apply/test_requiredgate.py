"""F4 (v2.4.1): the required-field table. Every REQUIRED field of the filled form gets one status (FILLED / VERIFIED / LOST / EMPTY / NEEDS USER); LOST, EMPTY
and NEEDS USER block an auto-submit and are printed in red with their label. Pure tests over Field objects; the browser test of the whole RightShip-shaped
form is tests/apply/test_greenhouse_form.py."""
import pytest

from careerops.apply import requiredgate as RG
from careerops.apply.formloop import Field


def fld(question, kind="text", value="", required=True, key=None):
    return Field(key or f"{kind}:{question[:20]}", kind, question, question, required, "", [], value, None, question[:60])


def rows(fields, **kw):
    kw.setdefault("verified", [])
    kw.setdefault("lost", [])
    kw.setdefault("needs_user", {})
    kw.setdefault("attestations", [])
    kw.setdefault("sources", {})
    return RG.build(fields, **kw)


def by_label(rs):
    return {r.label: r for r in rs}


def test_an_empty_required_field_blocks():
    rs = rows([fld("First Name", value="Ada"), fld("Phone")], verified=["First Name"])
    t = by_label(rs)
    assert t["First Name"].status == "VERIFIED" and t["Phone"].status == "EMPTY"
    assert [r.label for r in RG.blocking(rs)] == ["Phone"]


def test_all_verified_passes_and_says_so():
    rs = rows([fld("First Name", value="Ada"), fld("Email", value="a@b.c")], verified=["First Name", "Email"])
    assert RG.blocking(rs) == []
    assert "nothing here blocks an auto-submit" in RG.render(rs, color=False)[-1]


def test_a_field_this_run_filled_and_the_form_emptied_is_lost():
    rs = rows([fld("Country", value="")], lost=["Country"])
    assert rs[0].status == "LOST" and rs[0].blocks


def test_a_value_nobody_read_back_is_filled_not_verified():
    rs = rows([fld("Notice period", value="Immediately")])
    assert rs[0].status == "FILLED" and not rs[0].blocks


def test_a_field_the_code_left_for_the_person_is_needs_user_with_the_reason():
    rs = rows([fld("Visa type")], needs_user={"Visa type": "answers.yaml has no `visa_type` entry"})
    assert rs[0].status == "NEEDS USER" and "visa_type" in rs[0].reason and rs[0].blocks


def test_an_attestation_checkbox_is_needs_user_and_never_ticked():
    q = "By checking this box, I consent to RightShip collecting, storing, and processing my responses"
    rs = rows([fld(q, kind="checkbox", key="checkbox:gdpr")])
    assert rs[0].status == "NEEDS USER" and "attestation" in rs[0].reason


def test_an_attestation_drop_down_is_needs_user_until_the_person_sets_it():
    q = "I declare that the information provided by me is true and accurate"
    assert rows([fld(q, kind="select", key="select:declare")])[0].status == "NEEDS USER"
    assert rows([fld(q, kind="select", key="select:declare", value="Yes")])[0].status == "FILLED"          # the person set it


def test_an_optional_field_is_not_in_the_required_table():
    assert rows([fld("Gender", required=False)]) == []
    assert len(rows([fld("Gender", required=False)], required_only=False)) == 1


def test_a_single_check_box_answered_no_is_filled_not_empty():
    rs = rows([fld("Person with disability", kind="checkbox")], sources={"person with disability": "answered No (equal opportunities)"})
    assert rs[0].status == "FILLED"


def test_the_source_of_each_value_is_carried():
    rs = rows([fld("Notice period", value="Immediately"), fld("Email", value="a@b.c")], sources={"notice period": "answers:notice_period", "email": "applicant"})
    assert {r.label: r.source for r in rs} == {"Notice period": "answers:notice_period", "Email": "applicant"}


def test_a_blocking_row_is_printed_in_red_with_its_label():
    rs = rows([fld("Phone")])
    lines = RG.render(rs, color=True)
    assert any(RG.RED in line and "Phone" in line for line in lines)
    assert any("BLOCKS AUTO-SUBMIT" in line and RG.RED in line for line in lines)
    assert not any(RG.RED in line for line in RG.render(rows([fld("Email", value="x")], verified=["Email"]), color=True))
    assert not any(RG.RED in line for line in RG.render(rs, color=False))


@pytest.mark.parametrize("status", RG.STATUSES)
def test_the_five_statuses_exist(status):
    assert status in ("FILLED", "VERIFIED", "LOST", "EMPTY", "NEEDS USER")
