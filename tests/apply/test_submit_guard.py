"""The one function that decides whether a page control could submit the application (apply/submit_guard.py). It reads the facts of an element
(tag, authored type, role, in a form, every name source) and never a browser, so these are plain dictionaries. A submit control gets no ref in the
form agent's snapshot and is refused again by the executor just before it acts."""
import pytest

from careerops.apply import submit_guard


def el(tag="button", **kw):
    base = {"tag": tag, "type": "", "role": "", "in_form": False, "aria_label": "", "labelledby": "", "placeholder": "", "text": "", "value": "",
            "name": "", "title": "", "alt": "", "id": "", "data": ""}
    base.update(kw)
    return base


# ------------------------------------------------------------------ the shapes
def test_an_icon_only_button_named_by_its_aria_label_is_a_submit():
    r = submit_guard.submit_reason(el(type="button", aria_label="Submit application"))
    assert r and "aria_label" in r


def test_an_input_button_with_a_german_value_is_a_submit():
    assert submit_guard.submit_reason(el("input", type="button", value="Absenden"))


@pytest.mark.parametrize("facts", [
    el(type="submit", text="Next"),                                   # the authored type is the shape, whatever the label says
    el("input", type="submit", value="Continue"),
    el("input", type="submit"),
    el("input", type="image", alt="Go"),
    el("input", type="image"),
    el(type="", in_form=True, text="Continue"),                      # a <button> with no type inside a <form> submits it
])
def test_the_submit_shapes_are_submits_whatever_they_say(facts):
    assert submit_guard.is_submit(facts)


@pytest.mark.parametrize("facts", [
    el(type="", in_form=False, text="Continue"),                     # no form to submit
    el(type="button", in_form=True, text="Next"),
    el(type="button", in_form=True, text="Continue to step 2"),
    el("a", text="Next"),
    el("div", role="button", text="Next page"),
    el(type="button", text="Back"),
    el(type="button", text="Upload"),
    el(type="button", text="Add another"),
    el(type="button", text="Select file"),
    el("input", type="button", value="Browse..."),
    el(type="button", text="Save draft"),
    el(type="button", aria_label="Next step"),
])
def test_ordinary_navigation_and_helper_controls_are_not_submits(facts):
    assert not submit_guard.is_submit(facts), facts


# ------------------------------------------------------------------ every name source, tested separately
@pytest.mark.parametrize("source", ["aria_label", "labelledby", "placeholder", "text", "value", "name", "title", "alt", "id", "data"])
def test_every_name_source_is_tested_on_its_own(source):
    facts = el(type="button", **{source: "submit-application"})
    r = submit_guard.submit_reason(facts)
    assert r and source in r


def test_a_display_label_that_says_next_does_not_hide_a_submit_in_another_name_source():
    assert submit_guard.is_submit(el(type="button", text="Next", aria_label="Submit application"))
    assert submit_guard.is_submit(el(type="button", text="Next", title="Absenden"))
    assert submit_guard.is_submit(el(type="button", text="Next", id="btnSubmitApplication"))       # camel case is split into words
    assert submit_guard.is_submit(el(type="button", text="Next", data="bottom-navigation-submit"))
    assert submit_guard.is_submit(el("a", role="button", text="Next", labelledby="Send application"))


@pytest.mark.parametrize("label", [
    "Submit", "Submit application", "SUBMIT APPLICATION", "Submitting...", "Send", "Send application", "Finish", "Finalize application",
    "Complete application", "Confirm and submit", "Review and submit",
    "Absenden", "Bewerbung abschicken", "Jetzt senden", "Einreichen",                                       # German
    "Envoyer", "Soumettre ma candidature", "Valider ma candidature",                                      # French
    "Enviar", "Enviar solicitud", "Enviar candidatura",                                                  # Spanish / Portuguese
    "Inviare", "Invia candidatura", "Verzenden", "Versturen", "Indienen", "Skicka in", "Send inn", "Lähetä", "Wyślij", "Odeslat", "Gönder",
    "提交", "提交申请", "送信", "送信する", "제출", "Отправить",                                             # Chinese / Japanese / Korean / Russian
])
def test_the_multilingual_submit_vocabulary(label):
    assert submit_guard.is_submit(el(type="button", text=label)), label


@pytest.mark.parametrize("label", ["Apply", "Apply now", "Apply for this job", "Bewerben", "Postuler", "Solicitar", "応募", "I'm interested"])
def test_an_apply_control_is_a_submit_inside_a_form_and_the_way_in_outside_one(label):
    assert submit_guard.is_submit(el("a", role="button", in_form=True, text=label)), label
    assert not submit_guard.is_submit(el("a", text=label, in_form=False)), label
    assert not submit_guard.is_submit(el(type="button", text=label, in_form=False)), label


def test_a_field_is_not_a_control_so_its_name_does_not_matter():
    for tag, typ in (("input", "text"), ("input", "email"), ("textarea", ""), ("select", ""), ("input", "file"), ("input", "checkbox")):
        assert not submit_guard.is_submit(el(tag, type=typ, name="send_updates", aria_label="Send me job alerts", id="submit_field"))


def test_the_reason_names_the_source_and_the_word():
    r = submit_guard.submit_reason(el(type="button", title="Enviar solicitud"))
    assert r and "title" in r and "enviar" in r.lower()
    assert "type=submit" in submit_guard.submit_reason(el(type="submit", text="Next"))
    assert "<form>" in submit_guard.submit_reason(el(type="", in_form=True, text="Next"))


def test_the_label_the_human_sees_is_the_best_name_source():
    assert submit_guard.label_of(el(type="button", aria_label="Submit application", text="")) == "Submit application"
    assert submit_guard.label_of(el(type="submit", value="Absenden")) == "Absenden"
    assert submit_guard.label_of(el("input", type="image", alt="Go")) == "Go"
    assert submit_guard.label_of(el(type="submit")) == "submit"
