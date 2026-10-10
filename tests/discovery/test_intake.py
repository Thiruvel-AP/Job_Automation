"""`careerops add-url` / `live --url`: one job ad from its link into data/jds/inbox/. Saved fixture responses stand in for every
HTTP reply (tests/fixtures/intake/); no test reaches the network."""
import json

import pytest

from careerops import cli
from careerops.discovery import intake, manual
from conftest import FIX

DIR = FIX / "intake"
GH_URL = "https://job-boards.greenhouse.io/monzo/jobs/4412345"
LEVER_URL = "https://jobs.lever.co/northwind/0f3b2c1d-5a6e-4b7f-8c9d-1e2f3a4b5c6d"
ASHBY_URL = "https://jobs.ashbyhq.com/synthesia/aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"


class Resp:
    def __init__(self, body: bytes | str, status: int = 200):
        self.content = body.encode() if isinstance(body, str) else body
        self.status_code = status
        self.text = self.content.decode("utf-8")
        self.ok = status < 400

    def json(self):
        return json.loads(self.text)


@pytest.fixture
def web(monkeypatch):
    """Replaces the one network call. `web.add(url_part, fixture_name_or_Resp)`; `web.urls` is every URL asked for, in order."""
    class Web:
        routes: dict = {}
        urls: list = []

        def add(self, part, reply, status=200):
            self.routes[part] = reply if isinstance(reply, Resp) else Resp((DIR / reply).read_bytes(), status)

    w = Web()
    w.routes, w.urls = {}, []

    def fake_get(url, **kw):
        w.urls.append(url)
        for part, reply in w.routes.items():
            if part in url:
                return reply
        raise AssertionError(f"unexpected request to {url}: add a route in the test")

    monkeypatch.setattr(intake, "_get", fake_get)
    monkeypatch.setattr(intake, "_sleep", lambda s: None)
    intake._last.clear()
    return w


# ------------------------------------------------------------------ url -> target
@pytest.mark.parametrize("url, kind, board, ad_id", [
    (GH_URL, "greenhouse", "monzo", "4412345"),
    ("https://boards.greenhouse.io/monzo/jobs/4412345?gh_src=x", "greenhouse", "monzo", "4412345"),
    ("https://boards.greenhouse.io/embed/job_app?for=monzo&token=4412345", "greenhouse", "monzo", "4412345"),
    (LEVER_URL, "lever", "northwind", "0f3b2c1d-5a6e-4b7f-8c9d-1e2f3a4b5c6d"),
    (LEVER_URL + "/apply", "lever", "northwind", "0f3b2c1d-5a6e-4b7f-8c9d-1e2f3a4b5c6d"),
    (ASHBY_URL, "ashby", "synthesia", "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"),
    ("https://example.com/careers/data-analyst", "page", "", None)])
def test_parse_url(url, kind, board, ad_id):
    t = intake.parse_url(url)
    assert (t.kind, t.board) == (kind, board) and (ad_id is None or t.ad_id == ad_id)


def test_the_eu_lever_host_uses_the_eu_api():
    assert intake.parse_url("https://jobs.eu.lever.co/acme/0f3b2c1d-5a6e-4b7f-8c9d-1e2f3a4b5c6d").api_host == "api.eu.lever.co"
    assert intake.parse_url(LEVER_URL).api_host == "api.lever.co"


@pytest.mark.parametrize("url", ["", "not a url", "ftp://example.com/job", "file:///etc/passwd",
                                 "https://job-boards.greenhouse.io/monzo", "https://jobs.lever.co/northwind/not-a-uuid",
                                 "https://jobs.ashbyhq.com/synthesia"])
def test_a_bad_link_is_refused_in_one_line(url):
    with pytest.raises(intake.IntakeError) as e:
        intake.parse_url(url)
    assert "\n" not in str(e.value)


# ------------------------------------------------------------------ Greenhouse
def test_greenhouse_uses_the_job_endpoint_and_unescapes_and_strips_the_content(web):
    web.add("boards-api.greenhouse.io", "greenhouse_job.json")
    ad = intake.fetch_ad(GH_URL)
    assert web.urls == ["https://boards-api.greenhouse.io/v1/boards/monzo/jobs/4412345"]
    assert (ad.company, ad.title, ad.location, ad.url) == ("Monzo Bank", "Data Scientist", "London, UK", GH_URL)
    assert "<" not in ad.text and "&amp;" not in ad.text and "&lt;" not in ad.text and "&#" not in ad.text
    assert "Experience with LangChain & vector search" in ad.text and "- Python, SQL and scikit-learn" in ad.text


def test_greenhouse_without_a_company_name_uses_the_board_slug(web):
    body = json.loads((DIR / "greenhouse_job.json").read_text(encoding="utf-8"))
    del body["company_name"]
    web.add("boards-api.greenhouse.io", Resp(json.dumps(body)))
    assert intake.fetch_ad(GH_URL).company == "Monzo"


def test_a_closed_posting_says_so(web):
    web.add("boards-api.greenhouse.io", Resp("{}", 404))
    with pytest.raises(intake.IntakeError, match="HTTP 404.*closed"):
        intake.fetch_ad(GH_URL)


def test_a_server_error_and_a_connection_error_are_one_line_errors(web, monkeypatch):
    web.add("boards-api.greenhouse.io", Resp("oops", 503))
    with pytest.raises(intake.IntakeError, match="HTTP 503"):
        intake.fetch_ad(GH_URL)
    import requests

    def boom(url, **kw):
        raise requests.ConnectionError("refused")
    monkeypatch.setattr(intake, "_get", boom)
    with pytest.raises(intake.IntakeError, match="could not be fetched.*ConnectionError"):
        intake.fetch_ad(GH_URL)


def test_an_ad_under_the_jd_minimum_is_refused(web):
    body = json.loads((DIR / "greenhouse_job.json").read_text(encoding="utf-8"))
    body["content"] = "&lt;p&gt;Short.&lt;/p&gt;"
    web.add("boards-api.greenhouse.io", Resp(json.dumps(body)))
    with pytest.raises(intake.IntakeError, match="a job description needs at least 500"):
        intake.fetch_ad(GH_URL)


# ------------------------------------------------------------------ Lever and Ashby
def test_lever_uses_the_single_posting_endpoint_and_joins_the_sections(web):
    web.add("api.lever.co", "lever_posting.json")
    ad = intake.fetch_ad(LEVER_URL)
    assert web.urls == ["https://api.lever.co/v0/postings/northwind/0f3b2c1d-5a6e-4b7f-8c9d-1e2f3a4b5c6d"]
    assert (ad.company, ad.title, ad.location, ad.url) == ("Northwind", "AI Engineer", "Bristol, UK", LEVER_URL)
    assert "What you will do" in ad.text and "- Build agents" in ad.text and "equal opportunity" in ad.text


def test_ashby_fetches_the_board_once_and_picks_the_posting_by_its_id(web):
    web.add("api.ashbyhq.com", "ashby_board.json")
    ad = intake.fetch_ad(ASHBY_URL)
    assert web.urls == ["https://api.ashbyhq.com/posting-api/job-board/synthesia"]
    assert (ad.company, ad.title, ad.location) == ("Synthesia", "Machine Learning Engineer", "London, UK")
    assert ad.url == ASHBY_URL and "machine learning models" in ad.text


def test_ashby_says_so_when_the_posting_is_not_in_the_board(web):
    web.add("api.ashbyhq.com", "ashby_board.json")
    with pytest.raises(intake.IntakeError, match="no posting 99999999-.*in its public list"):
        intake.fetch_ad("https://jobs.ashbyhq.com/synthesia/99999999-2222-3333-4444-555555555555")


# ------------------------------------------------------------------ any other page
def test_a_plain_page_gives_its_main_text_without_scripts_or_navigation(web):
    web.add("example.com", "page_plain.html")
    ad = intake.fetch_ad("https://example.com/careers/data-analyst")
    assert ad.source == "page" and len(ad.text) >= intake.MIN_PAGE_CHARS
    for noise in ("NAVIGATION-NOISE", "HEADER-NOISE", "FOOTER-NOISE", "tracking", "color: red"):
        assert noise not in ad.text
    assert ad.text.startswith("Data Analyst") and "Point 15." in ad.text
    assert ad.company == "Acme Analytics" and ad.title.startswith("Data Analyst")            # og:site_name, <title>


def test_a_page_with_json_ld_gives_the_posting_fields(web):
    web.add("example.com", "page_jsonld.html")
    ad = intake.fetch_ad("https://example.com/jobs/42")
    assert (ad.company, ad.title, ad.location) == ("Orbital Labs", "Applied Scientist", "Cambridge, GB")
    assert len(ad.text) >= intake.MIN_PAGE_CHARS and "<p>" not in ad.text and "Loading" not in ad.text


def test_a_page_under_2000_characters_is_refused_with_a_clear_message(web):
    web.add("example.com", "page_short.html")
    with pytest.raises(intake.IntakeError) as e:
        intake.fetch_ad("https://example.com/jobs/short")
    msg = str(e.value)
    assert "only" in msg and "characters of text found" in msg and "at least 2000" in msg and "by hand" in msg and "\n" not in msg


def test_a_response_over_the_size_limit_is_refused(web, monkeypatch):
    monkeypatch.setattr(intake, "MAX_BYTES", 100)
    web.add("example.com", "page_plain.html")
    with pytest.raises(intake.IntakeError, match="not a job ad"):
        intake.fetch_ad("https://example.com/x")


# ------------------------------------------------------------------ pacing: at most one request per second per host
def test_requests_to_one_host_are_a_second_apart(web, monkeypatch):
    now = [100.0]
    slept = []
    monkeypatch.setattr(intake, "_clock", lambda: now[0])
    monkeypatch.setattr(intake, "_sleep", lambda s: (slept.append(s), now.__setitem__(0, now[0] + s)))
    web.add("api.lever.co", "lever_posting.json")
    web.add("example.com", "page_plain.html")
    intake.fetch_ad(LEVER_URL)
    assert slept == []                                                         # the first request to a host waits for nothing
    now[0] += 0.25
    intake.fetch_ad(LEVER_URL)                                                 # same host 0.25 s later
    assert slept == [pytest.approx(0.75)]
    intake.fetch_ad("https://example.com/x")                                   # another host: no wait
    assert slept == [pytest.approx(0.75)]


def test_every_request_carries_the_shared_user_agent(monkeypatch):
    seen = {}

    class R(Resp):
        pass

    def fake(url, headers=None, timeout=None, **kw):
        seen.update(headers=headers, timeout=timeout)
        return R("{}")
    monkeypatch.setattr(intake.requests, "get", fake)
    monkeypatch.setattr(intake, "_sleep", lambda s: None)
    intake._get("https://example.com/x")
    from careerops.discovery.sources import UA
    assert seen["headers"] == UA and seen["timeout"]


# ------------------------------------------------------------------ the inbox file
def test_the_file_has_the_headers_the_live_loop_reads_and_survives_read_jd(web, settings):
    web.add("boards-api.greenhouse.io", "greenhouse_job.json")
    path, created, ad = intake.add_url(settings, GH_URL)
    assert created and path.parent == settings.path("jds") / "inbox" and path.name == "monzo-bank-data-scientist-4412345.txt"
    text, sha = manual.read_jd(path)                                            # >= 500 bytes, UTF-8
    assert manual.header_fields(text) == {"company": "Monzo Bank", "title": "Data Scientist", "url": GH_URL,
                                          "location": "London, UK"}
    assert text.split("\n\n", 1)[1].startswith("About the role")


def test_company_and_title_can_be_set_by_hand(web, settings):
    web.routes["example.com"] = Resp(b"<html><body><main><p>" + b"Build models with Python. " * 120 + b"</p></main></body></html>")
    with pytest.raises(intake.IntakeError, match="company could not be read.*--company and --title"):
        intake.add_url(settings, "https://example.com/j/1")                    # a page that names neither
    path, created, ad = intake.add_url(settings, "https://example.com/j/1", company="Acme Ltd", title="Data Scientist")
    assert created and manual.header_fields(path.read_text(encoding="utf-8"))["company"] == "Acme Ltd"


def test_an_existing_file_is_never_overwritten(web, settings):
    web.add("boards-api.greenhouse.io", "greenhouse_job.json")
    path, created, _ = intake.add_url(settings, GH_URL)
    path.write_text(path.read_text(encoding="utf-8") + "\nMY NOTE\n", encoding="utf-8")
    again, created2, _ = intake.add_url(settings, GH_URL)
    assert again == path and created and not created2 and "MY NOTE" in path.read_text(encoding="utf-8")


@pytest.mark.parametrize("field, value", [("company", "<company name>"), ("title", "AI > Engineer"), ("company", "o"),
                                          ("title", "x")])
def test_a_header_live_would_reject_is_not_written(web, settings, field, value):
    web.add("boards-api.greenhouse.io", "greenhouse_job.json")
    with pytest.raises(intake.IntakeError):
        intake.add_url(settings, GH_URL, **{field: value})
    inbox = settings.path("jds") / "inbox"
    assert not inbox.exists() or not list(inbox.iterdir())


def test_file_names_are_ascii_and_short():
    ad = intake.Ad("Zoë & Co. Ltd", "Senior/Staff Data Scientist (ML) — Platform " * 3, "u", "", "t", "page", "abc123")
    name = intake.file_name(ad)
    assert name.isascii() and name.endswith("-abc123.txt") and name.startswith("zoe-co-ltd-") and len(name) < 120
    assert "/" not in name and " " not in name


# ------------------------------------------------------------------ the commands
def _cfg(monkeypatch, settings):
    monkeypatch.setattr(cli, "load_settings", lambda path=None: settings)
    monkeypatch.setattr(cli, "load_dotenv", lambda p: None)


def test_add_url_command_writes_the_file_and_says_what_next(web, settings, monkeypatch, capsys):
    _cfg(monkeypatch, settings)
    web.add("api.lever.co", "lever_posting.json")
    cli.main(["add-url", LEVER_URL])
    out = capsys.readouterr().out
    assert "Wrote" in out and "northwind-ai-engineer-0f3b2c1d-5a6e-4b7f-8c9d-1e2f3a4b5c6d.txt" in out and "careerops live" in out
    cli.main(["add-url", LEVER_URL])
    assert "Already in the inbox, left as it is" in capsys.readouterr().out


def test_add_url_command_exits_1_with_the_message_on_a_refusal(web, settings, monkeypatch, capsys):
    _cfg(monkeypatch, settings)
    web.add("example.com", "page_short.html")
    with pytest.raises(SystemExit) as e:
        cli.main(["add-url", "https://example.com/jobs/short"])
    assert e.value.code == 1 and "characters of text found" in capsys.readouterr().err


def test_add_url_writes_nothing_when_the_fetch_fails(web, settings, monkeypatch, capsys):
    _cfg(monkeypatch, settings)
    web.add("api.lever.co", Resp("{}", 404))
    with pytest.raises(SystemExit):
        cli.main(["add-url", LEVER_URL])
    inbox = settings.path("jds") / "inbox"
    assert not inbox.exists() or not list(inbox.iterdir())


def test_live_url_is_refused_under_mock(settings, monkeypatch, capsys):
    _cfg(monkeypatch, settings)
    with pytest.raises(SystemExit) as e:
        cli.main(["live", "--mock", "--url", LEVER_URL])
    assert e.value.code == 1 and "never uses the network" in capsys.readouterr().err


def test_live_url_fetches_the_ad_then_works_on_that_one_file_only(web, settings, monkeypatch, capsys):
    _cfg(monkeypatch, settings)
    web.add("api.lever.co", "lever_posting.json")
    inbox = settings.path("jds") / "inbox"
    inbox.mkdir(parents=True)
    (inbox / "other.txt").write_text("another job waiting its turn", encoding="utf-8")
    seen = {}

    class FakeLive:
        def __init__(self, db, s, hooks, **kw):
            seen.update(kw)

        def run(self):
            return 0
    monkeypatch.setattr("careerops.apply.live.Live", FakeLive)
    cli.main(["live", "--url", LEVER_URL, "--max", "3"])
    assert seen["only"] == inbox / "northwind-ai-engineer-0f3b2c1d-5a6e-4b7f-8c9d-1e2f3a4b5c6d.txt" and seen["only"].exists()
    assert seen["resume_cmd"] == f"careerops live --max 3 --url {LEVER_URL}"           # a rerun finds the same file, never refetches
    assert "Fetched northwind-ai-engineer" in capsys.readouterr().out
    assert (inbox / "other.txt").exists()


def test_live_without_url_still_looks_at_the_whole_inbox(settings, monkeypatch):
    _cfg(monkeypatch, settings)
    seen = {}

    class FakeLive:
        def __init__(self, db, s, hooks, **kw):
            seen.update(kw)

        def run(self):
            return 0
    monkeypatch.setattr("careerops.apply.live.Live", FakeLive)
    cli.main(["live"])
    assert seen["only"] is None


# ------------------------------------------------------------------ 401 / 403 / 429: no retry, no workaround
BLOCKED = "This site blocks automated access — copy the job text and use --clipboard"


@pytest.mark.parametrize("status", [401, 403, 429])
@pytest.mark.parametrize("url, host", [(GH_URL, "boards-api.greenhouse.io"), ("https://example.com/jobs/ml", "example.com")])
def test_a_blocked_site_is_asked_once_and_the_message_points_to_the_clipboard(web, status, url, host):
    web.add(host, Resp("denied", status))
    with pytest.raises(intake.BlockedError) as e:
        intake.fetch_ad(url)
    assert str(e.value) == BLOCKED and isinstance(e.value, intake.IntakeError)
    assert len(web.urls) == 1                                          # no retry, no second route to the same page


@pytest.mark.parametrize("argv", [["add-url"], ["live", "--url"]])
@pytest.mark.parametrize("status", [401, 403, 429])
def test_the_commands_exit_non_zero_with_the_message_and_write_nothing(web, settings, monkeypatch, capsys, argv, status):
    _cfg(monkeypatch, settings)
    web.add("example.com", Resp("denied", status))
    with pytest.raises(SystemExit) as e:
        cli.main([*argv, "https://example.com/jobs/ml"])
    assert e.value.code == 1 and BLOCKED in capsys.readouterr().err and len(web.urls) == 1
    inbox = settings.path("jds") / "inbox"
    assert not inbox.exists() or not list(inbox.iterdir())


def test_other_http_errors_keep_their_own_messages(web):
    web.add("boards-api.greenhouse.io", Resp("oops", 500))
    with pytest.raises(intake.IntakeError, match="HTTP 500") as e:
        intake.fetch_ad(GH_URL)
    assert not isinstance(e.value, intake.BlockedError)


# ------------------------------------------------------------------ the Windows clipboard
JD_TEXT = ("About the role\r\nWe build forecasting models for energy networks in Bristol, £50k–£60k.\r\n"
           + "You will own pipelines, evaluation and deployment of machine learning services. " * 30)
ANSWERS = ("Northwind Energy", "Data Scientist", "https://careers.example.com/jobs/12")


@pytest.fixture
def clip(monkeypatch):
    """`clip.set(bytes)` is what powershell.exe's Get-Clipboard would print."""
    class Clip:
        data = b""

        def set(self, data):
            self.data = data if isinstance(data, bytes) else data.encode("utf-8")
    c = Clip()
    monkeypatch.setattr(intake, "_run_clipboard", lambda: c.data)
    return c


def _answers(monkeypatch, *answers):
    it, seen = iter(answers), []
    monkeypatch.setattr("builtins.input", lambda prompt="": (seen.append(prompt), next(it))[1])
    return seen


def test_the_clipboard_is_read_with_the_agreed_powershell_command(monkeypatch):
    calls = []

    class Done:
        returncode, stdout = 0, b"ok"
    monkeypatch.setattr(intake.subprocess, "run", lambda cmd, **kw: (calls.append(cmd), Done())[1])
    assert intake._run_clipboard() == b"ok"
    assert calls[0][0] == "powershell.exe" and calls[0][-1] == "[Console]::OutputEncoding=[Text.Encoding]::UTF8; Get-Clipboard"


def test_read_clipboard_strips_carriage_returns_and_a_bom_and_keeps_utf8(clip):
    clip.set(b"\xef\xbb\xbf" + JD_TEXT.encode("utf-8") + b"\r\n")
    text = intake.read_clipboard()
    assert "\r" not in text and "£50k–£60k" in text and not text.startswith(chr(0xFEFF)) and text == text.strip()


def test_a_clipboard_under_2000_characters_is_refused(clip):
    clip.set("x" * 1999)
    with pytest.raises(intake.IntakeError, match="1999 characters.*at least 2000"):
        intake.read_clipboard()
    clip.set("x" * 2000)
    assert len(intake.read_clipboard()) == 2000


def test_a_missing_powershell_is_a_one_line_error(monkeypatch):
    def gone(*a, **kw):
        raise FileNotFoundError("powershell.exe")
    monkeypatch.setattr(intake.subprocess, "run", gone)
    with pytest.raises(intake.IntakeError, match="powershell.exe was not found"):
        intake._run_clipboard()


def test_add_clipboard_writes_company_title_txt_that_live_accepts(clip, settings, monkeypatch):
    clip.set(JD_TEXT)
    seen = _answers(monkeypatch, *ANSWERS)
    path, created, ad = intake.add_clipboard(settings, input)
    assert created and seen == ["Company: ", "Job title: ", "Application URL: "]
    assert path == settings.path("jds") / "inbox" / "northwind-energy-data-scientist.txt"
    text, _ = manual.read_jd(path)
    assert manual.header_fields(text) == {"company": "Northwind Energy", "title": "Data Scientist",
                                          "url": "https://careers.example.com/jobs/12"}
    assert "£50k–£60k" in text and "\r" not in text


@pytest.mark.parametrize("bad, problem", [("o", "shorter than"), ("<company>", "placeholder"), ("", "is blank")])
def test_a_bad_company_is_asked_again_like_live_does(clip, settings, monkeypatch, capsys, bad, problem):
    clip.set(JD_TEXT)
    _answers(monkeypatch, bad, *ANSWERS)
    _, _, ad = intake.add_clipboard(settings, input)
    assert ad.company == "Northwind Energy" and problem in capsys.readouterr().out


def test_a_url_that_is_not_a_web_link_is_asked_again(clip, settings, monkeypatch, capsys):
    clip.set(JD_TEXT)
    _answers(monkeypatch, "Northwind Energy", "Data Scientist", "careers page", "<url>", "https://careers.example.com/jobs/12")
    _, _, ad = intake.add_clipboard(settings, input)
    assert ad.url == "https://careers.example.com/jobs/12" and "must start with http" in capsys.readouterr().out


def test_three_bad_answers_write_nothing(clip, settings, monkeypatch):
    clip.set(JD_TEXT)
    _answers(monkeypatch, "Northwind Energy", "x", "y", "z")
    with pytest.raises(intake.IntakeError, match="no usable title"):
        intake.add_clipboard(settings, input)
    inbox = settings.path("jds") / "inbox"
    assert not inbox.exists() or not list(inbox.iterdir())


def test_an_existing_clipboard_file_is_left_as_it_is(clip, settings, monkeypatch):
    clip.set(JD_TEXT)
    _answers(monkeypatch, *ANSWERS)
    path, created, _ = intake.add_clipboard(settings, input)
    before = path.read_bytes()
    clip.set(JD_TEXT + "\nA different ad.")
    _answers(monkeypatch, *ANSWERS)
    again, created2, _ = intake.add_clipboard(settings, input)
    assert created and not created2 and again == path and path.read_bytes() == before


def test_live_clipboard_saves_the_file_then_works_on_that_one_file_only(clip, settings, monkeypatch, capsys):
    _cfg(monkeypatch, settings)
    clip.set(JD_TEXT)
    _answers(monkeypatch, *ANSWERS)
    inbox = settings.path("jds") / "inbox"
    inbox.mkdir(parents=True)
    (inbox / "other.txt").write_text("another job waiting its turn", encoding="utf-8")
    seen = {}

    class FakeLive:
        def __init__(self, db, s, hooks, **kw):
            seen.update(kw)

        def run(self):
            return 0
    monkeypatch.setattr("careerops.apply.live.Live", FakeLive)
    cli.main(["live", "--clipboard", "--max", "2"])
    assert seen["only"] == inbox / "northwind-energy-data-scientist.txt" and seen["only"].exists()
    assert seen["resume_cmd"] == "careerops live --max 2"               # the rerun must not read the clipboard again
    assert "Saved northwind-energy-data-scientist.txt" in capsys.readouterr().out
    assert (inbox / "other.txt").exists()


def test_live_clipboard_exits_1_with_the_reason_and_runs_nothing(clip, settings, monkeypatch, capsys):
    _cfg(monkeypatch, settings)
    clip.set("too short")
    monkeypatch.setattr("careerops.apply.live.Live", lambda *a, **k: pytest.fail("the loop must not start"))
    with pytest.raises(SystemExit) as e:
        cli.main(["live", "--clipboard"])
    assert e.value.code == 1 and "at least 2000" in capsys.readouterr().err


def test_live_refuses_url_together_with_clipboard(settings, monkeypatch, capsys):
    _cfg(monkeypatch, settings)
    with pytest.raises(SystemExit) as e:
        cli.main(["live", "--url", LEVER_URL, "--clipboard"])
    assert e.value.code == 1 and "use one of them" in capsys.readouterr().err
