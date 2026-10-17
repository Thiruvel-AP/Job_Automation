"""Outreach drafts (careerops/outreach.py). Gmail is `FakeGoogle`, which answers a draft create and fails the test on any other call, so a
send could not slip through; DNS and the company site are fakes too. Nothing here touches the network."""
import json
import sys
from pathlib import Path

import pytest
import yaml

from careerops import cli, contacts, outreach, tracker
from careerops.googleapi import GoogleAPI
from careerops.review import workflow
from outreach_fakes import APPLIED_AT, HOOK_LINE, FakeFetcher, FakeGoogle, make_applied, mail_domains

AD = "Questions? Write to hiring.team@northwind.example or priya.nair@northwind.example. Req ID: R-45821."
SUBJECT = "Application: AI Engineer at Northwind Health AI – Ada Lovelace"


@pytest.fixture
def row(settings, db, job, tmp_path):
    return make_applied(settings, db, {**job, "description": AD}, tmp_path)


@pytest.fixture
def google(settings):
    fake = FakeGoogle()
    return fake, GoogleAPI(settings, request=fake, token="t", sleep=lambda s: None)


def give(db, row, email, *, name="", role="", source="jd", mx="ok", hunter=None):
    contacts._save(db, row["id"], contacts.Contact(email=email, name=name, role=role, source=source, mx_status=mx, hunter_status=hunter))


def set_hook_line(row, line):
    p = Path(row["out_dir"]) / "report.json"
    rep = json.loads(p.read_text(encoding="utf-8"))
    rep["hook"]["letter_sentence"] = line
    p.write_text(json.dumps(rep), encoding="utf-8")


def body_of(msg) -> str:
    return msg.get_body(("plain",)).get_content()


# ------------------------------------------------------------------ the draft
def test_a_draft_is_created_never_sent_with_the_subject_the_hook_line_and_both_pdfs(settings, db, row, google):
    fake, api = google
    give(db, row, "hiring.team@northwind.example")
    res = outreach.draft_for_job(db, settings, row, api=api)
    assert [a for a, _ in res.drafted] == ["hiring.team@northwind.example"] and res.status == "drafted 1"
    assert [(m, u) for m, u, *_ in fake.calls] == [("POST", "https://gmail.googleapis.com/gmail/v1/users/me/drafts")]     # create, and only that
    msg = fake.drafts[0]
    assert msg["Subject"] == SUBJECT and msg["To"] == "hiring.team@northwind.example" and msg["From"] is None
    assert [p.get_filename() for p in msg.iter_attachments()] == ["Ada Lovelace CV.pdf", "Ada Lovelace Cover Letter.pdf"]
    body = body_of(msg)
    assert HOOK_LINE in body                                                               # verbatim
    assert body.splitlines()[0] == "Hello Northwind Health AI team,"                       # a role address has no first name
    assert "I have just applied for AI Engineer (R-45821)." in body
    assert outreach.ASK in body and outreach.OPT_OUT.replace("you'd", "you'd") in body
    assert body.rstrip().endswith("I won't contact you again.")
    for line in ("Ada Lovelace", "+44 7700 900123", "https://www.linkedin.com/in/ada-lovelace-test/", "https://github.com/ada-lovelace-test"):
        assert line in body
    assert len(body.split()) <= 120


def test_a_named_contact_is_greeted_by_first_name_and_a_job_without_a_req_id_has_no_brackets(settings, db, job, tmp_path, google):
    fake, api = google
    row = make_applied(settings, db, {**job, "description": "No reference number here."}, tmp_path)
    give(db, row, "grace@northwind.example", name="Grace Murray Hopper", role="Hiring Manager", source="user")
    outreach.draft_for_job(db, settings, row, api=api)
    body = body_of(fake.drafts[0])
    assert body.splitlines()[0] == "Dear Grace," and "I have just applied for AI Engineer." in body


def test_no_letter_chosen_means_only_the_cv_is_attached(settings, db, job, tmp_path, google):
    fake, api = google
    row = make_applied(settings, db, {**job, "description": AD}, tmp_path, letter=False)
    give(db, row, "hiring.team@northwind.example")
    outreach.draft_for_job(db, settings, row, api=api)
    assert [p.get_filename() for p in fake.drafts[0].iter_attachments()] == ["Ada Lovelace CV.pdf"]


def test_a_cv_gone_from_the_output_folder_is_taken_from_the_archive_folder(settings, db, row, google, tmp_path):
    fake, api = google
    archive = tmp_path / "archive"
    archive.mkdir()
    (archive / "Ada Lovelace CV.pdf").write_bytes(b"%PDF archived")
    (Path(row["out_dir"]) / "Ada Lovelace CV.pdf").unlink()
    db.update_job(row["id"], application_dir=str(archive))
    give(db, row, "hiring.team@northwind.example")
    outreach.draft_for_job(db, settings, dict(db.job(row["id"])), api=api)
    cv = next(fake.drafts[0].iter_attachments())
    assert cv.get_filename() == "Ada Lovelace CV.pdf" and cv.get_content() == b"%PDF archived"
    (archive / "Ada Lovelace CV.pdf").unlink()
    with pytest.raises(outreach.OutreachError, match="CV PDF is not there"):
        outreach.draft_for_job(db, settings, dict(db.job(row["id"])), api=api)


def test_a_job_with_no_hook_line_gets_no_draft(settings, db, job, tmp_path, google):
    fake, api = google
    row = make_applied(settings, db, {**job, "description": AD}, tmp_path, hook=False)
    give(db, row, "hiring.team@northwind.example")
    with pytest.raises(outreach.OutreachError, match="no hook letter line"):
        outreach.draft_for_job(db, settings, row, api=api)
    assert fake.calls == [] and db.conn.execute("SELECT COUNT(*) FROM outreach_drafts").fetchone()[0] == 0


# ------------------------------------------------------------------ limits and the do-not-contact list
def test_one_draft_per_contact_and_two_per_job_however_often_it_runs(settings, db, row, google):
    fake, api = google
    for email in ("a@northwind.example", "b@northwind.example", "c@northwind.example"):
        give(db, row, email)
    res = outreach.draft_for_job(db, settings, row, api=api)
    assert [a for a, _ in res.drafted] == ["a@northwind.example", "b@northwind.example"] and res.status == "drafted 2"
    assert len(fake.drafts) == 2
    again = outreach.draft_for_job(db, settings, row, api=api)
    assert again.drafted == [] and again.status == "drafted 2" and len(fake.drafts) == 2               # no third, no second to the same person
    assert db.conn.execute("SELECT COUNT(*) FROM outreach_drafts WHERE job_id=?", (row["id"],)).fetchone()[0] == 2


def test_a_second_run_with_one_contact_makes_no_second_draft_for_them(settings, db, row, google):
    fake, api = google
    give(db, row, "a@northwind.example")
    outreach.draft_for_job(db, settings, row, api=api)
    again = outreach.draft_for_job(db, settings, row, api=api)
    assert len(fake.drafts) == 1 and again.status == "drafted 1"


def test_the_do_not_contact_list_is_honoured_for_an_address_and_a_domain(settings, db, row, google):
    fake, api = google
    contacts.ensure_dnc_file(settings).write_text("value,note\nrudi@northwind.example,asked\nagency.example,all\n", encoding="utf-8")
    for email in ("rudi@northwind.example", "x@agency.example", "ok@northwind.example"):
        give(db, row, email)
    res = outreach.draft_for_job(db, settings, row, api=api)
    assert [a for a, _ in res.drafted] == ["ok@northwind.example"]
    assert dict(res.skipped) == {"rudi@northwind.example": "do-not-contact (rudi@northwind.example)", "x@agency.example": "do-not-contact (agency.example)"}
    assert [m["To"] for m in fake.drafts] == ["ok@northwind.example"]


def test_nothing_is_drafted_when_every_contact_is_blocked_and_the_status_says_why(settings, db, row, google):
    fake, api = google
    contacts.ensure_dnc_file(settings).write_text("value,note\nnorthwind.example,no\n", encoding="utf-8")
    give(db, row, "a@northwind.example")
    res = outreach.draft_for_job(db, settings, row, api=api)
    assert fake.calls == [] and res.status.startswith("no draft: a@northwind.example: do-not-contact")
    assert outreach.status(db, row["id"]) == res.status and outreach.follow_up_due(db, settings, row) == ""


def test_a_domain_that_takes_no_mail_an_unverified_hunter_address_and_a_name_without_an_address_get_no_draft(settings, db, row, google):
    fake, api = google
    give(db, row, "dead@gone.example", mx="no_mx")
    give(db, row, "maybe@northwind.example", source="hunter", hunter="accept_all")
    contacts._save(db, row["id"], contacts.Contact(name="Grace Hopper", source="user"))
    res = outreach.draft_for_job(db, settings, row, api=api)
    assert fake.calls == [] and res.drafted == []
    assert dict(res.skipped) == {"dead@gone.example": "domain check: no_mx", "maybe@northwind.example": "unverified address (Hunter: accept_all)",
                                 "Grace Hopper": "no address yet"}


def test_a_verified_hunter_address_is_drafted_and_a_domain_check_that_never_ran_is_run_first(settings, db, row, google):
    fake, api = google
    give(db, row, "grace@northwind.example", name="Grace Hopper", source="hunter", mx=None, hunter="valid")
    res = outreach.draft_for_job(db, settings, row, api=api, resolver=mail_domains("northwind.example"))
    assert [a for a, _ in res.drafted] == ["grace@northwind.example"]
    assert contacts.stored(db, row["id"])[0]["mx_status"] == "ok"


# ------------------------------------------------------------------ the body checks
@pytest.mark.parametrize("line,why", [
    ("At Northwind Research Lab our team built a tool-calling LLM agent.", "uses 'our'"),
    ("At Northwind Research Lab we built a tool-calling LLM agent.", "uses 'we'"),
    ("At Northwind Research Lab I built a tool-calling LLM agent that cut costs by 7391%.", "a number the fact bank does not have: 7391"),
    ("At Northwind Research Lab I built a tool-calling LLM agent. " + "It was careful and kept careful notes. " * 9, "words, over the 120 allowed"),
])
def test_a_body_with_we_our_an_invented_number_or_too_many_words_is_refused_and_nothing_is_drafted(settings, db, row, google, line, why):
    fake, api = google
    set_hook_line(row, line)
    give(db, row, "a@northwind.example")
    res = outreach.draft_for_job(db, settings, row, api=api)
    assert fake.calls == [] and res.drafted == [] and why in res.skipped[0][1] and "body refused" in res.skipped[0][1]
    assert res.status.startswith("no draft: ")


def test_another_companys_name_in_the_body_is_refused_but_the_targets_and_your_own_employers_are_fine(settings, db, row, google):
    fake, api = google
    db.upsert_job({"id": "x:globex", "source": "x", "company": "Globex Corporation", "title": "Data Scientist", "location": "Bristol, UK",
                   "url": "u", "apply_url": "u", "description": "d"})
    give(db, row, "a@northwind.example")
    set_hook_line(row, "At Globex Corporation I built a tool-calling LLM agent.")
    assert "names 'Globex Corporation'" in outreach.draft_for_job(db, settings, row, api=api).skipped[0][1]
    assert fake.calls == []
    set_hook_line(row, "At Northwind Health AI and at Northwind Research Lab I built a tool-calling LLM agent.")
    assert outreach.draft_for_job(db, settings, row, api=api).drafted                          # the target, and a bank employer


# ------------------------------------------------------------------ the test draft (--to)
def test_to_makes_one_test_draft_to_that_address_and_records_nothing(settings, db, row, google):
    fake, api = google
    give(db, row, "a@northwind.example")
    res = outreach.draft_for_job(db, settings, row, api=api, to="Apthiruvel@Gmail.com")
    assert res.test and res.status == "test draft" and [a for a, _ in res.drafted] == ["apthiruvel@gmail.com"]
    assert len(fake.drafts) == 1 and fake.drafts[0]["To"] == "apthiruvel@gmail.com"
    assert [p.get_filename() for p in fake.drafts[0].iter_attachments()] == ["Ada Lovelace CV.pdf", "Ada Lovelace Cover Letter.pdf"]
    assert HOOK_LINE in body_of(fake.drafts[0])
    assert db.conn.execute("SELECT COUNT(*) FROM outreach_drafts").fetchone()[0] == 0           # no limit used, nothing to show in the Sheet
    assert outreach.status(db, row["id"]) == "" and outreach.follow_up_due(db, settings, row) == ""
    with pytest.raises(outreach.OutreachError, match="not an e-mail address"):
        outreach.draft_for_job(db, settings, row, api=api, to="nobody")


def test_the_follow_up_is_seven_days_after_applying_and_follows_the_setting(settings, db, row, google):
    fake, api = google
    give(db, row, "a@northwind.example")
    assert outreach.follow_up_due(db, settings, row) == ""
    outreach.draft_for_job(db, settings, row, api=api)
    assert APPLIED_AT.startswith("2026-10-17") and outreach.follow_up_due(db, settings, row) == "2026-10-24"
    settings.raw["outreach"]["follow_up_days"] = 10
    assert outreach.follow_up_due(db, settings, row) == "2026-10-27"


def test_req_ids_are_taken_from_the_ad_only_when_it_names_one():
    assert outreach.req_id({"description": "Requisition ID: 2026-0044"}) == "2026-0044"
    assert outreach.req_id({"description": "Job reference: JR1234567"}) == "JR1234567"
    assert outreach.req_id({"description": "We need 3 years of Python and a reference letter"}) == ""
    assert outreach.req_id({"description": None}) == ""


# ------------------------------------------------------------------ after mark-applied, and the review screen
def test_outreach_is_off_by_default_and_a_draft_after_mark_applied_needs_mode_drafts(settings, db, row, monkeypatch, capsys):
    assert outreach.mode(settings) == "off" and not outreach.drafts_on(settings)
    monkeypatch.setattr(outreach, "draft_for_job", lambda *a, **k: pytest.fail("a draft was made with outreach.mode off"))
    monkeypatch.setattr(contacts, "discover", lambda *a, **k: pytest.fail("contacts were looked up with outreach.mode off"))
    monkeypatch.setattr(tracker, "sync", lambda *a, **k: {row["id"]: "added"})
    cli._after_applied(db, settings, row["id"])
    assert "tracker: row added" in capsys.readouterr().out


def test_with_mode_drafts_the_drafts_are_made_then_the_row_is_sent_and_a_failure_never_blocks_it(settings, db, row, monkeypatch, capsys):
    settings.raw["outreach"]["mode"] = "drafts"
    seen = []
    monkeypatch.setattr(contacts, "discover", lambda *a, **k: seen.append("discover") or [])
    monkeypatch.setattr(outreach, "draft_for_job", lambda *a, **k: seen.append("draft") or outreach.Result(status="drafted 1"))
    monkeypatch.setattr(tracker, "sync", lambda *a, **k: seen.append("sync") or {row["id"]: "updated"})
    cli._after_applied(db, settings, row["id"])
    out = capsys.readouterr().out
    assert seen == ["discover", "draft", "sync"] and "outreach: drafted 1" in out and "tracker: row updated" in out

    seen.clear()
    monkeypatch.setattr(outreach, "draft_for_job", lambda *a, **k: (_ for _ in ()).throw(outreach.OutreachError("no hook")))
    cli._after_applied(db, settings, row["id"])
    captured = capsys.readouterr()
    assert "outreach: no drafts made (no hook)" in captured.err and "tracker: row updated" in captured.out and seen == ["discover", "sync"]
    assert db.job(row["id"])["status"] == "applied"


@pytest.fixture
def cfg(tmp_path, settings):
    p = tmp_path / "settings.yaml"
    p.write_text(yaml.safe_dump(settings.raw), encoding="utf-8")
    return p


def test_the_cli_makes_a_test_draft_and_says_nothing_was_sent(cfg, settings, db, row, google, capsys, monkeypatch):
    fake, api = google
    monkeypatch.setattr(outreach, "drafts_api", lambda s: api)
    monkeypatch.setattr(contacts, "discover", lambda *a, **k: pytest.fail("a test draft looks nobody up"))
    cli.main(["--config", str(cfg), "outreach", "draft", row["id"], "--to", "apthiruvel@gmail.com"])
    out = capsys.readouterr().out
    assert "Draft created in Gmail for apthiruvel@gmail.com" in out and "Nothing was sent" in out and "Outreach: test draft" in out
    assert len(fake.drafts) == 1


def test_the_cli_drafts_for_the_jobs_contacts_and_refreshes_the_tracker_row(cfg, settings, db, row, google, capsys, monkeypatch):
    fake, api = google
    give(db, row, "hiring.team@northwind.example")
    monkeypatch.setattr(outreach, "drafts_api", lambda s: api)
    monkeypatch.setattr(contacts, "discover", lambda *a, **k: [])
    synced = []
    monkeypatch.setattr(tracker, "sync", lambda db_, s, ids, **k: synced.append(ids) or {ids[0]: "updated"})
    cli.main(["--config", str(cfg), "outreach", "draft", row["id"]])
    out = capsys.readouterr().out
    assert "Outreach: drafted 1" in out and synced == [[row["id"]]] and len(fake.drafts) == 1


def test_the_cli_exits_1_when_no_draft_could_be_made_and_refuses_mock_and_a_missing_job(cfg, settings, db, row, google, capsys, monkeypatch):
    fake, api = google
    monkeypatch.setattr(outreach, "drafts_api", lambda s: api)
    monkeypatch.setattr(contacts, "discover", lambda *a, **k: [])
    with pytest.raises(SystemExit) as e:
        cli.main(["--config", str(cfg), "outreach", "draft", row["id"]])                      # no contact at all
    assert e.value.code == 1 and "no draft: no contact with an address" in capsys.readouterr().out and fake.calls == []
    for argv, msg in ((["outreach", "draft", row["id"], "--mock"], "never uses the network"), (["outreach", "draft", "nope:1"], "no such job"),
                      (["outreach", "draft"], "needs a job id")):
        with pytest.raises(SystemExit):
            cli.main(["--config", str(cfg), *argv])
        assert msg in capsys.readouterr().err


def test_the_old_linkedin_note_form_of_outreach_still_needs_its_flags(cfg, capsys):
    with pytest.raises(SystemExit):
        cli.main(["--config", str(cfg), "outreach"])
    assert "outreach draft JOB" in capsys.readouterr().err


def test_approving_a_job_shows_the_contacts_step_only_with_mode_drafts_and_never_on_the_mock_root(settings, db, row, monkeypatch):
    from careerops import mockroot
    calls = []
    monkeypatch.setattr(contacts, "review_step", lambda db_, s, job, ask: calls.append(job["id"]))
    workflow.contacts_step(db, row, settings, input)
    assert calls == []                                                                         # mode off
    settings.raw["outreach"]["mode"] = "drafts"
    workflow.contacts_step(db, row, settings, input)
    assert calls == [row["id"]]
    workflow.contacts_step(db, row, mockroot.mock_settings(settings), input)
    assert calls == [row["id"]]                                                                # the mock root has no network


def test_review_job_runs_the_contacts_step_after_approve_and_not_after_reject(settings, db, row, monkeypatch):
    settings.raw["outreach"]["mode"] = "drafts"
    steps = []
    monkeypatch.setattr(workflow, "contacts_step", lambda db_, job, s, ask: steps.append(job["id"]))
    monkeypatch.setattr(workflow, "load_report", lambda job: {"letter": {"status": "ok"}, "letters": None})
    monkeypatch.setattr(workflow, "summary", lambda *a, **k: "SUMMARY")
    monkeypatch.setattr(workflow, "_ask_judge", lambda *a, **k: None)
    monkeypatch.setattr(workflow, "letter_status", lambda rep: "ok")
    monkeypatch.setattr(workflow, "approve", lambda db_, job, rep, prefs, confirm=None: db.update_job(job["id"], status="approved"))
    monkeypatch.setattr(workflow, "reject", lambda *a, **k: None)
    monkeypatch.setattr(workflow.judge, "log_decision", lambda *a, **k: None)
    typed = iter(["r", "x", "a"])
    assert workflow.review_job(db, db.job(row["id"]), settings, {}, ask=lambda p: next(typed)) == "rejected" and steps == []
    typed = iter(["a"])
    assert workflow.review_job(db, db.job(row["id"]), settings, {}, ask=lambda p: next(typed)) == "approved" and steps == [row["id"]]
