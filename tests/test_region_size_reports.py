"""v2.4 reporting: the tracker Sheet's Region and Size columns (appended on the right, nothing reordered), `careerops patterns` by region and size with reply rates, and the
`careerops doctor` lines (Companies House key, sources with no `regions:` tag, the share of the queue whose size is unknown). No network."""
import pytest
import yaml

from careerops import setup, tracker
from careerops.apply import decided
from careerops.reporting import patterns
from outreach_fakes import FakeSheet, make_applied
from triage_fakes import write_ad

TAB = "Applications"


# ------------------------------------------------------------------ the tracker
@pytest.fixture
def applied(settings, db, job, tmp_path):
    return make_applied(settings, db, job, tmp_path)


def test_region_and_size_are_the_last_two_columns_and_nothing_before_them_moved():
    assert tracker.HEADERS[-2:] == ["Region", "Size"] and tracker.HEADERS[:36][-1] == "Notes" and tracker.HEADERS[0] == "Job ID"
    assert len(tracker.HEADERS) == 38 and len(set(tracker.HEADERS)) == 38


def test_the_record_carries_the_region_and_the_size(settings, db, applied):
    rec = tracker.record(db, settings, db.job(applied["id"]))
    assert rec["Region"] == "uk" and rec["Size"] == "Unknown"                                  # Bristol, UK; no size recorded yet
    db.update_job(applied["id"], size="sme", size_label="small", size_evidence="override: x")
    assert tracker.record(db, settings, db.job(applied["id"]))["Size"] == "SME (small)"


def test_the_size_of_an_inbox_ad_scored_at_the_shortlist_reaches_the_record(settings, db, applied):
    j = db.job(applied["id"])
    db.conn.execute("INSERT INTO triage_scores (key, company, title, t, factors, scored_on, size, size_label, size_evidence) VALUES (?,?,?,?,?,?,?,?,?)",
                    ("|".join(decided.key_of(j["company"], j["title"])), j["company"], j["title"], 4.0, "{}", "2026-10-01", "large", "large", "ad: FTSE 100"))
    db.conn.commit()
    assert tracker.record(db, settings, db.job(applied["id"]))["Size"] == "Large"


def test_an_older_sheet_gets_the_two_columns_after_its_last_one_and_keeps_its_order(settings, db, applied):
    old = list(tracker.HEADERS[:36])
    sheet = FakeSheet([old])
    tracker.sync(db, settings, [applied["id"]], sheet=sheet, tab=TAB)
    assert sheet.rows[0] == old + ["Region", "Size"]
    row = dict(zip(sheet.rows[0], sheet.rows[1]))
    assert row["Job ID"] == applied["id"] and row["Region"] == "uk" and row["Size"] == "Unknown"


# ------------------------------------------------------------------ patterns
def seed(db, n, location, size, events=(), company=None):
    jid = f"manual:r{n}"
    db.upsert_job({"id": jid, "source": "manual", "company": company or f"Co{n}", "title": "Data Scientist", "location": location, "description": "A data role."})
    db.update_job(jid, status="applied", applied_at="2026-10-01T09:00:00+00:00", family="data_scientist", size=size)
    for ev in events:
        db.conn.execute("INSERT INTO outcomes (job_id, event, on_date, source) VALUES (?,?,?,?)", (jid, ev, "2026-10-05", "manual"))
    db.conn.commit()
    return jid


def test_the_report_breaks_down_by_region_and_size_with_the_reply_rate(db, settings):
    seed(db, 1, "London, UK", "sme", ["interview"])
    seed(db, 2, "Bristol, UK", "large")
    seed(db, 3, "Leeds, UK", "large", ["rejected"])
    seed(db, 4, "New York, NY", "large")
    seed(db, 5, "Dublin", "sme", ["acknowledged"])
    seed(db, 6, "Remote", None)
    text = "\n".join(patterns.report(patterns.rows_from_db(db), settings, "outcome log"))
    assert "By region:" in text and "By company size:" in text
    assert "  uk: 3 applied · 2 replies (67%) · 1 interviews · 1 rejections" in text
    assert "  ie: 1 applied · 1 replies (100%)" in text and "  us: 1 applied · 0 replies (0%)" in text and "  unknown: 1 applied · 0 replies (0%)" in text
    assert "  large: 3 applied · 1 replies (33%)" in text and "  sme: 2 applied · 2 replies (100%)" in text and "  unknown: 1 applied" in text


def test_the_sheets_own_region_and_size_cells_are_used_when_filled(db, settings):
    jid = seed(db, 1, "London, UK", None)
    header = list(tracker.HEADERS)
    row = [""] * len(header)
    for h, v in (("Job ID", jid), ("Status", "Interview"), ("Region", "ie"), ("Size", "SME (small)")):
        row[header.index(h)] = v
    rows = patterns.rows_from_sheet(FakeSheet([header, row]), TAB, db)
    assert (rows[0].region, rows[0].size) == ("ie", "sme")
    blank = [""] * len(header)
    blank[header.index("Job ID")], blank[header.index("Status")] = jid, "Applied"
    r = patterns.rows_from_sheet(FakeSheet([header, blank]), TAB, db)[0]
    assert (r.region, r.size) == ("uk", "unknown")                                            # no cell: worked out from the job


def test_a_sheet_without_the_new_columns_is_still_read(db):
    jid = seed(db, 1, "London, UK", "large")
    header = list(tracker.HEADERS[:36])
    row = [""] * 36
    row[header.index("Job ID")], row[header.index("Status")] = jid, "Applied"
    r = patterns.rows_from_sheet(FakeSheet([header, row]), TAB, db)[0]
    assert (r.region, r.size) == ("uk", "large")


# ------------------------------------------------------------------ doctor
def sources_file(settings, tmp_path, entries):
    p = tmp_path / "sources.yaml"
    p.write_text(yaml.safe_dump({"sources": entries}), encoding="utf-8")
    settings.raw["paths"]["sources"] = str(p)


SOURCES = [{"id": "seek-au", "name": "SEEK AU", "url": "https://www.seek.com.au", "country": "AU", "method": "gmail_alerts", "domains": ["seek.com.au"]},
           {"id": "reed-uk", "name": "Reed", "url": "https://www.reed.co.uk", "country": "GB", "method": "gmail_alerts", "domains": ["reed.co.uk"], "regions": ["uk"]}]


def checks_by_name(settings):
    return {name: (ok, hint) for name, ok, hint in setup.region_size_checks(settings)}


def test_doctor_says_whether_the_companies_house_key_is_there(settings, monkeypatch):
    monkeypatch.delenv("COMPANIES_HOUSE_API_KEY", raising=False)
    ok, hint = checks_by_name(settings)["Companies House key (COMPANIES_HOUSE_API_KEY) absent"]
    assert ok == setup.WARN and "COMPANIES_HOUSE_API_KEY" in hint                              # a warning: it never fails the doctor
    monkeypatch.setenv("COMPANIES_HOUSE_API_KEY", "k")
    assert checks_by_name(settings)["Companies House key (COMPANIES_HOUSE_API_KEY) present"][0] is True


def test_doctor_lists_the_sources_without_a_regions_tag_and_the_lines_to_add(settings, tmp_path):
    sources_file(settings, tmp_path, SOURCES)
    ok, hint = checks_by_name(settings)["1 source(s) in sources.yaml have no `regions:` tag (treated as [any])"]
    assert ok == setup.WARN and "seek-au: regions: [au]" in hint and "reed-uk" not in hint and "never edits sources.yaml" in hint


def test_doctor_is_quiet_about_regions_when_every_source_is_tagged_or_there_is_no_file(settings, tmp_path):
    assert not [n for n in checks_by_name(settings) if "regions:" in n]
    sources_file(settings, tmp_path, [SOURCES[1]])
    assert not [n for n in checks_by_name(settings) if "regions:" in n]


def test_doctor_reports_the_share_of_the_queue_with_an_unknown_size_without_writing_anything(settings, monkeypatch):
    monkeypatch.setenv("COMPANIES_HOUSE_API_KEY", "")
    inbox = settings.path("jds") / "inbox"
    for i, company in enumerate(("Acme", "Bigco", "Mystery", "Other")):
        write_ad(inbox, f"{i}.txt", company=company)
    settings.path("company_size_overrides").write_text("Acme: sme\nBigco: large\n", encoding="utf-8")
    got = checks_by_name(settings)
    name = "company size unknown for 2 of 4 queued ad(s) (50%)"
    assert got[name][0] is True                                                                 # not more than half: no warning
    write_ad(inbox, "9.txt", company="Third")
    assert any(n.startswith("company size unknown for 3 of 5") and ok == setup.WARN for n, (ok, _) in checks_by_name(settings).items())
    assert not settings.path("company_size").exists()                                           # read only: not even the cache is written


def test_doctor_has_no_size_line_for_an_empty_inbox(settings):
    assert not [n for n in checks_by_name(settings) if n.startswith("company size unknown")]


@pytest.mark.slow          # doctor looks for playwright's browser
def test_doctor_includes_these_checks_and_a_warning_does_not_fail_it(settings, monkeypatch):
    monkeypatch.setattr(setup, "region_size_checks", lambda s: [("a region note", setup.WARN, "do the thing")])
    assert ("a region note", setup.WARN, "do the thing") in setup.doctor(settings, ping=False)
