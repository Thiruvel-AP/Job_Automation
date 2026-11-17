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
        urls: list = []                 # every URL asked for except a robots.txt
        robots_urls: list = []          # every robots.txt asked for
        robots_routes: dict = {}

        def add(self, part, reply, status=200):
            self.routes[part] = reply if isinstance(reply, Resp) else Resp((DIR / reply).read_bytes(), status)

        def robots(self, host_part, text="", status=200):
            """What the host's robots.txt says (default: it has none: 404, everything allowed)."""
            self.robots_routes[host_part] = Resp(text, status)

    w = Web()
    w.routes, w.urls, w.robots_urls, w.robots_routes = {}, [], [], {}

    def fake_get(url, **kw):
        if url.endswith("/robots.txt"):
            w.robots_urls.append(url)
            return next((r for part, r in w.robots_routes.items() if part in url), Resp("", 404))
        w.urls.append(url)
        for part, reply in w.routes.items():
            if part in url:
                return reply
        raise AssertionError(f"unexpected request to {url}: add a route in the test")

    monkeypatch.setattr(intake, "_get", fake_get)
    monkeypatch.setattr(intake, "_sleep", lambda s: None)
    intake._last.clear()
    intake._default_robots.memory.clear()
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
    assert slept == [pytest.approx(1.0)]                       # robots.txt first (no wait), then the posting a second later
    now[0] += 0.25
    intake.fetch_ad(LEVER_URL)                                                 # same host 0.25 s later; robots.txt is cached
    assert slept == [pytest.approx(1.0), pytest.approx(0.75)]
    intake.fetch_ad("https://example.com/x")                                   # another host: its robots.txt, then the page
    assert slept == [pytest.approx(1.0), pytest.approx(0.75), pytest.approx(1.0)]


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
                                          "source": "greenhouse", "location": "London, UK"}
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
GO = ("", "y")                      # "Copy the job ad now, then press Enter" and "Use this? [y/N]"


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
    seen = _answers(monkeypatch, *GO, *ANSWERS)
    path, created, ad = intake.add_clipboard(settings, input)
    assert created and seen == ["", "Use this? [y/N] ", "Company: ", "Job title: ", "Application URL: "]
    assert path == settings.path("jds") / "inbox" / "northwind-energy-data-scientist.txt"
    text, _ = manual.read_jd(path)
    assert manual.header_fields(text) == {"company": "Northwind Energy", "title": "Data Scientist",
                                          "url": "https://careers.example.com/jobs/12", "source": "clipboard"}
    assert "£50k–£60k" in text and "\r" not in text


@pytest.mark.parametrize("bad, problem", [("o", "shorter than"), ("<company>", "placeholder"), ("", "is blank")])
def test_a_bad_company_is_asked_again_like_live_does(clip, settings, monkeypatch, capsys, bad, problem):
    clip.set(JD_TEXT)
    _answers(monkeypatch, *GO, bad, *ANSWERS)
    _, _, ad = intake.add_clipboard(settings, input)
    assert ad.company == "Northwind Energy" and problem in capsys.readouterr().out


def test_a_url_that_is_not_a_web_link_is_asked_again(clip, settings, monkeypatch, capsys):
    clip.set(JD_TEXT)
    _answers(monkeypatch, *GO, "Northwind Energy", "Data Scientist", "careers page", "<url>", "https://careers.example.com/jobs/12")
    _, _, ad = intake.add_clipboard(settings, input)
    assert ad.url == "https://careers.example.com/jobs/12" and "must start with http" in capsys.readouterr().out


def test_three_bad_answers_write_nothing(clip, settings, monkeypatch):
    clip.set(JD_TEXT)
    _answers(monkeypatch, *GO, "Northwind Energy", "x", "y", "z")
    with pytest.raises(intake.IntakeError, match="no usable title"):
        intake.add_clipboard(settings, input)
    inbox = settings.path("jds") / "inbox"
    assert not inbox.exists() or not list(inbox.iterdir())


def test_an_existing_clipboard_file_is_left_as_it_is(clip, settings, monkeypatch):
    clip.set(JD_TEXT)
    _answers(monkeypatch, *GO, *ANSWERS)
    path, created, _ = intake.add_clipboard(settings, input)
    before = path.read_bytes()
    clip.set(JD_TEXT + "\nA different ad.")
    _answers(monkeypatch, *GO, *ANSWERS)
    again, created2, _ = intake.add_clipboard(settings, input)
    assert created and not created2 and again == path and path.read_bytes() == before


def test_live_clipboard_saves_the_file_then_works_on_that_one_file_only(clip, settings, monkeypatch, capsys):
    _cfg(monkeypatch, settings)
    clip.set(JD_TEXT)
    _answers(monkeypatch, *GO, *ANSWERS)
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
    _answers(monkeypatch, "")
    monkeypatch.setattr("careerops.apply.live.Live", lambda *a, **k: pytest.fail("the loop must not start"))
    with pytest.raises(SystemExit) as e:
        cli.main(["live", "--clipboard"])
    assert e.value.code == 1 and "at least 2000" in capsys.readouterr().err


def test_live_refuses_url_together_with_clipboard(settings, monkeypatch, capsys):
    _cfg(monkeypatch, settings)
    with pytest.raises(SystemExit) as e:
        cli.main(["live", "--url", LEVER_URL, "--clipboard"])
    assert e.value.code == 1 and "use one of them" in capsys.readouterr().err


# ------------------------------------------------------------------ B1: wait, preview, confirm; refuse a pasted command
def test_it_says_to_copy_the_ad_waits_for_enter_then_shows_the_size_and_three_lines(clip, settings, monkeypatch, capsys):
    clip.set(JD_TEXT)
    seen = _answers(monkeypatch, *GO, *ANSWERS)
    intake.add_clipboard(settings, input)
    out = capsys.readouterr().out
    assert out.index("Copy the job ad now, then press Enter") < out.index("characters. First lines:")
    assert f"{len(JD_TEXT.replace(chr(13), '').strip()):,} characters. First lines:" in out
    lines = out.split("First lines:\n", 1)[1].splitlines()[:3]
    assert lines[0].strip() == "About the role" and lines[1].strip().startswith("We build forecasting models")
    assert lines[2].strip().startswith("You will own pipelines")                 # the third non-empty line
    assert seen[0] == "" and seen[1] == "Use this? [y/N] "


def test_the_clipboard_is_read_after_enter_not_before(clip, settings, monkeypatch):
    """The ad copied while the prompt is showing is the one used: the clipboard is read only once Enter has been pressed."""
    clip.set("an older clipboard that is far too short")
    it = iter([*GO, *ANSWERS])

    def answer(prompt=""):
        if prompt == "":
            clip.set(JD_TEXT)                                                   # the user copies the ad, then presses Enter
        return next(it)
    monkeypatch.setattr("builtins.input", answer)
    _, created, ad = intake.add_clipboard(settings, input)
    assert created and ad.text.startswith("About the role")


@pytest.mark.parametrize("answer", ["", "n", "N", "no", "maybe", "  "])
def test_anything_but_y_at_use_this_writes_nothing(clip, settings, monkeypatch, answer):
    clip.set(JD_TEXT)
    seen = _answers(monkeypatch, "", answer)
    with pytest.raises(intake.IntakeError, match="not used; nothing was written"):
        intake.add_clipboard(settings, input)
    assert seen == ["", "Use this? [y/N] "]                                    # company / title / url were never asked
    inbox = settings.path("jds") / "inbox"
    assert not inbox.exists() or not list(inbox.iterdir())


@pytest.mark.parametrize("answer", ["y", "Y", " y "])
def test_y_goes_on(clip, settings, monkeypatch, answer):
    clip.set(JD_TEXT)
    _answers(monkeypatch, "", answer, *ANSWERS)
    assert intake.add_clipboard(settings, input)[1] is True


def test_under_2000_characters_is_refused_after_enter_before_any_question(clip, settings, monkeypatch):
    clip.set("x" * 1999)
    seen = _answers(monkeypatch, "")
    with pytest.raises(intake.IntakeError, match="1999 characters.*at least 2000"):
        intake.add_clipboard(settings, input)
    assert seen == [""]


@pytest.mark.parametrize("start", ["careerops live --clipboard", "careerops live", "powershell.exe -NoProfile -Command Get-Clipboard",
                                   "PowerShell Get-Clipboard", "explorer.exe .", "Explorer ."])
@pytest.mark.parametrize("prefix", ["", "$ ", "> ", "PS D:\\VS_Code> "])
def test_text_that_starts_like_a_shell_command_is_refused(clip, settings, monkeypatch, start, prefix):
    clip.set(prefix + start + "\n" + "x" * 2100)
    _answers(monkeypatch, "")
    with pytest.raises(intake.IntakeError, match="looks like a shell command"):
        intake.add_clipboard(settings, input)


@pytest.mark.parametrize("start", ["cd /mnt/d/VS_Code", "git status", "source .venv/bin/activate", "cd ..", "git"])
def test_cd_git_and_source_in_lowercase_are_refused_too(clip, start):
    clip.set(start + "\n" + "x" * 2100)
    with pytest.raises(intake.IntakeError, match=f"starts with '{start.split()[0]}'"):
        intake.read_clipboard()


@pytest.mark.parametrize("start", ["Source Digital is hiring a Data Scientist", "Git is a tool we use daily; we need a Data Scientist",
                                   "Cd Labs: Data Scientist", "Careers at Acme", "Exploring data is what you will do"])
def test_an_ad_that_begins_with_an_ordinary_word_is_not_taken_for_a_command(clip, start):
    clip.set(start + "\n" + "You will build models in Python. " * 80)
    assert intake.read_clipboard().startswith(start)


def test_live_clipboard_refuses_a_pasted_command_and_runs_nothing(clip, settings, monkeypatch, capsys):
    _cfg(monkeypatch, settings)
    clip.set("careerops live --clipboard\n" + "x" * 2100)
    _answers(monkeypatch, "")
    monkeypatch.setattr("careerops.apply.live.Live", lambda *a, **k: pytest.fail("the loop must not start"))
    with pytest.raises(SystemExit) as e:
        cli.main(["live", "--clipboard"])
    assert e.value.code == 1 and "looks like a shell command" in capsys.readouterr().err


def test_live_clipboard_declined_runs_nothing(clip, settings, monkeypatch, capsys):
    _cfg(monkeypatch, settings)
    clip.set(JD_TEXT)
    _answers(monkeypatch, "", "n")
    monkeypatch.setattr("careerops.apply.live.Live", lambda *a, **k: pytest.fail("the loop must not start"))
    with pytest.raises(SystemExit) as e:
        cli.main(["live", "--clipboard"])
    assert e.value.code == 1 and "not used" in capsys.readouterr().err


# ------------------------------------------------------------------ B2: company and title answers
@pytest.mark.parametrize("bad", ["o", " x ", "<company>", "Acme <Ltd>", "Acme >", "<", ">", ""])
def test_a_company_that_is_short_or_a_placeholder_is_asked_again(clip, settings, monkeypatch, capsys, bad):
    clip.set(JD_TEXT)
    _answers(monkeypatch, *GO, bad, *ANSWERS)
    assert intake.add_clipboard(settings, input)[2].company == "Northwind Energy"
    out = capsys.readouterr().out
    assert "try again" in out and ("is blank" in out or "shorter than 2 characters" in out or "< or >" in out)


@pytest.mark.parametrize("bad", ["o", "<job title>", "AI > ML", "", "  "])
def test_a_title_that_is_short_or_a_placeholder_is_asked_again(clip, settings, monkeypatch, capsys, bad):
    clip.set(JD_TEXT)
    _answers(monkeypatch, *GO, "Northwind Energy", bad, "Data Scientist", ANSWERS[2])
    assert intake.add_clipboard(settings, input)[2].title == "Data Scientist"
    assert "try again" in capsys.readouterr().out


def test_two_characters_is_enough_for_a_name(clip, settings, monkeypatch):
    clip.set(JD_TEXT)
    _answers(monkeypatch, *GO, "BP", "ML", ANSWERS[2])
    ad = intake.add_clipboard(settings, input)[2]
    assert (ad.company, ad.title) == ("BP", "ML")


# ------------------------------------------------------------------ C1: robots.txt before every fetch
def test_robots_txt_is_read_before_the_page_and_a_disallowed_path_is_refused_without_fetching_it(web):
    web.robots("example.com", "User-agent: *\nDisallow: /private/\n")
    web.add("example.com", "page_plain.html")
    with pytest.raises(intake.RobotsRefused, match="example.com/robots.txt disallows /private/job/1.*--clipboard"):
        intake.fetch_ad("https://example.com/private/job/1")
    assert web.robots_urls == ["https://example.com/robots.txt"] and web.urls == []         # the page itself was never asked for


def test_an_allowed_path_is_fetched_and_robots_txt_is_read_only_once_per_host(web):
    web.robots("example.com", "User-agent: *\nDisallow: /private/\n")
    web.add("example.com", "page_plain.html")
    intake.fetch_ad("https://example.com/careers/a")
    intake.fetch_ad("https://example.com/careers/b")
    assert web.robots_urls == ["https://example.com/robots.txt"] and len(web.urls) == 2


def test_the_documented_apis_are_checked_too(web):
    web.robots("boards-api.greenhouse.io", "User-agent: *\nDisallow: /embed/\n")
    web.add("boards-api.greenhouse.io", "greenhouse_job.json")
    assert intake.fetch_ad(GH_URL).title == "Data Scientist"
    assert web.robots_urls == ["https://boards-api.greenhouse.io/robots.txt"]
    web.robots("api.lever.co", "User-agent: *\nDisallow: /v0/\n")
    web.add("api.lever.co", "lever_posting.json")
    with pytest.raises(intake.RobotsRefused, match="disallows /v0/postings"):
        intake.fetch_ad(LEVER_URL)


@pytest.mark.parametrize("status", [401, 403, 429, 500])
def test_a_robots_txt_that_cannot_be_read_refuses_a_page_host_and_the_page_is_not_asked_for(web, status):
    web.robots("example.com", "denied", status)
    web.add("example.com", "page_plain.html")
    with pytest.raises(intake.RobotsRefused, match="cannot be checked.*--clipboard"):
        intake.fetch_ad("https://example.com/careers/a")
    assert web.urls == []


def test_the_robots_cache_is_kept_in_paths_robots_for_a_day(web, settings):
    web.add("boards-api.greenhouse.io", "greenhouse_job.json")
    web.robots("boards-api.greenhouse.io", "User-agent: *\nDisallow: /embed/\n")
    intake.add_url(settings, GH_URL)
    assert (settings.path("robots") / "boards-api.greenhouse.io.json").exists()
    web.robots_urls.clear()
    intake.add_url(settings, "https://boards.greenhouse.io/monzo/jobs/4412345?x=1")      # a new cache object, same day: read from the file
    assert web.robots_urls == []


def test_the_commands_print_the_robots_refusal_and_write_nothing(web, settings, monkeypatch, capsys):
    _cfg(monkeypatch, settings)
    web.robots("example.com", "User-agent: *\nDisallow: /\n")
    for argv in (["add-url"], ["live", "--url"]):
        with pytest.raises(SystemExit) as e:
            cli.main([*argv, "https://example.com/jobs/ml"])
        assert e.value.code == 1 and "robots.txt disallows /jobs/ml" in capsys.readouterr().err
    inbox = settings.path("jds") / "inbox"
    assert web.urls == [] and (not inbox.exists() or not list(inbox.iterdir()))


def test_a_redirect_is_followed_by_hand_and_the_new_host_is_checked_too(web):
    class Redirect(Resp):
        headers = {"Location": "https://other.example.org/private/x"}
    web.routes["example.com/jobs/go"] = Redirect("", 302)
    web.robots("other.example.org", "User-agent: *\nDisallow: /private/\n")
    web.add("other.example.org", "page_plain.html")
    with pytest.raises(intake.RobotsRefused, match="other.example.org/robots.txt disallows /private/x"):
        intake.fetch_ad("https://example.com/jobs/go")
    assert web.urls == ["https://example.com/jobs/go"]


def test_a_redirect_to_a_fine_page_is_followed_and_to_linkedin_is_not(web):
    class Redirect(Resp):
        headers = {"Location": "/careers/real"}
    web.routes["example.com/jobs/short-link"] = Redirect("", 301)
    web.add("example.com/careers/real", "page_plain.html")
    assert intake.fetch_ad("https://example.com/jobs/short-link").source == "page"
    assert web.urls == ["https://example.com/jobs/short-link", "https://example.com/careers/real"]

    class ToLinkedIn(Resp):
        headers = {"Location": "https://www.linkedin.com/jobs/view/1"}
    web.routes["example.com/jobs/ln"] = ToLinkedIn("", 302)
    web.urls.clear()
    with pytest.raises(intake.RobotsRefused, match="LinkedIn"):
        intake.fetch_ad("https://example.com/jobs/ln")
    assert web.urls == ["https://example.com/jobs/ln"]


def test_a_redirect_loop_stops(web):
    class Loop(Resp):
        headers = {"Location": "https://example.com/jobs/loop"}
    web.routes["example.com"] = Loop("", 302)
    with pytest.raises(intake.IntakeError, match="too many redirects"):
        intake.fetch_ad("https://example.com/jobs/loop")
    assert len(web.urls) == intake.MAX_REDIRECTS + 1


def test_requests_do_not_follow_redirects_on_their_own(monkeypatch):
    seen = {}
    monkeypatch.setattr(intake.requests, "get", lambda url, **kw: seen.update(kw) or Resp("{}"))
    intake._get("https://example.com/x")
    assert seen["allow_redirects"] is False


# ------------------------------------------------------------------ C3: LinkedIn is never fetched
@pytest.mark.parametrize("url", ["https://www.linkedin.com/jobs/view/3841", "https://uk.linkedin.com/jobs/view/1/",
                                 "https://linkedin.com/comm/jobs/view/1", "https://lnkd.in/dXyz"])
def test_a_linkedin_link_is_refused_before_any_request(web, url):
    with pytest.raises(intake.RobotsRefused) as e:
        intake.fetch_ad(url)
    assert "job-alert emails" in str(e.value) and "--clipboard" in str(e.value)
    assert web.urls == [] and web.robots_urls == []


def test_the_commands_refuse_linkedin_with_exit_1(web, settings, monkeypatch, capsys):
    _cfg(monkeypatch, settings)
    for argv in (["add-url"], ["live", "--url"]):
        with pytest.raises(SystemExit) as e:
            cli.main([*argv, "https://www.linkedin.com/jobs/view/1"])
        assert e.value.code == 1 and "LinkedIn ads come in through job-alert emails" in capsys.readouterr().err
    assert web.urls == [] and web.robots_urls == []


# ------------------------------------------------------------------ C2: CV-Library
CVL_ROBOTS = (DIR / "robots_cvlibrary.txt").read_text(encoding="utf-8")


@pytest.mark.parametrize("url", [
    "https://www.cv-library.co.uk/job/apply/219034011",
    "https://www.cv-library.co.uk/job/apply/219034011/data-scientist",
    "https://www.cv-library.co.uk/job/apply/219034011?jobId=219034011&utm_source=alert",
    "https://www.cv-library.co.uk/job/219034011",
    "https://www.cv-library.co.uk/job/219034011/data-scientist?utm_medium=email",
    "https://cv-library.co.uk/job/219034011/"])
def test_a_cv_library_apply_or_job_link_becomes_the_plain_job_page(url):
    t = intake.parse_url(url)
    assert (t.kind, t.url, t.ad_id) == ("cvlibrary", "https://www.cv-library.co.uk/job/219034011", "219034011")


@pytest.mark.parametrize("url", ["https://www.cv-library.co.uk/jobs/data-scientist", "https://www.cv-library.co.uk/data-scientist-jobs-in-london",
                                 "https://www.cv-library.co.uk/search-jobs?q=data+scientist", "https://www.cv-library.co.uk/jobs?q=python&page=2",
                                 "https://www.cv-library.co.uk/job/", "https://www.cv-library.co.uk/job/apply/",
                                 "https://www.cv-library.co.uk/?q=python", "https://www.cv-library.co.uk/job/abc"])
def test_a_cv_library_search_or_query_url_is_never_fetched(web, url):
    with pytest.raises(intake.IntakeError, match="only a single CV-Library job link.*never fetched"):
        intake.fetch_ad(url)
    assert web.urls == [] and web.robots_urls == []


def test_a_cv_library_job_is_read_from_its_json_ld_and_nothing_else(web):
    web.robots("cv-library.co.uk", CVL_ROBOTS)
    web.add("cv-library.co.uk/job/219034011", "cvlibrary_job.html")
    ad = intake.fetch_ad("https://www.cv-library.co.uk/job/apply/219034011/data-scientist?jobId=219034011")
    assert web.urls == ["https://www.cv-library.co.uk/job/219034011"]                         # the apply URL was never asked for
    assert web.robots_urls == ["https://www.cv-library.co.uk/robots.txt"]
    assert (ad.company, ad.title, ad.location, ad.source) == ("Brightwell Analytics Ltd", "Data Scientist", "Bristol, England, GB", "cvlibrary")
    assert ad.salary == "GBP 45,000-55,000 per year" and ad.posted == "2026-10-06" and ad.agency is False
    assert ad.url == "https://www.cv-library.co.uk/job/219034011" and "Python, SQL and scikit-learn" in ad.text
    for noise in ("SIMILAR-JOB-NOISE", "Similar jobs", "NAV-NOISE", "FOOTER-NOISE", "<p>", "&lt;", "Truncated teaser"):
        assert noise not in ad.text


def test_a_cv_library_page_without_json_ld_gives_the_description_element_without_the_sidebar(web):
    web.robots("cv-library.co.uk", CVL_ROBOTS)
    web.add("cv-library.co.uk/job/777", "cvlibrary_job_plain.html")
    ad = intake.fetch_ad("https://www.cv-library.co.uk/job/777")
    assert (ad.company, ad.title, ad.location) == ("Northgate Recruitment Solutions", "Machine Learning Engineer", "Leeds, West Yorkshire")
    assert ad.salary == "£50,000 - £60,000 per annum" and ad.agency is True            # an agency: flagged, not dropped
    assert ad.text.startswith("Our client, a growing fintech") and "- Strong Python and SQL" in ad.text
    for noise in ("SIMILAR-JOB-NOISE", "Similar jobs", "SHARE-NOISE", "RECOMMENDED-NOISE", "NAV-NOISE", "FOOTER-NOISE"):
        assert noise not in ad.text


def test_a_cv_library_page_with_no_description_is_refused_in_one_line(web):
    web.robots("cv-library.co.uk", CVL_ROBOTS)
    web.routes["cv-library.co.uk/job/5"] = Resp("<html><body><main><h1>Data Scientist</h1><p>Login to see this job.</p></main></body></html>")
    with pytest.raises(intake.IntakeError, match="description block was not found.*--clipboard"):
        intake.fetch_ad("https://www.cv-library.co.uk/job/5")


def test_the_cv_library_inbox_file_has_the_headers_live_reads(web, settings):
    web.robots("cv-library.co.uk", CVL_ROBOTS)
    web.add("cv-library.co.uk/job/777", "cvlibrary_job_plain.html")
    path, created, ad = intake.add_url(settings, "https://www.cv-library.co.uk/job/apply/777")
    text, _ = manual.read_jd(path)
    assert manual.header_fields(text) == {"company": "Northgate Recruitment Solutions", "title": "Machine Learning Engineer",
                                          "url": "https://www.cv-library.co.uk/job/777", "source": "cvlibrary",
                                          "location": "Leeds, West Yorkshire", "salary": "£50,000 - £60,000 per annum",
                                          "agency": "yes"}


def test_cv_library_robots_would_refuse_the_apply_path_itself(web):
    """Belt and braces: even if the rewrite were bypassed, robots.txt disallows /job/apply/ and the page is not fetched."""
    web.robots("cv-library.co.uk", CVL_ROBOTS)
    with pytest.raises(Exception, match="disallows /job/apply/1"):
        intake._default_robots.check("https://www.cv-library.co.uk/job/apply/1", intake._robots_get)


def test_the_job_from_cv_library_keeps_its_source_agency_flag_and_posted_date_in_the_database(web, settings, db):
    from careerops.apply.live import Folders  # noqa: F401 — live reads these headers; the job row is what matters here
    web.robots("cv-library.co.uk", CVL_ROBOTS)
    web.add("cv-library.co.uk/job/219034011", "cvlibrary_job.html")
    path, _, ad = intake.add_url(settings, "https://www.cv-library.co.uk/job/219034011")
    hdr = manual.header_fields(path.read_text(encoding="utf-8"))
    assert hdr["posted"] == "2026-10-06" and "agency" not in hdr
    jid = manual.add_jd(db, ad.text, company=ad.company, title=ad.title, url=ad.url, source="cvlibrary", posted=ad.posted, agency=ad.agency)
    row = db.job(jid)
    assert (row["source"], row["posted_at"], row["agency"]) == ("cvlibrary", "2026-10-06", 0)
