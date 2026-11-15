# Scheduling the twice-daily discovery (Windows Task Scheduler)

`scripts/discover.sh` finds jobs; it does nothing else. It runs

```
careerops discover --ats --reed --adzuna --gmail-alerts --report
```

which reads the Greenhouse / Lever / Ashby boards, Reed, Adzuna and your Gmail job alerts, writes each new ad that passes the role and
place filters to `data/jds/inbox/`, and prints the report (counts per source and how many ads are in each freshness tier). It never
tailors a CV, never opens an application form and never sends anything: you run `careerops live` (or `careerops live --fresh`) yourself.

Everything it prints is appended to `data/logs/discover_<YYYY-MM-DD>.log`, so the 07:45 and 17:45 runs of one day share one file. Its exit
code is the command's: **non-zero when a source failed** (most often a lapsed Gmail sign-in; see below), so Task Scheduler shows the
task as failed and you notice. A missing Reed or Adzuna key is only a note in the log, not a failure.

## The task (you create it; this repo does not)

The task runs a WSL command at **07:45** and **17:45** every day:

```
wsl.exe -e bash -lc "cd /mnt/d/VS_Code/DataScience/Projects/career-ops && scripts/discover.sh"
```

In the Task Scheduler app (`taskschd.msc`):

1. **Create Task** (not "Basic Task"), name it `career-ops discover`.
2. **General:** "Run only when user is logged on" is fine (WSL starts with your session); leave "Run with highest privileges" off.
3. **Triggers:** New, Daily, start 07:45, repeat every 1 day; add a second trigger, Daily, 17:45.
4. **Actions:** New, Start a program.
   - Program/script: `wsl.exe`
   - Add arguments: `-e bash -lc "cd /mnt/d/VS_Code/DataScience/Projects/career-ops && scripts/discover.sh"`
5. **Conditions:** untick "Start the task only if the computer is on AC power" if the laptop is often on battery.
6. **Settings:** tick "Run task as soon as possible after a scheduled start is missed" (a laptop asleep at 07:45 then runs it when
   it wakes) and "If the task fails, restart every 10 minutes, up to 2 times".

The same thing from a PowerShell prompt, if you prefer a command (run it yourself, once):

```powershell
$action = New-ScheduledTaskAction -Execute "wsl.exe" -Argument '-e bash -lc "cd /mnt/d/VS_Code/DataScience/Projects/career-ops && scripts/discover.sh"'
$times  = @("07:45", "17:45") | ForEach-Object { New-ScheduledTaskTrigger -Daily -At $_ }
$set    = New-ScheduledTaskSettingsSet -StartWhenAvailable -RestartCount 2 -RestartInterval (New-TimeSpan -Minutes 10)
Register-ScheduledTask -TaskName "career-ops discover" -Action $action -Trigger $times -Settings $set
```

Test it once by hand before trusting it: right-click the task, **Run**, then open `data/logs/discover_<today>.log`; or run
`wsl.exe -e bash -lc "cd /mnt/d/VS_Code/DataScience/Projects/career-ops && scripts/discover.sh"` in a terminal and read the exit code
(`$LASTEXITCODE`).

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

