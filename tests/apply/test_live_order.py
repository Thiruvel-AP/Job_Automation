"""`careerops live`, order after tailoring: the selected jobs are tailored first (freshness tier, then the pre-tailoring ESTIMATE of P,
limited by --max); the review queue is then sorted by tier and the FINAL P, and a table is printed before the first prompt.
Nothing is dropped. Fakes for everything that costs money; archive, inbox and database are under tmp_path."""
import re

import pytest

from careerops.apply.live import EXIT_BUDGET, Live
from careerops.discovery import gates
from test_live import Fake, archive, inbox, put, session, yes     # noqa: F401  (fixtures and helpers of the live tests)

CEILINGS = {"Alpha": 0.25, "Beta": 0.90, "Gamma": 0.60}           # what tailoring finds, by company
ESTIMATES = {"Alpha": 0.95, "Beta": 0.60, "Gamma": 0.40}          # the triage-time estimate, by company


@pytest.fixture(autouse=True)
def estimates(monkeypatch):
    """The estimate is read from the JD text: 'Posting N.' is replaced below by the company name so the stand-in can tell them apart."""
    def pre(text, fb, settings):
        return next(v for k, v in ESTIMATES.items() if f"MARK-{k}" in text)
    monkeypatch.setattr(gates, "pre_ceiling", pre)


class Tailoring(Fake):
    """Tailoring finds the CEILINGS: the order the estimate gave is not the order the final P gives."""

    def tailor(self, jid):
        super().tailor(jid)
        self.db.update_job(jid, ceiling=CEILINGS[self.db.job(jid)["company"]])


def three(inbox, jd, titles=("AI Engineer", "AI Engineer", "AI Engineer")):
    for n, (co, title) in enumerate(zip(("Alpha", "Beta", "Gamma"), titles), 1):
        put(inbox, f"{n}.txt", jd + f"\nMARK-{co}\n", company=co, title=title, n=n)


def test_the_review_order_follows_the_final_p_not_the_estimate(db, settings, inbox, archive, jd):
    put(inbox, "1.txt", jd + "\nMARK-Alpha\n", company="Alpha", n=1)
    put(inbox, "2.txt", jd + "\nMARK-Beta\n", company="Beta", n=2)
    fake = Tailoring(db, settings, review="skipped")
    live, lines = session(db, settings, fake)
    assert live.run() == 0
    tailored = [db.job(j)["company"] for n, j in fake.calls if n == "tailor"]
    reviewed = [db.job(j)["company"] for n, j in fake.calls if n == "review"]
    assert tailored == ["Alpha", "Beta"]                  # the estimate order: Alpha 0.95 first
    assert reviewed == ["Beta", "Alpha"]                  # the final P order: Beta 0.90 beats Alpha 0.25
    assert fake.names() == ["tailor", "tailor", "review", "review"]                # every tailoring before the first review


def test_the_inbox_listing_calls_the_number_an_estimate(db, settings, inbox, archive, jd):
    put(inbox, "1.txt", jd + "\nMARK-Alpha\n", company="Alpha", n=1)
    put(inbox, "2.txt", jd + "\nMARK-Beta\n", company="Beta", n=2)
    live, lines = session(db, settings, Tailoring(db, settings, review="skipped"))
    live.run()
    listing = [l for l in lines if "1.txt" in l and "estimate" in l]
    assert listing and "estimate P 0.71" in listing[0]          # 0.95 x S 0.75 (no register in the test)
    assert any("estimate" in l and "tailoring" in l for l in lines)       # the heading says it is not the final P


def test_the_final_table_is_printed_before_the_first_prompt_with_every_column(db, settings, inbox, archive, jd):
    three(inbox, jd, titles=("AI Engineer", "Senior AI Engineer", "Manager, Applied AI"))
    fake = Tailoring(db, settings, {"Tailor it anyway": ["y", "y"]}, review="skipped")       # the triage rules skip a senior / manager title
    lines: list[str] = []
    seen = []
    real = fake.review
    fake.review = lambda jid: (seen or seen.append(list(lines)), real(jid))[1]            # the lines printed when the first prompt comes
    live = Live(db, settings, fake.hooks(), out=lines.append, resume_cmd="careerops live --max 5")
    live.run()
    before = seen[0]
    head = next(i for i, l in enumerate(before) if l.startswith("Review queue by freshness tier, then final P"))
    table = before[head:]
    assert table[1].split("|")[0].split()[-1] == "company" and all(c in table[1] for c in ("role", "age", "tier", "level", "ATS ceiling", "P"))
    rows = [l for l in table if re.match(r"\s+\d+\. ", l)]
    assert [re.match(r"\s+\d+\. (\w+)", r).group(1) for r in rows] == ["Beta", "Alpha", "Gamma"]      # 0.90 x 0.6 (senior), 0.25, 0.60 x 0.3 (manager)
    assert "LEVEL: senior" in rows[0] and "LEVEL: manager" in rows[2] and "ATS ceiling 0.90" in rows[0] and "ATS ceiling 0.25" in rows[1] and "LEVEL" not in rows[1]
    assert sum("Apply:" in l for l in table) == 3                                             # one Apply line per job


def test_the_level_factor_changes_the_final_order(db, settings, inbox, archive, jd):
    # Beta (0.90) is a director role: P = 0.90 x 1.0 x 0.3 = 0.27, below Gamma (0.60) and above Alpha (0.25)
    three(inbox, jd, titles=("AI Engineer", "Director, Applied AI", "AI Engineer"))
    fake = Tailoring(db, settings, {"Tailor it anyway": ["y"]}, review="skipped")
    session(db, settings, fake)[0].run()
    assert [db.job(j)["company"] for n, j in fake.calls if n == "review"] == ["Gamma", "Beta", "Alpha"]
    assert len(db.jobs()) == 3                                                   # nobody was dropped


def test_max_limits_the_tailoring_to_the_best_estimates_and_the_rest_stays_in_the_inbox(db, settings, inbox, archive, jd):
    three(inbox, jd)
    fake = Tailoring(db, settings, review="skipped")
    live, lines = session(db, settings, fake, max_jobs=2)
    live.run()
    assert sorted(db.job(j)["company"] for n, j in fake.calls if n == "tailor") == ["Alpha", "Beta"]      # Gamma has the lowest estimate
    assert (inbox / "3.txt").exists() and any("--max 2 reached" in l for l in lines)


def test_a_spent_budget_still_lets_you_review_what_was_tailored(db, settings, inbox, archive, jd):
    put(inbox, "1.txt", jd + "\nMARK-Alpha\n", company="Alpha", n=1)
    put(inbox, "2.txt", jd + "\nMARK-Beta\n", company="Beta", n=2)
    fake = Tailoring(db, settings, review="skipped")
    tailor = fake.tailor

    def out_of_budget(jid):
        if db.job(jid)["company"] == "Beta":
            fake.calls.append(("tailor", jid))                 # the second job: tailor_jobs returned without tailoring
            return
        tailor(jid)
    fake.tailor = out_of_budget
    live, lines = session(db, settings, fake)
    assert live.run() == EXIT_BUDGET
    assert [db.job(j)["company"] for n, j in fake.calls if n == "review"] == ["Alpha"]
    assert {j["company"]: j["status"] for j in db.jobs()} == {"Alpha": "skipped", "Beta": "triaged"}
    assert (inbox / "2.txt").exists()


def test_the_summary_lists_every_job_in_the_order_it_was_reviewed(db, settings, inbox, archive, jd):
    put(inbox, "1.txt", jd + "\nMARK-Alpha\n", company="Alpha", n=1)
    put(inbox, "2.txt", jd + "\nMARK-Beta\n", company="Beta", n=2)
    live, _ = session(db, settings, Tailoring(db, settings, review="skipped"))
    live.run()
    assert [r.company for r in live.results] == ["Alpha", "Beta"] and [r.status for r in live.results] == ["skipped", "skipped"]
    text = next(settings.path("live").glob("*.md")).read_text()
    assert text.count("## ") == 2


def test_a_retailor_run_tailors_once_not_again_at_the_review_pass(db, settings, inbox, archive, jd):
    put(inbox, "1.txt", jd + "\nMARK-Alpha\n", company="Alpha", n=1)
    first = Tailoring(db, settings, review="quit")
    session(db, settings, first)[0].run()
    assert db.jobs()[0]["status"] == "needs_review"
    again = Tailoring(db, settings, review="skipped")
    session(db, settings, again, retailor=True)[0].run()
    assert again.names() == ["tailor", "review"]            # sent back to triaged and tailored once; the review pass does not do it twice
