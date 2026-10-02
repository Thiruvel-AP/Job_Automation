"""career-ops command line.

  careerops init                create data/ folders and .env (idempotent)
  careerops doctor              check xelatex, fonts, playwright, LLM keys, fact-bank TODOs
  careerops sponsors            refresh the gov.uk sponsor register
  careerops discover            pull jobs from configured boards, triage with rule gates
  careerops retriage            re-run triage on every non-applied job (back-fills family/tier/ceiling_pre)
  careerops add-jd              add a job by hand (LinkedIn, email, truncated Adzuna JD)
  careerops tailor [--limit N]  JD analysis → tailored CV + letter → review queue
  careerops review              approve / edit / reject (logs preference data)
  careerops apply               open approved jobs' forms, fill basics, you submit
  careerops export              write the Excel view
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
from pathlib import Path

from .discovery import gates, sources
from .discovery import register as sponsors
from .review import workflow as review
from .config import load_settings, load_yaml
from .db import DB, now
from .reporting.excel import export
from .llm import MockLLM, Router
from .setup import doctor, init, load_dotenv


def _ctx(args):
    s = load_settings(args.config)
    load_dotenv(s.root / ".env")          # cron-friendly: no `set -a; . .env` needed
    db = DB(s.path("db"))
    return s, db


def _llm(args, s, db):
    return MockLLM() if args.mock else Router(s, db)


def _status(v: gates.Verdict) -> str:
    return "skipped" if not v.passed else "needs_jd" if v.needs_full_jd else "triaged"


def _verdict_fields(v: gates.Verdict) -> dict:
    return {"status": _status(v), "reason": "; ".join(v.reasons) or None, "flags": v.flags,
            "sponsor_name": v.sponsor_name, "sponsor_score": v.sponsor_score,
            "family": v.family, "tier": v.tier, "ceiling_pre": v.ceiling_pre}


def triage_new(db: DB, s) -> dict[str, int]:
    fb = load_yaml(s.path("fact_bank"))
    gates.load_roles(s, fb)                   # fail fast on a renamed project id
    idx = sponsors.SponsorIndex(db) if db.sponsor_count() else None
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
    idx = sponsors.SponsorIndex(db) if db.sponsor_count() else None
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
                          flags=flags)
            counts["post_tailor"] += 1
    counts["applied_untouched"] = len(db.jobs("applied"))
    return counts


def cmd_sponsors(args):
    s, db = _ctx(args)
    n = sponsors.refresh(db, s)
    print(f"Loaded {n} Skilled Worker sponsor name variants.")


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


def cmd_tailor(args):
    from .tailoring.tailor import tailor_job

    s, db = _ctx(args)
    llm = _llm(args, s, db)
    fb = load_yaml(s.path("fact_bank"))
    jobs = db.jobs("triaged", limit=args.limit)
    print(f"Tailoring {len(jobs)} job(s){' with MockLLM' if args.mock else ''}.")
    for job in jobs:
        print(f"- {job['company']} | {job['title']}")
        try:
            rep = tailor_job(dict(job), fb, llm, s, s.path("outputs"))
            f = rep["final"]
            db.update_job(job["id"], status="needs_review", keywords=rep["keywords"], ceiling=rep["ceiling"],
                          score=f["score"], score_stemmed=f["score_stemmed"], letter_score=rep["letter"]["score"],
                          attempts=len(rep["attempts"]), out_dir=rep["out_dir"])
            db.log(job["id"], "tailor", {"score": f["score"], "ceiling": rep["ceiling"], "band": f["band"]})
            print(f"  ATS {f['score']:.0%} (stemmed {f['score_stemmed']:.0%}, ceiling {rep['ceiling']:.0%}) "
                  f"→ {f['band']} band, {f['pages']} page(s)")
        except Exception as e:  # noqa: BLE001 — one bad job must not kill the cron run
            db.update_job(job["id"], status="error", reason=str(e)[:500])
            db.log(job["id"], "error", traceback.format_exc())
            print(f"  ERROR: {e}")


def cmd_review(args):
    s, db = _ctx(args)
    review.run(db, s, load_yaml(s.path("fact_bank")), args.limit)


def cmd_apply(args):
    from .apply.assist import assist

    s, db = _ctx(args)
    applicant = load_yaml(s.path("applicant"))
    jobs = [db.job(args.id)] if args.id else db.jobs("approved", limit=args.limit)
    for job in jobs:
        if job is None:
            continue
        print(review.summary(job, review.load_report(job)))
        if input("Open application form? [y/N] ").strip().lower() != "y":
            continue
        r = assist(job, review.load_report(job), applicant, s.path("browser_profile"))
        if r["confirmation"].lower() == "x":
            db.log(job["id"], "apply", {"aborted": True, **r})
            continue
        db.update_job(job["id"], status="applied", applied_at=now(), confirmation=r["confirmation"] or None)
        db.log(job["id"], "apply", r)
        print("  marked applied.")
    export(db, s.path("excel"))


def cmd_export(args):
    s, db = _ctx(args)
    n = export(db, s.path("excel"))
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


def cmd_companies(args):
    from .discovery import ats, companies_house

    s, db = _ctx(args)
    if args.snapshot:
        sponsors.snapshot(db, s)
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


def cmd_status(args):
    _, db = _ctx(args)
    for st, n in db.conn.execute("SELECT status, COUNT(*) FROM jobs GROUP BY status ORDER BY 2 DESC"):
        print(f"{st:14s} {n}")


def main(argv=None):
    ap = argparse.ArgumentParser(prog="careerops", description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--config", default=None, help="path to settings.yaml")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init").set_defaults(fn=cmd_init)
    sub.add_parser("doctor").set_defaults(fn=cmd_doctor)
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
        t.set_defaults(fn=fn)
    r = sub.add_parser("review"); r.add_argument("--limit", type=int); r.set_defaults(fn=cmd_review)
    p = sub.add_parser("apply"); p.add_argument("--id"); p.add_argument("--limit", type=int); p.set_defaults(fn=cmd_apply)
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
