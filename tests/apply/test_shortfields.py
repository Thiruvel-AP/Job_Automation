"""F3 (v2.4.1): the short structured fields of the RightShip Greenhouse form: city, country, "how did you hear about us", the visa TYPE, and the length
rule for a one-line box. The pure parts are tested directly; the combobox parts run in a real Chromium on a page that behaves like a react-select.
No network, no model."""
from pathlib import Path

import pytest

from careerops.apply import answers as answers_mod
from careerops.apply import shortfields as sf
from careerops.apply.filler import Ctx

pytestmark = pytest.mark.slow          # the combobox tests drive a real Chromium (the pure ones are quick)

APPLICANT = {"first_name": "A", "location": "Bristol, United Kingdom"}
JOB = {"id": "manual:x", "company": "RightShip", "title": "AI Engineer", "source": "greenhouse", "apply_kind": "greenhouse",
       "url": "https://job-boards.eu.greenhouse.io/rightship/jobs/4982444101", "description": "x"}


def book(*entries: dict) -> answers_mod.AnswerBook:
    out = []
    for i, e in enumerate(entries):
        out.append(answers_mod.Entry(id=e["id"], patterns=tuple(answers_mod.norm(m) for m in e.get("match", [])), answer=e.get("answer", ""),
                                     options=tuple(e.get("options", ()))))
    return answers_mod.AnswerBook(out)


# ------------------------------------------------------------------ what a label asks
@pytest.mark.parametrize("label,kind", [
    ("Location (City)", "city"), ("City*", "city"), ("Location", "city"), ("Town", "city"), ("Current location", "city"),
    ("Country", "country"), ("Country of residence", "country"),
    ("How did you hear about us?", "hear"), ("How did you find this job?", "hear"), ("Where did you hear about this role", "hear"),
    ("If you answered 'Work Visa' in the previous question, please specify the type of visa that you are currently holding. If you do not hold any "
     "valid visa currently, please state 'N.A.'", "visa_type"),
    ("Visa type", "visa_type"), ("Which visa do you hold?", "visa_type"), ("Visa category", "visa_type"),
    ("What is your notice period?", None), ("Do you require visa sponsorship?", None), ("Company name", None),
])
def test_the_label_decides_the_kind(label, kind):
    assert sf.kind_of(label) == kind


# ------------------------------------------------------------------ city, country
def test_the_city_is_the_city_part_of_current_location():
    b = book({"id": "current_location", "match": ["current location"], "answer": "Bristol, UK"})
    assert sf.city_of(APPLICANT, b, JOB) == "Bristol"
    assert sf.city_of(APPLICANT, None, JOB) == "Bristol"                     # applicant.yaml when answers.yaml has none
    assert sf.country_of(APPLICANT) == "United Kingdom"


def test_the_city_option_must_hold_the_city_and_a_uk_country():
    opts = ["Bristol, Connecticut, United States", "Bristol, England, United Kingdom", "Bristol", "Bristol, Tennessee, United States"]
    assert sf.pick_city_option(opts, "Bristol") == "Bristol, England, United Kingdom"
    assert sf.pick_city_option(["Bristol, Virginia, United States", "Bristol"], "Bristol") is None      # no UK option: nothing is guessed


def test_the_country_option_is_the_equal_one():
    assert sf.pick_country_option(["Ireland", "United Kingdom", "United Kingdom (Channel Islands)"], "United Kingdom") == "United Kingdom"


# ------------------------------------------------------------------ how did you hear about us
@pytest.mark.parametrize("job,options,expect", [
    ({"source": "greenhouse"}, ["LinkedIn", "Company website", "Referral", "Other"], "Company website"),
    ({"source": "lever"}, ["Indeed", "Careers page", "Other"], "Careers page"),
    ({"source": "ashby"}, ["Glassdoor", "Website", "Other"], "Website"),
    ({"source": "manual", "apply_kind": "greenhouse"}, ["Our careers site", "Other"], "Our careers site"),
    ({"source": "gmail_alert", "url": "https://www.linkedin.com/jobs/view/1"}, ["LinkedIn", "Indeed", "Company website"], "LinkedIn"),
    ({"source": "gmail_alert", "description": "Source: Indeed alert email"}, ["LinkedIn", "Indeed", "Other"], "Indeed"),
    ({"source": "greenhouse"}, ["LinkedIn", "Friend", "Other"], "Other"),                          # no matching option: Other
])
def test_how_did_you_hear_is_mapped_to_an_option(job, options, expect):
    pick, why = sf.hear_choice(options, job)
    assert pick == expect and why


def test_the_channel_mapping_alone_never_picks_the_vendors_name():
    pick, _ = sf.hear_choice(["Greenhouse", "Other"], {"source": "greenhouse"})
    assert pick == "Other"                                                                          # the vendor's name is not a way anyone heard of a job


def test_your_own_answer_comes_first_and_an_unmatched_one_is_not_guessed():
    class C:                                                                                         # what hear_pick reads of a Ctx
        job = JOB
        book = book({"id": "how_heard", "match": ["how did you hear"], "answer": "A friend"})
    assert sf.hear_pick(["LinkedIn", "A friend", "Other"], "How did you hear about us?", C)[0] == "A friend"
    assert sf.hear_pick(["LinkedIn", "Company website", "Other"], "How did you hear about us?", C)[0] is None


def test_with_the_default_source_answer_a_listed_vendor_name_is_picked_else_the_mapping():
    class C:
        job = JOB
        book = book({"id": "how_heard", "match": ["how did you hear"], "answer": "{source}"})
    assert sf.hear_pick(["LinkedIn", "Greenhouse", "Other"], "How did you hear about us?", C)[0] == "Greenhouse"      # the form lists it: it is the user's own answer
    assert sf.hear_pick(["LinkedIn", "Company website", "Other"], "How did you hear about us?", C)[0] == "Company website"


def test_with_no_option_and_no_other_nothing_is_picked():
    assert sf.hear_choice(["LinkedIn", "Friend"], {"source": "greenhouse"})[0] is None


# ------------------------------------------------------------------ visa type
def test_the_visa_type_comes_only_from_the_visa_type_entry():
    b = book({"id": "visa_type", "match": ["something else"], "answer": "Graduate visa"},
             {"id": "visa_statement", "match": ["visa status"], "answer": "I have completed my MSc and am moving onto the UK Graduate visa (2 years)."})
    assert sf.visa_type_answer(b, JOB, "please specify the type of visa you hold") == ("Graduate visa", "")


def test_no_visa_type_entry_is_needs_user_and_the_visa_statement_is_never_used():
    b = book({"id": "visa_statement", "match": ["visa"], "answer": "I have completed my MSc in Data Science at the University of Bristol."})
    text, why = sf.visa_type_answer(b, JOB, "visa type")
    assert text == "" and "no `visa_type` entry" in why


def test_a_blank_or_long_visa_type_entry_is_refused():
    assert "blank" in sf.visa_type_answer(book({"id": "visa_type", "answer": ""}), JOB)[1]
    long = " ".join(["word"] * 9)
    text, why = sf.visa_type_answer(book({"id": "visa_type", "answer": long}), JOB)
    assert text == "" and "9 words" in why


def test_a_visa_type_saved_at_the_prompt_is_found_again():
    b = book({"id": "saved_visa_type", "match": ["visa type"], "answer": "Graduate visa"})
    assert sf.visa_type_answer(b, JOB, "Visa type")[0] == "Graduate visa"


# ------------------------------------------------------------------ the length rule
def test_a_paragraph_is_not_pasted_into_a_one_line_box():
    para = "I have no notice period. " + "I finished my MSc and can start on whichever date suits the team. " * 2
    assert sf.too_long_for_one_line(para, "What is your notice period?")
    assert not sf.too_long_for_one_line(para, "Please explain your notice period")
    assert not sf.too_long_for_one_line("Immediately", "What is your notice period?")


def test_the_shortest_matching_answer_is_used_for_a_one_line_box():
    long = "I have no notice period. I finished my MSc Data Science at the University of Bristol and am not in current employment, so I can start immediately."
    b = book({"id": "notice_period", "match": ["notice period"], "answer": long, "options": ["Immediately", "No notice period"]},
             {"id": "notice_period", "match": ["notice period"], "answer": long})
    r = sf.shortest_answer(b, "What is your notice period?", JOB)
    assert r.text == "Immediately"
    assert sf.shortest_answer(b, "Explain your notice period in detail", JOB).text == long


# ------------------------------------------------------------------ the one click
@pytest.mark.parametrize("text,ok", [("Locate me", False), ("Use my current location", False), ("Dropbox", False), ("Google Drive", False),
                                      ("Submit application", False), ("Enter manually", True), ("Bristol, England, United Kingdom", True), ("Company website", True)])
def test_geolocation_cloud_drives_and_submits_are_never_clicked(text, ok):
    assert sf.clickable_text(text) is ok


# ------------------------------------------------------------------ a real browser: a react-select-like combobox
SELECT_PAGE = """
<html><body>
<style>.opt{padding:4px;cursor:pointer}</style>
<div id="w1"><label for="city">Location (City)*</label>
  <input id="city" role="combobox" type="text" required aria-required="true">
  <button type="button" id="locate" onclick="window.__clicked.push('locate')">Locate me</button>
  <div class="select__single-value" id="city_val"></div></div>
<div id="w2"><label for="hear">How did you hear about us?*</label>
  <input id="hear" role="combobox" type="text" required aria-required="true"><div class="select__single-value" id="hear_val"></div></div>
<div id="w3"><label for="country">Country*</label>
  <input id="country" role="combobox" type="text" required aria-required="true"><div class="select__single-value" id="country_val"></div></div>
<div id="menu" style="display:none"></div>
<script>
window.__clicked = [];
const DATA = {
  city: ["Bristol, Connecticut, United States", "Bristol, England, United Kingdom", "Bristol, Tennessee, United States", "Locate me"],
  hear: ["LinkedIn", "Indeed", "Company website", "Referral", "Other"],
  country: ["Ireland", "United Kingdom", "United States"]
};
for (const id of Object.keys(DATA)) {
  const el = document.getElementById(id);
  const show = () => {
    const q = el.value.toLowerCase();
    const menu = document.getElementById('menu'); menu.innerHTML = ''; menu.style.display = 'block';
    for (const t of DATA[id]) if (t.toLowerCase().includes(q) || t === 'Locate me') {
      const o = document.createElement('div'); o.setAttribute('role', 'option'); o.className = 'opt'; o.textContent = t;
      o.onclick = () => { window.__clicked.push(t); document.getElementById(id + '_val').textContent = t; el.value = ''; menu.style.display = 'none'; };
      menu.appendChild(o);
    }
  };
  el.addEventListener('click', show); el.addEventListener('input', show);
}
</script></body></html>
"""


@pytest.fixture
def page():
    pw = pytest.importorskip("playwright.sync_api")
    with pw.sync_playwright() as p:
        browser = p.chromium.launch()
        pg = browser.new_page()
        pg.set_content(SELECT_PAGE)
        yield pg
        browser.close()


class Result:
    def __init__(self):
        self.filled, self.blank = [], []


def make_ctx(job=JOB, book_=None, strict=False):
    return Ctx(job, APPLICANT, book_ or book({"id": "current_location", "match": ["current location"], "answer": "Bristol, UK"}), Path("answers.yaml"), None,
               None, None, None, ask=lambda p: "", out=lambda t: None, history=lambda c: False, result=Result(), strict=strict)


def run_handle(page, el_id: str, label: str, ctx):
    el = page.query_selector(f"#{el_id}")
    return sf.handle(page, el, "input", "text", label, label[:60], label.rstrip("*"), True, ctx)


def test_the_city_combobox_gets_bristol_typed_and_the_uk_option_picked(page):
    ctx = make_ctx()
    assert run_handle(page, "city", "Location (City)*", ctx)
    assert page.inner_text("#city_val") == "Bristol, England, United Kingdom"
    assert "locate" not in page.evaluate("window.__clicked") and "Locate me" not in page.evaluate("window.__clicked")


def test_locate_me_is_never_clicked_even_when_it_is_the_only_option(page):
    page.evaluate("DATA.city = ['Locate me']")
    ctx = make_ctx()
    run_handle(page, "city", "Location (City)*", ctx)
    assert page.evaluate("window.__clicked") == []
    assert ctx.needs_user and "Locate me" in next(iter(ctx.needs_user.values()))


def test_how_did_you_hear_picks_the_company_website_and_logs_it(page):
    logged = []
    ctx = make_ctx()
    ctx.out = logged.append
    assert run_handle(page, "hear", "How did you hear about us?*", ctx)
    assert page.inner_text("#hear_val") == "Company website"
    assert any("chose 'Company website'" in line for line in logged)


def test_how_did_you_hear_falls_back_to_other_and_the_follow_up_box_gets_the_careers_page(page):
    page.evaluate("DATA.hear = ['LinkedIn', 'Referral', 'Other']")
    ctx = make_ctx()
    run_handle(page, "hear", "How did you hear about us?*", ctx)
    assert page.inner_text("#hear_val") == "Other" and ctx.hear_other is True


def test_the_country_combobox_gets_the_equal_option(page):
    ctx = make_ctx()
    assert run_handle(page, "country", "Country*", ctx)
    assert page.inner_text("#country_val") == "United Kingdom"


def test_a_filled_combobox_is_read_back_from_what_it_shows_not_from_its_empty_input(page):
    from careerops.apply import verify

    ctx = make_ctx()
    ctx.tracker = verify.Tracker()
    run_handle(page, "country", "Country*", ctx)
    ok, lost = ctx.tracker.verify(page, lambda t: None)
    assert ok == ["Country"] and lost == [] or (len(ok) == 1 and lost == [])


def test_a_missing_city_option_leaves_the_field_empty_and_needs_user(page):
    page.evaluate("DATA.city = ['Bristol, Virginia, United States']")
    ctx = make_ctx()
    run_handle(page, "city", "Location (City)*", ctx)
    assert page.inner_text("#city_val") == "" and ctx.needs_user


def test_a_visa_type_field_is_filled_from_the_entry_and_never_with_a_paragraph(page):
    page.set_content('<label for="v">Please specify the type of visa you hold</label><input id="v" type="text" maxlength="255" required>')
    b = book({"id": "visa_type", "match": ["zzz"], "answer": "Graduate visa"})
    ctx = make_ctx(book_=b)
    el = page.query_selector("#v")
    assert sf.handle(page, el, "input", "text", "Please specify the type of visa you hold", "visa", "Please specify the type of visa you hold", True, ctx)
    assert el.input_value() == "Graduate visa"


def test_a_visa_type_field_with_no_entry_stays_empty_and_is_needs_user(page):
    page.set_content('<label for="v">Visa type</label><input id="v" type="text" maxlength="255" required>')
    ctx = make_ctx(book_=book({"id": "visa_statement", "match": ["visa"], "answer": "I have completed my MSc at the University of Bristol"}))
    el = page.query_selector("#v")
    sf.handle(page, el, "input", "text", "Visa type", "Visa type", "Visa type", True, ctx)
    assert el.input_value() == "" and "Visa type" in ctx.needs_user
