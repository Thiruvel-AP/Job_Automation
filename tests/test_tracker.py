"""The Google Sheet tracker (careerops/tracker.py). The Sheet is `FakeSheet` (same four methods as SheetsClient) and Google's HTTP
API is a fake `request`: no network. Rows are checked by header text, which is how the tracker finds them too."""
import pytest
import yaml

from careerops import cli, contacts, tracker
from careerops.db import DB
from careerops.discovery import gmail
from careerops.googleapi import GoogleAPI
from outreach_fakes import FakeGoogle, FakeSheet, Resp, make_applied

TAB = "Applications"


@pytest.fixture
def applied(settings, db, job, tmp_path):
    return make_applied(settings, db, job, tmp_path)


def sheet_with_headers(headers=None):
    return FakeSheet([list(headers or tracker.HEADERS)])


def only_row(sheet) -> dict:
    rows = sheet.table()
    assert len(rows) == 1, rows
    return rows[0]


# ------------------------------------------------------------------ the header and the record
def test_the_36_headers_fill_a1_to_aj_and_job_id_is_first():
    """The first 36 never move (v2.4 appended Region and Size after Notes: AK and AL)."""
    assert len(tracker.HEADERS) == 38 and len(set(tracker.HEADERS)) == 38
    assert tracker.HEADERS[0] == "Job ID" and tracker.HEADERS[33] == "Status" and tracker.HEADERS[35] == "Notes" and tracker.HEADERS[36:] == ["Region", "Size"]
    assert [tracker.column_letter(i) for i in (0, 25, 26, 33, 35, 36, 37)] == ["A", "Z", "AA", "AH", "AJ", "AK", "AL"]


def test_the_record_carries_the_job_its_report_and_the_applied_date(settings, db, applied):
    rec = tracker.record(db, settings, db.job(applied["id"]))
    assert set(rec) <= set(tracker.HEADERS)
    assert rec["Job ID"] == "manual:northwind1" and rec["Date applied"] == "2026-10-17"
    assert (rec["Company"], rec["Role"], rec["Posting URL"]) == ("Northwind Health AI", "AI Engineer", "https://example.com/job")
    assert rec["Posting age (h)"] == 24.0                                  # posted 2026-10-16 09:00, applied 2026-10-17 09:00
    assert rec["Sponsor match"] == "exact (Skilled Worker A)" and rec["Role family"] == "ai_engineer"
    assert (rec["ATS strict (%)"], rec["ATS ceiling (%)"], rec["Stretch"]) == (78.5, 91.0, "No")
    assert (rec["Hook ID"], rec["Hook headline"]) == ("h_agent_harness", "agents that recover from tool failure")
    assert (rec["CV file"], rec["Letter"]) == ("Ada Lovelace CV.pdf", "Ada Lovelace Cover Letter.pdf")
    assert rec["Projects on CV"] == "Tool Agent, Forecaster" and rec["Bullets on CV"] == "az1, az2, p1, p2, p3"
    assert rec["Top JD keywords"] == "Kubernetes, LangChain, Python, CI/CD"           # heaviest first
    assert rec["Learn list"] == "Kubernetes, CI/CD"                                    # what the fact bank cannot claim
    assert "LangChain" in rec["Question to ask"] and rec["Status"] == "Applied"
    assert rec["JD text"] == applied["description"][:45_000]


def test_a_job_without_a_report_or_a_letter_still_gets_a_row(settings, db, job, tmp_path):
    row = make_applied(settings, db, job, tmp_path, letter=False)
    assert tracker.record(db, settings, row)["Letter"] == "none"
    db.update_job(job["id"], out_dir=str(tmp_path / "gone"))
    rec = tracker.record(db, settings, db.job(job["id"]))
    assert rec["Company"] == "Northwind Health AI" and rec["CV file"] == "" and rec["ATS strict (%)"] == ""


@pytest.mark.parametrize("location,text,mode", [
    ("Bristol, UK", "A great job in our office.", "On-site"),
    ("Bristol, UK", "This is a hybrid role, two days a week in the office.", "Hybrid"),
    ("Remote", "This is a fully remote role. Work from anywhere.", "Remote"),
    ("Bristol, UK", "Hybrid working. The team is distributed.", "Hybrid"),
])
def test_work_mode_is_remote_hybrid_or_on_site(settings, location, text, mode):
    from careerops.discovery import workcountry
    job = {"location": location, "description": text, "title": "Data Scientist"}
    assert tracker.work_mode(job, workcountry.applicant_visa(settings)) == mode


def test_the_jd_text_is_cut_at_45000_characters_the_sheet_cell_limit_is_50000(settings, db, job, tmp_path):
    row = make_applied(settings, db, job, tmp_path, description="x" * 60_000)
    rec = tracker.record(db, settings, row)
    assert len(rec["JD text"]) == tracker.JD_MAX == 45_000
    sheet = sheet_with_headers()
    tracker.upsert(sheet, TAB, rec)
    assert len(only_row(sheet)["JD text"]) == 45_000


# ------------------------------------------------------------------ one row per job
def test_the_first_sync_adds_one_row_and_the_second_updates_it_instead_of_adding_another(settings, db, applied):
    sheet = sheet_with_headers()
    assert tracker.sync(db, settings, [applied["id"]], sheet=sheet, tab=TAB) == {applied["id"]: "added"}
    row = only_row(sheet)
    assert row["Job ID"] == applied["id"] and row["Status"] == "Applied" and row["Date applied"] == "2026-10-17"
    assert row["Interview date"] == "" and row["Notes"] == ""
    assert tracker.sync(db, settings, [applied["id"]], sheet=sheet, tab=TAB) == {applied["id"]: "unchanged"}      # idempotent
    assert len(sheet.table()) == 1 and len(sheet.appends) == 1 and len(sheet.writes) == 0

    contacts._save(db, applied["id"], contacts.Contact(email="jobs@northwind.example", source="jd", mx_status="ok"))
    assert tracker.sync(db, settings, [applied["id"]], sheet=sheet, tab=TAB) == {applied["id"]: "updated"}
    assert len(sheet.table()) == 1 and only_row(sheet)["Contact 1 email"] == "jobs@northwind.example"
    assert len(sheet.appends) == 1                                                  # still the one row, changed in place
    assert set(sheet.writes[-1]) == {f"'Applications'!{tracker.column_letter(tracker.HEADERS.index(h))}2"
                                     for h in ("Contact 1 email", "Contact 1 source")}       # only the cells that changed


def test_sync_with_no_ids_sends_every_applied_job_and_only_those(settings, db, job, tmp_path):
    a = make_applied(settings, db, job, tmp_path)
    b = make_applied(settings, db, {**job, "id": "manual:second", "company": "Second Co"}, tmp_path / "b")
    db.upsert_job({**job, "id": "manual:notyet", "company": "Not Yet"})                # a job that is not applied is not a row
    sheet = sheet_with_headers()
    res = tracker.sync(db, settings, sheet=sheet, tab=TAB)
    assert res == {a["id"]: "added", b["id"]: "added"}
    assert sorted(r["Company"] for r in sheet.table()) == ["Northwind Health AI", "Second Co"]
    assert set(tracker.sync(db, settings, sheet=sheet, tab=TAB).values()) == {"unchanged"} and len(sheet.table()) == 2


def test_status_interview_date_and_notes_that_you_wrote_survive_every_sync(settings, db, applied):
    sheet = sheet_with_headers()
    tracker.sync(db, settings, [applied["id"]], sheet=sheet, tab=TAB)
    i = {h: tracker.HEADERS.index(h) for h in ("Status", "Interview date", "Notes", "Outreach status")}
    sheet.rows[1][i["Status"]], sheet.rows[1][i["Interview date"]], sheet.rows[1][i["Notes"]] = "Interview", "2026-10-30", "phone screen booked"
    db.log(applied["id"], "outreach", {"status": "no draft: x"})                            # something else changes, so a write happens
    contacts._save(db, applied["id"], contacts.Contact(email="a@northwind.example", source="jd", mx_status="ok"))
    assert tracker.sync(db, settings, [applied["id"]], sheet=sheet, tab=TAB) == {applied["id"]: "updated"}
    row = only_row(sheet)
    assert (row["Status"], row["Interview date"], row["Notes"]) == ("Interview", "2026-10-30", "phone screen booked")
    written = {a1 for w in sheet.writes for a1 in w}
    assert not any(a1.startswith(f"'Applications'!{tracker.column_letter(i[h])}") for a1 in written for h in ("Status", "Interview date", "Notes"))


def test_status_is_written_as_applied_only_while_its_cell_is_empty(settings, db, applied):
    sheet = sheet_with_headers()
    tracker.sync(db, settings, [applied["id"]], sheet=sheet, tab=TAB)
    col = tracker.HEADERS.index("Status")
    sheet.rows[1][col] = ""                                                                # you cleared it
    contacts._save(db, applied["id"], contacts.Contact(email="a@northwind.example", source="jd", mx_status="ok"))
    tracker.sync(db, settings, [applied["id"]], sheet=sheet, tab=TAB)
    assert only_row(sheet)["Status"] == "Applied"
    sheet.rows[1][col] = "Offer"
    contacts._save(db, applied["id"], contacts.Contact(email="b@northwind.example", source="jd", mx_status="ok"))
    tracker.sync(db, settings, [applied["id"]], sheet=sheet, tab=TAB)
    assert only_row(sheet)["Status"] == "Offer"


def test_columns_are_found_by_header_text_so_moved_and_extra_columns_are_fine(settings, db, applied):
    headers = list(reversed(tracker.HEADERS))[:34] + ["My own column"]                       # reordered, two headers gone, one extra
    sheet = FakeSheet([headers])
    tracker.sync(db, settings, [applied["id"]], sheet=sheet, tab=TAB)
    row = only_row(sheet)
    assert row["Company"] == "Northwind Health AI" and row["Job ID"] == applied["id"] and row["My own column"] == ""
    assert sheet.rows[0][:35] == headers                                                    # nothing reordered or renamed
    appended = sheet.rows[0][35:]
    assert set(appended) == set(tracker.HEADERS) - set(headers)                              # the missing ones, at the end
    assert sheet.grown                                                                       # the grid was widened first
    sheet.rows[1][headers.index("My own column")] = "keep me"
    contacts._save(db, applied["id"], contacts.Contact(email="a@northwind.example", source="jd", mx_status="ok"))
    tracker.sync(db, settings, [applied["id"]], sheet=sheet, tab=TAB)
    assert only_row(sheet)["My own column"] == "keep me"


def test_an_empty_tab_gets_all_36_headers_then_the_row(settings, db, applied):
    sheet = FakeSheet([])
    tracker.sync(db, settings, [applied["id"]], sheet=sheet, tab=TAB)
    assert sheet.rows[0] == tracker.HEADERS and only_row(sheet)["Job ID"] == applied["id"]


def test_the_outreach_columns_follow_the_drafts(settings, db, applied):
    db.conn.execute("INSERT INTO outreach_drafts VALUES (?,?,?,?,?)", (applied["id"], "a@northwind.example", "d1", "s", "2026-10-17"))
    db.conn.commit()
    rec = tracker.record(db, settings, db.job(applied["id"]))
    assert rec["Outreach status"] == "drafted 1" and rec["Follow-up due"] == "2026-10-24"        # applied 17 Oct + 7 days


def test_two_contacts_at_most_are_shown_best_source_first(settings, db, applied):
    for email, src in (("c@northwind.example", "company_site"), ("b@northwind.example", "jd"), ("a@northwind.example", "jd")):
        contacts._save(db, applied["id"], contacts.Contact(email=email, source=src, mx_status="ok"))
    contacts._save(db, applied["id"], contacts.Contact(name="Grace Hopper", role="Hiring Manager", email="grace@northwind.example", source="user",
                                                       mx_status="ok"))
    rec = tracker.record(db, settings, db.job(applied["id"]))
    assert (rec["Contact 1 name"], rec["Contact 1 role"], rec["Contact 1 email"], rec["Contact 1 source"]) == (
        "Grace Hopper", "Hiring Manager", "grace@northwind.example", "user")
    assert rec["Contact 2 email"] == "b@northwind.example" and "Contact 3 email" not in rec


# ------------------------------------------------------------------ the Sheets API calls
class SheetsHTTP(FakeGoogle):
    def __call__(self, method, url, headers=None, params=None, json=None):
        self.calls.append((method, url, headers, params, json))
        if method == "GET" and "/values/" in url:
            return Resp({"values": [["Job ID"]]})
        return Resp({})


def test_the_sheets_calls_are_raw_paced_and_carry_the_token_only_in_the_header(settings):
    http, sleeps, now = SheetsHTTP(), [], [0.0]
    api = GoogleAPI(settings, request=http, token="TOKEN123", sleep=lambda s: sleeps.append(s) or now.__setitem__(0, now[0] + s),
                    clock=lambda: now[0])
    sheet = tracker.SheetsClient(api, "SHEET-ID")
    assert sheet.get_values("'Applications'!1:1") == [["Job ID"]]
    sheet.update_cells({"'Applications'!B2": "2026-10-17", "'Applications'!C2": 85.2})
    sheet.append_row("Applications", ["x", 1])
    (m1, u1, h1, p1, _), (m2, u2, _, _, b2), (m3, u3, _, p3, b3) = http.calls
    assert m1 == "GET" and u1.startswith("https://sheets.googleapis.com/v4/spreadsheets/SHEET-ID/values/") and h1["Authorization"] == "Bearer TOKEN123"
    assert u2.endswith("/values:batchUpdate") and b2["valueInputOption"] == "RAW"
    assert b2["data"] == [{"range": "'Applications'!B2", "values": [["2026-10-17"]]}, {"range": "'Applications'!C2", "values": [[85.2]]}]
    assert u3.endswith(":append") and p3 == {"valueInputOption": "RAW", "insertDataOption": "INSERT_ROWS"} and b3 == {"values": [["x", 1]]}
    assert all("TOKEN123" not in str(c[1:2] + c[3:]) for c in http.calls)                   # never in a URL, a param or a body
    assert len(sleeps) == 2 and all(abs(s - 1.0) < 1e-9 for s in sleeps)                    # one request a second to the host


def test_a_google_refusal_is_one_line_without_the_token(settings):
    def refuse(method, url, headers=None, params=None, json=None):
        return Resp({"error": {"code": 403, "message": "Google Sheets API has not been used in project 1 before or it is disabled."}}, 403)
    sheet = tracker.SheetsClient(GoogleAPI(settings, request=refuse, token="TOKEN123", sleep=lambda s: None), "S")
    from careerops.googleapi import GoogleError
    with pytest.raises(GoogleError, match=r"HTTP 403: Google Sheets API has not been used") as e:
        sheet.get_values("'Applications'!1:1")
    assert "TOKEN123" not in str(e.value)


def test_not_signed_in_or_no_sheet_id_is_a_tracker_error_that_says_what_to_do(settings, db, applied):
    with pytest.raises(tracker.TrackerError, match="not signed in to Gmail.*--gmail-auth"):
        tracker.sync(db, settings, [applied["id"]])                                      # no token file in the tmp secrets folder
    settings.raw["tracker"]["sheet_id"] = ""
    assert not tracker.configured(settings)
    with pytest.raises(tracker.TrackerError, match="tracker.sheet_id is not set"):
        tracker.sync(db, settings, [applied["id"]])


def test_a_token_from_before_the_sheets_scope_is_refused_with_the_fix(settings, db, applied):
    gmail._write_token(settings, {"refresh_token": "rt", "access_token": "x", "expires_at": 9e12, "scope": gmail.SCOPE})
    with pytest.raises(tracker.TrackerError, match="lacks .*spreadsheets.*--gmail-auth"):
        tracker.sync(db, settings, [applied["id"]])


# ------------------------------------------------------------------ the commands
@pytest.fixture
def cfg(tmp_path, settings):
    p = tmp_path / "settings.yaml"
    p.write_text(yaml.safe_dump(settings.raw), encoding="utf-8")
    return p


def test_tracker_sync_dry_run_prints_the_rows_and_uses_no_network_or_token(cfg, settings, db, applied, capsys, monkeypatch):
    import requests
    monkeypatch.setattr(requests, "request", lambda *a, **k: pytest.fail("network"))
    cli.main(["--config", str(cfg), "tracker", "sync", "--dry-run"])
    out = capsys.readouterr().out
    assert "--- manual:northwind1" in out and "Company: Northwind Health AI" in out and "1 row(s) would be sent; nothing was." in out


def test_tracker_sync_through_the_cli_reports_added_then_unchanged(cfg, settings, db, applied, capsys, monkeypatch):
    sheet = sheet_with_headers()
    monkeypatch.setattr(tracker, "client", lambda s, api=None: (sheet, TAB))
    cli.main(["--config", str(cfg), "tracker", "sync"])
    assert "added: manual:northwind1" in capsys.readouterr().out and len(sheet.table()) == 1
    cli.main(["--config", str(cfg), "tracker", "sync", "--id", applied["id"]])
    assert "unchanged: manual:northwind1" in capsys.readouterr().out and len(sheet.table()) == 1


def test_tracker_sync_fails_with_a_message_when_not_signed_in(cfg, settings, db, applied, capsys):
    with pytest.raises(SystemExit) as e:
        cli.main(["--config", str(cfg), "tracker", "sync"])
    assert e.value.code == 1 and "not signed in to Gmail" in capsys.readouterr().err


def test_marking_a_job_applied_updates_the_tracker_and_a_tracker_failure_never_undoes_it(cfg, settings, db, job, tmp_path, capsys, monkeypatch):
    # the follow-ups of an application: the row is written, and when Google says no the job is still applied
    make_applied(settings, db, job, tmp_path)
    sheet = sheet_with_headers()
    monkeypatch.setattr(tracker, "client", lambda s, api=None: (sheet, TAB))
    cli._after_applied(db, settings, job["id"])
    assert "tracker: row added" in capsys.readouterr().out and len(sheet.table()) == 1
    monkeypatch.setattr(tracker, "client", lambda s, api=None: (_ for _ in ()).throw(tracker.TrackerError("Google said no")))
    cli._after_applied(db, settings, job["id"])
    assert "tracker: not updated (Google said no)" in capsys.readouterr().err
    assert db.job(job["id"])["status"] == "applied"


def test_the_mock_root_never_reaches_the_tracker_or_gmail(settings, db, job, tmp_path, monkeypatch):
    from careerops import mockroot
    make_applied(settings, db, job, tmp_path)
    mock = mockroot.mock_settings(settings)
    assert mockroot.is_mock(mock) and not mockroot.is_mock(settings)
    monkeypatch.setattr(tracker, "client", lambda *a, **k: pytest.fail("the mock root reached the tracker"))
    cli._after_applied(db, mock, job["id"])
