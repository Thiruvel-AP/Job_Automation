"""career-ops command line.

  careerops init                create data/ folders and .env (idempotent)
  careerops doctor              check xelatex, fonts, playwright, LLM keys, fact-bank TODO/CONFIRM items
  careerops llm-check           one tiny request per LLM provider: reachable, model, latency, JSON, json_mode
  careerops calibrate [--compare REPORT] [--only ID ..] [--limit N] [--provider NAME] [--freeze-keywords [--force]] [--summarise]
                                tailor every JD in data/calibration/manifest.yaml with the real model and measure it
  careerops sponsors            download the gov.uk sponsor register if its link changed, diff it against the last one
  careerops discover            pull jobs from configured boards, triage with rule gates
  careerops discover --ats | --reed | --adzuna | --gmail-alerts | --jobbank [--limit N] [--dry-run]
                                find open jobs and write each as a JD file in data/jds/inbox/ (best priority first); a job outside
                                the UK is written as `Status: listed_only` (live will not apply to it without a visa block);
                                every run ends with a health table of the sources in config/sources.yaml
  careerops sources check | list [--mock]
                                health of every source in config/sources.yaml (method reachable, robots.txt, credentials, alerts
                                in the last 7 days, postings) -> table + data/discover/health/<date>.csv; `list` prints the registry
  careerops discover --gmail-auth | --report
                                sign in to Google once (read mail, create drafts, edit the tracker Sheet; never sends) · counts per
                                source, resolved vs the manual queue
  careerops retriage            re-run triage on every non-applied job (back-fills family/tier/ceiling_pre)
  careerops add-jd              add a job by hand (LinkedIn, email, truncated Adzuna JD); --file must be a UTF-8 file of >= 500 bytes
  careerops add-url URL [--company C] [--title T]
                                fetch one job ad (Greenhouse, Lever, Ashby API; else the page) into data/jds/inbox/
  careerops judge-report        the LLM judge vs your review decisions: agreement, kappa, false approves (review.judge)
  careerops live [--max N] [--dry-run] [--url URL | --clipboard]
                                every JD in data/jds/inbox/ through add-jd → tailor → review → apply assist, one job at
                                a time, with you at every gate and an archive check after each job (see README)
  careerops tailor [--limit N | --id JOB] [--one-page]
                                JD analysis → tailored CV (two full pages; --one-page for one) + letter → review queue
  careerops review              approve / edit / reject (logs preference data)
  careerops apply [--no-letter] open approved jobs' forms, fill basics, you submit; then archive what was sent
  careerops mark-applied --id JOB [--ref REF] [--date YYYY-MM-DD] [--no-letter]
                                archive + mark an application you made outside the assist
  careerops outcome --id JOB --event applied|acknowledged|screen|interview|offer|rejected|withdrawn [--date D] [--note T]
                                log what happened after an application (mark-applied logs 'applied' itself)
  careerops outcomes [--import-archive [--apply]]
                                the funnel applied -> any reply -> screen -> interview -> offer with 95% Wilson
                                intervals, split by role family, tier, cv_pages, sponsor_match; --import-archive lists
                                the archive folders as approximate 'applied' events (dry run; --apply writes the DB only)
  careerops import-linkedin PDF [--mock] [--fact-bank F] [--out F] [--diff F]
                                a LinkedIn profile saved as PDF -> config/profile/fact_bank.proposed.yaml (fact-bank schema)
                                and data/linkedin_diff.md (every mismatch with fact_bank.yaml); fact_bank.yaml is only read
  careerops export              write the Excel view (Jobs, Summary, Outcomes)
  careerops export-prefs        preference log → DPO pairs jsonl
  careerops run                 discover + tailor + export (what cron calls)
  careerops status              counts per status

  Track A (sponsor-first company discovery) — milestones M1–M3, stubs until built:
  careerops companies --snapshot | --refresh-ch | --resolve | --queue | --set NAME --url URL
  careerops learning            ranked skills-gap list across all tailored jobs (M4)
  careerops outreach --id JOB --person NAME --title TITLE   LinkedIn note + follow-up drafts (M4)
  careerops tracker sync [--id JOB] [--dry-run]
                                write every applied job (or one) to your Google Sheet, one row per Job ID, updated never duplicated;
                                --dry-run prints the rows and touches no network (tracker.py)
  careerops contact add JOB "First Last" [--role R] [--email E] [--domain D] | contact list JOB
                                a person you found on LinkedIn: stored with a domain check; no --email asks Hunter once if
                                HUNTER_API_KEY is set (contacts.py)
  careerops outreach draft JOB [--to ADDRESS]
                                Gmail DRAFTS (never sent) to the job's contacts with the CV attached; --to makes one test draft
                                to that address (outreach.py). Runs after mark-applied when outreach.mode is `drafts`.
  careerops set-country --id JOB --country XX
                                the country a job's work is in (W): decides the visa answers; a job whose W cannot be told is
                                `needs_country` (not tailored, never applied to) until you give it
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
import traceback
from datetime import date
from pathlib import Path

from . import ats
from .apply import visa as visa_mod
from .apply.archive import mark_applied_unarchived, preflight, record_application
from .apply.errors import ArchiveError, ArchiveIncomplete
from .discovery import gates, manual, sources
from .discovery import register as sponsors
from .review import judge
from .review import workflow as review
from . import mockroot
from .config import load_settings, load_yaml
from .db import DB, now
from .reporting.excel import export
from .tailoring.queue import order, select_for_run
from .llm import BudgetExhausted, MockLLM, Router
from .setup import doctor, init, load_dotenv

STRETCH_FLAG = "Stretch"      # job flag: the honest ATS ceiling is under the band (kept and labelled, never dropped)


def _ctx(args):
    s = load_settings(args.config)
    load_dotenv(s.root / ".env")          # cron-friendly: no `set -a; . .env` needed
    if getattr(args, "mock", False):
        # --mock never opens the real DB, outputs, exports or archive: it gets its own data root and one seeded job
        s = mockroot.mock_settings(s)
        mockroot.ensure_dirs(s)
        db = DB(s.path("db"))
        # `run` and `tailor` start from a freshly seeded job, once per process (`run` calls tailor, then export);
        # `review --mock` after `run --mock` must see the tailored job, so it never reseeds.
        if getattr(args, "cmd", None) in ("run", "tailor") and not getattr(args, "_mock_seeded", False):
            args._mock_seeded = True
            print(f"MOCK data root {mockroot.mock_root(s)} — real data/ is not touched.")
            status = mockroot.seed(db, lambda d: triage_new(d, s))
            if status != "triaged":
                reason = db.job(mockroot.SEED_JOB_ID)["reason"]
                raise SystemExit(f"mock seed job is '{status}' after triage ({reason}); "
                                 "today's rules reject the fixture JD")
        return s, db
    return s, DB(s.path("db"))


def _llm(args, s, db):
    if args.mock:
        return MockLLM()
    try:
        return Router(s, db, only=getattr(args, "provider", None))
    except ValueError as e:
        _fail(str(e))


_status = gates.verdict_status
_verdict_fields = gates.verdict_fields


def triage_new(db: DB, s) -> dict[str, int]:
    fb = load_yaml(s.path("fact_bank"))
    gates.load_roles(s, fb)                   # fail fast on a renamed project id
    idx = sponsors.load_index(db)
    counts = {"triaged": 0, "skipped": 0, "needs_jd": 0, "needs_country": 0}
    for job in db.jobs("new"):
        f = _verdict_fields(gates.triage(dict(job), s, idx, fb))
        db.update_job(job["id"], **f)
        counts[f["status"]] += 1
    return counts


PRE_TAILOR = ("new", "triaged", "skipped", "needs_jd", "needs_country")
RULES_SKIP = "new rules would skip: "


def retriage(db: DB, s) -> dict[str, int]:
    """Re-run triage on every non-applied job (M4b back-fill of family/tier/ceiling_pre).
    Pre-tailoring rows get a fresh verdict. Post-tailoring rows keep their status and gain one
    'new rules would skip: …' flag if today's rules would reject them. `applied` rows are never touched."""
    fb = load_yaml(s.path("fact_bank"))
    gates.load_roles(s, fb)
    idx = sponsors.load_index(db)
    counts = {"triaged": 0, "skipped": 0, "needs_jd": 0, "needs_country": 0, "post_tailor": 0, "post_tailor_would_skip": 0}
    with db.conn:
        for job in db.jobs():
            if job["status"] == "applied":
                continue
            v = gates.triage(dict(job), s, idx, fb)
            if job["status"] in PRE_TAILOR and not job["out_dir"]:     # a tailored job skipped in review is post-tailoring
                f = _verdict_fields(v)
                db.update_job(job["id"], commit=False, **f)
                counts[f["status"]] += 1
                continue
            flags = [x for x in json.loads(job["flags"] or "[]") if not x.startswith(RULES_SKIP)]
            if not v.passed:
                flags.append(RULES_SKIP + "; ".join(v.reasons))
                counts["post_tailor_would_skip"] += 1
            db.update_job(job["id"], commit=False, family=v.family, tier=v.tier, ceiling_pre=v.ceiling_pre,
                          flags=flags, sponsor_match=v.sponsor_match, sponsor_route=v.sponsor_route,
                          sponsor_rating=v.sponsor_rating)
            counts["post_tailor"] += 1
    counts["applied_untouched"] = len(db.jobs("applied"))
    return counts


def cmd_sponsors(args):
    s, db = _ctx(args)
    try:
        snap = sponsors.take_snapshot(db, s)
    except sponsors.RegisterFormatError as e:
        _fail(str(e))
    print(sponsors.summary_line(snap, db))
    where = str(snap.path) if snap else (db.conn.execute(
        "SELECT path FROM register_snapshots ORDER BY day DESC LIMIT 1").fetchone() or [None])[0]
    print(f"Snapshots are stored in {s.path('register_dir')}" + (f" (latest: {where})" if where else ""))


def cmd_discover(args):
    """`discover` with a source flag (--ats --reed --adzuna --gmail-alerts) writes ads to data/jds/inbox/; --report prints the counts;
    --gmail-auth signs in to Google once (Gmail read, Gmail drafts, Sheets; never sends). With none of them it keeps its older job: the boards in sources: into the
    database, then triage (what `run` and cron call)."""
    # Alerts first: they are the fresh jobs, and a run takes at most discover.max_new_per_run new files; the boards (older ads) fill what is left.
    sources_asked = [n for n in ("gmail_alerts", "reed", "adzuna", "ats", "jobbank") if getattr(args, n, False)]
    if getattr(args, "report", False) or getattr(args, "gmail_auth", False) or sources_asked:
        return _discover_inbox(args, sources_asked)
    from .discovery import discover as disc
    from .discovery import registry as registry_mod

    s, db = _ctx(args)
    new = seen = 0
    run = disc.Run()
    try:
        reg = registry_mod.load(s)
    except registry_mod.RegistryError as e:
        _fail(str(e))
    for label, job in sources.fetch_all(s):
        if isinstance(job, Exception):
            print(f"  ! {label}: {job}")
            continue
        seen += 1
        added = db.upsert_job(job)
        new += added
        if added:
            src = reg.label_source(label)
            run.credit(src.id if src else "other", "new")
    print(f"Fetched {seen} postings, {new} new.")
    print("Triage:", triage_new(db, s))
    _print_health(s, reg, run=run)


def _discover_inbox(args, sources_asked: list[str]) -> None:
    from .discovery import applykind
    from .discovery import discover as disc
    from .discovery import gmail
    from .discovery.http import Fetcher
    from .discovery.robots import RobotsCache

    s, db = _ctx(args)
    if getattr(args, "report", False) and not sources_asked and not getattr(args, "gmail_auth", False):
        print(disc.format_report(s))                    # --report alone: counts only; with a source flag it runs the sources first (below)
        return
    if getattr(args, "gmail_auth", False):
        try:
            gmail.authorize(s)
        except gmail.GmailAuthError as e:
            _fail(str(e))
        return
    fetcher = Fetcher(RobotsCache(s.path("robots")), never=applykind.NEVER_FETCH)       # linkedin, indeed, cv-library and Workday: never asked
    d = disc.Discover(s, db, fetcher, fb=load_yaml(s.path("fact_bank")), dry_run=getattr(args, "dry_run", False),
                      limit=getattr(args, "limit", None))
    steps = {"ats": ("ats", d.from_boards), "reed": ("reed", d.from_reed), "adzuna": ("adzuna", d.from_adzuna),
             "gmail_alerts": ("gmail", d.from_gmail), "jobbank": ("jobbank", d.from_jobbank)}
    for name in sources_asked:
        label, fn = steps[name]
        print(f"discover: {label} ...")
        try:
            fn()
        except Exception as e:  # noqa: BLE001 — one source failing must not lose what the others found
            d.run.notes.append(f"{label}: {type(e).__name__}: {e}")
            d.run.failures.append(f"{label}: {type(e).__name__}: {e}")
            print(f"  ! {label}: {e}")
        c = d.run.sources.get(label, {})
        print("  " + (", ".join(f"{k} {v}" for k, v in c.items() if v) or "nothing found"))
        if label == "ats":
            print("\n".join(d.ats_report()))
    for n in d.run.notes:
        print(f"  note: {n}")
    d.finish()
    if any(c.get("resolved") or c.get("unresolved") for c in d.run.by_source.values()):
        print("\n".join(disc.resolution_lines(d.run.by_source, "Alerts resolved to the employer's page (per source):")))
        print("\n".join(disc.step_lines(d.run.sources, "Alerts resolved by step:")))
    alert_check = d.board_check_lines(d.lookup.findings)
    if alert_check:
        print("\n".join(alert_check).replace("ats: ", "alerts: ", 1))
    _print_health(s, d.registry, run=d.run, fetcher=fetcher, cache=d.cache, save=not d.dry_run)
    lines = d.listing()
    print(f"\n{len(d.run.written)} new ad(s) {'would be ' if d.dry_run else ''}written to {d.inbox}" + (", best first:" if lines else "."))
    print("\n".join(lines))
    if len(d.run.written) > len(lines):
        print(f"  (the top {len(lines)} of {len(d.run.written)} by freshness tier, then priority P = F x S)")
    queued = sum(c.get("to_manual_queue", 0) for c in d.run.sources.values())
    over = sum(c.get("to_manual_queue_overflow", 0) for c in d.run.sources.values())
    if queued or over:
        print(f"{queued + over} link(s) could not be resolved to a full ad: the best {queued} are in {disc.manual_queue_path(s)} "
              "(copy each ad and run `careerops live --clipboard`)" + (f", {over} more in {disc.overflow_path(s)}" if over else ""))
    if any(c.get("role_filtered") or c.get("place_filtered") for c in d.run.sources.values()) and not d.dry_run:
        print(f"Titles the role / place filters turned away, with the reason: {disc.filtered_path(s, disc.firstseen.day(d.today))}")
    if not d.dry_run:
        disc.save_run(s, d.run)
    if d.run.written and not d.dry_run:
        print("Next: careerops live   (works through the inbox best priority first)")
    if getattr(args, "report", False):
        print("\n" + disc.format_report(s))             # the counts per source and the freshness tiers, after this run
    if d.run.failures:                                  # a scheduled run (scripts/discover.sh) must show as failed
        print("\nFAILED: " + "; ".join(d.run.failures[:5]), file=sys.stderr)
        sys.exit(1)


def _print_health(s, reg, *, run=None, fetcher=None, cache=None, save: bool = True) -> list:
    """The health table of the sources (discovery/health.py), printed and saved to data/discover/health/<date>.csv. A source that
    fails shows in its row; the check as a whole never stops the command that called it."""
    from .discovery import health
    from .discovery.http import Fetcher
    from .discovery.robots import RobotsCache

    if not len(reg):
        print("\nSource health: no sources in config/sources.yaml.")
        return []
    print("\nSource health:")
    try:
        rows = health.check_all(s, reg, fetcher or Fetcher(RobotsCache(s.path("robots"))), run=run, cache=cache, say=print)
        print(health.format_table(rows))
        if save:
            print(f"saved to {health.save(s, rows)}")
        else:
            print("(dry run: not saved)")
        return rows
    except Exception as e:  # noqa: BLE001
        print(f"  ! the health check failed: {type(e).__name__}: {e}")
        return []


def cmd_sources(args):
    """`sources list` prints the registry (no network); `sources check` runs the health check of every source."""
    from .discovery import registry as registry_mod

    s = load_settings(args.config)
    load_dotenv(s.root / ".env")
    if args.mock:
        s = mockroot.mock_settings(s)
        mockroot.ensure_dirs(s)
    try:
        reg = registry_mod.load(s)
    except registry_mod.RegistryError as e:
        _fail(str(e))
    if args.what == "list":
        for src in reg:
            extra = f" token={src.token}" if src.token else (f" domains={','.join(src.domains)}" if src.domains else "")
            print(f"{src.id:16s} {src.method:13s} {src.country:5s} {'' if src.enabled else '(disabled) '}{src.name}{extra}")
        print(f"{len(reg)} source(s) in {s.path('sources')}")
        return
    rows = _print_health(s, reg)
    if rows and all(r.status in ("blocked", "unreachable", "error") for r in rows if r.source.enabled):
        sys.exit(1)


def cmd_set_country(args):
    """Give a job the place its work is in (W; a country or a region such as EU): used when the status is needs_country, or when the guess
    is wrong. Wins over the ad and survives a re-triage; the job is triaged again if it was waiting for it. Changes nothing else."""
    from .discovery import countries, workcountry

    s, db = _ctx(args)
    job = db.job(args.id)
    if job is None:
        _fail(f"no such job: {args.id}")
    code = countries.place_code(args.country)
    if not code:
        _fail(f"{args.country!r} is not a country or region this tool knows: use a code or name such as DE, Ireland, EU")
    if job["status"] == "applied":
        _fail(f"{args.id} is already applied: its record is left as it is")
    db.update_job(args.id, work_country=code, work_evidence=f"{workcountry.SET_BY_YOU}: set-country {args.country}")
    home = workcountry.rules_from(workcountry.applicant_visa(s)).home
    print(f"{args.id}: work location set by you" + ("." if code == home else f": {countries.place_name(code)}."))    # home is never printed
    if job["status"] == "needs_country":
        db.update_job(args.id, status="new")
        print("Triage:", manual.triage_job(db, s, args.id))


def cmd_retriage(args):
    s, db = _ctx(args)
    print("Retriage:", retriage(db, s))


def cmd_add_jd(args):
    s, db = _ctx(args)
    try:
        jd = manual.read_jd(args.file)[0] if args.file else sys.stdin.read()
    except manual.JDError as e:
        _fail(str(e))
    if args.id:
        if db.job(args.id) is None:
            _fail(f"no such job: {args.id}")
        db.update_job(args.id, description=jd, status="new")
        print(f"Updated JD for {args.id}")
    else:
        if not (args.company and args.title):
            _fail("add-jd needs --company and --title (or --id to replace the JD of an existing job)")
        try:
            jid = manual.add_jd(db, jd, company=args.company, title=args.title, url=args.url, location=args.location)
        except (manual.JobExists, manual.JDError) as e:
            _fail(str(e))
        print(f"Added {jid}")
    print("Triage:", triage_new(db, s))


def cmd_add_url(args):
    """Fetch one job ad from its link into data/jds/inbox/ (a .txt with Company/Title/URL headers); `live` does the rest."""
    from .discovery import intake

    s = load_settings(args.config)
    try:
        path, created, ad = intake.add_url(s, args.url, args.company, args.title)
    except intake.IntakeError as e:
        _fail(str(e))
    print(f"{'Wrote' if created else 'Already in the inbox, left as it is:'} {path}\n"
          f"  {ad.company} | {ad.title} | {ad.location or 'location not stated'} | {len(ad.text):,} characters ({ad.source})\n"
          "Next: careerops live" + ("" if created else f"   (or: careerops live --url {args.url})"))


def _finish_letters(db: DB, s, fb: dict, llm, only: str | None = None) -> None:
    """Write the letters of jobs waiting as `letter_pending` (their CV is done; the provider allowed to write letters had
    no budget). Each is tried in turn; one that still cannot be written stays pending, untouched. A letter is never
    written by a provider the task excludes, so this either succeeds on the right one or waits."""
    from .review.workflow import load_report
    from .tailoring.tailor import letter_summary, regenerate_letter

    waiting = [j for j in db.jobs("letter_pending") if only is None or j["id"] == only]
    if not waiting:
        return
    est = s["llm"].get("letter_token_estimate", 3000)
    print(f"{len(waiting)} job(s) waiting for a letter.")
    for job in waiting:
        print(f"- {job['company']} | {job['title']}")
        try:
            llm.begin_job(est, task="cover_letter")        # the budget of the model that writes letters
        except BudgetExhausted as e:
            print(f"  STOP: no provider has the daily budget for a letter (~{est:,} tokens): {e}\n"
                  "  the remaining jobs stay letter_pending; re-run after the budget resets (00:00 UTC).")
            return
        try:
            rep = regenerate_letter(dict(job), fb, llm, s, load_report(job))
            if rep["letter"]["status"] == "pending":
                print(f"  still pending: {letter_summary(rep['letter'])}")
                continue
            db.update_job(job["id"], status="needs_review", letter_score=rep["letter"]["score"])
            db.log(job["id"], "tailor", {"letter": rep["letter"]["status"], "finished_pending_letter": True})
            print(f"  letter {letter_summary(rep['letter'])} -> needs_review")
        except Exception as e:  # noqa: BLE001 — one job must not stop the others
            db.log(job["id"], "error", traceback.format_exc())
            print(f"  letter NOT written: {e}")
        finally:
            llm.end_job()


def tailor_jobs(db: DB, s, fb: dict, llm, jobs: list, *, one_page: bool = False, mock: bool = False,
                re_extract: bool = False) -> None:
    """Tailor these jobs one after the other (the loop `careerops tailor` and `careerops live` share). Each job ends as
    needs_review, letter_pending, error, or — when the day's token budget is short — stays as it was. A job's keywords
    are extracted once and kept in its record: a job tailored again reuses them (no extract_keywords call, so the same
    keyword list and ceiling) unless `re_extract` (--re-extract) or its JD has changed."""
    from .tailoring.freeze import frozen_for
    from .tailoring.tailor import letter_summary, tailor_job

    print(f"Tailoring {len(jobs)} job(s){' with MockLLM' if mock else ''}.")
    est = s["llm"].get("job_token_estimate", 10000)
    for n, job in enumerate(jobs):
        print(f"- {job['company']} | {job['title']}")
        try:
            using = llm.begin_job(est)       # daily token budget check: never start a job that can't be finished
        except BudgetExhausted as e:
            print(f"  STOP: not enough daily LLM token budget for a whole job (~{est:,} tokens): {e}\n"
                  f"  {len(jobs) - n} job(s) left untouched in 'triaged'. Nothing was half-tailored. Re-run after the "
                  "budget resets (00:00 UTC) or start Ollama.")
            break
        names = [p.name for p in getattr(llm, "providers", [])]
        if names and set(using) != set(names):
            print(f"  daily token budget: this job uses {', '.join(using)} only")
        frozen, why_not = (None, "") if re_extract else frozen_for(job)
        if frozen:
            print(f"  keywords: reusing the extraction frozen on {frozen['date']} (--re-extract to redo it)")
        elif why_not or (re_extract and "analysis" in job.keys() and job["analysis"]):
            print(f"  keywords: extracting again ({why_not or '--re-extract'})")
        try:
            rep = tailor_job(dict(job), fb, llm, s, s.path("outputs"), frozen, one_page=one_page,
                             on_extracted=lambda rec, jid=job["id"]: db.update_job(jid, analysis=rec))
            f = rep["final"]
            pending = rep["letter"]["status"] == "pending"      # no provider may write the letter now: the CV is done
            no_letter = rep["letter"]["status"] == "none"       # letter.on_failure: none and every draft was rejected
            off = rep["letter"]["status"] == "off"              # letter.mode: off, no letter was asked for
            flags = [x for x in json.loads(job["flags"] or "[]") if not x.startswith(STRETCH_FLAG)]
            if rep.get("stretch"):         # an honest ceiling under the band: kept and labelled, never dropped (and never auto-approved)
                flags.append(f"{STRETCH_FLAG}: honest ceiling {rep['ceiling']:.0%} is under the {s['scoring']['band'][0]:.0%} band")
            db.update_job(job["id"], status="letter_pending" if pending else "needs_review", keywords=rep["keywords"],
                          ceiling=rep["ceiling"], score=f["score"], score_stemmed=f["score_stemmed"],
                          letter_score=rep["letter"]["score"], attempts=len(rep["attempts"]), out_dir=rep["out_dir"],
                          cv_pages=rep["cv_pages"], page2_fill=rep["page2_fill"], flags=flags)
            db.log(job["id"], "tailor", {"score": f["score"], "ceiling": rep["ceiling"], "band": f["band"],
                                         "letter": rep["letter"]["status"]})
            cv_pages, letter_pages = f["pages"], rep["letter"]["pages"]
            max_pages = 1 if rep["one_page"] else s["scoring"]["max_pages"]
            short = (not rep["one_page"] and max_pages == 2
                     and rep["page2_fill"] < s["scoring"]["min_fill_page2"])     # the bullets ran out before page 2 filled
            over = cv_pages > max_pages or (letter_pages != 1 and not pending and not no_letter and not off)
            if rep["letter"]["status"] in ("fallback", "none"):
                print(f"  letter {letter_summary(rep['letter'])}")
            if rep.get("ats"):
                print("  " + ats.AtsScores(**{k: v for k, v in rep["ats"].items() if k in ats.AtsScores.__dataclass_fields__}).line())
            print(f"  ATS {f['score']:.0%} (stemmed {f['score_stemmed']:.0%}, ceiling {rep['ceiling']:.0%}) "
                  f"→ {f['band']} band | CV {cv_pages} page(s)"
                  + (f", page 2 {rep['page2_fill']:.0%} full" + (" (the fact bank has no more content to add)" if short else "")
                     if not rep["one_page"] else "") + ", "
                  + (f"letter {letter_summary(rep['letter'])}: the job waits as letter_pending" if pending
                     else f"{letter_summary(rep['letter'])}: review asks for 'no letter' to approve" if no_letter
                     else f"letter {letter_summary(rep['letter'])}" if off
                     else f"letter {letter_pages} page(s)")
                  + (f"  !! expected {max_pages} and 1" if over else ""))
            u = rep["llm_usage"]
            if u["calls"]:
                print(f"  LLM tokens: {u['total_tokens']:,} (prompt {u['prompt_tokens']:,}, completion "
                      f"{u['completion_tokens']:,}, of which reasoning {u['reasoning_tokens']:,}) in {len(u['calls'])} calls")
                for task, t in u["by_task"].items():
                    per_call = "/".join(f"{n:,}" if n is not None else "?" for n in t["prompt_tokens_per_call"])
                    print(f"    {task}: {t['calls']} call(s), prompt tokens {per_call}, total {t['total_tokens']:,}")
        except Exception as e:  # noqa: BLE001 — one bad job must not kill the cron run
            db.update_job(job["id"], status="error", reason=str(e)[:500])
            db.log(job["id"], "error", traceback.format_exc())
            print(f"  ERROR: {e}")
        finally:
            llm.end_job()


def cmd_tailor(args):
    s, db = _ctx(args)
    llm = _llm(args, s, db)
    fb = load_yaml(s.path("fact_bank"))
    gates.load_roles(s, fb)
    if args.id:                                    # exactly this job: its status must say tailoring is what it waits for
        job = db.job(args.id)
        if job is None:
            _fail(f"no such job: {args.id}")
        if job["status"] == "letter_pending":
            _finish_letters(db, s, fb, llm, only=args.id)
            return
        if job["status"] != "triaged":
            _fail(f"{args.id} is '{job['status']}', not 'triaged' (nothing to tailor)")
        jobs = [job]
    else:
        _finish_letters(db, s, fb, llm)
        jobs = select_for_run(db.jobs("triaged"), args.limit, s)
    tailor_jobs(db, s, fb, llm, jobs, one_page=getattr(args, "one_page", False), mock=args.mock,
                re_extract=getattr(args, "re_extract", False))


def cmd_review(args):
    s, db = _ctx(args)
    review.run(db, s, load_yaml(s.path("fact_bank")), args.limit, make_llm=lambda: _llm(args, s, db))


def _fail(msg: str) -> None:
    print(f"ERROR: {msg}", file=sys.stderr)
    sys.exit(1)


def _archive(db: DB, s, job, rep: dict, confirmation: str | None, applied_on, no_letter: bool) -> bool:
    """Archive a submitted application and mark it applied, then draft the outreach and update the tracker row. True if fully archived;
    problems are printed. The job is applied whatever the archive says, so the follow-ups run either way."""
    ok = _archive_files(db, s, job, rep, confirmation, applied_on, no_letter)
    _after_applied(db, s, job["id"])
    return ok


def _after_applied(db: DB, s, job_id: str) -> None:
    """Outreach drafts (outreach.mode: drafts), then the tracker row (tracker.sheet_id). Neither can undo or block the application
    record: a failure is printed and the application stays recorded. Never runs on the mock root (no network there)."""
    if mockroot.is_mock(s):
        return
    from . import contacts, outreach, tracker

    job = db.job(job_id)
    if outreach.drafts_on(s):
        try:
            contacts.discover(db, s, job)
            res = outreach.draft_for_job(db, s, job)
            print(f"  outreach: {res.status}" + "".join(f"\n    no draft to {a}: {w}" for a, w in res.skipped))
        except Exception as e:  # noqa: BLE001  (Google, DNS, a missing file: the application is already recorded)
            print(f"  outreach: no drafts made ({e})", file=sys.stderr)
    if tracker.configured(s):
        try:
            print(f"  tracker: row {tracker.sync(db, s, [job_id])[job_id]}")
        except Exception as e:  # noqa: BLE001
            print(f"  tracker: not updated ({e}); `careerops tracker sync` sends it again", file=sys.stderr)


def _archive_files(db: DB, s, job, rep: dict, confirmation: str | None, applied_on, no_letter: bool) -> bool:
    """Archive a submitted application and mark it applied. True if fully archived; problems are printed."""
    try:
        res = record_application(db, s, job, rep, confirmation=confirmation, applied_on=applied_on,
                                 no_letter=no_letter)
    except ArchiveIncomplete as e:
        print(f"  APPLIED, but the archive is incomplete — finish by hand: {e}", file=sys.stderr)
        return False
    except ArchiveError as e:                      # refused before writing; the application itself was submitted
        mark_applied_unarchived(db, job, confirmation, str(e), applied_on)
        print(f"  APPLIED, but NOT archived: {e}", file=sys.stderr)
        return False
    print(f"  marked applied; archived to {res.folder}")
    for f in res.flags:
        print(f"  FLAG: {f}")
    return True


def cmd_live(args):
    """Interactive batch: every JD in data/jds/inbox through add-jd -> tailor -> review -> apply assist, one job at a time."""
    from .apply.assist import assist
    from .apply.live import Hooks, Live

    if args.yes:
        _fail("live never answers a gate for you: --yes is not available (approve, skip and 'did you submit' are yours)")
    if args.max < 1:
        _fail("--max must be at least 1")
    if args.url and args.mock:
        _fail("--url fetches the ad over the network; --mock never uses the network (put a JD file in the mock inbox instead)")
    if args.url and args.clipboard:
        _fail("--url and --clipboard are two ways to get one ad into the inbox: use one of them")
    s, db = _ctx(args)
    only = None
    if args.url:
        from .discovery import intake
        try:
            only, created, ad = intake.add_url(s, args.url, args.company, args.title)
        except intake.IntakeError as e:
            _fail(str(e))
        print(f"{'Fetched' if created else 'Already in the inbox:'} {only.name}  ({ad.company} | {ad.title})")
    elif args.clipboard:
        from .discovery import intake
        try:
            only, created, ad = intake.add_clipboard(s, input)
        except intake.IntakeError as e:
            _fail(str(e))
        print(f"{'Saved' if created else 'Already in the inbox:'} {only.name}  ({ad.company} | {ad.title}, "
              f"{len(ad.text):,} characters from the clipboard)")
    dry_run = args.dry_run or args.mock
    if args.mock and not args.dry_run:
        print("--mock implies --dry-run: the mock data root has no real application form to open.")
    llm = _llm(args, s, db)
    fb = load_yaml(s.path("fact_bank"))
    gates.load_roles(s, fb)

    def tailor(jid: str) -> None:
        job = db.job(jid)
        if job["status"] == "letter_pending":
            _finish_letters(db, s, fb, llm, only=jid)
        else:
            tailor_jobs(db, s, fb, llm, [job], mock=args.mock, re_extract=getattr(args, "re_extract", False))

    def review_it(jid: str) -> str:
        return review.review_job(db, db.job(jid), s, fb, make_llm=lambda: _llm(args, s, db), allow="aelgnowsq",
                                 skip_means_no=True)           # live has no reject key: a skipped job is a "no" for the judge log

    def preflight_it(jid: str) -> None:
        job = db.job(jid)
        preflight(db, s, job, review.load_report(job))
        visa_mod.require_plan(job, load_yaml(s.path("applicant")))           # W unknown / no answers for it: refused before a form opens

    def assist_it(jid: str) -> dict:
        job = db.job(jid)
        r = assist(job, review.load_report(job), load_yaml(s.path("applicant")), s.path("browser_profile"))
        db.log(jid, "apply", r)
        return r

    def mark_applied_it(jid: str, confirmation: str | None) -> bool:
        job = db.job(jid)
        ok = _archive(db, s, job, review.load_report(job), confirmation, None, False)
        export(db, s.path("excel"), floor=s["roles"]["floor"])
        return ok

    resume = ("careerops" + (f" --config {args.config}" if args.config else "") + f" live --max {args.max}"
              + (f" --url {args.url}" if args.url else "")      # no --clipboard here: that job is in the inbox now
              + (" --fresh" if getattr(args, "fresh", False) else "")
              + (" --dry-run" if args.dry_run else "") + (" --mock" if args.mock else ""))      # resume: not --retailor
    code = Live(db, s, Hooks(ask=input, tailor=tailor, review=review_it, preflight=preflight_it, assist=assist_it,
                             mark_applied=mark_applied_it),
                max_jobs=args.max, dry_run=dry_run, resume_cmd=resume, retailor=args.retailor, only=only,
                fresh=getattr(args, "fresh", False)).run()
    if code:
        sys.exit(code)


def cmd_apply(args):
    from .apply.assist import assist

    s, db = _ctx(args)
    applicant = load_yaml(s.path("applicant"))
    jobs = [db.job(args.id)] if args.id else order(db.jobs("approved"), s, tailored=True)[: args.limit]
    failures = 0
    for job in jobs:
        if job is None:
            continue
        rep = review.load_report(job)
        try:                                       # refuse before a form is opened: letter, right files, root, paths
            preflight(db, s, job, rep, no_letter=args.no_letter)
            visa_mod.require_plan(job, applicant)                  # W unknown / no answers for it: no form is opened
        except ArchiveError as e:
            print(f"  REFUSED {job['id']}: {e}", file=sys.stderr)
            failures += 1
            continue
        print(review.summary(job, rep, s))
        if input("Open application form? [y/N] ").strip().lower() != "y":
            continue
        r = assist(job, rep, applicant, s.path("browser_profile"))
        if r["confirmation"].lower() == "x":
            db.log(job["id"], "apply", {"aborted": True, **r})
            continue
        db.log(job["id"], "apply", r)
        if not _archive(db, s, job, rep, r["confirmation"] or None, None, args.no_letter):
            failures += 1
    export(db, s.path("excel"), floor=s["roles"]["floor"])
    if failures:
        sys.exit(1)


def cmd_mark_applied(args):
    """Record an application made outside the assist (by hand, by email, on a portal Playwright can't drive)."""
    s, db = _ctx(args)
    job = db.job(args.id)
    if job is None:
        _fail(f"no such job: {args.id}")
    if not job["out_dir"] or not (Path(job["out_dir"]) / "report.json").exists():
        _fail(f"{args.id} has no tailored CV/letter (run `careerops tailor` first)")
    rep = review.load_report(job)
    try:
        preflight(db, s, job, rep, applied_on=args.date, no_letter=args.no_letter)
    except ArchiveError as e:                      # nothing submitted by us, nothing changed: just refuse
        _fail(str(e))
    ok = _archive(db, s, job, rep, args.ref or None, args.date, args.no_letter)
    export(db, s.path("excel"), floor=s["roles"]["floor"])
    if not ok:
        sys.exit(1)


def cmd_outcome(args):
    """Log what happened after an application (screen, interview, offer, rejection ...). Nothing is sent anywhere."""
    from .reporting import outcomes

    s, db = _ctx(args)
    if db.job(args.id) is None:
        _fail(f"no such job: {args.id}")
    day = args.date or date.today()
    if outcomes.record(db, args.id, args.event, day, args.note or "", "manual"):
        print(f"Logged {args.event} for {args.id} on {day.isoformat()}.")
    else:
        print(f"Already logged: {args.event} for {args.id} on {day.isoformat()}.")


def cmd_outcomes(args):
    """The funnel (applied -> any reply -> screen -> interview -> offer), or, with --import-archive, the read-only backfill
    from the archive: a dry run listing what would be added, unless --apply (which writes to the database only)."""
    from .reporting import outcomes

    s, db = _ctx(args)
    if args.apply and not args.import_archive:
        _fail("--apply goes with --import-archive")
    if args.import_archive:
        try:
            props, skipped = outcomes.propose_from_archive(db, s.path("applications"))
        except outcomes.OutcomeError as e:
            _fail(str(e))
        if skipped:
            print(f"{len(skipped)} folder(s) already have a job and are skipped.")
        for p in props:
            print(f"  {p.company} / {p.role}: applied ~{p.applied_on.isoformat()} (folder date, approximate)")
        if args.apply:
            print(f"Imported {outcomes.apply_proposals(db, props)} application(s) into the database "
                  "(the archive was only listed).")
        else:
            print(f"Dry run: {len(props)} application(s) would be imported. Re-run with --apply to write them to the "
                  "database (the archive itself is never written).")
        return
    print(outcomes.format_report(outcomes.build_report(db)))


def cmd_export(args):
    s, db = _ctx(args)
    n = export(db, s.path("excel"), floor=s["roles"]["floor"])
    print(f"Wrote {n} rows to {s.path('excel')}")


def cmd_export_prefs(args):
    s, _ = _ctx(args)
    src = s.path("preferences")
    if not src.exists():
        print("No preference data yet — review some jobs first.")
        return
    out = Path(args.out)
    n = 0
    with open(src, encoding="utf-8") as f, open(out, "w", encoding="utf-8") as g:
        for line in f:
            row = json.loads(line)
            if row.get("kind") != "edit_pair":
                continue
            kws = ", ".join(k["term"] for k in row["keywords"][:30])
            prompt = (f"Reword this CV bullet to mirror the job's wording without adding facts.\n"
                      f"JD keywords: {kws}\nBullet: {row['source']}")
            g.write(json.dumps({"prompt": prompt, "chosen": row["chosen"], "rejected": row["rejected"]}) + "\n")
            n += 1
    print(f"Wrote {n} DPO pairs to {out}")


def cmd_run(args):
    if args.mock:
        print("Discovery skipped under --mock (no network); using the seeded fixture job.")
    else:
        cmd_discover(args)
    cmd_tailor(args)
    cmd_export(args)


def cmd_init(args):
    s = load_settings(args.config)
    made = init(s)
    DB(s.path("db"))                      # creates the schema
    print("Created:\n  " + "\n  ".join(made) if made else "Everything already in place.")


def cmd_doctor(args):
    s = load_settings(args.config)
    load_dotenv(s.root / ".env")
    checks = doctor(s, ping=not getattr(args, "no_ping", False))
    for name, ok, hint in checks:
        tag = "ok" if ok is True else "warn" if ok == "warn" else "!!"
        print(f"  [{tag}] {name}" + ("" if ok is True else f"  → {hint}"))
    sys.exit(0 if all(ok for _, ok, _ in checks) else 1)           # a warning ("warn") does not fail the run


def cmd_llm_check(args):
    from .llm import check

    s, db = _ctx(args)
    rows = check.check_all(Router(s, db))
    for r in rows:
        print("\n".join(check.format_row(r)))
    sys.exit(0 if any(r["ok"] for r in rows) else 1)


def cmd_calibrate(args):
    from .reporting import calibrate as cal

    s, db = _ctx(args)
    # --mock moves every generated-data path (reports included) under data/mock; the manifest stays where the user keeps it
    manifest = Path(args.manifest) if args.manifest else load_settings(args.config).path("calibration") / "manifest.yaml"
    out = Path(args.out) if args.out else s.path("calibration")
    if args.provider and args.mock:
        _fail("--provider pins a real provider; --mock uses no provider")
    try:
        if args.summarise:
            stale = cal.rubric_predates_run(out)
            merged = cal.summarise(out)
            if stale:
                print(cal.STALE_BANNER)
            print(f"Merged the rubric into {merged}")
            return
        if args.freeze_keywords:
            kdir = s.path("frozen_keywords")
            res = cal.freeze_keywords(s, _llm(args, s, db), manifest, kdir, only=args.only, limit=args.limit,
                                      force=args.force)
            print(f"Froze {len(res['written'])} JD(s)" + (f"; {len(res['errors'])} not frozen" if res["errors"] else "")
                  + f". Every later `careerops calibrate` reads them from {kdir}.")
            if res["errors"]:
                sys.exit(1)
            return
        res = cal.calibrate(s, db, _llm(args, s, db), load_yaml(s.path("fact_bank")), manifest, out, only=args.only,
                            limit=args.limit, compare_to=Path(args.compare) if args.compare else None)
    except cal.ManifestError as e:
        _fail(str(e))
    print(f"Wrote {res['report']}\n      {res['json']}\n      {res['rubric']} (rate the CVs and letters, then "
          "`careerops calibrate --summarise`)")
    if res["aggregates"]["errors"] or res["aggregates"]["not_run"]:
        sys.exit(1)


def cmd_companies(args):
    from .discovery import ats, companies_house

    s, db = _ctx(args)
    if args.snapshot:
        cmd_sponsors(args)
    elif args.refresh_ch:
        print(companies_house.join(db, s, companies_house.download_bulk(s)))
    elif args.resolve:
        from .discovery.http import Fetcher
        from .discovery.robots import RobotsCache
        print(ats.resolve(db, s, limit=args.limit, fetcher=Fetcher(RobotsCache(s.path("robots")))))
    elif args.set:
        from .discovery.http import Fetcher
        from .discovery.robots import RobotsCache
        print(ats.detect(args.url, fetcher=Fetcher(RobotsCache(s.path("robots")))))
    else:   # --queue (default)
        raise NotImplementedError("M3 — manual queue listing")


def cmd_import_linkedin(args):
    from .tailoring import linkedin

    s = load_settings(args.config)
    if args.mock:
        s = mockroot.mock_settings(s)          # the proposal and the diff go under data/mock; the fact bank stays the real one, read-only
    pdf = Path(args.pdf)
    if not pdf.is_file():
        _fail(f"{pdf} not found")
    fact_bank = Path(args.fact_bank) if args.fact_bank else s.path("fact_bank")
    out = Path(args.out) if args.out else s.path("fact_bank_proposed")
    diff = Path(args.diff) if args.diff else s.path("linkedin_diff")
    try:
        r = linkedin.import_linkedin(pdf, fact_bank, out, diff)
    except linkedin.LinkedInError as e:
        _fail(str(e))
    p = r["parsed"]
    print(f"LinkedIn: {len(p['experience'])} experience, {len(p['education'])} education, {len(p['projects'])} projects, "
          f"{len(p['certifications'])} certifications, {len(p['publications'])} publications, {len(p['languages'])} languages")
    for w in r["warnings"]:
        print(f"  ! {w}")
    print(f"vs {fact_bank.name}: {r['matched']} match, {r['mismatches']} mismatches, {r['unverified']} unverified "
          f"(the PDF holds only part of that section)")
    print(f"proposal -> {out}")
    print(f"mismatches -> {diff}")
    print(f"{fact_bank.name} was not modified: merge by hand.")


def cmd_learning(args):
    from .tailoring import learning

    _, db = _ctx(args)
    for g in learning.aggregate(db)[: args.top]:
        print(f"{g.priority:6.1f}  {g.term}  (in {g.jobs} jobs, required in {g.required_in})")


def cmd_contact(args):
    """`contact add JOB "First Last"`: a person you found yourself; `contact list JOB`. Nothing is sent."""
    from . import contacts, tracker

    s, db = _ctx(args)
    job = db.job(args.job)
    if job is None:
        _fail(f"no such job: {args.job}")
    if args.what == "list":
        rows = contacts.stored(db, args.job)
        print("\n".join(contacts.describe(r, s) for r in rows) if rows else f"No contacts stored for {args.job}.")
        return
    if not args.name:
        _fail('contact add needs a name: careerops contact add JOB "First Last" --role "Hiring Manager"')
    try:
        c = contacts.add(db, s, job, args.name, args.role or "Hiring Manager", args.email or "", args.domain or "")
    except (contacts.ContactError, OSError) as e:
        _fail(str(e))
    print("Stored: " + contacts.describe({"name": c.name, "role": c.role, "email": c.email, "source": c.source,
                                          "mx_status": c.mx_status, "hunter_status": c.hunter_status}, s))
    for n in c.notes:
        print(f"  note: {n}")
    if job["status"] == "applied" and tracker.configured(s):
        try:
            tracker.sync(db, s, [args.job])
            print("  tracker row updated")
        except Exception as e:  # noqa: BLE001
            print(f"  tracker: not updated ({e})", file=sys.stderr)


def cmd_tracker(args):
    """`tracker sync`: every applied job (or --id) to the Google Sheet; --dry-run prints the rows and uses no network."""
    from . import tracker

    s, db = _ctx(args)
    ids = [args.id] if args.id else None
    if args.dry_run:
        rows = [db.job(j) for j in ids] if ids else db.jobs("applied")
        fb = load_yaml(s.path("fact_bank"))
        rows = [r for r in rows if r is not None and r["applied_at"]]
        for r in rows:
            print(f"--- {r['id']}")
            for h, v in tracker.record(db, s, r, fb).items():
                text = " ".join(str(v).split())
                print(f"  {h}: {text[:90] + ('...' if len(text) > 90 else '')}")
        print(f"{len(rows)} row(s) would be sent; nothing was.")
        return
    if args.id and (db.job(args.id) is None or not db.job(args.id)["applied_at"]):
        _fail(f"{args.id} is not an applied job")
    try:
        res = tracker.sync(db, s, ids)
    except tracker.TrackerError as e:
        _fail(str(e))
    for jid, what in res.items():
        print(f"  {what}: {jid}")
    print(f"Tracker: {len(res)} job(s), " + ", ".join(f"{sum(v == k for v in res.values())} {k}" for k in ("added", "updated", "unchanged")) + ".")


def cmd_outreach(args):
    if args.action == "draft":
        return cmd_outreach_draft(args)
    if not (args.id and args.person and args.title):
        _fail("outreach: use `outreach draft JOB`, or --id JOB --person NAME --title TITLE for the LinkedIn note")
    from .review import outreach

    s, db = _ctx(args)
    job = db.job(args.id)
    out = outreach.draft(dict(job), review.load_report(job), args.person, args.title, args.team, _llm(args, s, db))
    print(f"NOTE ({len(out.note)} chars):\n{out.note}\n\nFOLLOW-UP:\n{out.message}")


def cmd_outreach_draft(args):
    """`outreach draft JOB [--to ADDRESS]`: Gmail drafts, never sent. With --to, one test draft to that address, nothing recorded."""
    from . import contacts, outreach, tracker

    if args.mock:
        _fail("outreach draft talks to Gmail; --mock never uses the network")
    if not args.job_id:
        _fail("outreach draft needs a job id: careerops outreach draft JOB [--to ADDRESS]")
    s, db = _ctx(args)
    job = db.job(args.job_id)
    if job is None:
        _fail(f"no such job: {args.job_id}")
    try:
        if not args.to:
            contacts.discover(db, s, job)
        res = outreach.draft_for_job(db, s, job, to=args.to)
    except Exception as e:  # noqa: BLE001  (OutreachError, GoogleError, GmailAuthError, DNS: one line)
        _fail(str(e))
    for addr, did in res.drafted:
        print(f"Draft created in Gmail for {addr} (draft id {did}). Nothing was sent: open Drafts, read it, send it yourself.")
    for addr, why in res.skipped:
        print(f"  no draft to {addr}: {why}")
    print(f"Outreach: {res.status}")
    if not res.test and job["status"] == "applied" and tracker.configured(s):
        try:
            tracker.sync(db, s, [args.job_id])
            print("  tracker row updated")
        except Exception as e:  # noqa: BLE001
            print(f"  tracker: not updated ({e})", file=sys.stderr)
    if not res.drafted:
        sys.exit(1)


def llm_usage_lines(s, db: DB) -> list[str]:
    """Per provider: tokens and requests used today against tpd / rpd (the router's own count, UTC day)."""
    out = []
    for p in s["llm"]["providers"]:
        tok, req = db.llm_tokens_today(p["name"]), db.llm_calls_today(p["name"])
        tpd, rpd = p.get("tpd"), p.get("rpd")
        t = f"{tok:,} / {tpd:,} tokens ({tok / tpd:.0%})" if tpd else f"{tok:,} tokens (no tpd set)"
        r = f"{req:,} / {rpd:,} requests" if rpd else f"{req:,} requests"
        out.append(f"  {p['name']:8s} {t:34s} {r}")
    return out


def cmd_judge_report(args):
    """The judge against your decisions: n, agreement, Cohen's kappa, false approves, and what gate mode still needs."""
    s, _ = _ctx(args)
    path = judge.log_path(s)
    print(judge.format_report(judge.read_log(path), path))


def priority_lines(db: DB, s, n: int = 10) -> list[str]:
    """The n first jobs still open (not applied, rejected or skipped): freshness tier and age, then P = F x S. An order, not a filter:
    every job is ranked, none is left out."""
    from .discovery import priority as prio

    open_jobs = [j for j in db.jobs() if j["status"] not in ("applied", "rejected", "skipped")]
    ranked = sorted(open_jobs, key=lambda j: prio.sort_key(j, s, tailored=bool(j["out_dir"])))[:n]
    out = []
    for j in ranked:
        p = prio.priority(j, s, tailored=bool(j["out_dir"]))
        out.append(f"  T{p.tier} {p.age():>7}  {p.p:.2f} = {p.f:.2f} x {p.s:.2f}  {j['company']} | {j['title']}  [{j['status']}; {p.s_reason}]")
    return out


def cmd_status(args):
    s, db = _ctx(args)
    print("LLM usage today (UTC):")
    print("\n".join(llm_usage_lines(s, db)) + "\n")
    for st, n in db.conn.execute("SELECT status, COUNT(*) FROM jobs GROUP BY status ORDER BY 2 DESC"):
        print(f"{st:14s} {n}")
    rows = list(db.conn.execute("SELECT COALESCE('T' || tier, '-') AS t, status, COUNT(*) FROM jobs GROUP BY 1, 2"))
    if not rows:
        return
    tiers = sorted({r[0] for r in rows}, key=lambda t: (t == "-", t))
    statuses = sorted({r[1] for r in rows})
    cell = {(r[0], r[1]): r[2] for r in rows}
    print("\ntier  " +" ".join(f"{s:>12s}" for s in statuses))
    for t in tiers:
        print(f"{t:5s} " + " ".join(f"{cell.get((t, s), 0):12d}" for s in statuses))
    top = priority_lines(db, s)
    if top:
        print("\nTop jobs, freshest tier first, then P = F x S (not yet applied):")
        print("\n".join(top))
    archived = list(db.conn.execute(
        "SELECT c.folder, COUNT(*) AS n FROM archive_index r JOIN archive_index c ON c.kind='company' "
        "AND c.folder = CASE WHEN instr(r.folder, '/') > 0 THEN substr(r.folder, 1, instr(r.folder, '/') - 1) "
        "ELSE r.folder END WHERE r.kind='role' GROUP BY c.folder ORDER BY n DESC, c.folder LIMIT 10"))
    if archived:                                   # one row per submission (resubmissions in vN count too)
        print("\nArchived applications (top 10 companies):")
        for company, n in archived:
            print(f"  {n:3d}  {company}")


def main(argv=None):
    ap = argparse.ArgumentParser(prog="careerops", description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--config", default=None, help="path to settings.yaml")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init").set_defaults(fn=cmd_init)
    dr = sub.add_parser("doctor")
    dr.add_argument("--no-ping", action="store_true", help="do not send the llm.models models a tiny request (the one "
                    "check that spends provider quota)")
    dr.set_defaults(fn=cmd_doctor)
    sub.add_parser("llm-check", help="one tiny request per LLM provider: reachable, model, latency, JSON, json_mode"
                   ).set_defaults(fn=cmd_llm_check)
    ca = sub.add_parser("calibrate", help="tailor the calibration JDs with the real model and measure the result")
    ca.add_argument("--manifest", help="default: <paths.calibration>/manifest.yaml")
    ca.add_argument("--out", help="folder for report-<stamp>.md/.json, rubric.csv and runs/ (default: paths.calibration)")
    ca.add_argument("--only", nargs="+", metavar="ID", help="just these manifest ids")
    ca.add_argument("--limit", type=int, help="at most N JDs (the daily token budget is the usual reason)")
    ca.add_argument("--compare", metavar="REPORT", help="an earlier report-<stamp>.md (or .json): show before/after")
    ca.add_argument("--provider", metavar="NAME", help="use only this llm.providers entry (e.g. ollama): no fallback, "
                    "so a run cannot mix providers; the report records which provider and model served every call")
    ca.add_argument("--freeze-keywords", action="store_true", help="extract each JD's keywords once and write "
                    "config/calibration/keywords/<id>.yaml (no tailoring); later runs read them instead of extracting. "
                    "Never overwrites an existing file unless --force")
    ca.add_argument("--force", action="store_true", help="with --freeze-keywords: replace an existing frozen file")
    ca.add_argument("--summarise", action="store_true", help="merge the filled-in rubric.csv into the latest report")
    ca.add_argument("--mock", action="store_true", help="deterministic MockLLM and an isolated data root, no API calls")
    ca.set_defaults(fn=cmd_calibrate)
    sub.add_parser("sponsors").set_defaults(fn=cmd_sponsors)
    c = sub.add_parser("companies")
    g = c.add_mutually_exclusive_group()
    g.add_argument("--snapshot", action="store_true", help="M1: download today's register, diff vs yesterday")
    g.add_argument("--refresh-ch", action="store_true", help="M2: Companies House bulk join + sector filter")
    g.add_argument("--resolve", action="store_true", help="M3: probe ATS boards for in-scope companies")
    g.add_argument("--queue", action="store_true", help="M3: list companies needing a careers URL")
    g.add_argument("--set", metavar="NAME", help="M3: attach a careers URL to a company")
    c.add_argument("--url")
    c.add_argument("--limit", type=int, default=300)
    c.set_defaults(fn=cmd_companies)
    lr = sub.add_parser("learning"); lr.add_argument("--top", type=int, default=25); lr.set_defaults(fn=cmd_learning)
    o = sub.add_parser("outreach", help="`outreach draft JOB [--to ADDRESS]`: Gmail drafts, never sent; or --id --person --title "
                                        "for the LinkedIn note (M4)")
    o.add_argument("action", nargs="?", choices=["draft"])
    o.add_argument("job_id", nargs="?", help="with `draft`: the job")
    o.add_argument("--to", help="with `draft`: one test draft to this address (nothing recorded, no limit used)")
    o.add_argument("--id"); o.add_argument("--person")
    o.add_argument("--title"); o.add_argument("--team")
    o.add_argument("--mock", action="store_true")
    o.set_defaults(fn=cmd_outreach)
    ct = sub.add_parser("contact", help="people to write to about a job: `contact add JOB \"First Last\" --role R [--email E]`, `contact list JOB`")
    ct.add_argument("what", choices=["add", "list"])
    ct.add_argument("job", help="job id")
    ct.add_argument("name", nargs="?", help="with add: \"First Last\" (you found them on LinkedIn; it is never fetched)")
    ct.add_argument("--role", help="default: Hiring Manager")
    ct.add_argument("--email", help="their address if you know it; without it Hunter is asked once (HUNTER_API_KEY)")
    ct.add_argument("--domain", help="the company's domain for Hunter when it cannot be told from the job link")
    ct.set_defaults(fn=cmd_contact)

    tk = sub.add_parser("tracker", help="`tracker sync`: applied jobs to your Google Sheet (one row per job, never duplicated)")
    tk.add_argument("what", choices=["sync"])
    tk.add_argument("--id", help="just this job")
    tk.add_argument("--dry-run", dest="dry_run", action="store_true", help="print the rows, send nothing, use no network")
    tk.set_defaults(fn=cmd_tracker)

    au = sub.add_parser("add-url", help="fetch one job ad from its link into data/jds/inbox/ (Greenhouse, Lever, Ashby, or any "
                                        "page with at least 2000 characters of text)")
    au.add_argument("url")
    au.add_argument("--company", help="use this company name instead of the one the page or API gives")
    au.add_argument("--title", help="use this job title instead of the one the page or API gives")
    au.set_defaults(fn=cmd_add_url)
    jr = sub.add_parser("judge-report", help="LLM judge vs your review decisions: agreement, kappa, false approves")
    jr.add_argument("--mock", action="store_true", help="read the isolated data/mock judge log")
    jr.set_defaults(fn=cmd_judge_report)
    dc = sub.add_parser("discover", help="find open jobs and write them to data/jds/inbox/ (--ats --reed --adzuna --gmail-alerts); "
                                         "with none of these: pull the boards in sources: into the database and triage")
    dc.add_argument("--ats", action="store_true", help="Greenhouse / Lever / Ashby boards of the companies in your archive "
                    "folder, then discover.sources (and register companies among them); prints, per company, the tokens tried and the board found")
    dc.add_argument("--reed", action="store_true", help="the Reed Jobseeker API (REED_API_KEY in .env); agency posts are flagged")
    dc.add_argument("--adzuna", action="store_true", help="the Adzuna API (snippets only: resolved via the employer's board)")
    dc.add_argument("--gmail-alerts", dest="gmail_alerts", action="store_true", help="LinkedIn / CV-Library / Indeed / Reed alert "
                    "emails through the read-only Gmail API; unresolved ones go to data/jds/manual_queue.csv")
    dc.add_argument("--jobbank", action="store_true", help="Canada's Job Bank open-data CSV: matching rows are listed in "
                    "data/discover/listed_only.csv (the file has no employer or ad text)")
    dc.add_argument("--gmail-auth", dest="gmail_auth", action="store_true", help="sign in to Google once: read mail, create drafts, edit the tracker Sheet; never sends (opens your browser)")
    dc.add_argument("--report", action="store_true", help="counts per source over all runs, resolved vs the manual queue")
    dc.add_argument("--limit", type=int, help="at most N new inbox files this run (default discover.max_new_per_run)")
    dc.add_argument("--dry-run", dest="dry_run", action="store_true", help="find and rank, write nothing")
    dc.set_defaults(fn=cmd_discover)
    so = sub.add_parser("sources", help="the source registry (config/sources.yaml): `check` = health of every source, `list`")
    so.add_argument("what", choices=["check", "list"])
    so.add_argument("--mock", action="store_true", help="use the isolated data/mock folders (no real data/ is read or written)")
    so.set_defaults(fn=cmd_sources)
    sc = sub.add_parser("set-country", help="give a job the country its work is in (visa answers and priority follow it); "
                                            "triages a needs_country job again")
    sc.add_argument("--id", required=True)
    sc.add_argument("--country", required=True, help="a code or name: GB, DE, Ireland")
    sc.add_argument("--mock", action="store_true", help="use the isolated data/mock database")
    sc.set_defaults(fn=cmd_set_country)
    sub.add_parser("retriage").set_defaults(fn=cmd_retriage)
    a = sub.add_parser("add-jd")
    a.add_argument("--id"); a.add_argument("--company"); a.add_argument("--title")
    a.add_argument("--url"); a.add_argument("--location", default="United Kingdom"); a.add_argument("--file")
    a.set_defaults(fn=cmd_add_jd)
    for name, fn in (("tailor", cmd_tailor), ("run", cmd_run)):
        t = sub.add_parser(name)
        t.add_argument("--limit", type=int, default=10)
        t.add_argument("--id", help="tailor exactly this job (it must be 'triaged'); --limit is ignored")
        t.add_argument("--mock", action="store_true", help="deterministic MockLLM, no API calls")
        t.add_argument("--one-page", action="store_true", help="one-page CV with the old template (default: two full pages)")
        t.add_argument("--re-extract", action="store_true", help="extract the JD keywords again instead of reusing the "
                       "job's frozen first extraction")
        t.set_defaults(fn=fn)
    lv = sub.add_parser("live", help="interactive batch: JD files in data/jds/inbox through add-jd, tailor, review, apply "
                                     "assist, with an archive check after every job")
    lv.add_argument("--max", type=int, default=5, help="at most N JD files this session (default 5)")
    lv.add_argument("--fresh", action="store_true", help="tier 0 only: the inbox files posted at most 24 hours ago (older files stay "
                                                         "in the inbox, untouched); the order is still P = F x S")
    lv.add_argument("--dry-run", action="store_true", help="stop after tailor + review: no apply step")
    lv.add_argument("--retailor", action="store_true", help="a job already started from this JD is tailored again "
                    "(CV, letter, review) instead of resumed; never an applied job")
    lv.add_argument("--re-extract", action="store_true", help="extract the JD keywords again (a job keeps its first "
                    "extraction otherwise, so --retailor gives the same keywords and ATS)")
    lv.add_argument("--url", help="fetch this job ad into the inbox first (like add-url) and work on that one file only")
    lv.add_argument("--company", help="with --url: the company name to use instead of the page's")
    lv.add_argument("--title", help="with --url: the job title to use instead of the page's")
    lv.add_argument("--clipboard", action="store_true", help="read the job text from the Windows clipboard (from WSL), "
                    "ask for company / title / URL, save it to the inbox and work on that one file only")
    lv.add_argument("--yes", "-y", action="store_true", help=argparse.SUPPRESS)       # refused with an explanation
    lv.add_argument("--mock", action="store_true", help="MockLLM and the isolated data/mock root (implies --dry-run)")
    lv.set_defaults(fn=cmd_live)
    r = sub.add_parser("review"); r.add_argument("--limit", type=int)
    r.add_argument("--mock", action="store_true", help="MockLLM for the letter-regenerate key, no API calls")
    r.set_defaults(fn=cmd_review)
    p = sub.add_parser("apply"); p.add_argument("--id"); p.add_argument("--limit", type=int)
    p.add_argument("--no-letter", action="store_true", help="go ahead although the cover-letter PDF is missing")
    p.set_defaults(fn=cmd_apply)
    ma = sub.add_parser("mark-applied", help="archive + mark an application made outside the assist")
    ma.add_argument("--id", required=True); ma.add_argument("--ref", help="confirmation / reference")
    ma.add_argument("--date", type=date.fromisoformat, metavar="YYYY-MM-DD", help="date applied (default: today)")
    ma.add_argument("--no-letter", action="store_true", help="go ahead although the cover-letter PDF is missing")
    ma.set_defaults(fn=cmd_mark_applied)
    ou = sub.add_parser("outcome", help="log what happened after an application")
    ou.add_argument("--id", required=True, help="job id")
    ou.add_argument("--event", required=True, choices=["applied", "acknowledged", "screen", "interview", "offer",
                                                       "rejected", "withdrawn"])
    ou.add_argument("--date", type=date.fromisoformat, metavar="YYYY-MM-DD", help="when it happened (default: today)")
    ou.add_argument("--note", help="free text")
    ou.add_argument("--mock", action="store_true", help="use the isolated data/mock database")
    ou.set_defaults(fn=cmd_outcome)
    os_ = sub.add_parser("outcomes", help="the application funnel with 95%% Wilson intervals; --import-archive backfills")
    os_.add_argument("--import-archive", action="store_true", help="list <paths.applications>/<Company>/<Role> folders and "
                     "propose manual jobs + 'applied' events dated by folder mtime (dry run)")
    os_.add_argument("--apply", action="store_true", help="with --import-archive: write the proposals to the database "
                     "(never to the archive)")
    os_.add_argument("--mock", action="store_true", help="use the isolated data/mock database and archive")
    os_.set_defaults(fn=cmd_outcomes)
    il = sub.add_parser("import-linkedin", help="LinkedIn profile PDF -> fact_bank.proposed.yaml + a list of every mismatch "
                        "with fact_bank.yaml (read-only towards it)")
    il.add_argument("pdf", help="the profile saved as PDF, e.g. config/reference/linkedin.pdf")
    il.add_argument("--fact-bank", help="default: paths.fact_bank (only read)")
    il.add_argument("--out", help="default: paths.fact_bank_proposed")
    il.add_argument("--diff", help="default: paths.linkedin_diff")
    il.add_argument("--mock", action="store_true", help="write the proposal and the diff under data/mock")
    il.set_defaults(fn=cmd_import_linkedin)
    sub.add_parser("export").set_defaults(fn=cmd_export)
    e = sub.add_parser("export-prefs"); e.add_argument("--out", default="data/feedback/dpo_pairs.jsonl")
    e.set_defaults(fn=cmd_export_prefs)
    sub.add_parser("status").set_defaults(fn=cmd_status)
    args = ap.parse_args(argv)
    try:
        args.fn(args)
    except NotImplementedError as e:
        print(f"Not built yet: {e}. See docs/IMPLEMENTATION.md.")
        sys.exit(2)
    except judge.GateRefused as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
