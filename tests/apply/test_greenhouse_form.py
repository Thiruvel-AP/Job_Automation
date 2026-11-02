"""The RightShip Greenhouse EU form (31 Oct 2026), rebuilt as a local page: every problem the dry run showed, in one run of the assist's `fill_page`.
Country / Location (City) / How did you hear about us are script-built drop-downs; the cover letter block has Attach / Dropbox / Google Drive / Enter
manually; the visa-type box wants a few words; the declaration drop-down and the GDPR check box are attestations. Chromium on a local page, MockLLM, no
network; nothing is submitted. (RightShip's own page was not fetched: the markup is a stand-in for its structure.)"""
import copy

import pytest
import yaml
from conftest import PROFILE_FIX
from playwright.sync_api import sync_playwright
from test_freetext import AD_PHRASE, BOOK, FB, JD, REP, WHY, llm_with

from careerops.apply import answers as answers_mod
from careerops.apply import assist as assist_mod
from careerops.apply import freetext as ft
from careerops.apply import requiredgate as RG

pytestmark = pytest.mark.slow          # these tests drive Chromium

APPLICANT = yaml.safe_load((PROFILE_FIX / "applicant.yaml").read_text(encoding="utf-8"))
APPLICANT["location"] = "Bristol, United Kingdom"
JOB_ROW = {"id": "manual:ff68fffd015a", "company": "Northwind Health AI", "title": "AI Engineer", "source": "greenhouse", "apply_kind": "greenhouse",
           "url": "https://job-boards.eu.greenhouse.io/northwind/jobs/4982444101", "location": "London, UK", "description": JD}
NOTICE = ("I have no notice period. I finished my MSc Data Science at the University of Bristol and am not in current employment, so I can start "
          "immediately, on whichever date suits the team.")
SALARY = "I'd welcome the opportunity to discuss salary expectations during the interview process, once there's a clearer picture of the role's full scope."

Q_MOTIVATION = "What motivated you to apply for this position?"
Q_VISA = ("If you answered 'Work Visa' in the previous question, please specify the type of visa that you are currently holding. "
          "If you do not hold any valid visa currently, please state 'N.A.'")
Q_RIGHTS = "What are your working rights in the country where this role is advertised for?"
Q_DECLARE = "I declare that the information provided by me is true and accurate. Furthermore, I willingly consent to being contacted by the company"
Q_GDPR = "By checking this box, I consent to the company collecting, storing, and processing my responses to the demographic data surveys above."

PAGE = f"""<html><body><form id="f">
<style>.opt{{padding:4px;cursor:pointer}}</style>
<label for="first_name">First Name*</label><input id="first_name" name="first_name" type="text" required>
<label for="last_name">Last Name*</label><input id="last_name" name="last_name" type="text" required>
<label for="email">Email*</label><input id="email" name="email" type="text" required>
<div><label for="country">Country*</label><input id="country" role="combobox" type="text" required><div class="select__single-value" id="country_v"></div></div>
<label for="phone">Phone*</label><input id="phone" name="phone" type="tel" required>
<div><label for="candidate-location">Location (City)*</label><input id="candidate-location" role="combobox" type="text" required>
<div class="select__single-value" id="candidate-location_v"></div><button type="button" onclick="window.__clicked.push('locate')">Locate me</button></div>
<div class="f"><h4>Resume/CV*</h4><input type="file" id="resume" name="resume" required></div>
<div class="f"><h4>Cover Letter</h4><button type="button">Attach</button><button type="button" onclick="window.__clicked.push('dropbox')">Dropbox</button>
<button type="button" onclick="window.__clicked.push('drive')">Google Drive</button><button type="button">Enter manually</button>
<input type="file" id="cover_letter" name="cover_letter" style="display:none"></div>
<div><label for="rights">{Q_RIGHTS}*</label><input id="rights" role="combobox" type="text" required><div class="select__single-value" id="rights_v"></div></div>
<label for="visa_type">{Q_VISA}*</label><input id="visa_type" name="visa_type" type="text" maxlength="255" required>
<label for="notice">What is your notice period?*</label><input id="notice" name="notice" type="text" maxlength="255" required>
<label for="motivation">{Q_MOTIVATION}*</label><textarea id="motivation" name="motivation" required></textarea>
<label for="salary">Please provide your base salary expectations (in local currency, excluding bonus and commission).*</label>
<input id="salary" name="salary" type="text" maxlength="255" required>
<div><label for="hear">How did you hear about us?*</label><input id="hear" role="combobox" type="text" required><div class="select__single-value" id="hear_v"></div></div>
<label for="linkedin">Please include a link to your LinkedIn profile.*</label><input id="linkedin" name="linkedin" type="text" required>
<div><label for="declare">{Q_DECLARE}*</label><input id="declare" role="combobox" type="text" required><div class="select__single-value" id="declare_v"></div></div>
<label><input type="checkbox" name="gdpr" required> {Q_GDPR}*</label>
<div id="menu" style="display:none"></div>
<button type="submit" id="submit">Submit application</button></form>
<script>
window.__clicked = [];
const DATA = {{
  country: ["Ireland", "United Kingdom", "United States"],
  "candidate-location": ["Bristol, Connecticut, United States", "Bristol, England, United Kingdom", "Locate me"],
  rights: ["Citizen", "Permanent Resident", "Work Visa", "Require sponsorship"],
  hear: ["LinkedIn", "Indeed", "Company website", "Referral", "Other"],
  declare: ["Yes", "No"]
}};
for (const id of Object.keys(DATA)) {{
  const el = document.getElementById(id);
  const show = () => {{
    const q = el.value.toLowerCase();
    const menu = document.getElementById('menu'); menu.innerHTML = ''; menu.style.display = 'block';
    for (const t of DATA[id]) if (t.toLowerCase().includes(q) || t === 'Locate me') {{
      const o = document.createElement('div'); o.setAttribute('role', 'option'); o.className = 'opt'; o.textContent = t;
      o.onclick = () => {{ window.__clicked.push(t); document.getElementById(id + '_v').textContent = t; el.value = ''; menu.style.display = 'none'; }};
      menu.appendChild(o);
    }}
  }};
  el.addEventListener('click', show); el.addEventListener('input', show);
}}
document.getElementById('f').addEventListener('submit', e => {{ e.preventDefault(); document.body.dataset.submitted = '1'; }});
</script></body></html>"""


def book(visa_type: str = "") -> answers_mod.AnswerBook:
    entries = [
        answers_mod.Entry("notice_period", (answers_mod.norm("notice period"),), NOTICE, options=("Immediately", "No notice period")),
        answers_mod.Entry("notice_period", (answers_mod.norm("notice period"),), NOTICE),
        answers_mod.Entry("salary_expectation", (answers_mod.norm("salary expectations"),), SALARY),
        answers_mod.Entry("current_location", (answers_mod.norm("current location"),), "Bristol, UK"),
        answers_mod.Entry("how_heard", (answers_mod.norm("how did you hear"),), "{source}"),
        answers_mod.Entry("visa_statement", (answers_mod.norm("visa status"), answers_mod.norm("type of visa")), ""),
        answers_mod.Entry("privacy_consent", (answers_mod.norm("i declare"), answers_mod.norm("consent to")), "Yes"),      # code must still never choose it
    ]
    if visa_type:
        entries.append(answers_mod.Entry("visa_type", (answers_mod.norm("zzz"),), visa_type))
    return answers_mod.AnswerBook(entries, BOOK.motivations, BOOK.style)


@pytest.fixture
def page():
    with sync_playwright() as p:
        browser = p.chromium.launch()
        pg = browser.new_page()
        pg.set_content(PAGE)
        yield pg
        browser.close()


@pytest.fixture
def files(tmp_path):
    cv, letter = tmp_path / "Ada CV.pdf", tmp_path / "Ada Cover Letter.pdf"
    cv.write_bytes(b"%PDF-1.4\n%%EOF")
    letter.write_bytes(b"%PDF-1.4\n%%EOF")
    return str(cv), str(letter)


def run(page, files, settings, monkeypatch, visa_type="", llm=None, job=JOB_ROW):
    monkeypatch.setattr(assist_mod, "check_job_files", lambda *a, **k: None)
    free = ft.for_job(job, REP, FB, llm or llm_with(WHY, ad_phrases=[AD_PHRASE]), settings, book(visa_type), ask=lambda p: "", out=lambda s: None)
    return assist_mod.fill_page(page, APPLICANT, files[0], files[1], letter_text="x", job=job, book=book(visa_type), freetext=free, quiet_ms=50)


def table(report) -> dict:
    return {r["label"]: r for r in report["required_table"]}


def test_every_problem_of_the_rightship_dry_run_is_fixed(page, files, settings, monkeypatch):
    r = run(page, files, settings, monkeypatch)
    v = lambda sel: page.input_value(sel)                                               # noqa: E731
    shown = lambda sel: page.inner_text(sel)                                            # noqa: E731
    assert shown("#candidate-location_v") == "Bristol, England, United Kingdom"        # 3: Location (City)
    assert shown("#hear_v") == "Company website"                                       # 4: How did you hear about us
    assert shown("#country_v") == "United Kingdom"
    assert v("#visa_type") == ""                                                       # 5: not the Graduate-visa paragraph
    assert v("#notice") == "Immediately"                                               # a short form in a one-line box
    assert v("#salary") == SALARY                                                      # 6: the deflection sentence, unchanged
    assert v("#motivation") == WHY and len(WHY.split()) >= 60                          # 2: drafted, not left empty
    assert page.eval_on_selector("#cover_letter", "e => e.files.length ? e.files[0].name : ''") == "Ada Cover Letter.pdf"          # 1
    assert page.evaluate("window.__clicked") == [] or all(c not in ("locate", "Locate me", "dropbox", "drive") for c in page.evaluate("window.__clicked"))
    assert page.evaluate("document.body.dataset.submitted") is None


def test_the_required_table_blocks_only_on_what_the_person_must_do(page, files, settings, monkeypatch):
    r = run(page, files, settings, monkeypatch)
    t = table(r)
    blocking = {label: row["status"] for label, row in t.items() if row["status"] in RG.BLOCKING}
    names = " | ".join(blocking)
    assert len(blocking) == 4, blocking
    assert "type of visa" in names and "I declare" in names and "By checking this box" in names and "working rights" in names
    assert all(s == "NEEDS USER" for s in blocking.values())
    assert "answers.yaml has no `visa_type` entry" in next(row["reason"] for label, row in t.items() if "type of visa" in label)
    assert "attestation" in next(row["reason"] for label, row in t.items() if "I declare" in label)
    for label in ("First Name", "Last Name", "Email", "Phone", "Country", "Location (City)", "How did you hear about us?"):
        assert t[label]["status"] in ("VERIFIED", "FILLED"), (label, t[label])


def test_a_visa_type_entry_fills_the_box_and_the_table_clears_it(page, files, settings, monkeypatch):
    r = run(page, files, settings, monkeypatch, visa_type="Graduate visa")
    assert page.input_value("#visa_type") == "Graduate visa"
    assert not any("type of visa" in label for label, row in table(r).items() if row["status"] in RG.BLOCKING)


def test_the_declaration_and_the_consent_are_never_set_even_with_a_yes_in_answers_yaml(page, files, settings, monkeypatch):
    r = run(page, files, settings, monkeypatch)
    assert page.inner_text("#declare_v") == "" and page.eval_on_selector("[name=gdpr]", "e => e.checked") is False
    assert any("I declare" in a for a in r["attestations"]) and any("By checking this box" in a for a in r["attestations"])


def test_the_table_is_printed_with_every_status_and_blocking_rows_say_so(page, files, settings, monkeypatch, capsys):
    run(page, files, settings, monkeypatch)
    out = capsys.readouterr().out
    assert "REQUIRED FIELDS" in out and "BLOCKS AUTO-SUBMIT: 4 required field(s)" in out and "NEEDS USER" in out and "VERIFIED" in out


def test_with_no_jd_the_motivation_box_is_handed_off_and_nothing_is_drafted(page, files, settings, monkeypatch):
    from careerops.apply import formfind

    llm = llm_with(WHY, ad_phrases=[AD_PHRASE])
    short = {**JOB_ROW, "description": "Short ad. " * 10}
    with pytest.raises(formfind.HandOff, match="no JD"):
        run(page, files, settings, monkeypatch, llm=llm, job=short)
    assert page.input_value("#motivation") == "" and llm.seen == []


def test_the_dry_run_record_has_every_field_the_screenshot_and_the_files(page, files, settings, monkeypatch, tmp_path):
    import json

    from careerops.apply import dryrun

    free = ft.for_job(JOB_ROW, REP, FB, llm_with(WHY, ad_phrases=[AD_PHRASE]), settings, book(), ask=lambda p: "", out=lambda s: None)
    monkeypatch.setattr(assist_mod, "check_job_files", lambda *a, **k: None)
    r = assist_mod.fill_page(page, APPLICANT, files[0], files[1], letter_text="x", job=JOB_ROW, book=book(), freetext=free, quiet_ms=50, record=True)
    rows = [RG.Row(**d) for d in r.pop("all_fields")]
    rep = {"out_dir": str(tmp_path / "data" / "outputs" / "job"), "ats": {"strict": 63.4, "ceiling": 75.3}, "hook": {"hook_id": "h1"}, "letter": {"status": "ok"}}
    folder = dryrun.record(dryrun.folder_for(rep, JOB_ROW), page, JOB_ROW, rep, rows, cv=files[0], letter=files[1], letter_text="x", freetext=free)
    assert folder.parent == tmp_path / "data" / "dryruns" and folder.name.endswith("_Northwind_Health_AI_AI_Engineer_4982444101")
    data = json.loads((folder / "fields.json").read_text(encoding="utf-8"))
    assert len(data) >= len([x for x in data if x["required"]]) and {"label", "kind", "required", "value", "source", "status"} <= set(data[0])
    assert all(len(x["value"]) <= 120 for x in data)
    assert (folder / "fields.md").read_text(encoding="utf-8").startswith("# Fields of the filled form")
    assert (folder / "form_full.png").stat().st_size > 0 and (folder / "cv.pdf").exists() and (folder / "letter.pdf").exists() and (folder / "letter.txt").exists()
    m = (folder / "motivation.txt").read_text(encoding="utf-8")
    assert "WORDS:" in m and "GUARD: passed" in m and WHY[:40] in m
    assert {x["source"] for x in data if x["label"] == "Email"} == {"applicant"}
    assert next(x for x in data if x["label"].startswith("What is your notice"))["source"] == "answers:notice_period"
