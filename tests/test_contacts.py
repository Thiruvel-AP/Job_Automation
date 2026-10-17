"""Contact discovery (careerops/contacts.py): the job ad, the company site, a name you give, Hunter, the domain check and the
do-not-contact list. DNS is `FakeResolver`, the Fetcher is `FakeFetcher`, Hunter is a route on it: no network, no DNS."""
import dns.exception
import dns.resolver
import pytest

from careerops import contacts
from careerops.discovery.http import Blocked, FetchError
from outreach_fakes import FakeFetcher, FakeResolver, Rec, mail_domains, make_applied

AD = ("About the role. Questions before you apply? Ask the team at hiring.team@northwind.example or Priya Nair "
      "(priya.nair@northwind.example). Do not write to noreply@northwind.example. Logo: header@2x.png. "
      "Applicant: ada.lovelace@example.org. Careers: https://northwind.example/careers")


@pytest.fixture
def job_row(settings, db, job, tmp_path):
    return make_applied(settings, db, {**job, "url": "https://boards.greenhouse.io/northwind/jobs/1", "apply_url": "https://northwind.example/apply/1",
                                       "description": AD}, tmp_path)


# ------------------------------------------------------------------ addresses and domains
def test_only_real_addresses_are_taken_from_an_ad_each_once_and_none_of_yours(settings):
    got = contacts.emails_in_text(AD, own={"ada.lovelace@example.org"})
    assert got == ["hiring.team@northwind.example", "priya.nair@northwind.example"]       # not noreply@, not header@2x.png, not yours
    assert contacts.emails_in_text("a@b.com, A@B.com; x@y.co.uk.") == ["a@b.com", "x@y.co.uk"]
    assert contacts.emails_in_text("no addresses here, or @handle, or a@b") == []


@pytest.mark.parametrize("host,reg", [("careers.example.co.uk", "example.co.uk"), ("www.example.com", "example.com"),
                                      ("example.com", "example.com"), ("a.b.example.org", "example.org")])
def test_registrable_domain(host, reg):
    assert contacts.registrable(host) == reg


def test_the_company_domain_skips_job_boards_and_comes_from_the_employers_own_link(db, job_row):
    assert contacts.company_domain(db, {"company": "Northwind Health AI", "url": "https://boards.greenhouse.io/x/1",
                                        "apply_url": "https://careers.northwind.example/apply/1"}) == "northwind.example"
    assert contacts.company_domain(db, {"company": "X", "url": "https://www.linkedin.com/jobs/view/1", "apply_url": None}) == ""
    db.conn.execute("INSERT INTO companies (norm, town_norm, careers_url) VALUES (?,?,?)", ("northwind health ai", "bristol",
                                                                                              "https://jobs.northwind-health.example/"))
    db.conn.commit()
    assert contacts.company_domain(db, {"company": "Northwind Health AI", "url": "https://boards.greenhouse.io/x/1", "apply_url": ""}) \
        == "northwind-health.example"                                                       # the companies table wins


# ------------------------------------------------------------------ the domain check (no SMTP)
def test_mx_ok_a_record_no_mx_invalid_and_unknown():
    r = FakeResolver({("mx.example", "MX"): [Rec("mail.mx.example.")], ("a-only.example", "A"): [object()],
                      ("null.example", "MX"): [Rec(".")], ("slow.example", "MX"): dns.resolver.NoNameservers(),
                      ("slow.example", "A"): dns.exception.Timeout(), ("gone.example", "MX"): dns.resolver.NXDOMAIN(),
                      ("gone.example", "A"): dns.resolver.NXDOMAIN(), ("empty.example", "MX"): dns.resolver.NoAnswer(),
                      ("empty.example", "A"): dns.resolver.NoAnswer()})
    nap = []
    assert contacts.mx_status("jobs@mx.example", r, nap.append) == "ok"
    assert contacts.mx_status("jobs@a-only.example", r, nap.append) == "a_record"          # no MX: the domain's own host takes the mail
    assert contacts.mx_status("jobs@null.example", r, nap.append) == "no_mx"               # a null MX (RFC 7505): takes none
    assert contacts.mx_status("jobs@gone.example", r, nap.append) == "no_mx"
    assert contacts.mx_status("jobs@empty.example", r, nap.append) == "no_mx"
    assert contacts.mx_status("not an address", r, nap.append) == "invalid"
    assert contacts.mx_status("jobs@slow.example", r, nap.append) == "unknown"             # DNS did not answer: not a "no"
    assert nap == [1, 1]                                                                     # only the timeouts were retried, after a pause


def test_the_domain_check_never_opens_a_smtp_connection(monkeypatch):
    import smtplib
    import socket
    monkeypatch.setattr(smtplib, "SMTP", lambda *a, **k: pytest.fail("SMTP probe"))
    monkeypatch.setattr(socket, "create_connection", lambda *a, **k: pytest.fail("a connection was opened"))
    assert contacts.mx_status("jobs@mx.example", mail_domains("mx.example")) == "ok"


def test_the_default_resolver_asks_the_public_servers_with_a_lifetime():
    r = contacts.default_resolver()
    assert r.nameservers == contacts.DNS_SERVERS and r.lifetime == contacts.DNS_LIFETIME_S


# ------------------------------------------------------------------ do-not-contact
def test_the_do_not_contact_list_matches_addresses_and_domains_and_a_missing_file_is_empty(settings):
    assert contacts.blocked_by(settings, "a@b.com") is None
    p = contacts.ensure_dnc_file(settings)
    assert p.read_text(encoding="utf-8") == "value,note\n" and p.parent == settings.path("outreach")
    p.write_text("value,note\nRudi@Agency.example,asked me to stop\nspam.example,whole domain\n@Other.example\n# a comment\n\n", encoding="utf-8")
    assert contacts.blocked_by(settings, "rudi@agency.example") == "rudi@agency.example"
    assert contacts.blocked_by(settings, "someone.else@agency.example") is None              # an address entry is that address only
    assert contacts.blocked_by(settings, "x@spam.example") == "spam.example"
    assert contacts.blocked_by(settings, "x@mail.spam.example") == "spam.example"             # a domain entry covers its subdomains
    assert contacts.blocked_by(settings, "x@other.example") == "other.example"
    assert contacts.blocked_by(settings, "x@notspam.example") is None


# ------------------------------------------------------------------ discovery: ad, then company site, two at most
def test_discovery_takes_the_ads_addresses_first_and_stops_at_two_without_fetching_the_site(settings, db, job_row):
    fetcher = FakeFetcher()
    rows = contacts.discover(db, settings, job_row, fetcher=fetcher, resolver=mail_domains("northwind.example"))
    assert [(r["email"], r["source"], r["mx_status"]) for r in rows] == [("hiring.team@northwind.example", "jd", "ok"),
                                                                         ("priya.nair@northwind.example", "jd", "ok")]
    assert fetcher.requested == []                                                            # two found: the site is not asked
    again = contacts.discover(db, settings, job_row, fetcher=fetcher, resolver=mail_domains("northwind.example"))
    assert len(again) == 2 and fetcher.requested == []                                        # idempotent


def test_discovery_reads_careers_jobs_and_contact_once_each_and_keeps_recruiting_mailtos_on_the_companys_domain(settings, db, job, tmp_path):
    row = make_applied(settings, db, {**job, "description": "No addresses in this ad.", "url": "https://boards.greenhouse.io/n/1",
                                      "apply_url": "https://careers.northwind.example/apply/1"}, tmp_path)
    page = ('<a href="mailto:Careers@Northwind.example?subject=Hi">Write</a> <a href="mailto:press@northwind.example">Press</a> '
            '<a href="mailto:jobs@other.example">Elsewhere</a> <a href="mailto:talent@northwind.example">Talent</a>')
    fetcher = FakeFetcher({"https://northwind.example/careers": page, "https://northwind.example/jobs": Blocked("robots.txt disallows /jobs"),
                           "https://northwind.example/contact": '<a href="mailto:hr@northwind.example">HR</a>'})
    rows = contacts.discover(db, settings, row, fetcher=fetcher, resolver=mail_domains("northwind.example"))
    assert [(r["email"], r["source"]) for r in rows] == [("careers@northwind.example", "company_site"), ("talent@northwind.example", "company_site")]
    assert [u for u, _ in fetcher.requested] == ["https://northwind.example/careers"]        # two found on the first page: the rest not asked


def test_a_blocked_or_failing_page_is_skipped_and_the_next_one_tried(settings, db, job, tmp_path):
    row = make_applied(settings, db, {**job, "description": "none", "url": "https://boards.greenhouse.io/n/1",
                                      "apply_url": "https://northwind.example/apply/1"}, tmp_path)
    fetcher = FakeFetcher({"https://northwind.example/careers": Blocked("robots.txt disallows /careers"),
                           "https://northwind.example/jobs": FetchError("HTTP 500"),
                           "https://northwind.example/contact": '<a href="mailto:recruitment@northwind.example">x</a>'})
    rows = contacts.discover(db, settings, row, fetcher=fetcher, resolver=mail_domains("northwind.example"))
    assert [r["email"] for r in rows] == ["recruitment@northwind.example"]
    assert [u for u, _ in fetcher.requested] == [f"https://northwind.example{p}" for p in contacts.SITE_PATHS]


def test_a_domain_that_cannot_be_told_means_no_site_request_at_all(settings, db, job, tmp_path):
    row = make_applied(settings, db, {**job, "description": "none", "url": "https://www.linkedin.com/jobs/view/9", "apply_url": ""}, tmp_path)
    fetcher = FakeFetcher()
    assert contacts.discover(db, settings, row, fetcher=fetcher, resolver=mail_domains()) == [] and fetcher.requested == []


def test_an_address_whose_check_was_unknown_is_checked_again_on_the_next_discover(settings, db, job_row, monkeypatch):
    monkeypatch.setattr(contacts.time, "sleep", lambda s: None)
    flaky = FakeResolver({("northwind.example", "MX"): dns.resolver.NoNameservers(), ("northwind.example", "A"): dns.resolver.NoNameservers()})
    first = contacts.discover(db, settings, job_row, fetcher=FakeFetcher(), resolver=flaky)
    assert {r["mx_status"] for r in first} == {"unknown"}
    second = contacts.discover(db, settings, job_row, fetcher=FakeFetcher(), resolver=mail_domains("northwind.example"))
    assert {r["mx_status"] for r in second} == {"ok"}


# ------------------------------------------------------------------ contact add, Hunter
def hunter(status, email="grace.hopper@northwind.example"):
    return lambda url, kw: {"data": {"email": email, "score": 90, "verification": {"status": status}}}


def test_contact_add_with_an_address_stores_it_as_given_and_checks_the_domain(settings, db, job_row):
    c = contacts.add(db, settings, job_row, "Grace Hopper", "Hiring Manager", "Grace.Hopper@Northwind.example", resolver=mail_domains("northwind.example"),
                     fetcher=FakeFetcher(), environ={"HUNTER_API_KEY": "KEY"})
    assert (c.email, c.source, c.mx_status, c.first_name) == ("grace.hopper@northwind.example", "user", "ok", "Grace")
    assert contacts.stored(db, job_row["id"])[0]["role"] == "Hiring Manager"


def test_hunter_is_asked_once_with_domain_and_name_and_the_key_in_a_header_not_the_url(settings, db, job_row):
    f = FakeFetcher({contacts.HUNTER_URL: hunter("valid")})
    c = contacts.add(db, settings, job_row, "Grace Hopper", "Hiring Manager", resolver=mail_domains("northwind.example"), fetcher=f,
                     environ={"HUNTER_API_KEY": "SECRETKEY"})
    assert len(f.requested) == 1
    url, kw = f.requested[0]
    assert url == contacts.HUNTER_URL and kw["params"] == {"domain": "northwind.example", "full_name": "Grace Hopper"}
    assert kw["headers"] == {"X-API-KEY": "SECRETKEY"} and kw["robots"] is False and "SECRETKEY" not in url
    assert (c.email, c.source, c.hunter_status, c.verified, c.mx_status) == ("grace.hopper@northwind.example", "hunter", "valid", True, "ok")


def test_hunter_accept_all_is_stored_but_treated_as_unverified(settings, db, job_row):
    f = FakeFetcher({contacts.HUNTER_URL: hunter("accept_all")})
    c = contacts.add(db, settings, job_row, "Grace Hopper", resolver=mail_domains("northwind.example"), fetcher=f, environ={"HUNTER_API_KEY": "k"})
    assert c.hunter_status == "accept_all" and not c.verified and any("unverified" in n for n in c.notes)
    row = contacts.stored(db, job_row["id"])[0]
    assert "unverified" in contacts.describe(row, settings) and row["hunter_status"] == "accept_all"


@pytest.mark.parametrize("environ,pages,why", [
    ({}, {}, "set HUNTER_API_KEY"),
    ({"HUNTER_API_KEY": "k"}, {contacts.HUNTER_URL: {"data": {"email": None}}}, "Hunter knows none"),
    ({"HUNTER_API_KEY": "k"}, {contacts.HUNTER_URL: FetchError("Hunter email finder: HTTP 401")}, "HTTP 401"),
])
def test_no_address_is_ever_guessed_the_name_is_kept_and_the_note_says_why(settings, db, job_row, environ, pages, why):
    f = FakeFetcher(pages)
    c = contacts.add(db, settings, job_row, "Grace Hopper", "Hiring Manager", resolver=mail_domains("northwind.example"), fetcher=f, environ=environ)
    assert c.email is None and c.mx_status is None and any(why in n for n in c.notes)
    row = contacts.stored(db, job_row["id"])[0]
    assert row["name"] == "Grace Hopper" and row["email"] is None
    assert len(f.requested) <= 1                                                             # Hunter at most once, nothing else asked


def test_hunter_is_not_asked_when_the_companys_domain_is_unknown_unless_you_pass_one(settings, db, job, tmp_path):
    row = make_applied(settings, db, {**job, "url": "https://www.linkedin.com/jobs/view/9", "apply_url": ""}, tmp_path)
    f = FakeFetcher({contacts.HUNTER_URL: hunter("valid")})
    c = contacts.add(db, settings, row, "Grace Hopper", resolver=mail_domains("northwind.example"), fetcher=f, environ={"HUNTER_API_KEY": "k"})
    assert c.email is None and f.requested == [] and any("--domain" in n for n in c.notes)
    c = contacts.add(db, settings, row, "Grace Hopper", domain="northwind.example", resolver=mail_domains("northwind.example"), fetcher=f,
                     environ={"HUNTER_API_KEY": "k"})
    assert c.email == "grace.hopper@northwind.example" and len(f.requested) == 1
    assert len(contacts.stored(db, row["id"])) == 1                                          # the named contact got its address, no duplicate


def test_contact_add_needs_a_name_and_a_real_address(settings, db, job_row):
    with pytest.raises(contacts.ContactError, match="needs a name"):
        contacts.add(db, settings, job_row, "  ", resolver=mail_domains())
    with pytest.raises(contacts.ContactError, match="not an e-mail address"):
        contacts.add(db, settings, job_row, "Grace Hopper", email="grace@", resolver=mail_domains())


def test_you_may_add_more_than_two_but_only_the_best_two_are_ranked_first(settings, db, job_row):
    contacts.discover(db, settings, job_row, fetcher=FakeFetcher(), resolver=mail_domains("northwind.example"))
    contacts.add(db, settings, job_row, "Grace Hopper", email="grace@northwind.example", resolver=mail_domains("northwind.example"))
    rows = contacts.stored(db, job_row["id"])
    assert len(rows) == 3 and rows[0]["source"] == "user" and [r["source"] for r in rows[1:]] == ["jd", "jd"]


def test_first_name_and_a_role_address_have_no_first_name():
    assert contacts.Contact(name="Grace Murray Hopper").first_name == "Grace" and contacts.Contact(email="jobs@x.example").first_name == ""


# ------------------------------------------------------------------ the review screen
def answers(*typed):
    it = iter(typed)
    return lambda prompt: next(it)


def test_after_approve_the_contacts_are_shown_with_source_and_domain_check_and_c_adds_one(settings, db, job_row):
    said = []
    contacts.review_step(db, settings, job_row, ask=answers("c", "Grace Hopper", "Hiring Manager", "grace@northwind.example", ""), say=said.append,
                         fetcher=FakeFetcher(), resolver=mail_domains("northwind.example"))
    text = "\n".join(said)
    assert "hiring.team@northwind.example" in text and "source jd" in text and "MX ok" in text
    assert "Grace Hopper <grace@northwind.example> | Hiring Manager | source user | MX ok" in text
    assert contacts.stored(db, job_row["id"])[0]["name"] == "Grace Hopper"


def test_the_review_step_with_nothing_found_says_to_find_the_person_on_linkedin_and_enter_continues(settings, db, job, tmp_path):
    row = make_applied(settings, db, {**job, "description": "none", "url": "https://www.linkedin.com/jobs/view/9", "apply_url": ""}, tmp_path)
    said = []
    contacts.review_step(db, settings, row, ask=answers(""), say=said.append, fetcher=FakeFetcher(), resolver=mail_domains())
    assert "none found" in " ".join(said) and "LinkedIn" in " ".join(said)


def test_a_failure_in_the_review_step_is_printed_and_never_stops_the_review(settings, db, job_row):
    said = []

    class Down:
        def resolve(self, *a, **k):
            raise RuntimeError("dns is down")
    contacts.review_step(db, settings, job_row, ask=answers(""), say=said.append, fetcher=FakeFetcher(), resolver=Down())
    assert any("could not look (dns is down)" in s for s in said)


def test_a_do_not_contact_entry_is_marked_in_the_list(settings, db, job_row):
    contacts.ensure_dnc_file(settings).write_text("value,note\nnorthwind.example,no\n", encoding="utf-8")
    said = []
    contacts.review_step(db, settings, job_row, ask=answers(""), say=said.append, fetcher=FakeFetcher(), resolver=mail_domains("northwind.example"))
    assert "DO NOT CONTACT" in "\n".join(said)


# ------------------------------------------------------------------ the CLI
def test_contact_add_and_list_through_the_cli(tmp_path, settings, db, job_row, capsys, monkeypatch):
    import yaml
    from careerops import cli
    cfg = tmp_path / "settings.yaml"
    cfg.write_text(yaml.safe_dump(settings.raw), encoding="utf-8")
    monkeypatch.setattr(contacts, "default_resolver", lambda: mail_domains("northwind.example"))
    monkeypatch.setattr(cli, "load_dotenv", lambda p: 0)               # the real .env may hold a HUNTER_API_KEY: no test may use it
    monkeypatch.delenv("HUNTER_API_KEY", raising=False)
    cli.main(["--config", str(cfg), "contact", "add", job_row["id"], "Grace Hopper", "--role", "Hiring Manager", "--email", "grace@northwind.example"])
    out = capsys.readouterr().out
    assert "Stored: Grace Hopper <grace@northwind.example> | Hiring Manager | source user | MX ok" in out
    cli.main(["--config", str(cfg), "contact", "list", job_row["id"]])
    assert "grace@northwind.example" in capsys.readouterr().out
    cli.main(["--config", str(cfg), "contact", "add", job_row["id"], "No Address"])
    assert "no address: pass --email, or set HUNTER_API_KEY" in capsys.readouterr().out
    with pytest.raises(SystemExit):
        cli.main(["--config", str(cfg), "contact", "add", "nope:1", "Someone"])


def test_init_creates_the_do_not_contact_file_and_never_rewrites_it(tmp_path, settings):
    from careerops import setup
    for k in ("register_dir", "companies_house_dir", "outputs", "browser_profile", "logs"):
        settings.raw["paths"][k] = str(tmp_path / "data" / k)
    setup.init(settings)
    p = settings.path("outreach") / "do_not_contact.csv"
    assert p.read_text(encoding="utf-8") == "value,note\n"
    p.write_text("value,note\nx.example,no\n", encoding="utf-8")
    setup.init(settings)
    assert p.read_text(encoding="utf-8") == "value,note\nx.example,no\n"
