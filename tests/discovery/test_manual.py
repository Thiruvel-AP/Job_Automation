"""JD files and hand-added jobs: validation, the internal add_jd, per-job triage. `add-jd --file` shares the validator."""
import pytest
import yaml

from careerops import cli
from careerops.discovery import manual

BODY = ("We are looking for an AI Engineer to build LLM agents in Python on AWS. " * 12)      # well over 500 bytes


def _write(p, data):
    p.write_bytes(data)
    return p


def _folder(d):
    (d / "folder.txt").mkdir()
    return d / "folder.txt"


def test_a_good_file_is_read_with_its_hash(tmp_path):
    f = tmp_path / "jd.txt"
    f.write_text(BODY, encoding="utf-8")
    text, sha = manual.read_jd(f)
    assert text == BODY and len(sha) == 64
    assert manual.read_jd(f)[1] == sha                                          # same bytes, same hash


def test_a_utf8_bom_is_dropped_not_rejected(tmp_path):
    f = tmp_path / "jd.txt"
    f.write_bytes(b"\xef\xbb\xbf" + BODY.encode())
    assert manual.read_jd(f)[0] == BODY


@pytest.mark.parametrize("make, words", [
    (lambda d: d / "nope.txt", "no such file"),
    (lambda d: _write(d / "empty.txt", b""), "0 bytes"),
    (lambda d: _write(d / "tiny.txt", b"x" * 499), "499 bytes"),
    (lambda d: _write(d / "latin1.txt", b"caf\xe9 " * 200), "not UTF-8"),
    (_folder, "folder")])
def test_a_bad_file_is_one_line_error_not_a_traceback(tmp_path, make, words):
    with pytest.raises(manual.JDError) as e:
        manual.read_jd(make(tmp_path))
    assert words in str(e.value) and "\n" not in str(e.value)


def test_exactly_500_bytes_is_enough(tmp_path):
    f = _write(tmp_path / "ok.txt", b"a" * 500)
    assert len(manual.read_jd(f)[0]) == 500


def _cfg(settings, tmp_path):
    cfg = tmp_path / "settings.yaml"
    cfg.write_text(yaml.safe_dump(settings.raw), encoding="utf-8")
    return str(cfg)


@pytest.mark.parametrize("setup, words", [("missing", "no such file"), ("tiny", "bytes"), ("binary", "not UTF-8")])
def test_add_jd_file_prints_one_error_line_and_exits_1(settings, tmp_path, capsys, setup, words):
    f = tmp_path / "jd.txt"
    if setup == "tiny":
        f.write_text("short")
    elif setup == "binary":
        f.write_bytes(b"\xff\xfe" * 400)
    with pytest.raises(SystemExit) as e:
        cli.main(["--config", _cfg(settings, tmp_path), "add-jd", "--company", "Acme", "--title", "AI Engineer",
                  "--file", str(f)])
    err = capsys.readouterr().err
    assert e.value.code == 1 and err.startswith("ERROR: ") and words in err and "Traceback" not in err
    assert err.count("\n") == 1


def test_add_jd_without_company_and_title_is_an_error_not_a_crash(settings, tmp_path, capsys):
    f = tmp_path / "jd.txt"
    f.write_text(BODY)
    with pytest.raises(SystemExit):
        cli.main(["--config", _cfg(settings, tmp_path), "add-jd", "--file", str(f)])
    assert "--company and --title" in capsys.readouterr().err


def test_add_jd_file_still_adds_the_job(settings, tmp_path, capsys):
    f = tmp_path / "jd.txt"
    f.write_text(BODY)
    cli.main(["--config", _cfg(settings, tmp_path), "add-jd", "--company", "Acme", "--title", "AI Engineer",
              "--location", "Bristol, UK", "--file", str(f)])
    out = capsys.readouterr().out
    assert "Added manual:" in out and "Triage:" in out


def test_header_lines_are_read_from_the_top_of_the_file():
    text = "Company: Acme Ltd\ntitle:  AI Engineer \nURL: https://acme.example/jobs/1\nLocation: Bristol, UK\n\nAbout us ..."
    assert manual.header_fields(text) == {"company": "Acme Ltd", "title": "AI Engineer",
                                          "url": "https://acme.example/jobs/1", "location": "Bristol, UK"}
    assert manual.header_fields("About us: we build things\nWe hire.") == {}
    assert manual.header_fields("\n" * 12 + "Company: too far down") == {}


def test_add_jd_returns_the_id_and_triage_job_triages_only_that_job(settings, db, jd):
    other = manual.add_jd(db, jd, company="Other Co", title="AI Engineer", url="https://o.example", location="Bristol, UK")
    jid = manual.add_jd(db, jd, company="Acme", title="AI Engineer", url="https://a.example", location="Bristol, UK")
    assert jid == manual.job_id_for("Acme", "AI Engineer", "https://a.example") and db.job(jid)["status"] == "new"
    assert manual.triage_job(db, settings, jid) == "triaged"
    assert db.job(jid)["family"] == "ai_engineer" and db.job(other)["status"] == "new"      # the other one was not triaged


def test_a_rules_skip_is_reported_with_its_reason(settings, db, jd):
    jid = manual.add_jd(db, jd, company="Acme", title="Plumber", url=None, location="Bristol, UK")
    assert manual.triage_job(db, settings, jid) == "skipped" and "target families" in db.job(jid)["reason"]


def test_adding_a_jd_never_resets_a_job_that_has_moved_on(settings, db, jd):
    jid = manual.add_jd(db, jd, company="Acme", title="AI Engineer", url="u", location="Bristol, UK")
    for status in ("needs_review", "approved", "applied", "letter_pending", "ready_not_submitted"):
        db.update_job(jid, status=status)
        with pytest.raises(manual.JobExists):
            manual.add_jd(db, jd, company="Acme", title="AI Engineer", url="u")
        assert db.job(jid)["status"] == status
    db.update_job(jid, status="triaged")
    assert manual.add_jd(db, jd + " v2", company="Acme", title="AI Engineer", url="u") == jid       # replaceable
    assert db.job(jid)["status"] == "new" and db.job(jid)["description"].endswith(" v2")


# ------------------------------------------------------------------ meta_problem: placeholders and stray keystrokes
@pytest.mark.parametrize("field, value", [("company", "<company name>"), ("title", "AI Engineer <remote>"), ("url", "https://x/<id>"),
                                          ("location", "London >"), ("company", "o"), ("title", " x "), ("company", ""),
                                          ("company", "  ")])
def test_meta_problem_rejects_placeholders_and_names_under_two_characters(field, value):
    assert manual.meta_problem(field, value)


@pytest.mark.parametrize("field, value", [("company", "Ab"), ("title", "AI Engineer"), ("url", "https://acme.example/jobs/1"),
                                          ("location", "London, UK"), ("url", None), ("location", "R")])
def test_meta_problem_accepts_real_values(field, value):
    assert manual.meta_problem(field, value) is None


def test_add_jd_refuses_a_placeholder_company_and_a_one_letter_title(db):
    for kw in ({"company": "<company>", "title": "AI Engineer"}, {"company": "Acme", "title": "o"},
               {"company": "Acme", "title": "AI Engineer", "url": "<url>"}):
        with pytest.raises(manual.JDError):
            manual.add_jd(db, BODY, **kw)
    assert db.jobs() == []


def test_add_jd_command_prints_the_error_and_adds_nothing(db, settings, monkeypatch, capsys, tmp_path):
    f = tmp_path / "jd.txt"
    f.write_text(BODY, encoding="utf-8")
    monkeypatch.setattr(cli, "load_settings", lambda path=None: settings)
    monkeypatch.setattr(cli, "load_dotenv", lambda p: None)
    with pytest.raises(SystemExit) as e:
        cli.main(["add-jd", "--file", str(f), "--company", "o", "--title", "AI Engineer"])
    assert e.value.code == 1 and "company 'o' is shorter than 2 characters" in capsys.readouterr().err and db.jobs() == []
