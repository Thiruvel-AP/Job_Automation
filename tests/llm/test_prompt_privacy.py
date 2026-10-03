"""Contact details never leave the machine: no prompt (system or user) carries the applicant's address, email,
phone or links. The CV and letter templates read them straight from the fact bank at render time."""
import re
from pathlib import Path

from careerops.tailoring.tailor import regenerate_letter, tailor_job

TASKS = {"extract_keywords", "tailor_cv", "cover_letter"}


def _private_strings(identity: dict) -> list[str]:
    out = [identity["address"], identity["email"], identity["email"].split("@")[0], identity["phone"],
           identity["phone"].replace(" ", ""), identity["phone"].lstrip("+").replace(" ", "")]
    for url in identity["links"].values():
        out += [url, re.sub(r"^https?://(www\.)?", "", url).rstrip("/")]
    return out


def test_fixture_identity_has_every_private_field(fb_fixture):
    ident = fb_fixture["identity"]
    assert all(ident.get(k) for k in ("address", "email", "phone")) and len(ident["links"]) == 3


def test_no_prompt_contains_address_email_phone_or_links(settings, fb_fixture, job, spy_llm):
    llm = spy_llm()
    rep = tailor_job(job, fb_fixture, llm, settings, settings.path("outputs"))
    regenerate_letter(job, fb_fixture, llm, settings, rep)             # review's 'l' sends a prompt too
    assert {s["task"] for s in llm.sent} >= TASKS and llm.calls.count("cover_letter") == 2
    secrets = _private_strings(fb_fixture["identity"])
    for sent in llm.sent:
        blob = sent["system"] + "\n" + sent["user"]       # the task context is MockLLM's side channel, not sent
        for secret in secrets:
            assert secret not in blob, f"{secret!r} leaked into the {sent['task']} prompt"


def test_the_private_details_do_reach_the_rendered_files(settings, fb_fixture, job, spy_llm):
    """Guards the guard: the test above is only meaningful if the details really are rendered somewhere."""
    from careerops.tailoring.render import pdf_text
    rep = tailor_job(job, fb_fixture, spy_llm(), settings, settings.path("outputs"))
    assert "12 Analytical Row" in pdf_text(Path(rep["letter_pdf"]))
    assert "ada.lovelace@example.org" in pdf_text(Path(rep["cv_pdf"]))
