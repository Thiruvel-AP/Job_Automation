"""The gate in front of tailoring (discovery/pretailor.py, called by cli.tailor_jobs for every job): blacklist, liveness, agency vs direct, with the repost signals
as flags; and the `discover --report` lines. A closed or expired posting is never tailored; nothing here spends a token. No network (a fake `get`), no model."""
import json
import shutil
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from careerops import cli
from careerops.db import DB
from careerops.discovery import liveness, pretailor
from careerops.discovery.http import Fetcher
from careerops.discovery.robots import RobotsCache

NOW = datetime(2026, 10, 27, 12, 0, tzinfo=timezone.utc)
OPEN_PAGE = "<html><body><h1>Data Scientist</h1><p>" + "We build forecasting tools for retailers. " * 8 + "</p><a href='/apply'>Apply now</a></body></html>"
HAYS = "Our client, Acme Ltd, is a leading retailer of forecasting tools."


class Net:
    def __init__(self, status=200, body=OPEN_PAGE):
        self.status, self.body, self.requests = status, body, []

    def get(self, url, **kw):
        self.requests.append(url)
        if url.endswith("/robots.txt"):
            return SimpleNamespace(status_code=404, text="", ok=False, headers={}, content=b"", url=url)
        return SimpleNamespace(status_code=self.status, text=self.body, ok=self.status < 400, headers={}, content=self.body.encode(), url=url)


def fetcher(net):
    return Fetcher(RobotsCache(), get=net.get, sleep=lambda s: None, clock=lambda: 0.0, gap_s=0.0)


@pytest.fixture
def db(tmp_path):
    return DB(tmp_path / "t.db")


@pytest.fixture
def world(db, settings, tmp_path):
    settings.raw["paths"]["jds"] = str(tmp_path / "jds")
    settings.raw["paths"]["jds_done"] = str(tmp_path / "done")
    settings.raw["paths"]["discover"] = str(tmp_path / "discover")
    settings.raw["paths"]["applications"] = str(tmp_path / "apps")
    settings.raw["paths"]["applications_auto"] = str(tmp_path / "apps_auto")
    (tmp_path / "jds" / "inbox").mkdir(parents=True)
    (tmp_path / "profile").mkdir()
    shutil.copy(settings.path("fact_bank"), tmp_path / "profile" / "fact_bank.yaml")          # the blacklist sits beside the fact bank: in tmp, not in the repo
    settings.raw["paths"]["fact_bank"] = str(tmp_path / "profile" / "fact_bank.yaml")
    return SimpleNamespace(db=db, s=settings, tmp=tmp_path, inbox=tmp_path / "jds" / "inbox", done=tmp_path / "done")


def add(db, id="x:1", company="Acme", title="Data Scientist", url="https://acme.com/jobs/12345", status="triaged", agency=0, description="", first_seen=None, location="London"):
    db.upsert_job({"id": id, "source": "manual", "company": company, "title": title, "location": location, "url": url, "apply_url": url,
                   "description": description or "A role. " * 40})
    fields = {"status": status, "agency": agency}
    if first_seen:
        fields["first_seen"] = first_seen
    db.update_job(id, **fields)
    return db.job(id)


def gate(w, job, net=None, answers=(), **kw):
    asked = []
    replies = list(answers)

    def ask(prompt):
        asked.append(prompt)
        return replies.pop(0) if replies else ""
    g = pretailor.run(w.db, w.s, job, fetcher=fetcher(net or Net()), cache=liveness.Cache(None), ask=ask, out=lambda s: None, now=NOW, archive=kw.pop("archive", []), **kw)
    g.asked = asked
    return g


def write_blacklist(w, text):
    p = w.s.path("fact_bank").parent / "blacklist.yaml"
    p.write_text(text, encoding="utf-8")
    return p


# ------------------------------------------------------------------ B1: a closed posting is never tailored
def test_an_open_job_goes_on_with_no_flag_and_one_request(world):
    net = Net()
    g = gate(world, add(world.db), net)
    assert not g.stop and g.flags == [] and [u for u in net.requests if "robots" not in u] == ["https://acme.com/jobs/12345"]


def test_a_closed_job_stops_is_filed_in_jds_done_closed_and_is_marked_closed(world):
    job = add(world.db)
    ad = world.inbox / "Acme - Data Scientist.txt"
    ad.write_text("Company: Acme\nTitle: Data Scientist\nURL: https://acme.com/jobs/12345\n\n" + "A role. " * 40, encoding="utf-8")
    other = world.inbox / "Other - ML Engineer.txt"
    other.write_text("Company: Other\nTitle: ML Engineer\nURL: https://other.com/jobs/9\n\n" + "A role. " * 40, encoding="utf-8")
    g = gate(world, job, Net(status=404, body=""))
    assert g.stop and g.decision == "closed" and "404" in g.reason
    assert world.db.job("x:1")["status"] == "closed" and "404" in world.db.job("x:1")["reason"]
    assert (world.done / "closed" / ad.name).is_file() and not ad.exists() and other.exists()
    assert (world.done / "closed" / ad.name.replace(".txt", ".reason.txt")).read_text(encoding="utf-8").startswith("closed")


def test_an_expired_phrase_closes_it(world):
    g = gate(world, add(world.db), Net(body=OPEN_PAGE.replace("We build", "This job has expired. We build")))
    assert g.stop and g.decision == "closed" and "expired" in g.reason


def test_an_uncertain_job_goes_on_with_a_flag(world):
    g = gate(world, add(world.db), Net(status=503, body=""))
    assert not g.stop and any(f.startswith("liveness uncertain") for f in g.flags)
    assert any(f.startswith("liveness uncertain") for f in json.loads(world.db.job("x:1")["flags"]))


def test_a_job_without_an_address_is_not_checked_and_not_flagged(world):
    net = Net()
    job = add(world.db, url="")
    g = gate(world, job, net)
    assert not g.stop and g.flags == [] and net.requests == []


def test_linkedin_is_never_fetched_even_for_a_job_found_there(world):
    net = Net()
    g = gate(world, add(world.db, url="https://www.linkedin.com/jobs/view/1"), net)
    assert net.requests == [] and not g.stop and any("liveness uncertain" in f for f in g.flags)


def test_offline_skips_the_network_but_not_the_other_checks(world):
    net = Net(status=404, body="")
    g = gate(world, add(world.db), net, offline=True)
    assert not g.stop and net.requests == []


def test_the_cache_is_used_so_a_second_job_pass_sends_nothing(world):
    cache = liveness.Cache(world.tmp / "l.json")
    net = Net()
    job = add(world.db)
    for _ in range(2):
        pretailor.run(world.db, world.s, job, fetcher=fetcher(net), cache=cache, ask=lambda p: "", out=lambda s: None, now=NOW, archive=[])
    assert len([u for u in net.requests if "robots" not in u]) == 1


# ------------------------------------------------------------------ B3: the blacklist
def test_a_blacklisted_company_asks_and_n_skips_it_before_anything_is_fetched(world):
    write_blacklist(world, "blacklist:\n  - {name: Acme, scope: company, since: 2026-03-01, reason: ghosted me twice}\n")
    net = Net()
    g = gate(world, add(world.db), net, answers=["n"])
    assert g.stop and g.decision == "skipped" and g.asked == ["Acme is on your blacklist since 2026-03-01: ghosted me twice. Apply anyway? [y/N] "]
    assert net.requests == [] and world.db.job("x:1")["status"] == "skipped" and "blacklist" in world.db.job("x:1")["reason"]


def test_silence_at_the_blacklist_question_is_a_no(world):
    write_blacklist(world, "- {name: Acme, scope: company, since: 2026-03-01, reason: r}\n")
    assert gate(world, add(world.db), answers=[""]).stop


def test_y_goes_on_and_says_so_in_a_flag(world):
    write_blacklist(world, "- {name: Acme, scope: company, since: 2026-03-01, reason: r}\n")
    g = gate(world, add(world.db), answers=["y"])
    assert not g.stop and "blacklisted (you chose to go on)" in g.flags


def test_a_domain_entry_hits_the_jobs_address_and_a_lookalike_does_not(world):
    write_blacklist(world, "- {name: acme.com, scope: domain, since: 2026-01-01, reason: r}\n")
    assert gate(world, add(world.db, url="https://careers.acme.com/jobs/12345"), answers=["n"]).stop
    assert not gate(world, add(world.db, id="x:2", url="https://notacme.com/jobs/12345")).stop


def test_a_blacklist_file_that_cannot_be_read_stops_the_job_and_changes_nothing(world):
    write_blacklist(world, "blacklist: [")
    g = gate(world, add(world.db))
    assert g.stop and "blacklist.yaml" in g.reason and world.db.job("x:1")["status"] == "triaged"


# ------------------------------------------------------------------ B4: agency vs direct
def test_the_same_role_via_an_agency_stops_until_you_say_which_channel_owns_it(world):
    add(world.db, id="x:2", company="Hays", agency=1, description=HAYS, status="needs_review", url="https://hays.example/jobs/2")
    g = gate(world, add(world.db), answers=["s"])
    assert g.stop and g.decision == "skipped" and "Hays" in g.asked[0] and "Which channel owns it" in g.asked[0]
    assert world.db.job("x:1")["status"] == "skipped" and world.db.job("x:2")["status"] == "needs_review"          # the other channel is left alone


def test_continue_goes_on_with_a_flag_and_silence_does_not(world):
    add(world.db, id="x:2", company="Hays", agency=1, description=HAYS, status="needs_review", url="https://hays.example/jobs/2")
    assert gate(world, add(world.db), answers=[""]).stop
    g = gate(world, world.db.job("x:1"), answers=["c"])
    assert not g.stop and "same role via another channel (you chose this one)" in g.flags


def test_the_archive_counts(world):
    g = gate(world, add(world.db), answers=["s"], archive=[("Acme Ltd", "Data Scientist")])
    assert g.stop and "Acme Ltd/Data Scientist" in g.asked[0]


def test_an_agency_that_hides_the_employer_is_asked_about_and_silence_is_a_no(world):
    job = add(world.db, company="Hays", agency=1, description="Our client is a leading retailer.")
    g = gate(world, job, answers=[""])
    assert g.stop and "does not name the employer" in g.asked[0] and world.db.job("x:1")["status"] == "skipped"
    assert not gate(world, world.db.job("x:1"), answers=["y"]).stop


def test_a_direct_job_with_no_other_channel_is_not_asked_anything(world):
    g = gate(world, add(world.db), archive=[("Other", "Data Scientist")])
    assert g.asked == [] and not g.stop


# ------------------------------------------------------------------ B2: flags only
def test_repost_and_evergreen_flags_are_attached_and_never_stop_a_job(world):
    add(world.db, id="x:1", url="https://acme.com/jobs/1", first_seen="2026-10-01T09:00:00+00:00")
    job = add(world.db, id="x:2", url="https://acme.com/jobs/2", first_seen="2026-10-21T09:00:00+00:00")
    g = gate(world, job)
    assert not g.stop and "possible repost (2 listings since 2026-10-01)" in g.flags
    assert "possible repost (2 listings since 2026-10-01)" in json.loads(world.db.job("x:2")["flags"])
    assert json.loads(world.db.job("x:2")["flags"]).count("possible repost (2 listings since 2026-10-01)") == 1
    gate(world, world.db.job("x:2"))                                                              # again: the flag is not added twice
    assert json.loads(world.db.job("x:2")["flags"]).count("possible repost (2 listings since 2026-10-01)") == 1


# ------------------------------------------------------------------ cli.tailor_jobs
def test_tailor_jobs_never_reaches_the_model_for_a_closed_posting(world, monkeypatch, capsys):
    job = add(world.db)
    calls = []
    monkeypatch.setattr("careerops.tailoring.tailor.tailor_job", lambda *a, **k: calls.append(1))
    llm = SimpleNamespace(begin_job=lambda est: calls.append("begin") or [], providers=[], end_job=lambda: None)
    monkeypatch.setattr(cli.usage, "report", lambda *a: None)
    cli.tailor_jobs(world.db, world.s, {}, llm, [job], fetcher=fetcher(Net(status=410, body="")), ask=lambda p: "")
    out = capsys.readouterr().out
    assert calls == [] and "closed" in out and "410" in out
    assert world.db.job("x:1")["status"] == "closed"


def test_tailor_jobs_in_mock_mode_never_uses_the_network(world, monkeypatch):
    job = add(world.db)
    monkeypatch.setattr(pretailor, "default_fetcher", lambda s: pytest.fail("the network was set up in --mock"))
    monkeypatch.setattr("careerops.tailoring.tailor.tailor_job", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("stop here")))
    llm = SimpleNamespace(begin_job=lambda est: [], providers=[], end_job=lambda: None)
    monkeypatch.setattr(cli.usage, "report", lambda *a: None)
    cli.tailor_jobs(world.db, world.s, {}, llm, [job], mock=True, ask=lambda p: "")
    assert world.db.job("x:1")["status"] == "error"                                              # it got past the gate to tailor_job


# ------------------------------------------------------------------ discover --report
def write_ad(w, name, company, title, url, first_seen="2026-10-25", posted=""):
    head = f"Company: {company}\nTitle: {title}\nURL: {url}\nFirst seen: {first_seen}\n" + (f"Posted: {posted}\n" if posted else "")
    (w.inbox / name).write_text(head + "\n" + "A role. " * 40, encoding="utf-8")


def test_the_report_counts_open_closed_and_uncertain_repost_and_evergreen(world):
    class Mixed(Net):
        def get(self, url, **kw):
            if "closed" in url:
                self.status, self.body = 404, ""
            elif "weird" in url:
                self.status, self.body = 503, ""
            else:
                self.status, self.body = 200, OPEN_PAGE
            return super().get(url, **kw)
    write_ad(world, "a.txt", "Acme", "Data Scientist", "https://acme.com/jobs/1001", "2026-10-01")
    write_ad(world, "b.txt", "Acme", "Data Scientist", "https://acme.com/jobs/1002", "2026-10-21")
    write_ad(world, "c.txt", "Bravo", "ML Engineer", "https://bravo.com/closed/2001", "2026-10-25", posted="2025-03-01")
    write_ad(world, "d.txt", "Delta", "NLP Engineer", "https://delta.com/weird/3001")
    write_ad(world, "e.txt", "Echo", "AI Engineer", "https://www.linkedin.com/jobs/view/4001")
    lines = pretailor.report_lines(world.s, world.db, fetcher(Mixed()), cache=liveness.Cache(None), now=NOW)
    text = "\n".join(lines)
    assert "open 2 · closed 1 · uncertain 2" in text and "5 ad(s)" in text
    assert "1 possible repost(s) (2 listings)" in text and "1 evergreen?" in text
    assert "Bravo | ML Engineer" in text and "404" in text


def test_the_report_without_a_fetcher_says_the_liveness_check_was_skipped(world):
    write_ad(world, "a.txt", "Acme", "Data Scientist", "https://acme.com/jobs/1001")
    text = "\n".join(pretailor.report_lines(world.s, world.db, None, now=NOW))
    assert "liveness: not checked" in text.lower() and "1 ad(s)" in text


def test_the_report_checks_at_most_the_cap_and_says_how_many_it_left(world):
    for n in range(5):
        write_ad(world, f"{n}.txt", f"Co{n}", "Data Scientist", f"https://co{n}.com/jobs/{1000 + n}")
    text = "\n".join(pretailor.report_lines(world.s, world.db, fetcher(Net()), cache=liveness.Cache(None), now=NOW, max_check=3))
    assert "open 3" in text and "2 not checked" in text


def test_the_report_changes_nothing_in_the_inbox_or_the_database(world):
    write_ad(world, "c.txt", "Bravo", "ML Engineer", "https://bravo.com/closed/2001")
    add(world.db)
    before = ([p.name for p in world.inbox.iterdir()], world.db.job("x:1")["status"])
    pretailor.report_lines(world.s, world.db, fetcher(Net(status=404, body="")), cache=liveness.Cache(None), now=NOW)
    assert ([p.name for p in world.inbox.iterdir()], world.db.job("x:1")["status"]) == before
