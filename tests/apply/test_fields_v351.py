"""9 Nov 2026 dry run, still open from 8 Nov: (a) RightShip's schema lists a required "Location" the page does not show; the page's "Location
(City)" is VERIFIED with that answer, so "Location" is COVERED and does not block; (b) fields.md and report.md list the options of the choice
fields left empty for you (the two Inclusion & Diversity questions, VeteranStatus). Pure tests over plan rows; no browser."""
from careerops.apply import answerplan as AP
from careerops.apply import dryrun, planfill
from careerops.apply import requiredgate as RG
from careerops.apply.formscan import DomField

PRONOUN = "Inclusion and Diversity is very important to us and we'd like to know what would be your preferred pronoun for us to refer to you?"
PRONOUNS = ["She / Her / Hers", "He / Him / His", "They / Them / Theirs", "Self-describe, please specify in the next question"]
VETERAN = ["I don't wish to answer", "I identify as one or more of the classifications of a protected veteran", "I am not a protected veteran"]


def row(n, label, typ="text", answer="", source="", status=AP.READY, origin="schema", required=True, options=None):
    return AP.Row(n, AP.Question(label.lower().replace(" ", "_"), typ, label, required, list(options or []), origin=origin), answer, source, status)


def dom(label, typ="text", options=None):
    return DomField("co-" + label[:8], typ, label, True, list(options or []))


def rightship() -> planfill.Result:
    loc = row(17, "Location", answer="Bristol", source="answers:current_location")
    city = row(22, "Location (City)", "combobox", "Bristol", "answers:current_location", origin="page")
    pron = row(14, PRONOUN, "combobox", source="equal_opportunities", status=AP.BLANK, options=PRONOUNS)
    vet = row(18, "VeteranStatus", "combobox", source="equal_opportunities", status=AP.BLANK, required=False, options=VETERAN)
    race = row(19, "Race", "combobox", answer="White", source="equal_opportunities:ethnicity", required=False)
    att = row(16, "I declare that the information provided by me is true and accurate.", "combobox", source="attestation",
              status=AP.NEEDS_USER, options=["Yes"])
    return planfill.Result(done=[
        planfill.Done(loc, None, planfill.NOT_FOUND, why="no such field on the page"),
        planfill.Done(city, dom("Location (City)", "combobox"), planfill.VERIFIED, "Bristol, England, United Kingdom"),
        planfill.Done(pron, dom(PRONOUN, "combobox", PRONOUNS), AP.BLANK, why="not listed in equal_opportunities: left blank"),
        planfill.Done(vet, dom("VeteranStatus", "combobox", VETERAN), AP.BLANK, why="not listed in equal_opportunities: left blank"),
        planfill.Done(race, None, planfill.NOT_FOUND, why="no such field on the page"),
        planfill.Done(att, dom(att.q.label, "combobox", ["Yes"]), AP.NEEDS_USER, why="attestation: you tick it yourself, code never does"),
    ])


def apply_cover(res: planfill.Result) -> planfill.Result:
    """What Filler.run does after the fill (the browser part is tested in test_planfill.py)."""
    cover = planfill.covered_by(res.done)
    for d in res.done:
        if cover.get(d.row.n):
            d.status, d.why = planfill.COVERED, f"not on the page; '{cover[d.row.n]}' holds the answer (VERIFIED)"
    return res


# ------------------------------------------------------------------ (a) a schema-only field covered by a VERIFIED one
def test_a_schema_location_is_covered_by_the_verified_location_city():
    res = rightship()
    assert planfill.covered_by(res.done) == {17: "Location (City)"}
    apply_cover(res)
    table = {r.label: r for r in res.required_rows()}
    assert table["Location"].status == "COVERED" and not table["Location"].blocks
    assert "Location" not in [r.label for r in RG.blocking(res.required_rows())]
    assert "Location" not in planfill.needs_user(res)


def test_a_schema_field_nothing_on_the_page_holds_still_blocks():
    res = rightship()
    res.done[1].status = planfill.LOST                                   # Location (City) was not read back
    assert planfill.covered_by(res.done) == {}
    assert {r.label: r.status for r in res.required_rows()}["Location"] == "EMPTY"
    other = row(30, "Postcode", answer="BS1 4XX", source="applicant:postcode")
    res2 = planfill.Result(done=[planfill.Done(other, None, planfill.NOT_FOUND), res.done[1]])
    res2.done[1].status = planfill.VERIFIED
    assert planfill.covered_by(res2.done) == {}                          # another label, another answer: not covered


def test_a_page_field_is_never_covered():
    res = rightship()
    res.done[0].row.q.origin = "page"
    assert planfill.covered_by(res.done) == {}


def test_covered_is_a_status_that_does_not_block():
    assert "COVERED" in RG.STATUSES and "COVERED" not in RG.BLOCKING


# ------------------------------------------------------------------ (b) the options of the empty choice fields
def test_fields_md_lists_the_options_of_the_empty_choice_fields():
    rows = apply_cover(rightship()).required_rows(required_only=False)
    md = dryrun.fields_md(rows)
    assert "## Options of the empty choice fields" in md
    tail = md.split("## Options of the empty choice fields", 1)[1]
    assert f"{PRONOUN[:60]}" in tail and " · ".join(PRONOUNS) in tail and "(required)" in tail
    assert "VeteranStatus: " + " · ".join(VETERAN) in tail
    assert "I declare" not in tail and "Race" not in tail                 # an attestation and a filled-or-absent field are not listed


def test_report_md_shows_the_options_next_to_an_empty_required_choice():
    rows = apply_cover(rightship()).required_rows(required_only=False)
    rep = dryrun.report_md({"company": "RightShip", "title": "AI Engineer"}, {}, rows, {"line": "ATS structure: n/a"})
    empty = rep.split("## Required fields still empty", 1)[1]
    assert f"{PRONOUN} — options: " + " · ".join(PRONOUNS) in empty
    assert "- Location\n" not in empty and "Location —" not in empty     # COVERED: not empty
    assert "I declare that the information provided by me is true and accurate.\n" in empty     # yours to tick; no options shown


def test_the_options_go_to_fields_json_too():
    rows = apply_cover(rightship()).required_rows(required_only=False)
    assert {r.label: r.options for r in rows}["VeteranStatus"] == VETERAN and RG.Row(**rows[0].as_dict()) == rows[0]


def test_the_formloop_table_carries_the_options_of_a_choice_field():
    from careerops.apply.formloop import Field
    f = Field("q1", "select", "Pronoun", "Pronoun", True, "", PRONOUNS, "", None, "Pronoun")
    rs = RG.build([f], verified=[], lost=[], needs_user={}, attestations=[], sources={})
    assert rs[0].status == "EMPTY" and rs[0].options == PRONOUNS and RG.empty_choices(rs) == [(1, rs[0])]
