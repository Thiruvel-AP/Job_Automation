"""career-ops command line.

  careerops init                create data/ folders and .env (idempotent)
  careerops doctor              check xelatex, fonts, playwright, LLM keys, fact-bank TODO/CONFIRM items
  careerops llm-check           one tiny request per LLM provider: reachable, model, latency, JSON, json_mode
  careerops calibrate [--compare REPORT] [--only ID ..] [--limit N] [--provider NAME] [--freeze-keywords [--force]] [--summarise]
                                tailor every JD in data/calibration/manifest.yaml with the real model and measure it
  careerops sponsors            download the gov.uk sponsor register if its link changed, diff it against the last one
  careerops discover            pull jobs from configured boards, triage with rule gates
  careerops retriage            re-run triage on every non-applied job (back-fills family/tier/ceiling_pre)
  careerops add-jd              add a job by hand (LinkedIn, email, truncated Adzuna JD)
  careerops tailor [--limit N] [--one-page]
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
  careerops export              write the Excel view (Jobs, Summary, Outcomes)
  careerops export-prefs        preference log → DPO pairs jsonl
  careerops run                 discover + tailor + export (what cron calls)
  careerops status              counts per status

  Track A (sponsor-first company discovery) — milestones M1–M3, stubs until built:
  careerops companies --snapshot | --refresh-ch | --resolve | --queue | --set NAME --url URL
  careerops learning            ranked skills-gap list across all tailored jobs (M4)
  careerops outreach --id JOB --person NAME --title TITLE   LinkedIn note + follow-up drafts (M4)
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
import traceback
from datetime import date
from pathlib import Path

from .apply.archive import mark_applied_unarchived, preflight, record_application
from .apply.errors import ArchiveError, ArchiveIncomplete
from .discovery import gates, sources
from .discovery import register as sponsors
from .review import workflow as review
from . import mockroot
from .config import load_settings, load_yaml
from .db import DB, now
from .reporting.excel import export
from .tailoring.queue import order, select_for_run
from .llm import BudgetExhausted, MockLLM, Router
from .setup import doctor, init, load_dotenv


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


def _status(v: gates.Verdict) -> str:
    return "skipped" if not v.passed else "needs_jd" if v.needs_full_jd else "triaged"


def _verdict_fields(v: gates.Verdict) -> dict:
    return {"status": _status(v), "reason": "; ".join(v.reasons) or None, "flags": v.flags,
            "sponsor_name": v.sponsor_name, "sponsor_score": v.sponsor_score, "sponsor_match": v.sponsor_match,
            "sponsor_route": v.sponsor_route, "sponsor_rating": v.sponsor_rating,
            "family": v.family, "tier": v.tier, "ceiling_pre": v.ceiling_pre}


def triage_new(db: DB, s) -> dict[str, int]:
    fb = load_yaml(s.path("fact_bank"))
    gates.load_roles(s, fb)                   # fail fast on a renamed project id
    idx = sponsors.load_index(db)
    counts = {"triaged": 0, "skipped": 0, "needs_jd": 0}
    for job in db.jobs("new"):
        f = _verdict_fields(gates.triage(dict(job), s, idx, fb))
        db.update_job(job["id"], **f)
        counts[f["status"]] += 1
    return counts


PRE_TAILOR = ("new", "triaged", "skipped", "needs_jd")
RULES_SKIP = "new rules would skip: "


def retriage(db: DB, s) -> dict[str, int]:
    """Re-run triage on every non-applied job (M4b back-fill of family/tier/ceiling_pre).
    Pre-tailoring rows get a fresh verdict. Post-tailoring rows keep their status and gain one
    'new rules would skip: …' flag if today's rules would reject them. `applied` rows are never touched."""
    fb = load_yaml(s.path("fact_bank"))
    gates.load_roles(s, fb)
    idx = sponsors.load_index(db)
    counts = {"triaged": 0, "skipped": 0, "needs_jd": 0, "post_tailor": 0, "post_tailor_would_skip": 0}
    with db.conn:
        for job in db.jobs():
            if job["status"] == "applied":
                continue
            v = gates.triage(dict(job), s, idx, fb)
            if job["status"] in PRE_TAILOR:
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


def cmd_discover(args):
    s, db = _ctx(args)
    new = seen = 0
    for label, job in sources.fetch_all(s):
        if isinstance(job, Exception):
            print(f"  ! {label}: {job}")
            continue
        seen += 1
        new += db.upsert_job(job)
    print(f"Fetched {seen} postings, {new} new.")
    print("Triage:", triage_new(db, s))


def cmd_retriage(args):
    s, db = _ctx(args)
    print("Retriage:", retriage(db, s))


def cmd_add_jd(args):
    s, db = _ctx(args)
    jd = Path(args.file).read_text(encoding="utf-8") if args.file else sys.stdin.read()
    if args.id:
        db.update_job(args.id, description=jd, status="new")
        print(f"Updated JD for {args.id}")
    else:
        jid = "manual:" + hashlib.sha1(f"{args.company}|{args.title}|{args.url}".encode()).hexdigest()[:12]
        db.upsert_job({"id": jid, "source": "manual", "company": args.company, "title": args.title,
                       "location": args.location, "url": args.url, "apply_url": args.url, "description": jd})
        db.update_job(jid, description=jd, status="new")
        print(f"Added {jid}")
    print("Triage:", triage_new(db, s))


def _finish_letters(db: DB, s, fb: dict, llm) -> None:
    """Write the letters of jobs waiting as `letter_pending` (their CV is done; the provider allowed to write letters had
    no budget). Each is tried in turn; one that still cannot be written stays pending, untouched. A letter is never
    written by a provider the task excludes, so this either succeeds on the right one or waits."""
    from .review.workflow import load_report
    from .tailoring.tailor import letter_summary, regenerate_letter

    waiting = db.jobs("letter_pending")
    if not waiting:
        return
    est = s["llm"].get("letter_token_estimate", 3000)
    print(f"{len(waiting)} job(s) waiting for a letter.")
    for job in waiting:
        print(f"- {job['company']} | {job['title']}")
        try:
            llm.begin_job(est)
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


def cmd_tailor(args):
    from .tailoring.tailor import letter_summary, tailor_job

    s, db = _ctx(args)
    llm = _llm(args, s, db)
    fb = load_yaml(s.path("fact_bank"))
    gates.load_roles(s, fb)
    _finish_letters(db, s, fb, llm)
    jobs = select_for_run(db.jobs("triaged"), args.limit, s)
    print(f"Tailoring {len(jobs)} job(s){' with MockLLM' if args.mock else ''}.")
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
        try:
            rep = tailor_job(dict(job), fb, llm, s, s.path("outputs"), one_page=getattr(args, "one_page", False))
            f = rep["final"]
            pending = rep["letter"]["status"] == "pending"      # no provider may write the letter now: the CV is done
            db.update_job(job["id"], status="letter_pending" if pending else "needs_review", keywords=rep["keywords"],
                          ceiling=rep["ceiling"], score=f["score"], score_stemmed=f["score_stemmed"],
                          letter_score=rep["letter"]["score"], attempts=len(rep["attempts"]), out_dir=rep["out_dir"],
                          cv_pages=rep["cv_pages"], page2_fill=rep["page2_fill"])
            db.log(job["id"], "tailor", {"score": f["score"], "ceiling": rep["ceiling"], "band": f["band"],
                                         "letter": rep["letter"]["status"]})
            cv_pages, letter_pages = f["pages"], rep["letter"]["pages"]
            max_pages = 1 if rep["one_page"] else s["scoring"]["max_pages"]
            short = (not rep["one_page"] and max_pages == 2
                     and rep["page2_fill"] < s["scoring"]["min_fill_page2"])     # the bullets ran out before page 2 filled
            over = cv_pages > max_pages or (letter_pages != 1 and not pending)
            if rep["letter"]["status"] == "fallback":
                print(f"  letter {letter_summary(rep['letter'])}")
            print(f"  ATS {f['score']:.0%} (stemmed {f['score_stemmed']:.0%}, ceiling {rep['ceiling']:.0%}) "
                  f"→ {f['band']} band | CV {cv_pages} page(s)"
                  + (f", page 2 {rep['page2_fill']:.0%} full" + (" (fact bank has no more bullets to add)" if short else "")
                     if not rep["one_page"] else "") + ", "
                  + (f"letter {letter_summary(rep['letter'])}: the job waits as letter_pending" if pending
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


def cmd_review(args):
    s, db = _ctx(args)
    review.run(db, s, load_yaml(s.path("fact_bank")), args.limit, make_llm=lambda: _llm(args, s, db))


def _fail(msg: str) -> None:
    print(f"ERROR: {msg}", file=sys.stderr)
    sys.exit(1)


def _archive(db: DB, s, job, rep: dict, confirmation: str | None, applied_on, no_letter: bool) -> bool:
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
        except ArchiveError as e:
            print(f"  REFUSED {job['id']}: {e}", file=sys.stderr)
            failures += 1
            continue
        print(review.summary(job, rep))
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
    checks = doctor(s)
    for name, ok, hint in checks:
        print(f"  [{'ok' if ok else '!!'}] {name}" + ("" if ok else f"  → {hint}"))
    sys.exit(0 if all(ok for _, ok, _ in checks) else 1)


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
        print(ats.resolve(db, s, limit=args.limit))
    elif args.set:
        print(ats.detect(args.url))
    else:   # --queue (default)
        raise NotImplementedError("M3 — manual queue listing")


def cmd_learning(args):
    from .tailoring import learning

    _, db = _ctx(args)
    for g in learning.aggregate(db)[: args.top]:
        print(f"{g.priority:6.1f}  {g.term}  (in {g.jobs} jobs, required in {g.required_in})")


def cmd_outreach(args):
    from .review import outreach

    s, db = _ctx(args)
    job = db.job(args.id)
    out = outreach.draft(dict(job), review.load_report(job), args.person, args.title, args.team, _llm(args, s, db))
    print(f"NOTE ({len(out.note)} chars):\n{out.note}\n\nFOLLOW-UP:\n{out.message}")


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
    sub.add_parser("doctor").set_defaults(fn=cmd_doctor)
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
    o = sub.add_parser("outreach")
    o.add_argument("--id", required=True); o.add_argument("--person", required=True)
    o.add_argument("--title", required=True); o.add_argument("--team")
    o.add_argument("--mock", action="store_true")
    o.set_defaults(fn=cmd_outreach)
    sub.add_parser("discover").set_defaults(fn=cmd_discover)
    sub.add_parser("retriage").set_defaults(fn=cmd_retriage)
    a = sub.add_parser("add-jd")
    a.add_argument("--id"); a.add_argument("--company"); a.add_argument("--title")
    a.add_argument("--url"); a.add_argument("--location", default="United Kingdom"); a.add_argument("--file")
    a.set_defaults(fn=cmd_add_jd)
    for name, fn in (("tailor", cmd_tailor), ("run", cmd_run)):
        t = sub.add_parser(name)
        t.add_argument("--limit", type=int, default=10)
        t.add_argument("--mock", action="store_true", help="deterministic MockLLM, no API calls")
        t.add_argument("--one-page", action="store_true", help="one-page CV with the old template (default: two full pages)")
        t.set_defaults(fn=fn)
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


if __name__ == "__main__":
    main()
