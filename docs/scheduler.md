# Scheduling the twice-daily discovery (Windows Task Scheduler)

`scripts/discover.sh` finds jobs; it does nothing else. It runs

```
careerops discover --ats --reed --gmail-alerts --gmail-label --report
```

which reads the Greenhouse / Lever / Ashby boards, Reed and your Gmail job alerts and LinkedIn label, writes each new ad that passes the role and
place filters to `data/jds/inbox/`, and prints the report (counts per source and how many ads are in each freshness tier). It never
tailors a CV, never opens an application form and never sends anything: you run `careerops live` (or `careerops live --fresh`) yourself.

Everything it prints is appended to `data/logs/discover_<YYYY-MM-DD>.log`, so the 07:45 and 17:45 runs of one day share one file. Its exit
code is the command's: **non-zero when a source failed** (most often a lapsed Gmail sign-in; see below), so Task Scheduler shows the
task as failed and you notice. A missing Reed or Adzuna key is only a note in the log, not a failure.

## The task: one command, run by you

`scripts/discover.sh` activates `.venv`, changes to `/mnt/d/VS_Code/DataScience/Projects/career-ops`, runs the command above and appends to
`data/logs/discover_<date>.log`; it exits non-zero when the command (or a source) failed.

From a PowerShell prompt, **you** run, once:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\scheduler_install.ps1
```

It registers the task `career-ops discover` with `Register-ScheduledTask`: two daily triggers, **07:45** and **17:45**, each running

```
wsl.exe -e bash -lc "/mnt/d/VS_Code/DataScience/Projects/career-ops/scripts/discover.sh"
```

It also turns on "run as soon as possible after a missed start" (a laptop asleep at 07:45 runs it on waking) and restarts a failed run twice, 10 minutes
apart. Nothing in this repo runs the script for you, and no code creates a scheduled task.

Test it: `Start-ScheduledTask -TaskName "career-ops discover"`, then read `data/logs/discover_<today>.log`; or run
`wsl.exe -e bash -lc "/mnt/d/VS_Code/DataScience/Projects/career-ops/scripts/discover.sh"` in a terminal and read `$LASTEXITCODE`.
Remove the task with `powershell -ExecutionPolicy Bypass -File scripts\scheduler_uninstall.ps1`.

`careerops doctor` watches it: `[warn]` when no `data/logs/discover_*.log` is newer than 24 hours ("run scripts/scheduler_install.ps1"), and `[!!]`
when a tracked file still spells the repo folder with its old name.

## What you do with the result

| When | What |
|---|---|
| After 07:45 | `careerops live --fresh` : only the tier-0 ads (posted in the last 24 hours); approve, pick the letter with `l`, read the note with `n` |
| After 17:45 | `careerops live --fresh` again, then `careerops live --max 10` for the older tiers if you have time |
| Any time | `careerops discover --report` : the counts per source and per freshness tier, no network |
| Weekly | `data/discover/filtered_<date>.csv` : the titles the role / place filters turned away, with the reason: look for a wrongly filtered role |

## The Gmail sign-in lapses about every 7 days

While the Google OAuth app is in *Testing*, a Gmail grant stops working after about 7 days. `careerops doctor` warns when
`data/secrets/gmail_token.json` is more than 6 days old; run `careerops discover --gmail-auth` again (it opens your browser; you grant
read-only access) before the next scheduled run, or that run exits non-zero with `FAILED: gmail: ...` and the alerts are skipped until you do.

## Limits

* The laptop must be on (and not asleep) at the trigger time, or "run as soon as possible" runs it at the next wake.
* Each host is asked at most once a second and every request carries the career-ops user agent; two runs a day stay well inside that.
* The Groq daily token budget is used only by `careerops live` / `tailor`, never by discovery.

## Region and size in a scheduled run (v2.4)

`scripts/discover.sh` has no terminal, so it never asks which region to target: it uses the last choice you made at a prompt (`data/state/targeting.json`), else `targeting.region`, else `uk`, and prints `region: uk (from last choice)`.
To pin it, add `--region uk` to the command in `scripts/discover.sh` (a flag is for that run only and is never remembered, so it cannot overwrite what you picked by hand). Sources tagged for other regions in `config/sources.yaml` do not run.

