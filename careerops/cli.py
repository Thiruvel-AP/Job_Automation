"""career-ops command line.

  careerops sponsors            refresh the gov.uk sponsor register
  careerops discover            pull jobs from configured boards, triage with rule gates
  careerops add-jd              add a job by hand (LinkedIn, email, truncated Adzuna JD)
  careerops tailor [--limit N]  JD analysis → tailored CV + letter → review queue
  careerops review              approve / edit / reject (logs preference data)
  careerops apply               open approved jobs' forms, fill basics, you submit
  careerops export              write the Excel view
  careerops export-prefs        preference log → DPO pairs jsonl
  careerops run                 discover + tailor + export (what cron calls)
  careerops status              counts per status
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


def _ctx(args):
    s = load_settings(args.config)
    db = DB(s.path("db"))
    return s, db


def _llm(args, s, db):
    return MockLLM() if args.mock else Router(s, db)


def triage_new(db: DB, s) -> dict[str, int]:
    idx = sponsors.SponsorIndex(db) if db.sponsor_count() else None
    counts = {"triaged": 0, "skipped": 0, "needs_jd": 0}
    for job in db.jobs("new"):
        v = gates.triage(dict(job), s, idx)
        if not v.passed:
            st = "skipped"
        elif v.needs_full_jd:
            st = "needs_jd"
        else:
            st = "triaged"
        db.update_job(job["id"], status=st, reason="; ".join(v.reasons) or None, flags=v.flags,
                      sponsor_name=v.sponsor_name, sponsor_score=v.sponsor_score)
        counts[st] += 1
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
        r = assist(job, review.load_report(job), applicant, s.root / "data" / "browser-profile")
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


def cmd_status(args):
    _, db = _ctx(args)
    for st, n in db.conn.execute("SELECT status, COUNT(*) FROM jobs GROUP BY status ORDER BY 2 DESC"):
        print(f"{st:14s} {n}")


def main(argv=None):
    ap = argparse.ArgumentParser(prog="careerops", description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--config", default=None, help="path to settings.yaml")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("sponsors").set_defaults(fn=cmd_sponsors)
    sub.add_parser("discover").set_defaults(fn=cmd_discover)
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
    args.fn(args)


if __name__ == "__main__":
    main()
