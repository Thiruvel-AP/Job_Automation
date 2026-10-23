"""The form-question loop (apply/formloop.py), on a fixture copied from the layout of the 23 Oct ElevenLabs Ashby form: three OPTIONAL text areas that
the scanner used to miss (it printed `YOU ANSWER: []` and freetext.py never ran). Every field is scanned and printed before filling; each free-text
field goes observe -> classify -> retrieve -> draft -> check -> fill -> verify, at most 3 passes, the form scanned again after each fill; a question the
library answers (similarity >= 0.85) fills without a draft; an unseen one is drafted and filled at once as a NEW DRAFT; a thing the fact bank never
mentions is not claimed (WEAK EVIDENCE); the gated auto-apply does not submit while a new draft is unapproved; "flagged as possible spam" after a submit
is an immediate hand-off that is recorded and not counted as one of the two allowed failures. Local forms in headless Chromium (no network, nothing
submitted anywhere, MockLLM as the model); Chromium missing = the tests fail."""
import csv
import functools
import json
from datetime import date
from pathlib import Path

import pytest
import yaml
from challenge_fakes import ChallengePage
from conftest import PROFILE_FIX
from playwright.sync_api import sync_playwright
from test_autoapply import APPLICANT, FIX, chromium, world  # noqa: F401  (the fixtures and the runner of the auto-apply tests)

from careerops import autoapply
from careerops.apply import answers as answers_mod
from careerops.apply import assist as assist_mod
from careerops.apply import autolog, formfind, formloop, handoff as handoff_mod
from careerops.apply import failures as F
from careerops.apply import freetext as ft
from careerops.llm import MockLLM

pytestmark = pytest.mark.slow          # these tests drive Chromium

FB = yaml.safe_load((PROFILE_FIX / "fact_bank.yaml").read_text(encoding="utf-8"))
BOOK = answers_mod.load(PROFILE_FIX / "answers.yaml")
JD = (PROFILE_FIX.parent / "tailoring" / "jd_northwind.txt").read_text(encoding="utf-8")
REP = {"keywords": [{"term": "llm", "weight": 3.0, "required": True, "alts": []}], "hook": {"proof_bullet_id": "az1"}}
LIMBIC_1, LIMBIC_2 = ft.SEEDS[0][2], ft.SEEDS[1][2]
DRAFT_AUTOMATION = "I built a tool-calling LLM agent (LangChain, AWS Bedrock) at Northwind Research Lab for a multi-omics cancer research platform."
DRAFT_STAKEHOLDERS = "At Northwind Research Lab I built a tool-calling LLM agent with LangChain and AWS Bedrock for a multi-omics cancer research platform."
CLAIMS_THE_MISSING_PART = "I built a tool-calling LLM agent (LangChain, AWS Bedrock) with non-engineering stakeholders at Northwind Research Lab."
Q_AGENT = "Describe an agent or LLM-powered system you have shipped. What did it do?"
Q_AUTOMATION = "Tell us about an automation or agent you took from prototype to production"
Q_STAKEHOLDERS = "Describe a project you built with non-engineering stakeholders"
Q_ANYTHING = "Anything else you would like us to know?"
VALUES_JS = "() => Object.fromEntries([...document.querySelectorAll('input,textarea,select')].map(e => [e.name, e.type === 'file' ? (e.files[0]||{}).name || '' : e.value]))"


class Script:
    """Answers the prompts in order, remembers them; an exhausted script answers ''."""

    def __init__(self, *replies):
        self.replies, self.prompts = list(replies), []

    def __call__(self, prompt):
        self.prompts.append(prompt)
        return self.replies.pop(0) if self.replies else ""


def llm_with(*drafts):
    """A model that returns the drafts in order (the last one again when it runs out) and records every context it was given."""
    seen, queue = [], list(drafts)

    def handler(ctx):
        seen.append(ctx)
        return {"answer": queue.pop(0) if len(queue) > 1 else queue[0], "ad_phrases": []}
    llm = MockLLM({"free_text_answer": handler})
    llm.seen = seen
    return llm


def llm_per(by_question: dict):
    """A model that answers each question from its own list of drafts (the last one again when it runs out) and records every context."""
    seen, queues = [], {q: list(d) for q, d in by_question.items()}

    def handler(ctx):
        seen.append(ctx)
        queue = queues[ctx["question"]]
        return {"answer": queue.pop(0) if len(queue) > 1 else queue[0], "ad_phrases": []}
    llm = MockLLM({"free_text_answer": handler})
    llm.seen = seen
    return llm


def no_model():
    def handler(ctx):
        raise AssertionError(f"the model was asked: {ctx['question']!r}")
    return MockLLM({"free_text_answer": handler})


def free(settings, llm, ask, rep=REP):
    return ft.for_job({"id": "ashby:elevenlabs:1", "company": "ElevenLabs", "title": "Senior AI Engineer", "description": JD}, rep, FB, llm, settings,
                      BOOK, ask=ask, out=lambda s: None, today=date(2026, 10, 23))


@pytest.fixture
def page(chromium):
    pg = chromium.new_page()
    pg.goto((FIX / "ashby_elevenlabs.html").resolve().as_uri())
    yield pg
    pg.close()


@pytest.fixture
def cv(tmp_path):
    p = tmp_path / "Ada Lovelace CV.pdf"
    p.write_bytes(b"%PDF-1.4\n%%EOF")
    return str(p)


def steps(folder: Path, question_part: str) -> list[str]:
    rows = [json.loads(line) for line in (folder / formloop.FILE).read_text(encoding="utf-8").splitlines()]
    return [r["step"] for r in rows if question_part in r["field"]]


# ------------------------------------------------------------------ the scanner
def test_the_scanner_finds_every_field_including_the_three_optional_text_areas(page):
    fields = {f.question: f for f in formloop.scan(page)}
    for q in (Q_AGENT, Q_AUTOMATION, Q_STAKEHOLDERS, Q_ANYTHING):
        assert fields[q].kind == "textarea" and fields[q].required is False and fields[q].free_text
    assert fields[Q_AGENT].maxlength == "1500" and fields[Q_AUTOMATION].maxlength == ""
    assert fields["Name"].required and fields["Email"].required and fields["Resume"].kind == "file"
    sel = fields["How did you hear about this role?"]
    assert sel.kind == "select" and sel.options[:3] == ["Select...", "LinkedIn", "Referral"]
    assert "What was the hardest part of building that feature (1-2 sentences)" not in fields           # hidden until the first text area has an answer


def test_the_scanner_reads_a_label_from_aria_label_or_the_nearest_text_and_groups_radios(page):
    page.set_content("""<form>
      <textarea aria-label="Why this team?"></textarea>
      <div><p>Tell us about a model you monitored</p><textarea name="monitor"></textarea></div>
      <fieldset><legend>Do you have a driving licence?</legend>
        <label><input type="radio" name="dl" value="y"> Yes</label><label><input type="radio" name="dl" value="n" checked> No</label></fieldset>
      <label><input type="checkbox" name="agree"> I agree to the privacy notice</label></form>""")
    by = {f.question: f for f in formloop.scan(page)}
    assert by["Why this team?"].kind == "textarea"
    assert by["Tell us about a model you monitored"].kind == "textarea"
    dl = by["Do you have a driving licence?"]
    assert dl.kind == "radio" and dl.options == ["Yes", "No"] and dl.value == "No"
    assert by["I agree to the privacy notice"].kind == "checkbox"
    assert len([f for f in formloop.scan(page) if f.kind == "radio"]) == 1                     # a group is one field


def test_the_table_of_all_fields_is_printed_before_anything_is_filled(page, cv, capsys):
    assist_mod.fill_page(page, APPLICANT, cv, None, book=BOOK, quiet_ms=50)
    out = capsys.readouterr().out
    first = out.index("FORM FIELDS (")
    assert first < out.index("Reading back every field")
    table = out[first:out.index("FORM FIELDS AFTER FILLING")]
    for q in (Q_AGENT, Q_AUTOMATION, Q_STAKEHOLDERS, Q_ANYTHING, "Name", "Resume"):
        assert q in table
    assert "textarea" in table and "Select..." in table                                         # type and options


# ------------------------------------------------------------------ the loop
def test_an_unseen_question_gets_a_generated_draft_filled_at_once_and_marked_new_draft(page, cv, settings, capsys):
    llm = llm_with(DRAFT_AUTOMATION, DRAFT_STAKEHOLDERS)
    report = assist_mod.fill_page(page, APPLICANT, cv, None, book=BOOK, quiet_ms=50, freetext=free(settings, llm, Script()))
    v = page.evaluate(VALUES_JS)
    assert v["automation"] == DRAFT_AUTOMATION and v["stake"] == DRAFT_STAKEHOLDERS                # in the field, though nobody has said y
    out = capsys.readouterr().out
    final = out[out.index("FORM FIELDS AFTER FILLING"):]
    assert final.count("NEW DRAFT") >= 2
    assert any("free text (NEW DRAFT)" in x and "prototype to production" in x for x in report["filled"])
    assert [r for r in report["manual"] if "NEW DRAFT" in r] and not report["required_empty"]
    assert all(rec.answer not in (DRAFT_AUTOMATION, DRAFT_STAKEHOLDERS) for rec in ft.Library(settings.path("answers_library")).records())    # not approved: not stored


def test_a_question_the_library_answers_fills_without_a_draft_and_without_asking(page, cv, settings, capsys):
    ask = Script()
    llm = llm_with(DRAFT_AUTOMATION, DRAFT_STAKEHOLDERS)
    report = assist_mod.fill_page(page, APPLICANT, cv, None, book=BOOK, quiet_ms=50, freetext=free(settings, llm, ask))
    v = page.evaluate(VALUES_JS)
    assert v["agent"] == LIMBIC_1                                                                   # "Describe an agent or LLM-powered system you have shipped..."
    assert v["hardest"] == LIMBIC_2                                                                 # the question that appeared after that answer
    assert [c["question"] for c in llm.seen] == [Q_AUTOMATION, Q_STAKEHOLDERS]                       # the model was never asked about the two library questions
    assert not any("agent or LLM-powered" in p or "hardest" in p for p in ask.prompts)
    assert any("free text (reused)" in x for x in report["filled"])
    assert "LIBRARY" in capsys.readouterr().out


def test_the_unrelated_optional_question_is_skipped_not_drafted(page, cv, settings, tmp_path):
    llm = llm_with(DRAFT_AUTOMATION, DRAFT_STAKEHOLDERS)
    assist_mod.fill_page(page, APPLICANT, cv, None, book=BOOK, quiet_ms=50, freetext=free(settings, llm, Script()))
    assert page.evaluate(VALUES_JS)["anything"] == "" and all(c["question"] != Q_ANYTHING for c in llm.seen)
    assert "skip" in steps(Path(cv).parent, "Anything else")


def test_a_field_that_appears_after_an_answer_is_found_by_the_rescan(page, cv, settings, tmp_path):
    assist_mod.fill_page(page, APPLICANT, cv, None, book=BOOK, quiet_ms=50, freetext=free(settings, llm_with(DRAFT_AUTOMATION), Script()))
    rows = [json.loads(line) for line in (Path(cv).parent / formloop.FILE).read_text(encoding="utf-8").splitlines()]
    assert any(r["step"] == "observe" and r.get("phase") == "appeared after a fill" and "hardest part" in r["field"] for r in rows)


def test_every_step_of_every_field_is_logged_in_the_job_folder(page, cv, settings):
    assist_mod.fill_page(page, APPLICANT, cv, None, book=BOOK, quiet_ms=50,
                         freetext=free(settings, llm_with(DRAFT_AUTOMATION, DRAFT_STAKEHOLDERS), Script()))
    folder = Path(cv).parent
    assert steps(folder, "prototype to production") == ["observe", "classify", "retrieve", "draft", "check", "fill", "verify", "status"]
    assert steps(folder, "agent or LLM-powered") == ["observe", "classify", "retrieve", "fill", "verify", "status"]            # a library answer: no draft, no check
    row = next(json.loads(line) for line in (folder / formloop.FILE).read_text(encoding="utf-8").splitlines()
               if json.loads(line)["step"] == "retrieve" and "agent or LLM-powered" in json.loads(line)["field"])
    assert row["origin"] == "reused" and "LLM feature" in row["library"]


def test_a_field_gets_at_most_three_passes(page, cv, settings):
    llm = llm_per({Q_AUTOMATION: [DRAFT_AUTOMATION], Q_STAKEHOLDERS: [CLAIMS_THE_MISSING_PART]})        # every draft claims what the bank does not have
    assist_mod.fill_page(page, APPLICANT, cv, None, book=BOOK, quiet_ms=50, freetext=free(settings, llm, Script()))
    ctxs = [c for c in llm.seen if c["question"] == Q_STAKEHOLDERS]
    assert len(ctxs) == formloop.MAX_PASSES == 3
    assert ctxs[0]["problems"] == [] and any("non-engineering" in p for p in ctxs[1]["problems"])    # the second pass is told why the first failed
    assert page.evaluate(VALUES_JS)["stake"] == ""                                                    # an optional question with no passing draft stays empty


# ------------------------------------------------------------------ weak evidence
def test_a_weak_evidence_draft_is_flagged_and_does_not_claim_the_missing_part(page, cv, settings, capsys):
    llm = llm_per({Q_AUTOMATION: [DRAFT_AUTOMATION], Q_STAKEHOLDERS: [CLAIMS_THE_MISSING_PART, DRAFT_STAKEHOLDERS]})
    assist_mod.fill_page(page, APPLICANT, cv, None, book=BOOK, quiet_ms=50, freetext=free(settings, llm, Script()))
    stake = [c for c in llm.seen if c["question"] == Q_STAKEHOLDERS]
    assert stake[0]["not_in_bank"] == ["non-engineering"]                                             # the model is told what the bank does not have
    text = page.evaluate(VALUES_JS)["stake"]
    assert text == DRAFT_STAKEHOLDERS and "non-engineering" not in text.lower()
    out = capsys.readouterr().out
    assert "WEAK EVIDENCE: non-engineering stakeholders vs az1" in out


def test_asks_reads_what_a_question_asks_for_beyond_its_kind():
    assert ft.asks("Describe an agent or LLM-powered system you have shipped") == []
    assert ft.asks(Q_AUTOMATION) == [("prototype", ["prototype"])]
    assert ft.asks(Q_STAKEHOLDERS) == [("non-engineering stakeholders", ["non-engineering", "stakeholders"])]
    assert ft.asks("Describe a time you used Salesforce", "ElevenLabs") == [("Salesforce", ["salesforce"])]


def test_the_bank_decides_what_is_unsupported(settings):
    f = free(settings, no_model(), Script())
    assert f.unsupported(Q_STAKEHOLDERS) == [("non-engineering stakeholders", ["non-engineering"])]           # stakeholders: the bank has them
    assert f.unsupported("Describe a project you built with sales stakeholders") == [("sales stakeholders", ["sales"])]
    assert f.unsupported(Q_AUTOMATION) == []                                                         # "prototype": dc1 has it


def test_a_question_that_overlaps_no_entry_is_answered_from_the_closest_true_entry_and_marked(settings):
    f = free(settings, llm_with(DRAFT_AUTOMATION), Script(), rep={"keywords": [], "hook": {}})
    p = f.propose("Describe a project you built about zzyzx", "", None)
    assert p.plan.fallback and p.text and p.weak.startswith("WEAK EVIDENCE: ") and p.plan.ids


def test_similarity_matches_the_seeded_limbic_question_at_or_above_the_threshold():
    seed = ft.SEEDS[0][0]
    for q in ("Describe an agent or LLM-powered system you have shipped…", Q_AGENT,
              "Describe an agent or LLM-powered system you have shipped. How did you measure it and what went wrong?"):
        assert ft.similarity(q, seed) >= ft.SIMILAR
    assert ft.similarity(Q_AUTOMATION, seed) < ft.SIMILAR and ft.similarity(Q_STAKEHOLDERS, seed) < ft.SIMILAR


# ------------------------------------------------------------------ approval
def test_y_stores_a_new_draft_in_the_library_and_clears_the_mark(page, cv, settings):
    report = assist_mod.fill_page(page, APPLICANT, cv, None, book=BOOK, quiet_ms=50,
                                  freetext=free(settings, llm_with(DRAFT_AUTOMATION, DRAFT_STAKEHOLDERS), Script("y", "y")))
    assert not [r for r in report["manual"] if "NEW DRAFT" in r]
    stored = [rec.answer for rec in ft.Library(settings.path("answers_library")).records()]
    assert DRAFT_AUTOMATION in stored and DRAFT_STAKEHOLDERS in stored
    assert sum("free text (approved)" in x for x in report["filled"]) == 2
    assert [d["status"] for d in report["drafts"]] == ["approved by you"] * 2


def test_an_edit_replaces_the_text_in_the_field_and_is_what_is_stored(page, cv, settings):
    edited = "I built a LangChain agent on AWS Bedrock for a cancer research platform."
    assist_mod.fill_page(page, APPLICANT, cv, None, book=BOOK, quiet_ms=50,
                         freetext=free(settings, llm_with(DRAFT_AUTOMATION, DRAFT_STAKEHOLDERS), Script("e", edited, "y")))
    assert page.evaluate(VALUES_JS)["automation"] == edited
    assert edited in [rec.answer for rec in ft.Library(settings.path("answers_library")).records()]


# ------------------------------------------------------------------ the gated auto-apply
def run_auto(world, monkeypatch, replies, llm, **extra):
    book = answers_mod.load(PROFILE_FIX / "answers.yaml")
    f = ft.for_job({**world.job, "description": JD}, REP, FB, llm, world.settings, book, ask=input, today=date(2026, 10, 23))
    monkeypatch.setattr(autoapply, "run", functools.partial(autoapply.run, freetext=f))
    return world.run(extra.pop("page", "ashby_elevenlabs.html"), replies, submit=True, **extra)


def test_auto_mode_does_not_submit_while_a_new_draft_is_unapproved(world, monkeypatch):
    r = run_auto(world, monkeypatch, {"[y] use": ""}, llm_with(DRAFT_AUTOMATION, DRAFT_STAKEHOLDERS))
    assert r.status == autolog.NEEDS_USER and "new draft not approved" in r.reason and world.submit_calls == 0
    assert world.state["values"]["automation"] == DRAFT_AUTOMATION                                   # the draft is in the field for the person who takes over
    assert world.state["values"]["agent"] == LIMBIC_1                                                # the library answer needed no approval
    assert sum("[y] use" in p for p in world.prompts) == 2                                           # one question per NEW draft, none for the library answers


def test_auto_mode_submits_once_every_new_draft_has_a_y_or_an_edit(world, monkeypatch):
    r = run_auto(world, monkeypatch, {"[y] use": ["y", "e"], "edited answer": "I built a LangChain agent on AWS Bedrock for a cancer research platform."},
                 llm_with(DRAFT_AUTOMATION, DRAFT_STAKEHOLDERS))
    assert r.status == autolog.SUBMITTED and world.submit_calls == 1
    ev = world.events
    assert ev.index("submit clicked") > max(i for i, e in enumerate(ev) if "[y] use" in e)


# ------------------------------------------------------------------ spam flags
@pytest.fixture
def ashby_host(monkeypatch):
    """The fixture pages are file:// URLs: give them the host the real form has."""
    monkeypatch.setattr(handoff_mod, "host_of", lambda url: "jobs.ashbyhq.com")
    monkeypatch.setattr(F, "host_of", lambda url: "jobs.ashbyhq.com")


def test_a_spam_flag_after_the_submit_is_an_immediate_hand_off_that_is_recorded_not_counted_and_never_retried(world, monkeypatch, ashby_host):
    log = F.Failures(world.settings)
    h = world.handoff()
    assert h.listed("https://jobs.ashbyhq.com/elevenlabs/1") is None                                 # not on apply.handoff_hosts before the flag
    r = run_auto(world, monkeypatch, {"[y] use": ["y", "y"]}, llm_with(DRAFT_AUTOMATION, DRAFT_STAKEHOLDERS),
                 page="ashby_elevenlabs.html?spam", handoff=h, failures=log)
    assert r.status == autolog.NEEDS_USER and r.handed_off and "spam" in r.reason and world.submit_calls == 1          # the button was pressed once, never again
    assert [row["kind"] for row in log.rows(world.job["id"])] == [F.SPAM_FLAG] and log.count(world.job["id"]) == 0 and log.gate(world.job["id"]) is None
    assert log.flagged()[0]["host"] == "jobs.ashbyhq.com"
    with open(log.path, encoding="utf-8", newline="") as fh:
        assert next(csv.DictReader(fh))["kind"] == "spam_flag"
    assert "jobs.ashbyhq.com" in h.hosts() and len(world.opened) == 1                               # the host is remembered, the person's browser opened
    paste = (Path(world.job["out_dir"]) / "answers_to_paste.txt").read_text(encoding="utf-8")
    assert DRAFT_AUTOMATION in paste and DRAFT_STAKEHOLDERS in paste


def test_the_next_job_on_a_host_that_flagged_a_submit_goes_straight_to_the_hand_off(world, monkeypatch, ashby_host):
    log = F.Failures(world.settings)
    run_auto(world, monkeypatch, {"[y] use": ["y", "y"]}, llm_with(DRAFT_AUTOMATION, DRAFT_STAKEHOLDERS), page="ashby_elevenlabs.html?spam",
             handoff=world.handoff(), failures=log)
    world.events.clear()
    world.job = {**world.job, "id": "ashby:elevenlabs:2"}
    r = run_auto(world, monkeypatch, {}, no_model(), handoff=world.handoff(), failures=log)
    assert r.handed_off and "handed a job over before" in r.reason and world.submit_calls == 1 and "browser closed" not in world.events
    assert len(world.opened) == 1 and log.count("ashby:elevenlabs:2") == 0


def test_the_drafted_answers_are_in_answers_to_paste_when_the_assist_hands_the_page_over(page, cv, settings, tmp_path, monkeypatch, job):
    monkeypatch.setattr(assist_mod, "check_job_files", lambda *a, **k: None)
    state = {"filled": False}
    real = assist_mod.fill_page

    def fill_then_block(*a, **k):
        out = real(*a, **k)
        state["filled"] = True
        return out
    monkeypatch.setattr(assist_mod, "fill_page", fill_then_block)
    monkeypatch.setattr(assist_mod.formfind, "blocker", lambda p: "a CAPTCHA / bot check is on the page" if state["filled"] else None)
    out = tmp_path / "joblog"
    out.mkdir()
    j = {**job, "out_dir": str(out), "apply_url": (FIX / "ashby_elevenlabs.html").resolve().as_uri()}
    h = handoff_mod.Handoff(settings, opener=lambda url: True, out=lambda s: None, clip=lambda t: True)
    r = assist_mod.assist(j, {"cv_pdf": cv}, APPLICANT, tmp_path / "profile", launch=lambda d: (page, lambda: None), ask=Script(), book=BOOK,
                          freetext=free(settings, llm_with(DRAFT_AUTOMATION, DRAFT_STAKEHOLDERS), Script()), handoff=h)
    paste = (out / "answers_to_paste.txt").read_text(encoding="utf-8")
    assert r["handoff"] and "FREE-TEXT ANSWERS DRAFTED FOR THIS FORM" in paste
    assert DRAFT_AUTOMATION in paste and "[NEW DRAFT]" in paste and "WEAK EVIDENCE: non-engineering stakeholders" in paste


# ------------------------------------------------------------------ spam text and the failure count (no browser)
@pytest.mark.parametrize("text", ["Your application submission was flagged as possible spam.", "We couldn't submit your application. Try again.",
                                  "Sorry, we could not submit your application", "This submission was flagged as spam"])
def test_a_spam_flag_message_is_a_blocker_and_its_own_failure_kind(text):
    reason = formfind.blocker(ChallengePage(text=text))
    assert reason and "spam" in reason and F.kind_of(reason) == F.SPAM_FLAG and F.kind_of(text) == F.SPAM_FLAG
    assert formfind.blocker(ChallengePage(text="Apply for Data Scientist. We never send spam.")) is None


def test_two_spam_rows_do_not_use_up_the_two_allowed_failures(settings):
    log = F.Failures(settings)
    for _ in range(3):
        assert log.record("j1", "https://jobs.ashbyhq.com/x/1", F.SPAM_FLAG, "flagged as possible spam") == 0
    assert log.count("j1") == 0 and log.gate("j1") is None and len(log.rows("j1")) == 3
    log.record("j1", "https://jobs.ashbyhq.com/x/1", F.NOT_FOUND, "FORM NOT FOUND x")
    log.record("j1", "https://jobs.ashbyhq.com/x/1", F.FIELD_LOST, "lost")
    assert log.count("j1") == 2 and log.gate("j1")


def test_paste_text_lists_drafts_the_person_has_not_approved(settings):
    drafts = [{"question": Q_AUTOMATION, "answer": DRAFT_AUTOMATION, "status": "NEW DRAFT", "weak": "WEAK EVIDENCE: prototype vs az1"}]
    text = handoff_mod.paste_text({"id": "j", "company": "ElevenLabs", "title": "AI Engineer"}, {}, APPLICANT, BOOK, None, "https://x.example/1", "why", [],
                                  settings, drafts)
    assert "FREE-TEXT ANSWERS DRAFTED FOR THIS FORM" in text and DRAFT_AUTOMATION in text and "[NEW DRAFT] [WEAK EVIDENCE: prototype vs az1]" in text
