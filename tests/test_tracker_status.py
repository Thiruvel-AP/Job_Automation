"""tracker.set_status: the one write the reply watch makes to the Sheet, after a `y`. It finds the row by Job ID, writes the Status cell and nothing else."""
import pytest

from careerops import tracker
from outreach_fakes import FakeSheet

TAB = "Applications"


def sheet_with(*rows):
    return FakeSheet([list(tracker.HEADERS), *rows])


def row(jid, status="Applied", notes="my note"):
    return [jid if h == "Job ID" else status if h == "Status" else notes if h == "Notes" else "x" if h == "Company" else "" for h in tracker.HEADERS]


def test_the_status_cell_of_the_jobs_row_is_written_and_nothing_else():
    sheet = sheet_with(row("a:1"), row("b:2"))
    assert tracker.set_status(sheet, TAB, "b:2", "Rejected") == "updated"
    assert [r["Status"] for r in sheet.table()] == ["Applied", "Rejected"]
    assert len(sheet.writes) == 1 and list(sheet.writes[0]) == ["'Applications'!AH3"]
    assert sheet.table()[1]["Notes"] == "my note" and sheet.appends == []


def test_a_status_that_is_already_there_is_not_written_again():
    sheet = sheet_with(row("a:1", status="Rejected"))
    assert tracker.set_status(sheet, TAB, "a:1", "Rejected") == "unchanged"
    assert sheet.writes == []


def test_columns_are_found_by_their_header_text_not_their_position():
    headers = ["Notes", "Status", "Job ID"]
    sheet = FakeSheet([headers, ["", "Applied", "a:1"]])
    assert tracker.set_status(sheet, TAB, "a:1", "Interview") == "updated"
    assert sheet.rows[1][1] == "Interview" and list(sheet.writes[0]) == ["'Applications'!B2"]


def test_a_job_with_no_row_is_an_error_that_says_to_sync():
    sheet = sheet_with(row("a:1"))
    with pytest.raises(tracker.TrackerError, match="tracker sync"):
        tracker.set_status(sheet, TAB, "zzz:9", "Rejected")
    assert sheet.writes == [] and sheet.appends == []


def test_a_tab_without_the_columns_is_an_error_and_nothing_is_added():
    sheet = FakeSheet([["Company", "Role"], ["x", "y"]])
    with pytest.raises(tracker.TrackerError, match="Status"):
        tracker.set_status(sheet, TAB, "a:1", "Rejected")
    assert sheet.writes == [] and sheet.grown == []


def test_a_later_sync_leaves_the_status_the_reply_watch_set(settings, db, job, tmp_path):
    from outreach_fakes import make_applied

    applied = make_applied(settings, db, job, tmp_path)
    sheet = FakeSheet([list(tracker.HEADERS)])
    tracker.upsert(sheet, TAB, tracker.record(db, settings, db.job(applied["id"])))
    tracker.set_status(sheet, TAB, applied["id"], "Rejected")
    tracker.upsert(sheet, TAB, tracker.record(db, settings, db.job(applied["id"])))
    assert sheet.table()[0]["Status"] == "Rejected"
