# career-ops

Local pipeline for UK DS/AI applications: **discover → triage → tailor (fact-bank only) → you review → assisted apply**.
What & why: [`docs/PLAN.md`](docs/PLAN.md) · how, milestone by milestone: [`docs/IMPLEMENTATION.md`](docs/IMPLEMENTATION.md).

## Setup (WSL2 Ubuntu)

```bash
sudo apt install texlive-xetex fonts-crosextra-carlito poppler-utils   # xelatex, Carlito, pdftotext/pdfinfo
cd ~/career-ops && python3 -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]" && playwright install chromium
careerops init                  # data/ folders + .env from .env.example (idempotent)
ollama pull qwen2.5:7b-instruct # local fallback; fits the 4060's 8 GB VRAM
ollama create qwen2.5-7b-8k -f config/ollama/Modelfile   # same model with an 8192-token context (Ollama's default is 4096)
# put GROQ_API_KEY (console.groq.com) and optional ADZUNA_APP_ID/KEY, REED_API_KEY in .env
careerops doctor                # every line should say [ok] (also sends each task's model a tiny request; --no-ping skips that)
careerops status                 # job counts, plus LLM tokens and requests used today vs the daily limits
careerops llm-check             # one tiny request per LLM provider: reachable, model, latency, JSON, json_mode (never prints keys)
pytest -q                       # no network or model needed; tests read tests/fixtures/profile/, never your config/profile/
pytest -q -m "not slow"         # skips every test that runs xelatex or Chromium (light on RAM, < ~2 GB)
pytest -q -m slow               # the other chunk: the tests that render PDFs
```

## Project structure

```
career-ops/
├── careerops/                    Python package (CLI entry: careerops.cli:main)
│   ├── cli.py                    every `careerops …` command
│   ├── config.py · db.py         settings loader · SQLite schema + state machine
│   ├── setup.py                  `init` (data layout, .env) and `doctor` (toolchain checks)
│   ├── discovery/                where companies and jobs come from
│   │   ├── register.py           UKVI sponsor register: download, parse, match · M1 snapshots + diff
│   │   ├── companies_house.py    M2 bulk join → company no., SIC codes, sector
│   │   ├── ats.py                M3 slug probe, verification, careers-page detector
│   │   ├── sources.py            Greenhouse · Lever · Ashby · Adzuna fetchers
│   │   ├── intake.py             `add-url` / `live --url`: one job ad from its link into the inbox (+ CV-Library pages)
│   │   ├── robots.py             robots.txt rules (RFC 9309) cached 24 h; hosts never fetched (linkedin.com)
│   │   ├── http.py               the polite client discovery shares: 1 req/s per host, robots, a 401/403/429 stops the host
│   │   ├── discover.py           `discover --ats --reed --adzuna --gmail-alerts --jobbank`: filters, dedupe, inbox files, manual queue, report
│   │   ├── registry.py · health.py   config/sources.yaml (every source and how it is read) · the health check of each source
│   │   ├── countries.py · jobbank.py country names/codes/places (the `listed_only` rule) · Canada's Job Bank open-data CSV
│   │   ├── reed.py · gmail.py    the Reed Jobseeker API · read-only Gmail API (OAuth by hand) and the alert-email parsers
│   │   ├── gates.py              rule triage (title, seniority, location, SRE, line mgmt…); sponsorship is never a rule
│   │   ├── priority.py           freshness tier, then P = F x S: orders the live inbox, discover's listing and status
│   │   ├── freshness.py          posted_at from the source (Greenhouse first_published, Lever createdAt ...), age in hours, tiers 0-3
│   │   └── manual.py             hand-added jobs: read_jd (validation), add_jd, triage_job
│   ├── hook.py                   the hook: the `hook_library` entry that fits the ad, used word for word (headline, profile line 1, its proof bullet first, letter sentence 1); no model
│   ├── ats.py                    negation-aware ATS score and the three numbers (strict CV, claimable ceiling, CV + letter) with the gate
│   ├── note.py · golden.py       the post-CV note.md · diff of a pipeline CV against tests/golden/anaplan/cv.txt
│   ├── tailoring/                JD → CV + cover letter
│   │   ├── scorer.py             weighted ATS score (literal + stemmed), honest ceiling, keywords
│   │   ├── faithfulness.py       guard: no new numbers, deny-listed tools, unevidenced keywords
│   │   ├── prompts.py            all LLM prompts in one place
│   │   ├── selection.py          which bullets and projects go on the CV (code, not the model)
│   │   ├── tailor.py             analyse → select → rephrase → guard → render → band loop → profile, letter
│   │   ├── profile.py            the CV profile, assembled in code: the hook's profile line + one or two `profile_facts` sentences (no model)
│   │   ├── grounding.py          cover-letter check: every sentence against its evidence bullet and the JD
│   │   ├── letters.py            `letter.mode: hook`: the letter ASSEMBLED in code (library letter line + two sentences from two bullets + 20-minute ask), the our/we/linked-phrase guard, relocation line, greeting
│   │   ├── render.py             Jinja2 → xelatex, page count, pdftotext
│   │   └── learning.py           M4 ranked skills-gap list
│   ├── llm/                      router.py (Groq/Ollama, quota fallback) · mock.py (deterministic)
│   ├── review/                   workflow.py (approve/edit/reject → preferences) · judge.py (LLM judge) · outreach.py (M4)
│   ├── apply/assist.py           Playwright: fill basics, upload PDFs, never submit
│   ├── apply/live.py · snapshot.py  `careerops live` loop · read-only archive snapshot + compare
│   ├── reporting/                excel.py (Excel view of SQLite) · calibrate.py (`careerops calibrate`)
│   └── training/rewards.py       Phase 2 GRPO reward functions
├── config/
│   ├── settings.yaml             paths, sources, gates, sectors, scoring band, LLM providers
│   ├── typography.yaml           CV typography: special hyphen spellings, the protected terms (every other hyphen is closed up when the PDF is rendered)
│   ├── taxonomy.txt              ATS keyword vocabulary
│   └── profile/                  fact_bank.yaml (the only truth the tailor may use) · applicant.yaml
├── templates/                    cv.tex.j2 · cover_letter.tex.j2
├── docs/                         PLAN.md · IMPLEMENTATION.md
├── scripts/crontab.example
├── tests/                        mirrors the package; fixtures/<area>/…; no network, no model
└── data/                         generated, git-ignored — created by `careerops init`
    ├── careerops.db              jobs · companies · events · LLM quota
    ├── register/                 YYYY-MM-DD.csv.gz snapshots · latest.csv
    ├── companies_house/          YYYY-MM/ bulk zips
    ├── outputs/<company_title_id>/  CV.tex/.pdf · Cover_Letter.tex/.pdf · report.json
    ├── exports/                  companies.xlsx · learning_list.md
    ├── feedback/preferences.jsonl   approve/edit/reject log → DPO pairs
    ├── jds/{inbox,done,rejected}/   `careerops live`: JD .txt files to do / finished / refused (+ .reason.txt)
    ├── judge/                    log.csv (the judge's verdict beside your decision) · verdicts/ (cache)
    ├── robots/                   <host>.json: each host's robots.txt rules, kept 24 h
    ├── discover/                 boards/ (posting lists, 6 h) · boards.json (token answers, 30 d) · first_seen.json · filtered.csv · gmail_seen.json · runs.jsonl · health/<date>.csv · listed_only.csv
    ├── secrets/                  gmail_client.json (you put it here) · gmail_token.json (read-only token); git-ignored like all of data/
    ├── live/                     state.json (resume) · one summary .md per live session
    ├── browser-profile/          Playwright logins for ATS sites
    └── logs/                     cron.log
```

Modules marked M1–M4 are stubs with final signatures; their commands print "Not built yet" until each milestone lands.

## Before the first real run

1. **Fact bank** — `config/profile/fact_bank.yaml`. Seeded from facts you've stated; fill every `TODO`, answer every `# CONFIRM` (`careerops doctor` counts both), delete anything untrue.
   Each bullet's `terms` list is what that bullet is *allowed* to claim when reworded. If a JD keyword shows up as a
   "genuine gap" but you really have it (e.g. *prompt engineering*, *CI/CD* aren't written anywhere yet), add it to the
   right bullet's `terms` — that is the only way it can ever appear on a CV.
2. **Boards** — `config/settings.yaml → sources`. Add Greenhouse/Lever/Ashby slugs from companies' careers URLs
   (`job-boards.greenhouse.io/<slug>`, `jobs.lever.co/<slug>`, `jobs.ashbyhq.com/<slug>`). Your 80-company sponsor
   tracker is the obvious seed list.
3. **Gates** — title/seniority/location regexes and the JD rules (SRE, line management, clearance). Sponsorship is not a gate: see
   "Priority" below.
4. **Applicant basics** — `config/profile/applicant.yaml`. Salary and "why us" are deliberately never filled; visa
   questions are answered from the `visa:` block (below), only when they are recognisable; equal-opportunities questions only
   from the `equal_opportunities:` block (below), and only the ones you list.

   ```yaml
   visa:
     home: GB                            # required: where you live; remote work with no restriction is done "at home"
     remote:
       label: "Remote"                   # the only wording shown for a remote role at home (review screen, forms, Excel); never the country
       summary: "..."                    # optional: the form's free text for a remote role at home, instead of the home block's
     by_country:
       GB:                               # required: the block of visa.home (`careerops doctor` fails without it)
         right_to_work: true             # "currently/now have the right to work"
         sponsorship_now: false          # "require sponsorship?" / "... currently ..."
         sponsorship_future: true        # "now or in the future" / "in the future"
         summary: "..."                  # free-text visa boxes get this verbatim
       IE: {...}                         # optional: a block for exactly this country, or a region key (EU, EMEA ...)
       default:                          # any place without its own block; doctor warns without it
         right_to_work: false
         sponsorship_now: true
         sponsorship_future: true
         summary: "I would need a work visa for {country} and am open to sponsorship."   # {country} = what is shown for W
   ```

   **Which block is used depends on where the work is done, W** (`discovery/workcountry.py`, plain rules over the `Location:` header
   and the ad text, no model): on-site / hybrid = the office country; **remote with no restriction = `visa.home`**; remote restricted to
   a place that includes home (the country itself, or EMEA / Europe when home is in it) = home; remote restricted to one place that
   excludes home ("US only", "EU only") = that place; anything else (two such places, no office, no location) is unknown. Answers =
   `by_country[W]`, else `by_country.default`. A job whose W is unknown gets the status **`needs_country`**: it is not tailored,
   `apply` and `live` refuse it before any form opens, and `careerops set-country --id JOB --country XX` (or a `Country: XX` line in
   its JD file for `live`) gives it the place, which wins over the ad. **Everything you read or send says only `visa.remote.label`
   for a remote role at home**: the review screen (`Work location: Remote — evidence: "..."`, the header location, the Visa answers
   line), `CHECK VISA ANSWER [W=Remote]`, `{country}` in a summary and the Excel Location column; the home country's names are hidden
   as `[home]` in the evidence sentence, which is otherwise the ad's own words. (The home block's own `summary` is your text and is used
   verbatim; give `visa.remote.summary` if it names the country.) A restriction to another place prints that place. The review screen
   always shows the evidence and the three booleans chosen. Priority S is `priority.sponsor_weights.non_home` (0.5) whenever W is not
   `visa.home`. The rules are conservative (a sentence with *not / cannot / outside* decides nothing; several places and none of them
   home is ambiguous), so a wrong guess is a `needs_country`, not a wrong answer: read the evidence line.

   A field is a visa field when its label mentions *right to work, visa, sponsor, immigration, work permit, work
   authorisation* or *share code*. Every one is outlined magenta and printed as `CHECK VISA ANSWER [W=IE]: <question> -> <value
   or EMPTY>`. Only the three booleans fill Yes/No boxes and dropdowns (a value that is not `true` / `false` is left to you), a
   question whose type is unclear (e.g. "Do you have the right to work?" with no *now/currently*), a question about a country
   other than W ("... to work in the UK?" on an Irish job) and any share-code box are left empty for you. Every other `never_fill`
   label stays empty and orange. Nothing is submitted. `careerops doctor` fails on an `@example.com/.org/.net` email, a `+44 0…`
   phone, a `location` with a UK postcode, a missing `visa.home` and a missing `visa.by_country[visa.home]`; it warns (shown,
   exit code unchanged) about a missing `visa.by_country.default`.

   **Equal-opportunities questions** (ethnicity first) are filled **only** when listed in `equal_opportunities` and never inferred from
   anything else:

   ```yaml
   equal_opportunities:
     ethnicity:
       answer: "Asian - Indian"          # for your reference; never typed anywhere
       match_any:                        # the form options you accept, best first
         - "Asian or Asian British - Indian"
         - "Asian, Asian British or Asian Welsh: Indian"
         - "Indian"
         - "Asian"
       # question: ["ethnic"]            # optional regexes for the question's label; built in for ethnicity, required for any other key
   ```

   For a listed question the assist picks, for each `match_any` text in order, the first form option **equal** to it ignoring case,
   dashes and spacing (`Asian or Asian British – Indian` = `asian or asian british - indian`; never "contains": `American Indian or
   Alaska Native` is not `Indian`). A select, a radio group or a checkbox group has options; no match, or a text box, leaves the field
   empty. Every equal-opportunities field on the form (ethnicity, gender, disability, religion, sexual orientation, veteran status,
   pronouns ...), listed or not, is outlined blue and printed as `CHECK EO ANSWER [ethnicity]: <question> -> <option or EMPTY (why)>`
   (`[not listed]` for the ones you did not list, which are always left blank; an unmatched list prints its options). `careerops
   doctor` validates the block when there is one: a mapping of questions, each with a non-empty `match_any` list of texts, an optional
   `answer` text, an optional `question` list of valid regexes (required unless the key is `ethnicity`), no other keys; an invalid
   entry is never used to fill anything.

## Daily use

```bash
careerops sponsors                 # gov.uk register: download only if its link changed, store data/register/YYYY-MM-DD.csv.gz, diff vs the last snapshot
careerops discover                 # pull + triage; skipped jobs keep their reason
careerops discover --ats --reed    # find open jobs and write them to data/jds/inbox/ (see "Finding jobs: careerops discover")
careerops discover --adzuna --gmail-alerts   # Adzuna results and LinkedIn / CV-Library / Indeed / Reed alert emails, resolved to full ads
careerops discover --jobbank       # Canada's Job Bank open-data CSV: matching rows are listed in data/discover/listed_only.csv
careerops discover --report        # counts per source, resolved vs the manual queue, and how many ads are in each freshness tier (no network)
scripts/discover.sh                # what the scheduler runs twice a day: discover --ats --reed --adzuna --gmail-alerts --report, logged to data/logs/discover_<date>.log (docs/scheduler.md)
careerops sources check            # health of every source in config/sources.yaml -> table + data/discover/health/<date>.csv (also ends every discover run)
careerops sources list             # the registry, no network
careerops retriage                 # re-run triage on all non-applied jobs (after changing roles/gates)
careerops add-jd --company X --title Y --url U --file jd.txt   # LinkedIn/email/agency roles (--file: UTF-8, at least 500 bytes, else a one-line error)
careerops add-jd --id adzuna:123 --file full_jd.txt            # replace a truncated Adzuna snippet
careerops add-url https://job-boards.greenhouse.io/monzo/jobs/123   # fetch one job ad into data/jds/inbox/ (see "Adding a job from its link")
careerops live --url URL           # the same fetch, then live works on that one file
careerops live --clipboard         # a site that blocks scripts: copy the job text, then this (asks Company / Title / URL; Windows clipboard via WSL)
careerops judge-report             # the LLM judge vs your decisions: agreement, kappa, false approves (see "The LLM judge")
careerops tailor --limit 15        # CV (two full pages) + note.md per job (letter.mode: `off` by default = no letter; `hook` = an assembled letter, `draft` = a model-drafted one), in queue order → data/outputs/<job>/ (+ report.json)
careerops tailor --one-page        # the old one-page CV (templates/cv.tex.j2) instead
careerops tailor --id JOB          # exactly this job (it must be 'triaged'; --limit is ignored)
careerops live --max 5             # JD files in data/jds/inbox/, one job at a time, you at every gate (see "Live sessions")
careerops live --fresh             # tier 0 only: the inbox files posted at most 24 hours ago (older ones stay in the inbox, untouched)
careerops live --dry-run           # the same, stopping after tailor + review: no apply step
careerops live --dry-run --retailor   # a JD already started is tailored again (after a fix) instead of resumed; never an applied job
careerops live --retailor --re-extract   # ... and the JD's keywords are extracted again (a job keeps its FIRST extraction otherwise, so --retailor gives the same keywords and ATS)
careerops tailor --id JOB --re-extract   # the same flag on tailor / run
careerops review                   # approve / edit (opens YAML of bullets) / reject
careerops apply                    # fills basics + uploads PDFs, YOU press submit, paste the reference
careerops mark-applied --id JOB    # an application made outside the assist: archive it and log 'applied'
careerops outcome --id JOB --event screen --date 2026-10-12 --note "phone"   # applied|acknowledged|screen|interview|offer|rejected|withdrawn
careerops outcomes                 # the funnel applied → any reply → screen → interview → offer: counts, rates, 95% Wilson intervals
careerops outcomes --import-archive          # DRY RUN: list <paths.applications>/<Company>/<Role> as approximate 'applied' events
careerops outcomes --import-archive --apply  # write them to the database only (the archive is only listed, never written)
careerops import-linkedin config/reference/linkedin.pdf   # LinkedIn profile PDF → config/profile/fact_bank.proposed.yaml + data/linkedin_diff.md (see below)
careerops export                   # data/exports/companies.xlsx (Jobs, Summary, Outcomes)
careerops export-prefs             # your edits → DPO pairs (chosen = yours, rejected = model's)
careerops status                   # counts per status, plus a tier × status table
careerops calibrate                # tailor the calibration JDs with the real model and measure it (see below)
```

### Finding jobs: `careerops discover`

`careerops discover --ats | --reed | --adzuna | --gmail-alerts` finds open jobs and writes each one as an inbox file
(`data/jds/inbox/<company>-<title>-<id>.txt`, headers `Company:`, `Title:`, `URL:`, `Source:`, then `Location:`, `Salary:`, `Posted:`,
`Agency: yes`). It adds nothing to the database and applies to nothing; `careerops live` then works through the inbox best priority
first. Without a source flag `discover` keeps its older job (the boards in `sources:` into the database, then triage; `run` and cron use
that). Common to every source: a title in `roles.families` and a UK or remote-UK place are kept (a bare "Remote" only when the ad says
UK; Reed, Adzuna and CV-Library are UK sites); closed postings are dropped; a job already in the database, in `inbox/`, `done/` or
`rejected/`, in the application archive (read only, company and role folder names) or earlier in the same run is dropped (company +
normalised title + place). Sponsorship is never a filter (see "Priority"). `--limit N` caps the new files (default 40), `--dry-run`
finds and ranks without writing, and every run is listed best priority first.

- **`--ats`** reads Greenhouse, Lever and Ashby boards through their documented public APIs. It starts from your companies, in this
  order and no other: (1) the top-level folder names under `paths.applications` (`D:\Professional\UK\Companies`; names only, nothing
  inside a folder is opened), (2) the employers you list in `discover.sources` (settings.yaml), (3) sponsor-register companies, only
  when they are also one of those (the register is never sampled). For each company it guesses up to four board tokens from the name
  (lower-case, Ltd / Limited / plc / Group / UK / Holdings / Technologies dropped, spaces and punctuation removed: `Monzo Bank Ltd` ->
  `monzobank`, `monzo-bank`, `monzo`) and asks each provider; a token counts only when the API answers 200 with a jobs list. Every
  answer, found or not, is kept 30 days (`discover.ats.board_cache_days`) in `data/discover/boards.json`, so a company is asked about
  once a month; a blocked host or an error is not kept. The run prints, per company, the tokens tried, the board found
  (`greenhouse/monzo`) or none and the jobs that matched the role and place, then the top 10 by priority P with title, company,
  location and URL. A token is a guess: a board with the right token but another company's name is read as this company, so look at
  the printed board before applying. The boards named token by token in `sources:` are still read. Posting lists are cached for 6
  hours. **Workable is not read**: its API needs the account owner's token and no public posting API is documented.
- **`--reed`** uses the Reed Jobseeker API (`REED_API_KEY`): each query in `discover.reed.queries` runs twice (direct employers, then
  recruitment agencies, whose jobs get `Agency: yes`), then every job worth keeping costs one request for its full description
  (`details_per_run`). An expired posting is closed.
- **`--adzuna`** uses the Adzuna API (`ADZUNA_APP_ID`/`ADZUNA_APP_KEY`, `sources.adzuna.queries`). Its descriptions are snippets, so a
  result is a **link**: it is looked for on the employer's own board (a known board, else the company's name is turned into up to four
  board tokens, each asked of Greenhouse, Lever and Ashby, answers cached 30 days); a CV-Library address is read like `add-url`;
  anything else goes to the manual queue. Adzuna's own redirect links are never followed.
- **`--gmail-alerts`** reads LinkedIn, CV-Library, Indeed and Reed job-alert emails with the read-only Gmail API. Per job it extracts
  title, company and link. LinkedIn and Indeed pages are **never fetched**: the job is looked for on the employer's own board by title
  (as for Adzuna); CV-Library job links go through the CV-Library reader; Reed links through the Reed API. A job that cannot be
  resolved is appended to `data/jds/manual_queue.csv` (`ts, source, company, title, location, link, reason`, one row per link, never
  rewritten) for you to open, copy the ad and run `careerops live --clipboard`. **Resolving**: for every alert's company, up to four
  board tokens (`Monzo Bank Ltd` -> `monzobank`, `monzo-bank`, `monzo`) are asked of the public Greenhouse, Lever and Ashby APIs (hits
  and misses cached 30 days in `data/discover/boards.json`, one request a second per host); the alert's title must match an open posting
  at normalised-title similarity >= `discover.title_match` (0.8) with the same level words ("Data Scientist" is not "Senior Data
  Scientist"). A board found only through a guessed token is also held to the alert's town and, on Greenhouse, to the board's own
  company name. The run prints, per source, how many alerts were resolved (`linkedin 12 of 20 (60%)`; `discover --report` adds all runs).
  **The queue is capped**: a run keeps its best `discover.manual_queue_max` (10) unresolved links in `manual_queue.csv`, ranked by title
  relevance (role tier 1 = 1.0, tier 2 = 0.8, tier 3 = 0.6, halved for a level the triage excludes) x S, and writes the rest to
  `data/jds/manual_queue_overflow.csv` (same columns plus `score`). **Filter audit**: every title the role or place filter turned away is
  in `data/discover/filtered_<date>.csv` (`ts, source, company, title, location, filter, reason, link`; one file per day holding the titles first turned away that day, a title is written once; a role reason
  lists the words such as "data" or "machine learning" that make it worth a second look). Messages are remembered in
  `data/discover/gmail_seen.json`. The alert parsers are written from the usual layout of these emails and tested on hand-made
  fixtures; they have not met a real mailbox.
- **`--jobbank`** reads Canada's Job Bank open-data CSV (Open Government Licence - Canada): the newest English monthly file is found
  through the open.canada.ca catalogue API and downloaded once (robots.txt first; the portal asks for a 20 s crawl delay, which the
  client keeps). The file is UTF-16, tab-separated, ~46 MB, and has **no employer, no link and no ad text**, so a row can never be an
  inbox ad: rows whose title is in a target role family and that were first posted within `discover.jobbank.max_days_old` days are
  only **listed** in `data/discover/listed_only.csv` (search the title on jobbank.gc.ca).
- **`--report`** prints counts per source (alerts, seen, role/place filtered, closed, duplicate, written, resolved, queued) for the last
  run and for all runs, and how many alerts were resolved vs how many sit in the manual queue.

#### The source registry, `config/sources.yaml`, and the health table

One entry per place jobs are looked for: `id`, `name`, `url`, `country` (an ISO code, or `multi`) and `method`: `greenhouse` /
`ashby` / `lever` (public job-board APIs; with a `token` that company's board, `openai` is Ashby token `openai`), `reed` (the Jobseeker
API), `jobbank_csv`, `gmail_alerts` (the source's alert emails, matched by the sender `domains` you list), or `dedupe_only`
(AIApply: never searched). Allowed access only: public/official APIs, pages robots.txt allows, your own Gmail read-only; a 401/403
or a robots.txt "no" stops that source and is never worked around. LinkedIn, Indeed, Glassdoor, Welcome to the Jungle, Wellfound, YC,
EURES, StepStone, IrishJobs, SEEK, MyCareersFuture, GOV.UK Find a Job, NHS Jobs, BlackRock and JPMorgan have no API this tool may use,
so they are `gmail_alerts`: set up a job alert on each site and the emails are read (LinkedIn, CV-Library, Indeed and Reed have
dedicated parsers, the rest a generic one that takes job links on the source's own domain; an alert it cannot read goes to the manual
queue as a link to the email). Google DeepMind has no Greenhouse token that answers (checked 2026-10-13), hence `gmail_alerts`.
**The sender domains of most alert sources are my best knowledge, not read off a real email**: if `sources check` shows
`quiet` (no alerts in 7 days) for a source whose alerts you do receive, correct its `domains` (the note in the file says which are
unverified).

`careerops sources check` and the end of every `careerops discover` run print one row per source (source, method, status, new jobs)
and save the full table (reachable, robots.txt, credentials, alerts in the last 7 days, postings, detail) to
`data/discover/health/<date>.csv` (a second check the same day appends its rows). Status: `ok`, `quiet` (works, found nothing),
`blocked` (robots.txt, 401 / 403 / 429), `unreachable`, `no_credentials` (no `REED_API_KEY`, not signed in to Gmail), `not_found`,
`no_file`, `error`, `disabled`. A source failing never stops the others, and a failing check never fails the command it ends.

#### Jobs outside the UK, and AIApply

A job whose place is another country (the ad's own country, else read from its place: `Toronto, ON` is Canada) is not dropped: it is
written to the inbox with `Country: Canada` and `Status: listed_only` headers (at most `discover.listed_only.max_per_run` a run,
counted apart from the UK limit; `discover.listed_only.enabled: false` drops them as before). Its priority S is
`priority.sponsor_weights.non_home` (0.5). **`careerops live` will not tailor or apply to such a file**: it prints `LISTED ONLY`, leaves
it in `inbox/` and does not count it towards `--max`, unless `applicant.yaml` has a block for exactly that country, which you add yourself
(`visa.by_country.CA`, a code or a name; `default` does not count, so a job abroad is never worked on just because a default exists).
A country with such a block also passes the triage location rule; the same location rule still skips a foreign job without one
(`live` asks "Tailor it anyway?"). An alert from a one-country source (SEEK AU) gives the job that
country unless its place says another.

`data/aiapply_applied.csv` (`paths.aiapply_applied`; optional, read only) lists what AIApply applied to: a job with the same company and
normalised title is skipped as a duplicate. Columns are matched by name (`company` / `employer` ..., `title` / `job title` / `position` ...).

Every request goes through one client: at most one per second per host, the honest `User-Agent`, `robots.txt` read first for pages and
the public job-board APIs, redirects followed by hand, and an HTTP 401, 403 or 429 stops that host for the rest of the run (no retry,
no workaround). linkedin.com is never contacted. The Reed, Adzuna and Gmail calls are credentialed API calls and do not read
`robots.txt`; their keys are never printed or put in a URL (Adzuna's `app_id`/`app_key` are query parameters by its design, so its
requests are not logged).

#### Getting a Reed API key

Sign up as a jobseeker at reed.co.uk, then request an API key at https://www.reed.co.uk/developers/jobseeker (the key is shown on that
page after you log in). Put it in `.env` as `REED_API_KEY=...`. The API uses HTTP Basic auth with the key as the user name and an empty
password; `careerops` does that for you.

#### Getting Gmail access (read-only)

1. In https://console.cloud.google.com create a project (any name), then **APIs & Services → Library → Gmail API → Enable**.
2. **APIs & Services → OAuth consent screen**: user type *External*, fill the app name and your e-mail, add yourself under *Test
   users*, and add the scope `.../auth/gmail.readonly` (read-only). Leave the app in *Testing* (a refresh token then lasts 7 days; run
   `--gmail-auth` again when it stops working) or publish it for your own use.
3. **APIs & Services → Credentials → Create credentials → OAuth client ID → Application type: Desktop app.** Download the JSON and
   save it as `data/secrets/gmail_client.json` (the folder is git-ignored; never commit it).
4. Run `careerops discover --gmail-auth`. Your browser opens at Google's consent page; you sign in and agree there (career-ops never
   types a password). The refresh token is saved in `data/secrets/gmail_token.json`. A token that was granted anything wider than
   `gmail.readonly` is refused, and the scope cannot send, delete or label mail.
5. `careerops discover --gmail-alerts` then reads the last `discover.gmail.query_days` days of alerts from the sender domains of the
   sources in `config/sources.yaml` (or, if you list kinds in `discover.gmail.senders`, only those of the four older ones).

### Adding a job from its link (`careerops add-url`, `careerops live --url`)

`careerops add-url <link>` fetches one job ad and writes `data/jds/inbox/<company>-<title>-<id>.txt` with `Company:`, `Title:`,
`URL:` and `Location:` header lines filled in, then the ad's text. It adds nothing to the database: `careerops live` does that, with
you at every gate. `careerops live --url <link>` is the same fetch followed by a live session on that one file (the rest of the
inbox is left alone; a rerun finds the file already there). `--company` / `--title` replace what the page or API gives.

**robots.txt first.** Before any fetch (a page, or one of the job-board APIs) the host's `robots.txt` is read once and cached in
`data/robots/<host>.json` for 24 hours; a path it disallows for this tool (user-agent token `career-ops`; `*` and `$` rules as in
RFC 9309) is refused with "<host>/robots.txt disallows <path> ...". A `robots.txt` that answers 401, 403, 429 or 5xx, or not at all,
refuses a page host too (the rules cannot be checked); the three documented public job-board API hosts (Greenhouse, Lever, Ashby) are
the one exception for a 401/403 on `robots.txt` itself, because their API is meant for programs and its own 401/403/429 still stops
the fetch. Redirects are followed one hop at a time, each checked the same way. **linkedin.com (and lnkd.in) is never fetched**: LinkedIn
ads come in through job-alert emails (`careerops discover --gmail-alerts`) or `--clipboard`.

**CV-Library.** `https://www.cv-library.co.uk/job/apply/<id>...` is rewritten to `https://www.cv-library.co.uk/job/<id>` (its
`robots.txt` disallows `/job/apply/`), and only that page is fetched: title, company or agency, location, salary and the description
block (the page's JSON-LD `JobPosting`, else its own description element), never the "similar jobs" sidebar. A recruitment-agency name
sets `Agency: yes` in the inbox file. CV-Library search or query URLs (`/jobs?q=...`; `robots.txt` has `Disallow: /*?`) are never fetched.
The extraction is tested on saved fixtures only; it has not been run against a live CV-Library page.

A site that answers HTTP 401, 403 or 429 is asked once: no retry and no workaround, the command exits 1 with "This site blocks
automated access — copy the job text and use --clipboard". Copy the job text in your browser, then run `careerops live --clipboard`
from WSL. It prints "Copy the job ad now, then press Enter" and waits (so the ad you copy while it waits is the one used), reads the
Windows clipboard (`powershell.exe ... Get-Clipboard`, UTF-8, `\r` removed), refuses text under 2000 characters or one that starts like a
shell command (`careerops`, `powershell`, `explorer`, `cd`, `git`, `source`), shows the character count and the first three lines and asks
`Use this? [y/N]` (anything but `y` writes nothing), then asks for Company, Title and URL (each at least 2 characters, no `<` or `>`;
the same checks as live's prompts; three bad answers write nothing), saves
`data/jds/inbox/<company>-<title>.txt` (an existing file is never overwritten) and works on that one file. A job from CV-Library,
Reed, Totaljobs, Indeed or LinkedIn gets a `BOARD:` line on the review screen: the sponsor register match may name the agency, not the
employer, so apply by hand on the site.

| Link | How it is fetched |
|---|---|
| `job-boards.greenhouse.io/<board>/jobs/<id>`, `boards.greenhouse.io/...` | `GET boards-api.greenhouse.io/v1/boards/<board>/jobs/<id>`; the HTML-escaped `content` is unescaped and stripped to text |
| `jobs.lever.co/<site>/<id>`, `jobs.eu.lever.co/...` | `GET api.lever.co/v0/postings/<site>/<id>` (`api.eu.lever.co` for the EU host): Lever's documented single-posting call |
| `jobs.ashbyhq.com/<board>/<id>` | Ashby documents no single-posting call, only the board list `GET api.ashbyhq.com/posting-api/job-board/<board>`; that one request is made and the posting picked out of it by its id |
| anything else | the page itself: a `JobPosting` JSON-LD block if it has one, else the text of `<main>` / `<article>` / the body without scripts, navigation and footers. **Under 2000 characters is refused** with a message (a page that builds its text with JavaScript, a login wall): save the ad by hand instead |

At most one request per second per host, with the same `User-Agent` as the other fetchers, public unauthenticated calls only. An
existing file of the same name is never overwritten. **Inbox checks** (also for hand-made files): a header holding `<` or `>` (an
unfilled `Company: <company name>`) or a company / title under 2 characters makes `live` move the file to `rejected/` with the
reason; an answer typed at the company / title / URL prompt that fails the same check is asked again (three in a row = blank = the
file is rejected).

### Live sessions (`careerops live`)

Put one job description per `.txt` file in `data/jds/inbox/` (created on first use, with `done/` and `rejected/` beside it), or fetch
one with `careerops add-url`. The
first lines may name the job: `Company: …`, `Title: …`, `URL: …`, `Location: …` (anything missing is asked for). Then
`careerops live` takes the files best first (freshness tier, then P = F x S; it prints the order), up to `--max` (default 5), and for each one:

1. checks the file (missing, under 500 bytes, not UTF-8 → moved to `rejected/` with a `.reason.txt` beside it, loop continues);
2. snapshots the archive (`paths.applications`; read-only `os.walk`: path, size, mtime);
3. adds the job (the internal `add-jd` function; the job id is passed on as a value, never read from printed output) and triages it;
4. tailors exactly that id, then asks you to review it: **approve / edit / skip** (plus `l` to pick the letter A / B / none, `n` to open
   the note, `o` to open the PDFs); skip leaves the job `skipped` and files the JD in `done/`;
5. unless `--dry-run`, and only after approval: you confirm *Open the application form?*, the existing apply assist fills the
   basics (it never submits), every `CHECK VISA ANSWER` line is printed, and *Did YOU submit the form? [y/N]* decides:
   `y` → mark-applied (archive + `applied`); anything else → status `ready_not_submitted`, nothing recorded as sent;
6. snapshots the archive again. A removed or changed path, or an addition outside this job's one new `<Company>/<Role>` folder,
   stops the whole loop (exit code 3) and prints the paths;
7. moves the JD to `done/<job id>-<name>.txt` (`:` in the id becomes `_`).

There is no `--yes`: every gate is answered by you. `--mock` runs everything on the isolated `data/mock` root with MockLLM
and implies `--dry-run`. **Ctrl-C** (or an unanswered prompt) prints the stage it stopped at and the command to resume (exit 130);
the JD stays in `inbox/`, and a rerun continues the same job at the status it reached. A finished JD is remembered by its
content hash in `data/live/state.json`, so the same text dropped in again is not redone. A dry run, a pending letter and a refused
apply also leave the JD in `inbox/`. Each session ends with a summary (id, company, role, status, sponsor-register match,
visa lines, archive check), printed and written to `data/live/<timestamp>.md`.

### Doctor: every task's model answers

`careerops doctor` sends one tiny request to each (provider, model) a task can be routed to: `extract_keywords`,
`rephrase_bullets`, `cover_letter` and `judge` (per `llm.task_providers` / `llm.models`), once per model, and reports a line **per
task**. A failure says its kind: `model_not_found`, `auth`, `timeout` (60 s), `unreachable`, `rate_limited`, `bad_json`, and what to
change. A task whose feature is off (`letter.mode`, `review.judge`) asks nothing; `--no-ping` sends nothing.

Two **reminders** (warnings, never a failure; a line appears only when one applies): from **2026-11-24** the MSc entry of the fact bank whose
`note` still says "Expected" (put the real grade in; you edit that file, the pipeline never does), and the **Gmail sign-in more than 6 days old**
(a grant made while the OAuth app is in Testing lapses after about 7 days; `careerops discover --gmail-auth` again). The age is the grant's own
time, kept as `granted_at` in `data/secrets/gmail_token.json` (a token written before that was kept is aged by its file)

### The LLM judge (`review.judge: off | shadow | gate`, default `shadow`)

A second model reads the JD, the CV text (no contact block), the letter if there is one and the fact-bank bullets they rest on, and
answers `{would_send, score 1-5, issues}`. It is **read-only**: it never edits a file, a report or a database row. Its model is
`llm.models.judge` (shipped: the local Ollama `qwen2.5-7b-8k`, 8192-token context; Groq's `llama-3.3-70b-versatile` answered 404
`model_not_found`), pinned to its provider by `llm.task_providers.judge`, and it must be **another family than the generator**
(gpt-oss): `careerops doctor` checks the configuration, and each judgement checks the models that actually wrote that job
(`report.json → llm_usage`; if Ollama stood in for Groq on a job, the judge declines that job). The prompt is fitted to the model's
context: the JD is cut to its role / requirements sections, then to the room left after the CV, letter and facts; the review screen
prints `Judge input: N prompt tokens (...)`, and the same number goes to `data/judge/log.csv` (`judge_input_tokens`) and the `events`
table. Ollama down, or an answer that is not JSON: tried twice, then **no verdict** (logged with the reason) and the review goes on.
`careerops doctor` sends a tiny request to every model any task can use (see below).

- **shadow**: the verdict is printed on the review screen and every approve / reject is logged with it in `data/judge/log.csv`
  (`careerops live` has no reject key, so there `s` skip counts as a "no"). The verdict can anchor you; decide before you read it
  if you want honest labels. `careerops judge-report` prints n, agreement, Cohen's kappa, false approves (judge yes / you no) and,
  with none, the rule-of-three bound 3/n on the false-approve rate.
- **gate**: a judge "yes" approves the job without the review prompt. **Refused in code** (`judge.GateRefused`, exit 1) until the log
  holds 60 human-labelled reviews and not one false approve; the 60 is a constant, not a setting. A judge "no", no verdict, a flagged
  job, a fallback letter or a lost letter all still come to you. Gate only sets the status to `approved`: opening a form, the visa
  answers and submitting stay yours.

An unquoted `off` in YAML is the boolean `false`; both `letter.mode` and `review.judge` accept it, but the shipped file quotes it.

### Letters (`letter.mode: off | draft | hook`, default `off`)

**`off`** (the default since 17 Oct) asks for no letter: a live run uploads the **CV only** (see below).

**`hook`** **assembles** the letter in code (`tailoring/letters.py`); no model writes any of it. It is the hook entry's `letter_line` word for
word, then **two sentences each built from one selected bullet's own words**, then the call to action: *"Could we take 20 minutes to talk about
production, deployment and testing?"* (`letter.closing_hook`; the problem phrase is the hook entry's own `terms` that the ad asked for, lower
case, never title case: only a name like AWS or MLOps keeps its capitals). A body sentence is `On <that bullet's terms the ad asked for>, here is
my closest example: at <employer>, I <the bullet's first clause>.` The clause is the longest cut of the bullet that keeps the sentence under the
paste limit (0.7 of its words in **any** fact-bank bullet, `letter_rules.paste_overlap`); a bullet no cut of which passes gives **no letter**
rather than a pasted one. The letter greets "Dear <Company> <team the ad names> team" ("Hi <agency> team" for a recruitment agency), carries
your relocation line when the job is in another town (`relocation: {willing, scope, remote_ok, letter_line}` in `applicant.yaml`; nothing for a
remote job or your own town; the file is only read), and is at most `scoring.letter_max_words` (250) words. **The guard** (`letters.claim_problems`,
also applied to a `draft` letter) rejects "our" and "we", "I help <Company>", and any noun phrase that links a bullet's object to a different
bullet's tool (*"Docker and AWS Lambda ran the test suite"*: the agent was deployed there, the tests are another bullet). Variant **B** (a gap
sentence) was the model's and is gone: B is always **none**. In review `l` cycles A → none; the chosen letter is the one the apply assist uploads
and the archive keeps (`letters/A/<name> Cover Letter.pdf`). With the hook off or none buildable for the job, the letters are **none**.

With `off` no letter is asked for: no letter call, no letter PDF, review says `Letter: off` and approving needs no extra words, and
the application is archived without one. With `draft` the model drafts one from the evidence **as fields** (`method`, `data`,
`outcome`), never as the bullet sentence, so it has to write its own; every letter check is unchanged. Your fact bank has no such
fields yet, so the fields are the bullet's `method` / `data` / `outcome` keys if you add them (the pipeline never writes your fact
bank), else `method` = its `terms`, `outcome` = its `result`, and `data` = `(not stated)`, which the prompt tells the model to leave
alone.

### What the review screen shows besides the score

- **Context terms**: team, product-area and organisation names in the JD (Growth, Payments, Fincrime, "product squad",
  "data science teams") are not skills. They are taken out of the keywords before scoring, out of the ATS numerator *and*
  denominator and out of the ceiling, and listed on their own line. A term is context when it ends in an organisational noun
  (squad, team, collective ...), is one of a capitalised list of three or more names in the JD, or the model named it under
  `context_terms`; so is a phrase that ends in a generic head word (`data discipline`, `engineering culture`, `growth
  mindset`, `central data function`: how the company works, not a skill). A taxonomy skill (Python, Looker ...) never is, and
  `loss function` / `window functions` stay skills. Live tailoring and `calibrate` both make keywords in
  `tailor.analyse`, so both apply the same filters (verbatim in the JD, 1-3 words, nothing generic, near-duplicates merged,
  context terms set aside, cap).
- **Possible evidence — you decide**: before a JD term is called a genuine gap, `config/evidence_families.txt` is checked
  (`A/B experiments` ~ hypothesis testing / experimental design, `quantitative analysis` ~ statistics ...). A hit is shown with the
  fact-bank entries behind it. It is never counted: not in the ceiling, not on the CV, not in the letter, and not a genuine gap
  either. If your experience does cover it, say so in `fact_bank.yaml` yourself.
- **Letter drafts**: the letter gets up to three drafts. Each retry is told every word the claim check has rejected so far ("Do
  not use 'X' unless it appears in the evidence bullets") plus the full text of every violation; each rejected draft's reasons
  are listed under the score, and a letter that still fails is the flagged fallback. No check is loosened. One exception to
  "retry": when a draft's only violations are unsupported qualifier words (specialised, integrated, extensive, deep, proven,
  seamless, robust, strong, solid ...; adjectives and adverbs, never a noun, verb, tool, number or capability), those words are
  deleted (a/an, commas and "and" tidied, nothing added), the full check is re-run on the edited draft, and it is accepted only
  if that passes. The review screen then says `Letter removed: <words>`. The letter prompt also bans these qualifiers up front.

### Calibration

`data/calibration/manifest.yaml` lists the JDs you tailored by hand (`id`, `company`, `title`, `jd_file`; optional `manual_ats`,
`reference_cv`, `outcome`). `careerops calibrate` tailors each with the configured model, at temperature 0 and a fixed seed, and writes
`data/calibration/report-<stamp>.md` (+ `.json`): per JD the keywords, ceiling, final score, rephrase coverage, guard blocks,
selection, violations, letter status (and whether it needed its one retry, with the words it was rejected for), pages and page-2 fill, the wording pass, tokens and seconds, then the aggregates. A JD whose employer is not on the sponsor register is flagged in the log line and the report and still tailored. It touches neither the jobs table nor `data/outputs/`
(CVs go under `data/calibration/runs/<stamp>/`) and stops, leaving the rest unmeasured, when the day's Groq tokens cannot cover another JD.

```bash
careerops calibrate --limit 3                      # a few JDs, to stay inside the 200K tokens a day
careerops calibrate --compare data/calibration/report-<earlier>.md   # before/after next to the new run
careerops calibrate --only cal-heathrow cal-quantcast
careerops calibrate --freeze-keywords --provider groq   # extract each JD's keywords once -> config/calibration/keywords/<id>.yaml (tracked in git; never overwrites without --force)
                                                       # every later run reads them: same ceilings across runs and providers; a missing file fails the run
careerops calibrate --provider ollama              # pin every call to one provider (no fallback); the report names the provider and model per call
careerops calibrate --summarise                    # merge the filled data/calibration/rubric.csv into the latest report; a rubric saved before the run prints "RUBRIC PREDATES THIS RUN" and the grade-based criteria are not ticked
careerops calibrate --mock                         # no model, own data root: checks the plumbing
```

### The two-page CV

Every CV is two full A4 pages. `templates/cv2.tex.j2` renders with the preamble and macros of your style reference
(`config/reference/cv_style_reference.tex`, git-ignored) line for line: 11pt Carlito (fontspec), margin 0.6in with top and bottom
0.5in, the `navy` / `ink` / `muted` palette, ragged2e `\RaggedRight`, no hyphenation, section headings 13pt bold navy
letter-spaced uppercase with a 0.8pt rule, the navy bullet dot, `\linespread{1.03}`, and the macros `\role`, `\roleNoLoc`,
`\project`, `\gh`, `\badge`, `\skillgroup`, `\num` and `\gap` (v2 of the reference, 16 Oct 2026). Only `needspace` (no heading or entry alone at the foot of a page)
and the linespread value (the fill step may raise it) are added. Order: Profile → Skills → Work Experience → Projects →
Education → [Research Focus, Publications] → Certifications & Achievements → [Languages]. If you change the reference, the
preamble test fails until `templates/cv2.tex.j2` and the pinned copy in `tests/tailoring/test_cv_template.py` follow it.

- **Layout** (as in the reference) — experience goes through `\role{org}{location}{dates}{title}` (`\roleNoLoc` when an entry has
  no location): the employer bold on the left, its location after a middle dot in muted italics, the dates muted on the right, the
  job title in italics on the next line. A project is `\project{name}{stack}{tag}`: the bold name, an italic muted stack line
  and a tag at the right, which is the award, else the project's own `badge:` (a short label you write in the fact bank, e.g. `Open source`;
  printed once if its text is already in the name), or a `(coursework)` / `(MSc coursework)` ending of the name as a navy `\badge`
  (`MSc Coursework`; a project with a GitHub link keeps its name and shows the link instead), else the link as `\gh` (`GitHub`).
  A skills group is `\skillgroup`: the navy bold label on a line of its own and its items below it (the first item of each group
  starts with a capital). There are four groups, in this order: Machine Learning, Deep Learning & Statistics · Python, Data Quality &
  Cloud · NLP, LLMs & Evaluation · Client Delivery & Communication (`cv.skill_groups` in `config/settings.yaml`: each fact-bank skill
  goes to one group by name or by the fact-bank group it sits in; `skills.core` and `cv.skills_always` are never cut; inside a group
  the skills are ordered by the JD keyword weight they carry, then by the fact bank's order, a pinned skill has no edge; a skill appears once:
  "Kubernetes" and "Kubernetes (AWS EKS)" are one (the qualified one stays), and an umbrella name from `cv.skill_umbrellas` (AWS) goes when a
  specific skill of it (AWS Lambda) is listed; TypeScript and JavaScript (`cv.skills_when_asked`) are shown only when the ad names them; a group
  with no skill row of its own but `from_terms` shows the matching `terms:` you wrote on your bullets, e.g. Client Delivery & Communication).
  A group the fact bank has nothing for is left off and the review
  screen says so. A project with an `award:` that the JD does not rank in its top three moves to Certifications & Achievements as a
  bold first line. An experience or project with `situation:` in the fact bank opens its first bullet with "<situation>: " (STAR);
  the model never writes it. Length rules, measured on the PDF: a bullet is at most 3 lines (longer: back to its fact-bank wording,
  else split at a sentence or semicolon, else reworded shorter by the model, else reported), the profile at most 7 lines (the hook
  sentence first), the 12pt headline exactly one line (its trailing stack list is dropped first). The contact line shows the
  portfolio as just "Portfolio". Education is `\roleNoLoc` (school with its location, dates, the degree with its grade and note) and, below, a muted
  small line with the modules and `details`. The header is the 22pt navy name, a 10pt contact line with short links (no
  `https://`) and an italic muted status line, all joined with ` · `. The certifications are one bullet joined with ` · `, each
  with its date in italics. A separator may end a line, never start one. Dates always read `Mon YYYY – Mon YYYY`
  (`cv.date_separator` in `config/settings.yaml`: `" – "` by default, `" to "` is the other choice; a fact-bank date without
  months, such as `2025 – 2026`, is printed as it is: a month is never invented). **Bold is only** what the macros make bold: the
  employer (and the school), the project name, the skill-row labels, the badge, the section headings, and the `result:` text of a
  bullet in `\num{}` (the measured outcome you wrote in the fact bank, found in the bullet even with up to two extra words inside
  it; a bullet that words it differently shows no bold and the review screen says which). The profile has no bold.
  **Italics are** the job title, locations, the degree line, a project's stack line, a certification's date, a publication's
  venue, and `(Expected)` / `(coursework)`.
  A fact-bank experience with `employment_type: freelance` prints
  as `Freelance · <location>` on the bold line (no "Self-employed"); a bank that predates the field and has an org starting
  "Self-employed" / "Freelance" is treated the same, with the location taken from `location:` or from the rest of the org
  text. Optional experience/education fields: `location`, `employment_type`; education: `location`, `modules` (a list, printed
  on the muted line under the degree), `grade`, `note`, `details`. Optional top-level sections, printed only when page 2 is short:
  `publications` (strings or `{title, venue, date}`), `languages` (strings or `{name, level}`), `research_focus` (text).

- **Skills** — `skills.core` in `fact_bank.yaml` is the first row ("Core"), in your order, and is never dropped. A skill
  appears once (case-insensitive, first row wins); the other groups list every skill with the JD's first; at most
  `scoring.max_skill_groups` (5) of them, the groups the JD matches least are left off, except the **Databases** row (any row whose name has "database" in it), which is
  always shown when the JD mentions SQL or databases. `careerops doctor` fails, naming the
  skill, if a core skill is on `deny_terms` or no bullet / skill row / project stack mentions it literally; the CV refuses to
  render a denied core skill.

- **Type scale** — the reference's: the name 22pt bold navy, section headings 13pt bold navy uppercase letter-spaced with a rule,
  everything else 11pt except the contact line, each project's stack line, the badge, the GitHub link and the modules line, which
  are 10pt (`\small`). (`templates/cv2.tex.j2`; `tests/tailoring/test_cv_template.py` and `test_cv_typography.py` measure the
  rendered PDF with `pdftotext -bbox`.)

- **Typography, applied at render time only** — after every faithfulness and claim check has run on the original wording, the text
  that reaches the PDF is tidied (`tailoring/typography.py`; `report.json` keeps the checked text). No em dash survives: `—`, `--`
  and a spaced hyphen used as a dash become a comma, a colon (after a name or title, and always in a project name) or parentheses
  (a pair around an aside with its own comma). Every hyphenated word is closed up (`class-imbalance → class imbalance`, `19,213-gene → 19,213 gene`), with
  the special spellings of `config/typography.yaml → rewrite` first (`e-commerce → ecommerce`, `5-star → five star`, `start-up → startup`, `multi-omics → multiomics` ...); its `protected` list
  (`scikit-learn`, `k-means`, `TF-IDF`, `gpt-oss`, `faster-whisper`, `Skip-gram`, `CIFAR-10`), the handles in your own profile links, number ranges (`3.9–3.11`), names with a
  number (`GPT-4o`), URLs and e-mail addresses are never touched, and
  neither is a hyphenated form that a JD keyword spells with the hyphen (the ATS looks for the JD's own spelling). The review screen
  has a `Typography:` line (em dashes left: must be none; en dashes used as prose dashes; results in bold) and lists the hyphenated
  words still in the PDF (none, unless a JD keyword spells it so). A project's award is printed in its heading only: it is
  taken out of that project's bullets and results, and a model rewording that adds it to a bullet is refused.

- **Selection** — bullets and projects are ranked by JD keywords plus the company domain and topics the JD names (customer,
  consumer, behaviour, segmentation, validation, statistics, bank, payments ...; `tailoring/domain.py`): a bullet that names one that
  no chosen bullet names yet gains like a keyword, and the score adds to its rank. The review screen prints the top 10 bullets and
  the top 10 projects with their scores (keyword + domain; `*` = on the CV). Inside a role or project the chosen bullets are printed
  in fact-bank order, not in the order they were picked.

- **Bullets** — chosen in code by greedy marginal keyword gain (ties to the more recent entry): newest entry 4–5, the others
  3–4 (`scoring.cv_bullets`), 5 projects (`scoring.cv_projects`) with 2–3 bullets each. A bullet whose words overlap a chosen
  one (Jaccard ≥ 0.6) is never added. Then content is added until page 2 is at least `scoring.min_fill_page2` (0.96, within two lines of the bottom margin) full,
  measured from the PDF (y of the last text line on page 2 over the text area), in this order: **further projects** (best JD
  match first, up to `scoring.cv_projects_max` = 6), more bullets of the shown projects, **more experience bullets** (by
  relevance), then the **optional fact-bank sections** (`publications`, `languages`, `research_focus`, in that order), and last
  the **linespread**, raised from the reference's 1.03 in steps of 0.02 up to `scoring.cv_linespread_max` (1.09) and stopped at the first value
  that fills page 2. Nothing is invented and the font is never shrunk below 11pt; if the content runs out the CV ends short
  and says so. The shrink step runs only when the CV is over 2 pages. `cv_pages`, `page2_fill` and `fill`
  (`added`, `sections`, `linespread`, `reached`) go into `report.json`; the review screen has a `Fill:` line (page-2 fill against
  the target, projects shown, optional sections, linespread) and flags a CV below the target.
- **Results** — give a bullet an optional `result:` in the fact bank (a measured outcome). The rephrase step may work it in;
  its numbers count as evidence for that bullet only. `report.json → needs_result` lists the bullets on that CV that have none
  (yours to fill in, never invented).
- **Keyword placement** — a JD keyword the CV text lacks (bullets, profile and skills together) is offered, as "include if it
  reads naturally", to the first selected bullet whose own `terms` evidence it: at most 2 per bullet, each keyword once. The
  faithfulness guard already counts a bullet's `terms` as evidence, so it is unchanged. `report.json → placement` and the
  calibration report list per JD what was placed, not placed (the model left it out) or blocked (guard refused), and the
  keywords no selected bullet could take.
- **Rewrite rules** (`tailoring/rewrite_rules.py`, tighten-only): a reworded bullet must keep any qualifier its source has
  (team, contributed, coursework, MSc, prototype(d), proof of concept/PoC, hackathon, assisted, supported, helped, or a form
  of it); it is rejected for adding filler (`leveraging|incorporating|applying … capabilities|techniques`, `using <gerund>
  with`, `<noun> communication demo`, a trailing `, demonstrating|showcasing|emphasizing|achieving|forming|highlighting|
  leveraging …` clause, a keyword tacked on bare after a phrase "… on a free T4 regression"); keywords it writes in take
  canonical casing (LLM, GenAI, NLP).
- **Letter evidence and fallback** — the two bullets a letter is written from are chosen in code from the bullets on the CV:
  every bullet (experience and projects, verb-led or noun-led) is ranked by overlap with the JD keywords **and** the JD's
  company domain (bank, fintech, payments, customers … see `tailoring/domain.py`; an entry's name counts, so "Banking
  Customer Churn" carries the domain its one-line bullet does not say, in the ranking and in the hook check), best first, the
  second from a different entry; review shows the two ids as `Letter evidence: …`. A
  draft with "my work on built …", "mirrors my work <noun>", a paragraph that starts with a bare verb, or a sentence that
  pastes a bullet — verbatim, or with **70% or more of its words in one bullet** ("I" and bracketed text left out) — is
  rejected like any other. A hook that says the company "mirrors / reflects / aligns with" my work must share a domain term
  with that work (a bank mission cannot mirror a cancer-research agent). With `letter.on_failure: template`, when the draft
  fails after the retries `letter_rules.fallback_letter` writes it (the default, `none`, writes no letter: next bullet), all first person: *<company hook sentence> I'd like to bring my experience to
  that work: at <org 1>, I <bullet 1 as a clause>.* · *At <org 2>, I <bullet 2 as a clause>.* · *I have not used <Gap> yet.*
  (only with a gap) · the closing question. A noun-led bullet reads "I worked on XGBoost churn classifier in R …". Gaps keep
  the JD's casing or a canonical spelling (Azure, Power BI, AutoGen, DSPy).
- **Letter model and failure policy** — the cover letter is written by Groq's `openai/gpt-oss-120b` (`llm.models.cover_letter`; every other
  task stays on `gpt-oss-20b`, all letter checks unchanged); the review screen lists tokens per model. When all three drafts are rejected,
  `letter.on_failure` decides: `none` (default) = **no letter** — no PDF, review shows `NO LETTER — write your own or apply without`, and
  approving needs you to type `no letter` (`l` tries again); `template` = the flagged template fallback above (approve with `send fallback`).
- **CV profile** — assembled in code (`tailoring/profile.py`), no model: the hook's `profile_line`, then one or two `profile_facts` chosen by
  keyword score (the same ranking) as sentences ending in a full stop; a fact that repeats the title ("Data Scientist") or only restates the
  sentences before it is left out. A project whose `award` text is already in its `name` prints it once.
- **Letter opening** — the claim half of the opening sentence (after "mirrors", a dash, ":" or ";") may use only the two
  evidence bullets; `actionable insights, user-centric, tangible outcomes, instantly, accurately, precise, smooth
  integration, efficient, exactly what I built` are vague filler unless the evidence says them. Rejected → one retry naming
  the phrase → any remaining problem → the template fallback above.
- **No-regression rule** — a rephrased or wording-pass bullet is kept only if it still hits every keyword (literal) the text it
  replaces hit; otherwise the earlier wording stays and `report.json` warns `no-regression: kept the earlier wording …`.
- **Wording pass** — after rephrasing, a keyword the CV has only in another form ("prototyped" for "prototyping") gets its
  exact form in the one bullet that carries its stem; the guard checks it like any rephrase and it is kept only if the score
  does not drop.

### Importing your LinkedIn profile (`careerops import-linkedin`)

Save your LinkedIn profile as a PDF (profile page → *Resources → Save to PDF*, or the browser's Print to PDF; click *Show all*
on Experience, Education, Licenses & certifications and Projects first: LinkedIn prints only the first few) and run

```
careerops import-linkedin config/reference/linkedin.pdf     # --mock writes under data/mock; --fact-bank/--out/--diff override the paths
```

It extracts experience (title, organisation, employment type, dates, location, bullets), education (degree, dates, grade,
modules, the "Research Focus" text), projects, certifications, publications and languages. It writes
`config/profile/fact_bank.proposed.yaml` (fact-bank schema, git-ignored, never read by the pipeline; entries that match one in
your bank keep its `id`; bullets carry LinkedIn's wording and an empty `terms:`) and `data/linkedin_diff.md`, which lists every
mismatch with `fact_bank.yaml`: title, organisation, dates, location, employment type, degree, modules, grade, and what is on one
side only. A section that LinkedIn printed only in part is reported as *unverified*, not as a mismatch. `fact_bank.yaml` is only
read; you merge by hand. Limits: the parser reads pdftotext's output of LinkedIn's own layout and drops what it cannot place; a
description LinkedIn cut off with "..." is marked in the diff; project technologies and links are not in the PDF.

### The hook (`hook.enabled`, default on)

Every CV carries **one hook**, taken from your **`hook_library`** (top level of `config/profile/fact_bank.yaml`, written by you) and **never
written by a model**. An entry is `{id, proof (a bullet id), terms, headline, profile_line, letter_line}`. For each job code scores every entry as
the **sum of keyword weight x [the keyword is one of its `terms`]** and picks the highest (a tie goes to the earlier entry; an entry that scores
nothing is never picked), then uses its strings **word for word**: the `headline` as the **12pt navy line under the name**, the `profile_line` as
**profile sentence 1**, the `proof` bullet **moved to the top of its role** (or project) with the entry's `situation:` opener, and the `letter_line`
as **sentence 1 of the letter** when letters are on. Nothing is typeset or reworded. An entry whose proof bullet is not in the bank, or that names
a deny-listed term, is skipped and `hook_notes` says why. The number is the first count-like string in the proof bullet's `result:` or `text`
(a model version like "Haiku 4.5" is not a result), never invented; a proof with none carries the flag `hook has no number`. A bank with **no
library, or no entry that matches the ad**, gets a hook from a fixed **code template** over the best proof bullets instead (checked: the number is
in it, no new number, no deny-listed term, no unsupported qualifier or name, none of *passionate, proven, dynamic, results-driven, extensive,
strong, skilled*, within its length, not a paste of any bullet), and the note says `no hook_library entry matched`. A reworded proof bullet that
loses the number is kept in its fact-bank wording. `tests/golden/anaplan/` holds the hand-made Anaplan application (CV, letters A and B by hand, the ad,
the keyword weights): the quality bar, checked by `tests/test_golden_anaplan.py` (strict ATS 72.5 within 0.1, the hook in all three places, two
pages, letters of at most 250 words) and, on what the pipeline writes, by `tests/test_golden_pipeline.py`. `python -m careerops.golden <pipeline
CV.pdf>` lists the lines only in your pipeline's CV and the lines only in the golden one.

### The note (`data/outputs/<job>/note.md`)

One page per job, written with the CV and kept current by edits and letter changes (`n` in review opens it): **Hook** (problem, proof, number),
**ATS** (the three numbers and the gate, per letter variant), **Learn list** (the ad's terms the bank cannot claim, deny-listed ones marked
*(confirmed absent)*), **Contact** (`TODO: you find on LinkedIn: hiring manager, <team>`: nothing here ever fetches linkedin.com), **Question**
(one, naming a term of the ad), **Flags** (visa or sponsorship wording, location or office policy, seniority or years asked, security
clearance, IR35, travel, the posting age, and anything the pipeline flagged) and **Gap** (empty now: letter B, the one that admitted a gap, is no longer made).

### Priority: sponsorship is a weight, never a filter

I hold the UK Graduate visa, so no job is dropped for sponsorship: not an employer missing from the register, not a recruitment agency,
not a JD that says "right to work without sponsorship" or "we do not offer sponsorship". And no job is dropped for being old.
`priority.py` ranks, lexicographically: **the freshness tier first, then P = F x S inside it**.

    tier 0  posted at most 24 h ago      tier 1  at most 72 h      tier 2  at most 7 days      tier 3  older (or undated)
    P = F x S      F = honest ATS ceiling (0-1)
    S = 1.00 exact register match · 0.90 fuzzy · 0.75 not on the register / agency / unknown · 0.65 the JD says no sponsorship

(the lowest applicable S; the weights are `priority.sponsor_weights` in `config/settings.yaml`). The old recency factor R = exp(-days / 14) is
gone: it drove a 64-day-old posting that was still open down to 0.01. The age comes from **`posted_at`, the time the source says** (stored on the job
with `posted_at_source`): Greenhouse `first_published` (not `updated_at`, which resets on a repost: the Anaplan ad was 69 days old, not 10),
Lever `createdAt`, Ashby `publishedAt`, Reed `date`, Adzuna `created`, a Gmail alert's arrival time (an upper bound); anything else counts
from the day discover first saw it (`First seen:`, `data/discover/first_seen.json`). The inbox file carries `Posted:` (an ISO date or time) and
`Posted via:` headers. The order applies to the `careerops live` inbox (it prints tier, age and P), `careerops live --fresh` (tier 0 only),
`careerops discover`'s listing, the manual queue, `careerops status`, and `discover --report` counts the tiers.
The review screen shows `Priority P=... = F ... x S ...`, the age in hours (`Posted: posted 5 h ago (Greenhouse first_published), tier 0`) and, as an
information line (not a flag), the sentences of the JD that speak of sponsorship or the right to work. The register notes ("Not on sponsor register", fuzzy match) are shown on the `Sponsor:` line,
not under `FLAGS:`, and they no longer stop the judge gate from approving.

### Sponsor register

`careerops sponsors` reads the gov.uk page, finds the CSV link and downloads only if the link (or its date) is new; the file
is stored as `data/register/YYYY-MM-DD.csv.gz` with a `register_snapshots` row. Column names come from the file's own header
(a missing field stops the run, naming it). `companies` / `company_events` record added, removed, readded and rating changes
(one transaction; a same-day rerun does nothing); jobs of a sponsor that lost its licence or dropped from A get a flag.
Triage matches a job's company against the register (case, punctuation, Ltd/Limited/PLC/LLP, `&`/`and`, both sides of `T/A`)
and stores `sponsor_match` = `exact` | `fuzzy(0.95)` | `none` plus route and rating. **Not on the register is a note
("Not on sponsor register") and a lower S, never a skip.** "Register not loaded" appears only when no snapshot exists.

### Outcomes

`mark-applied` (and `apply`) log an `applied` event; add the rest with `careerops outcome`. `careerops outcomes` prints the funnel
split by role family, tier, CV pages and sponsor match. A job applied more than 28 days ago with nothing logged since counts as
`no_response` in the report (computed, never stored). `--import-archive` is read-only on the archive: it only lists folders.

### Role tiers and queue order

`config/settings.yaml → roles` decides which titles are in scope and in what order they are worked:

| Tier | Family | CV headline |
|---|---|---|
| 1 | `data_scientist`, `ai_engineer` | Data Scientist / AI Engineer |
| 2 | `ml_engineer` | Machine Learning Engineer |
| 3 | `data_analyst` | Data Analyst |

Junior and mid only: titles matching `roles.levels.exclude` (Senior, Lead, Staff, Principal, Manager, III/IV,
Intern) are skipped with `level excluded (…)`; titles in no family are skipped with `title not in target families`.
Each family lists preferred fact-bank project ids — a prior for the tailoring prompt, not an order. If you rename a
project id in `fact_bank.yaml`, triage and tailor stop with an error naming the stale id.

Every queue (tailor, review, apply, Excel) sorts by

  ( honest ceiling < `roles.floor`,  tier,  −honest ceiling,  −posted/first-seen )

so jobs that can honestly reach 70 % come first, then tier 1 → 3, then best fit, then freshest. The tailor
queue uses `ceiling_pre` (taxonomy keywords vs the fact bank, computed at triage with no LLM); once a job is
tailored the LLM-keyword `ceiling` replaces it. `tailor --limit N` gives tier 3 at most
⌊`tier3_max_share`·N⌋ slots (5 of 25 by default) unless tiers 1–2 run out.

`careerops run --limit 25` (discover + tailor + export) is what `scripts/crontab.example` schedules.
`--mock` runs the whole thing with a deterministic stand-in model — use it after editing templates or the fact bank.
It is fully isolated: its own database and outputs under `data/mock/` (`paths.mock_root`), no network, and one seeded
fixture job (the Northwind AI Engineer JD), so it never touches your real jobs. `data/mock/` is safe to delete.
`careerops review --mock` after a mock run reviews that mock job.

## What the review screen tells you

- **ATS strict CV / claimable ceiling / CV + letter** (`ats.py`) — the three numbers in percent. A keyword is **not a hit where it is only
  mentioned in a negated way**: *not, no, never, haven't, have not, yet to, without, lack, gap, learning* (as a verb) within 6 tokens before it
  in the same sentence ("I have not yet used LightGBM"), and a text that says it lacks a term anywhere does not claim it elsewhere. *Strict* is the
  CV alone and is the one that is gated (band 70-85): below 70 with a ceiling of 70 or more, tailor once more; below 70 with a ceiling under 70,
  **STRETCH**: kept and labelled, never dropped (a flag on the job, so the judge gate never auto-approves it); above 85, de-stuff. *Claimable
  ceiling* is the weight share of the ad's keywords the fact bank backs (a deny-listed term never counts). *CV + letter* counts a keyword when the CV or
  the letter has it; the *letter alone* is shown and never gated.
- **Hook** — `Hook <hook id>: <problem> | proof <bullet> (<employer>) | number <n> [library|template]`, its headline, profile sentence 1 and letter sentence 1, and `!! hook missing from the CV` if one
  of the three places did not take it (see "The hook" below).
- **Letters** — A with its words and its CV + letter and letter-alone numbers, B as `none (why)`; `l` cycles A → none, `n` opens
  `note.md`.
- **ATS literal** — conservative whole-word match on the PDF's text layer (negation-aware, see above), singular/plural (trailing *s*/*es*) taken as
  one term (*prompt* = *prompts*); the 70–85 % band applies to this.
- **ATS stemmed** — also equates *communicating/communicate*, *modelling/modeling*, *end-to-end/end to end*,
  *classifier/classification*, *visualisation/visualization*.
  Literal → stemmed gap = wording, not substance.
- **T1 · Data Scientist** (second line) — the role tier and family; the CV headline uses the family's.
- **Flag `new rules would skip: …`** — added by `retriage` to an already-tailored job that today's rules would reject.
- **Honest ceiling** — best score the fact bank can support. Below 70 % → the loop doesn't waste calls; you decide.
- **Genuine gaps** — JD keywords with zero evidence. They stay off the page.
- **Guard blocked** — model claims that were reverted to the canonical bullet (new numbers, deny-listed tools,
  unevidenced keywords). Frequent blocks on one bullet = add honest `terms` or tighten the prompt.

## Verified here vs not

| Verified (tests / local run) | Not verifiable from the build sandbox — check on first live run |
|---|---|
| Register snapshots, diff, T/A and Ltd/PLC matching, exact/fuzzy/none (mocked HTTP, fixture CSVs) | the live gov.uk download: link still found, header fields as expected, a real day-to-day diff |
| Greenhouse/Lever parsers on recorded payload shapes | Ashby field names (coded defensively) |
| Gates, scorer, stemming, line-wrap/quote normalisation | Groq model id + free-tier caps (change often) |
| Faithfulness guard blocks invented numbers/tools/keywords | Real-LLM output quality on your JDs |
| Full tailor loop → 2-page xelatex PDF, no hyphenated words | Live ATS forms (Greenhouse/Lever/Ashby markup varies) |
| Router: JSON mode, auth header, 429 → fallback, daily quota | Adzuna quota and description length |
| Playwright fills a test form, uploads both PDFs, never submits | |
| Hook, negation-aware ATS, letters A / B, note, freshness tiers, scheduler script (fixture bank, MockLLM, fake network, a fake `careerops` for the script) | the real CV's strict ATS against the golden 72.5, a real Anaplan run, the Windows task itself |

`career-ops` and this pipeline stay off every CV and letter — both are on the deny list.
