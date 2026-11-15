"""v2.5: reaching the form (apply/formscan.py) and filling it from the answer plan (apply/planfill.py) in Chromium, on local pages. Nothing is
submitted; no network (pages are set with set_content or are local files); MockLLM for the one draft."""
import pytest
from playwright.sync_api import sync_playwright
from careerops.apply.browser import CHROMIUM_ARGS
from test_freetext import AD_PHRASE, FB, REP, WHY, llm_with
from test_greenhouse_form import APPLICANT, JOB_ROW, PAGE, book, plan_for

from careerops.apply import answerplan as AP
from careerops.apply import formscan, planfill, planfirst
from careerops.apply import freetext as ft
from careerops.apply.errors import BrowserClosed

pytestmark = pytest.mark.slow          # these tests drive Chromium

# every key event of the page, so a test can see that Enter is never pressed
SPY = "<script>window.__keys=[];document.addEventListener('keydown',e=>window.__keys.push(e.key),true);</script>"


@pytest.fixture
def browser():
    with sync_playwright() as p:
        b = p.chromium.launch(args=CHROMIUM_ARGS)
        yield b
        b.close()


@pytest.fixture
def page(browser):
    ctx = browser.new_context()
    pg = ctx.new_page()
    yield pg
    try:
        ctx.close()
    except Exception:  # noqa: BLE001
        pass


@pytest.fixture
def files(tmp_path):
    cv, letter = tmp_path / "Ada CV.pdf", tmp_path / "Ada Cover Letter.pdf"
    cv.write_bytes(b"%PDF-1.4\n%%EOF")
    letter.write_bytes(b"%PDF-1.4\n%%EOF")
    return str(cv), str(letter)


def sources(settings, files, visa_type=""):
    free = ft.for_job(JOB_ROW, REP, FB, llm_with(WHY, ad_phrases=[AD_PHRASE]), settings, book(visa_type), ask=lambda p: "", out=lambda s: None)
    return AP.Sources(APPLICANT, book(visa_type), JOB_ROW, plan_for(), files[0], files[1], "Hi team,\n\nBody.", "", free)


def plan_and_fill(page, settings, files, visa_type="", html=PAGE, url=""):
    body = html.replace("<body>", "<body>" + SPY, 1)
    if url:                                                    # served by a route: a real origin (history.pushState works), still no network
        page.route("https://forms.test/**", lambda route: route.fulfill(status=200, content_type="text/html", body=body))
        page.goto(url)
    else:
        page.set_content(body)
    frame, fields = formscan.dom_fields(page, wait_ms=2000)
    planfirst.read_combo_options(page, frame, fields)          # as the headless pre-scan does: a script-built list is opened, read, closed (Escape)
    src = sources(settings, files, visa_type)
    plan = AP.build(AP.questions_from_page(fields), src, "page", out=lambda s: None)
    out = []
    res = planfill.Filler(page, frame, job=JOB_ROW, cv=files[0], letter=files[1], out=out.append, quiet_ms=50).run(plan, fields, src)
    return plan, res, out


def status(res, label_start):
    return next(d for d in res.done if d.row.q.label.startswith(label_start))


def test_the_rightship_form_is_filled_from_the_plan_and_every_required_field_is_verified_or_yours(page, settings, files):
    plan, res, out = plan_and_fill(page, settings, files)
    rows = {r.label[:30]: r.status for r in res.required_rows()}
    yours = {k for k, v in rows.items() if v != "VERIFIED"}
    assert yours == {"If you answered 'Work Visa' in", "I declare that the information", "By checking this box, I consen"}, rows
    assert status(res, "Location (City)").value == "Bristol, England, United Kingdom"
    assert status(res, "Country").value == "United Kingdom" and status(res, "How did you hear").value == "Company website"
    assert status(res, "What are your working rights").value == "Work Visa"
    assert status(res, "What motivated you").value == WHY
    assert page.evaluate("window.__clicked").count("Locate me") == 0 and not set(page.evaluate("window.__clicked")) & {"locate", "dropbox", "drive"}
    assert page.evaluate("document.body.dataset.submitted") is None
    assert any(line.startswith("[") and "Location (City) … VERIFIED \"Bristol, England, United Kingdom\"" in line for line in out), out


def test_a_react_select_is_typed_and_its_option_clicked_and_enter_is_never_pressed(page, settings, files):
    plan, res, out = plan_and_fill(page, settings, files, visa_type="Graduate visa")
    keys = page.evaluate("window.__keys")
    assert keys and "Enter" not in keys                                             # typed one key at a time, never Enter
    assert status(res, "If you answered 'Work Visa'").status == planfill.VERIFIED


def test_a_form_inside_an_iframe_is_found_through_the_richest_frame(page):
    inner = "<form><label for=e>Email *</label><input id=e type=email required><label for=r>Resume/CV</label><input id=r type=file></form>"
    page.set_content(f"<html><body><h1>Careers</h1><iframe id=gh srcdoc='{inner}'></iframe></body></html>")
    got = formscan.reach(page, "", navigate=False, out=lambda s: None, wait_ms=3000)
    assert got.ok and got.frame != page.main_frame and {f.label for f in got.fields} >= {"Email", "Resume/CV"}


def test_a_cookie_banner_over_the_form_is_dismissed(page):
    page.set_content("""<html><body>
      <div id="onetrust-banner-sdk" style="position:fixed;inset:0;background:#fff;z-index:9">We use cookies
        <button id="onetrust-accept-btn-handler" onclick="this.parentElement.style.display='none'">Accept All Cookies</button></div>
      <form><label for=e>Email *</label><input id=e type=email required></form></body></html>""")
    note = formscan.dismiss_consent(page)
    assert note and not page.is_visible("#onetrust-banner-sdk")


def test_a_lazy_form_that_appears_only_after_scrolling_is_found(page):
    page.set_content("""<html><body><div style="height:3000px">Job description ...</div><div id=slot></div>
      <script>window.addEventListener('scroll', () => { if (window.scrollY > 1000 && !document.getElementById('e')) document.getElementById('slot')
        .innerHTML = '<form><label for=e>Email *</label><input id=e type=email required><label for=n>First name</label><input id=n></form>'; });</script>
      </body></html>""")
    got = formscan.reach(page, "", navigate=False, out=lambda s: None, wait_ms=1000)
    assert got.ok and any(f.label == "Email" for f in got.fields)


def test_a_navigation_during_the_fill_stops_the_run_with_the_message(page, settings, files):
    html = """<html><body><form><label for=first_name>First Name*</label><input id=first_name name=first_name required>
      <label for=email>Email*</label><input id=email name=email type=email required></form>
      <script>document.getElementById('first_name').addEventListener('input', () => history.pushState({}, '', '/submitted'));</script></body></html>"""
    plan, res, out = plan_and_fill(page, settings, files, html=html, url="https://forms.test/apply")
    assert res.navigated and any("PAGE NAVIGATED DURING FILL: check whether anything was submitted" in line for line in out)
    assert len(res.done) == 1                                                        # stopped after the field that moved the page


def test_a_context_closed_mid_fill_raises_browser_closed(page, settings, files, monkeypatch):
    real = planfill.Filler.put
    calls = []

    def put(self, row, dom):
        calls.append(row.n)
        if len(calls) == 3:
            self.page.context.close()
        return real(self, row, dom)
    monkeypatch.setattr(planfill.Filler, "put", put)
    with pytest.raises(BrowserClosed):
        plan_and_fill(page, settings, files)


# ------------------------------------------------------------------ what the real RightShip page showed (2 Nov 2026 stand-in run)
TWO_LISTS = """<html><body><form>
<label for=phone>Phone*</label><input id=phone name=phone type=tel required>
<ul id=dial role=listbox style="display:block"><li role=option>Afghanistan+93</li><li role=option>United Kingdom+44</li></ul>
<div class="select__container"><label for=hear>How did you hear about us?*</label>
<input id=hear role=combobox aria-controls=hear-listbox required><div class="select__single-value" id=hear_v></div>
<div class="select__menu" id=hear-listbox style="display:none"></div></div>
<div class=f><div id=upload-label-resume>Resume/CV*</div><label for=resume>Attach</label><input type=file id=resume name=resume required></div>
</form><script>
const el = document.getElementById('hear'), menu = document.getElementById('hear-listbox');
el.addEventListener('click', () => { menu.innerHTML = ''; menu.style.display = 'block';
  for (const t of ['LinkedIn', 'Company website', 'Other']) { const o = document.createElement('div'); o.setAttribute('role', 'option');
    o.className = 'select__option'; o.textContent = t; o.onclick = () => { document.getElementById('hear_v').textContent = t; menu.style.display = 'none'; };
    menu.appendChild(o); } });
document.getElementById('resume').addEventListener('change', e => {            // the form takes the file and replaces the input with a chip
  const name = e.target.files[0].name; const box = e.target.parentElement; e.target.remove();
  const chip = document.createElement('span'); chip.textContent = name + ' ×'; box.appendChild(chip); });
</script></body></html>"""


def test_a_drop_down_reads_only_its_own_list_and_an_upload_shown_as_a_chip_is_verified(page, settings, files):
    plan, res, out = plan_and_fill(page, settings, files, html=TWO_LISTS)
    hear = status(res, "How did you hear")
    assert hear.status == planfill.VERIFIED and hear.value == "Company website"                # never "United Kingdom+44" of the phone list
    assert hear.row.q.options == ["LinkedIn", "Company website", "Other"]
    cv = status(res, "Resume/CV")                                                                # the upload's heading, not its "Attach" button
    assert cv.status == planfill.VERIFIED and cv.value == "Ada CV.pdf"
