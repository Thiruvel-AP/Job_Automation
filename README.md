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
│   ├── googleapi.py              the one paced client for Sheets and Gmail with your token; refuses any send
│   ├── tracker.py                the Google Sheet: one row per applied job, found by header text (`mark-applied`, `tracker sync`)
│   ├── contacts.py               who to write to: the ad, the company site, a name you give, Hunter; MX domain check; do-not-contact list
│   ├── outreach.py               Gmail DRAFTS (never sent) to those contacts after you apply; claim guard on the body
│   ├── discovery/                where companies and jobs come from
│   │   ├── register.py           UKVI sponsor register: download, parse, match · M1 snapshots + diff
│   │   ├── companies_house.py    M2 bulk join → company no., SIC codes, sector
│   │   ├── ats.py                M3 slug probe, verification, careers-page detector
│   │   ├── sources.py            Greenhouse · Lever · Ashby · Adzuna fetchers
│   │   ├── intake.py             `add-url` / `live --url`: one job ad from its link into the inbox (+ CV-Library pages)
│   │   ├── robots.py             robots.txt rules (RFC 9309) cached 24 h; hosts never fetched (linkedin.com)
│   │   ├── applykind.py          apply_kind (greenhouse | lever | ashby | workday | employer_other | aggregator_only) from a URL; the hosts alert resolution never fetches
│   │   ├── http.py               the polite client discovery shares: 1 req/s per host, robots, a 401/403/429 stops the host
│   │   ├── discover.py           `discover --ats --reed --adzuna --gmail-alerts --jobbank`: filters, dedupe, inbox files, manual queue, report
│   │   ├── registry.py · health.py   config/sources.yaml (every source and how it is read) · the health check of each source
│   │   ├── countries.py · jobbank.py country names/codes/places (the `listed_only` rule) · Canada's Job Bank open-data CSV
│   │   ├── reed.py · gmail.py    the Reed Jobseeker API · read-only Gmail API (OAuth by hand) and the alert-email parsers
│   │   ├── gates.py              rule triage (title, seniority, location, SRE, line mgmt…); sponsorship is never a rule
│   │   ├── priority.py           freshness tier, then P = F x S x L x R (L = level fit, R = role fit from the title's family): orders the live inbox, discover's listing and status
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
│   │   ├── company_letter.py     `letter.mode: company`: the 200-320 word letter in up to seven short paragraphs (hook, what it shows, JD skills, roll-up, why this team, optional honest gap, the 20-minute ask) under a user guard and a company guard
│   │   ├── letters.py            `letter.mode: hook`: the letter ASSEMBLED in code (library letter line + two sentences from two bullets + 20-minute ask), the our/we/linked-phrase guard, relocation line, greeting
│   │   ├── render.py             Jinja2 → xelatex, page count, pdftotext
│   │   └── learning.py           M4 ranked skills-gap list
│   ├── llm/                      router.py (Groq/Ollama, quota fallback) · mock.py (deterministic)
│   ├── review/                   workflow.py (approve/edit/reject → preferences) · judge.py (LLM judge) · outreach.py (M4) · prep.py (interview prep pack)
│   ├── autoapply.py              gated auto-apply (Greenhouse / Ashby / Lever): the ONLY module that clicks a submit control, only for a job that passed the gate
│   ├── apply/freetext.py         drafts free-text form answers from the fact bank, guards them, you approve once (data/answers/library.jsonl)
│   ├── apply/formloop.py         scans EVERY field of a form (table before filling), runs the free-text loop (required or optional), logs formloop.jsonl
│   ├── apply/assist.py · filler.py   Playwright: fill, upload PDFs, never submit · the ONE field filler (applicant.yaml, then answers.yaml) that assist and autoapply share
│   ├── discovery/liveness.py · pretailor.py   is the posting still open (closed ones are never tailored) · the gate every job passes before tailoring
│   ├── discovery/repost.py · blacklist.py · channel.py   repost / evergreen? flags (information only) · your blacklist.yaml · same role via another channel (agency vs direct)
│   ├── apply/ats_vendor.py       host → vendor (greenhouse, lever, ashby, workday, icims, successfactors ...) and the direct address of the real form (Greenhouse `gh_jid` → job-boards.greenhouse.io)
│   ├── apply/formagent.py · submit_guard.py   the form agent for any other vendor: no submit action, a submit control has no ref, 40 steps per job / 8 per page, logs formagent.jsonl
│   ├── apply/knockout.py · attest.py   the knock-out pre-scan (sponsorship, years, degree, salary floor, on-site, clearance) · attestations code never answers
│   ├── apply/autogate.py · answers.py · autolog.py   the auto-apply gate · your answers.yaml (matching, save) · data/live/applied_auto.csv
│   ├── apply/live.py · snapshot.py  `careerops live` loop · read-only archive snapshot + compare
│   ├── reporting/                excel.py (Excel view of SQLite) · calibrate.py (`careerops calibrate`) · replies.py (reply watch) · followups.py
│   └── training/rewards.py       Phase 2 GRPO reward functions
├── config/
│   ├── settings.yaml             paths, sources, gates, sectors, scoring band, LLM providers
│   ├── typography.yaml           CV typography: special hyphen spellings, the protected terms (every other hyphen is closed up when the PDF is rendered)
│   ├── taxonomy.txt              ATS keyword vocabulary
│   └── profile/                  fact_bank.yaml (the only truth the tailor may use) · applicant.yaml · answers.yaml (your answers to form questions, for `live --auto`) · blacklist.yaml (yours, read only)
├── templates/                    cv.tex.j2 · cover_letter.tex.j2
├── docs/                         PLAN.md · IMPLEMENTATION.md
├── scripts/crontab.example · auto_census.py (read-only: how the database's jobs fare against the auto-apply gates)
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
    ├── secrets/                  gmail_client.json (you put it here) · gmail_token.json (read mail, create drafts, edit the Sheet); git-ignored like all of data/
    ├── outreach/                 do_not_contact.csv (`value,note`: addresses and domains no draft goes to; `careerops init` creates it)
    ├── live/                     state.json (resume) · one summary .md per live session · applied_auto.csv (every auto-apply run)
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
   Alaska Native` is not `Indian`). A select, a radio group or a checkbox group has options; no match leaves the field empty. A free-text
   box (a text box or text area) for an `eo_*` question of `answers.yaml` is typed with that entry's `answer` as you wrote it (`Male`), unless the
   box already holds text; a text box for a question listed only in `applicant.yaml` stays empty. Every equal-opportunities field on the form (ethnicity, gender, disability, religion, sexual orientation, veteran status,
   pronouns ...), listed or not, is outlined blue and printed as `CHECK EO ANSWER [ethnicity]: <question> -> <option or EMPTY (why)>`
   (`[not listed]` for the ones you did not list, which are always left blank; an unmatched list prints its options). A single check box
   ("Person with disability", "Veteran") whose answer is No stays unchecked and prints `-> left unchecked (answer: No)`, not EMPTY. `careerops
   doctor` validates the block when there is one: a mapping of questions, each with a non-empty `match_any` list of texts, an optional
   `answer` text, an optional `question` list of valid regexes (required unless the key is `ethnicity`), no other keys. The `eo_*` entries of
   `answers.yaml` (`eo_gender`, `eo_disability` ...: their `match` texts are the question, their `options` the options you accept, best first, else their
   `answer`) are read as well, after the ones in `applicant.yaml`: with `options: [Man, Male]` a "Man / Woman / ..." list gets "Man". An invalid
   entry is never used to fill anything.

## Daily use

```bash
careerops sponsors                 # gov.uk register: download only if its link changed, store data/register/YYYY-MM-DD.csv.gz, diff vs the last snapshot
careerops discover                 # pull + triage; skipped jobs keep their reason
careerops discover --ats --reed    # find open jobs and write them to data/jds/inbox/ (see "Finding jobs: careerops discover")
careerops discover --adzuna --gmail-alerts   # Adzuna results and LinkedIn / CV-Library / Indeed / Reed alert emails, resolved to full ads
careerops discover --jobbank       # Canada's Job Bank open-data CSV: matching rows are listed in data/discover/listed_only.csv
careerops discover --report        # counts per source, resolved vs the manual queue, the freshness tiers; then the inbox's liveness (open / closed / uncertain: one request a second per host, answers kept 6 h) and its repost and evergreen? counts
careerops discover --report --no-liveness   # the same without fetching any ad
scripts/discover.sh                # what the scheduler runs twice a day: discover --ats --reed --adzuna --gmail-alerts --report, logged to data/logs/discover_<date>.log (docs/scheduler.md)
careerops sources check            # health of every source in config/sources.yaml -> table + data/discover/health/<date>.csv (also ends every discover run)
careerops sources list             # the registry, no network
careerops retriage                 # re-run triage on all non-applied jobs (after changing roles/gates)
careerops retriage --ats           # score the `skipped (ats)` jobs again after the fact bank changed (dry run: a scratch folder; --apply keeps the new tailoring and brings back every job that now reaches the review band)
careerops retriage --decided       # dry run: inbox files that are copies of a job you skipped or applied to (--apply moves them to data/jds_done/)
careerops skip ID|URL|FILE         # (also --id ID, --url LINK) skip a job for good (--id ID still works): data/state/decisions.jsonl gets its keys, its JD leaves the inbox and approved/ folders; a job that is only a JD file (never in the database) or only a link is recorded the same way
careerops why ID|URL|FILE|ats:KEY  # every decision recorded for a job and the key that matched (id, ats:<vendor>:<board>:<id>, URL, JD file name, fingerprint); an OLD id the database and folders no longer hold is found in the decision log / events, with its keys and source (board, Gmail alert, shortlist log)
careerops backfill-decisions [--apply]   # the decision log's missing lines (skipped / applied jobs of the database, dated by the real event; JD files in jds_done/ the database never held); a dry run unless --apply
careerops unskip --id ID|URL|FILE  # undo a skip: the job, its copies and their JD files go back to the inbox (an applied job is final)
careerops unskip --shortlist [--match TEXT | --all]   # lists the ads you skipped at the shortlist and the parked ones; --match / --all brings them back
careerops add-jd --company X --title Y --url U --file jd.txt   # LinkedIn/email/agency roles (--file: UTF-8, at least 500 bytes, else a one-line error)
careerops add-jd --id adzuna:123 --file full_jd.txt            # replace a truncated Adzuna snippet
careerops add-url https://job-boards.greenhouse.io/monzo/jobs/123   # fetch one job ad into data/jds/inbox/ (see "Adding a job from its link")
careerops live --url URL           # the same fetch, then live works on that one file
careerops live --clipboard         # a site that blocks scripts: copy the job text, then this (asks Company / Title / URL; Windows clipboard via WSL)
careerops live --id manual:ff68fffd015a   # work on one job the database already has: its JD file from the inbox or approved/, or written again from the stored ad (a skipped / applied job is refused)
careerops judge-report             # the LLM judge vs your decisions: agreement, kappa, false approves (see "The LLM judge")
careerops tailor --limit 15        # CV (two full pages) + note.md per job (letter.mode: `off` by default = no letter; `hook` = an assembled letter, `draft` = a model-drafted one, `company` = a 200-320 word letter in up to seven paragraphs, v2.3), in queue order → data/outputs/<job>/ (+ report.json)
careerops tailor --one-page        # the old one-page CV (templates/cv.tex.j2) instead
careerops tailor --id JOB          # exactly this job (it must be 'triaged'; --limit is ignored)
careerops shortlist               # score the inbox (T, zero tokens), file what a hard stop disqualifies, offer the best 8; [a] / [1 3 5] approve ([a] leaves out a `possible repost of a job you skipped`: type its number), [s n] skip for good, [x] skip the rest of the list (see "Fewer, better-matched applications")
careerops shortlist --region uk,ie --size sme-first   # v2.4: the region(s) and the company size (asked once a day at a terminal, else remembered; see "Region and company size")
careerops discover --ats --gmail-alerts --report --region uk   # ... ends with `in region 41 · out 112 (parked) · unknown 9 | SME 14 · Large 22 · size? 31`
careerops live --max 5             # the ads you APPROVED (data/jds/approved/<date>/), one job at a time, you at every gate (see "Live sessions"); --include-unapproved takes the whole inbox (never with --auto)
careerops live --fresh             # tier 0 only: the approved files posted at most 24 hours ago (older ones stay where they are, untouched)
careerops patterns                 # replies / interviews / rejections by role family, ATS kind and triage band, from the Sheet's Status column (information only; --db: the outcome log)
careerops live --dry-run           # the same, stopping after tailor + review: no apply step
careerops live --auto --no-submit  # gated auto-apply, step 1: fill the forms of the jobs that pass every gate and screenshot them, never click Submit (see "Auto-apply")
careerops live --auto              # step 2, needs `apply: mode: auto`: jobs that pass every gate are submitted; the rest go to your review as before (`--confirm`: ask before every submit)
careerops live --dry-run --retailor   # a JD already started is tailored again (after a fix) instead of resumed; never an applied job. NOTE: --dry-run opens no form; to see a form filled use the assist (no flag) or --auto --no-submit
careerops live --retailor --url URL --record   # v2.4.1: the assist fills the form (you press nothing), and data/dryruns/<date>_<company>_<role>_<id>/ keeps fields.md/json (value, source, status), form_full.png, cv.pdf, letter.pdf/txt, motivation.txt
careerops live --retailor --re-extract   # ... and the JD's keywords are extracted again (a job keeps its FIRST extraction otherwise, so --retailor gives the same keywords and ATS)
careerops tailor --id JOB --re-extract   # the same flag on tailor / run
careerops review                   # approve / edit (opens YAML of bullets) / reject
careerops apply                    # fills basics + uploads PDFs, YOU press submit, paste the reference
careerops mark-applied --id JOB    # an application made outside the assist: archive it and log 'applied' (then the Sheet row, and the drafts if outreach.mode: drafts)
careerops tracker sync             # every applied job to your Google Sheet, one row per job, updated never duplicated (--dry-run prints the rows, no network)
careerops contact add JOB "First Last" --role "Hiring Manager" [--email x@y]   # a person you found on LinkedIn; no --email asks Hunter once
careerops outreach draft JOB [--to you@gmail.com]   # Gmail drafts with the CV attached, never sent; --to = one test draft to that address
careerops outcome --id JOB --event screen --date 2026-10-12 --note "phone"   # applied|acknowledged|screen|interview|offer|rejected|withdrawn
careerops outcomes                 # the funnel applied → any reply → screen → interview → offer: counts, rates, 95% Wilson intervals
careerops followups                # applications past their follow-up day with no reply, oldest first, with the contact if known (read-only)
careerops outcomes --import-archive          # DRY RUN: list <paths.applications> and <paths.applications_auto> /<Company>/<Role> as approximate 'applied' events
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
UK; Reed, Adzuna and CV-Library are UK sites); closed postings are dropped; a job you already skipped or applied to (by job id, or
by company family + normalised title, so another board's copy counts) is never written again and is counted as `already_decided`; a job
already in the database, in `inbox/`, `done/` or `rejected/`, in the application archive (read only, company and role folder names) or
earlier in the same run is dropped (company + normalised title + place). Sponsorship is never a filter (see "Priority"). `--limit N` caps the new files (default 40), `--dry-run`
finds and ranks without writing, and every run is listed best priority first.

- **`--ats`** reads Greenhouse, Lever and Ashby boards through their documented public APIs. It starts from your companies, in this
  order and no other: (1) the top-level folder names under `paths.applications` (your own folder of past submissions, read-only) and `paths.applications_auto` (the pipeline's archive; names only, nothing
  inside a folder is opened), (2) the employers you list in `discover.sources` (settings.yaml), (3) sponsor-register companies, only
  when they are also one of those (the register is never sampled). For each company it guesses up to four board tokens from the name
  (lower-case, Ltd / Limited / plc / Group / UK / Holdings / Technologies dropped, spaces and punctuation removed: `Monzo Bank Ltd` ->
  `monzobank`, `monzo-bank`, `monzo`) and asks each provider; a token counts only when the API answers 200 with a jobs list. Every
  answer, found or not, is kept 30 days (`discover.ats.board_cache_days`) in `data/discover/boards.json`, so a company is asked about
  once a month; a blocked host or an error is not kept. The run prints, per company, the tokens tried, the board found
  (`greenhouse/monzo`) or none and the jobs that matched the role and place, then the top 10 by priority P with title, company,
  location and URL. A token is a guess, so a board found only through a SHORTENED token (the first word, the first two words, the name
  without Group / Holdings / Technologies: `octagon` for Octagon Group, `focus` for Focus Group) is accepted only if the board's own
  company name matches the folder's at `discover.ats.board_name_match` (0.9). Greenhouse's API gives a name; Lever's and Ashby's do not, so
  a shortened token never vouches for itself there. Otherwise the board is `board_unverified`: no ad is read from it and the run lists
  it ("N board(s) rejected by the board-name check"). If one of them is the right company, name its token under `sources:` in
  settings.yaml (those boards are never name-checked). `discover.ignore_companies` (names, case and spacing ignored) lists archive folders
  that are not companies ("New folder", "AI Engineer"): `--ats` skips them; the folders themselves are never touched. The boards named
  token by token in `sources:` are still read. Posting lists are cached for 6
  hours. **Workable is not read**: its API needs the account owner's token and no public posting API is documented.
  **Workday** is read for the sites you list yourself, `sources.workday: [{company: Acme, url: 'https://acme.wd3.myworkdayjobs.com/en-US/External'}]`
  (also `https://<wdN>.myworkdaysite.com/recruiting/<tenant>/<site>`; empty by default; optional `queries: [...]` and `max_pages` per entry): the
  public JSON list the careers page itself uses (`POST .../wday/cxs/<tenant>/<site>/jobs`), only after the tenant's robots.txt allows `/wday/cxs/`, at
  one request a second, at most 10 list pages per tenant per run. A matching posting (role family, UK place) is read in full once. "Posted Today / N Days Ago"
  becomes the posted date, "30+ Days Ago" is freshness tier 3. An outage (HTTP 422, the maintenance page) is a note and is asked again next run,
  never "zero jobs". Workday careers PAGES are still never fetched.
- **`--reed`** uses the Reed Jobseeker API (`REED_API_KEY`): each query in `discover.reed.queries` runs twice (direct employers, then
  recruitment agencies, whose jobs get `Agency: yes`), then every job worth keeping costs one request for its full description
  (`details_per_run`). An expired posting is closed.
- **`--adzuna`** uses the Adzuna API (`ADZUNA_APP_ID`/`ADZUNA_APP_KEY`, `sources.adzuna.queries`). Its descriptions are snippets, so a
  result is a **link**: it is looked for on the employer's own board (a known board, else the company's name is turned into up to four
  board tokens, each asked of Greenhouse, Lever and Ashby, answers cached 30 days); anything else goes to the manual queue. Adzuna's own
  redirect links are never followed.
- **`--gmail-alerts`** reads LinkedIn, CV-Library, Indeed and Reed job-alert emails with the read-only Gmail API. Per job it extracts
  title, company and link. **linkedin.com, Indeed, CV-Library and Workday pages (`*.myworkdayjobs.com`, `*.myworkday.com`) are never
  fetched**, to resolve an alert or for anything else in discover (only the `/wday/cxs/` list of a site under `sources.workday` is read): the job is looked for on the employer's own board by title (as for
  Adzuna); Reed links go through the Reed API. A job that cannot be resolved stays in the queue (nothing is dropped): it is appended to
  `data/jds/manual_queue.csv` (`ts, source, company, title, location, link, reason, apply_kind`, one row per link; a file from before
  `apply_kind` gets the column added to its header, its rows kept) for you to open, copy the ad and run `careerops live --clipboard`. A
  job that WAS resolved but did not fit in the run's `max_new_per_run` is queued the same way, with the employer's page as its link (it
  used to be dropped; the alerts are now read first, so the fresh jobs get the room before the older board ads). **Resolving**: for every
  alert's company, up to four board tokens (`Monzo Bank Ltd` -> `monzobank`, `monzo-bank`, `monzo`) are asked of the public Greenhouse,
  Lever and Ashby APIs (hits and misses cached 30 days in `data/discover/boards.json`, one request a second per host; a shortened token
  must pass the board-name check above); the alert's title must match an open posting at normalised-title similarity >=
  `discover.title_match` (0.9) with the same level words ("Data Scientist" is not "Senior Data Scientist") and a place that agrees (a
  posting in another town is another job; a missing place agrees with anything). What is found carries `apply_url` and `apply_kind`
  (`greenhouse | lever | ashby | workday | employer_other | aggregator_only`; a Workday host is `workday`, recorded and never fetched).
  The run prints, per source, how many alerts were resolved (`linkedin 12 of 20 (60%)`) and by which step (`board_known`: a board you,
  the archive or the register named; `board_guessed`: found from the company's name; `reed`); `discover --report` adds all runs and
  the manual queue by `apply_kind`.
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

`careerops sources check` and the end of every `careerops discover` run print one row per source (source, method, status, alerts, jobs
parsed, new jobs) and save the full table (reachable, robots.txt, credentials, alerts in the last 7 days, postings, detail, `jobs_parsed`)
to `data/discover/health/<date>.csv` (a second check the same day appends its rows; a file from before a column existed gets it added).
For a `gmail_alerts` source `alerts` is its mails in the last `discover.health.alert_days`, `jobs parsed` the jobs read out of them in
that time (from the saved runs plus the one being checked) and `new` the ads written. It is `ok` only if jobs were parsed; mails that held
no job (a welcome or account mail) give `parsed_none`, with the first subject line in the detail column. Status: `ok`, `quiet` (works,
found nothing), `parsed_none`, `blocked` (robots.txt, 401 / 403 / 429), `unreachable`, `no_credentials` (no `REED_API_KEY`, not signed in
to Gmail), `not_found`, `no_file`, `error`, `disabled`. A source failing never stops the others, and a failing check never fails the command it ends.

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

#### Getting Gmail access (alerts, drafts and the Sheet)

1. In https://console.cloud.google.com create a project (any name), then **APIs & Services → Library → Gmail API → Enable**.
2. Also enable the **Google Sheets API** (same Library page) if you use the tracker.
   **APIs & Services → OAuth consent screen**: user type *External*, fill the app name and your e-mail, add yourself under *Test
   users*, and add the scopes `.../auth/gmail.readonly`, `.../auth/gmail.compose` and `.../auth/spreadsheets`. Leave the app in
   *Testing* (a refresh token then lasts 7 days; run `--gmail-auth` again when it stops working) or publish it for your own use.
3. **APIs & Services → Credentials → Create credentials → OAuth client ID → Application type: Desktop app.** Download the JSON and
   save it as `data/secrets/gmail_client.json` (the folder is git-ignored; never commit it).
4. Run `careerops discover --gmail-auth`. Your browser opens at Google's consent page; you sign in and agree there (career-ops never
   types a password). The refresh token is saved in `data/secrets/gmail_token.json`. A token that was granted anything other than
   those three scopes is refused, and so is a sign-in where a box was unticked. `gmail.compose` is the narrowest scope Google has for
   creating a draft and it is wide enough to send too: what keeps career-ops from sending is the code (see "Outreach drafts"), not the
   scope. A token from before these scopes still reads alerts; the tracker and the drafts ask you to run `--gmail-auth` again.
5. `careerops discover --gmail-alerts` then reads the last `discover.gmail.query_days` days of alerts from the sender domains of the
   sources in `config/sources.yaml` (or, if you list kinds in `discover.gmail.senders`, only those of the four older ones).

### The tracker, contacts and outreach drafts

**Tracker** (`careerops/tracker.py`, `tracker.sheet_id` / `tracker.tab` in `config/settings.yaml`). One row per applied job in the
`Applications` tab, keyed by `Job ID`. `mark-applied` (and `apply`, and `live`, which end the same way) writes it; `careerops tracker sync
[--id JOB]` sends every applied job again and is idempotent: a row is updated in place, never duplicated, and nothing deletes one. The 36
headers of row 1 (A1:AJ1: Job ID, Date applied, Company, ... Status, Interview date, Notes) are matched **by text, not position**: add,
move or insert columns freely; a header career-ops writes that row 1 lacks is appended at the end, nothing is reordered. **You own Status,
Interview date and Notes**: Status gets `Applied` only while its cell is empty, the other two are never written. On an update only the
cells whose value changed are written (RAW, so a URL or job id is never turned into a number or date; dates are `YYYY-MM-DD`); the JD text
is cut at 45,000 characters. `tracker sync --dry-run` prints the rows and uses no network. A tracker failure is printed, never undoes the
application, and `tracker sync` sends it again.

**Contacts** (`careerops/contacts.py`), at most two stored per job by discovery, in this order: (1) addresses in the job ad (`jd`); (2)
`mailto:` links on `/careers`, `/jobs` and `/contact` of the company's domain, each fetched once through the polite client (robots.txt,
one request a second) and kept only if they look like recruitment or HR (careers@, jobs@, recruitment@, talent@, hr@) on the company's own
domain (`company_site`); (3) `careerops contact add JOB "First Last" --role "Hiring Manager" [--email x@y]`: you find the person on
LinkedIn (it is never fetched). With no `--email` and `HUNTER_API_KEY` in `.env`, Hunter's Email Finder is asked once (domain + name; the
key goes in a header); its verification status is kept and `accept_all` counts as unverified: no draft goes to an unverified Hunter
address by itself. No address is ever guessed from a pattern. Every address gets a **domain check** (MX record, else an A record) with
dnspython; there is no SMTP `RCPT TO` probe (port 25 is usually blocked at home, a catch-all server says 250 to everything, and probing
can get your IP flagged). `data/outreach/do_not_contact.csv` (`value,note`: an address or a domain per row) is checked before any draft.

**Outreach drafts** (`careerops/outreach.py`, `outreach.mode: off | drafts`, default `off`). With `drafts`, after `mark-applied` the
job's best contacts get a Gmail **draft**: one per contact, two per job. **career-ops never sends**: the only Gmail call it makes is
`drafts.create`; the Google client refuses any path ending in `/send`, and `tests/test_never_sends.py` fails the build if `smtplib`,
`SMTP_SSL`, `messages().send`, `drafts().send` or a send endpoint appears anywhere in the package. You read the draft in Gmail and press
send. Subject: `Application: <role> at <company> – <your name>`. Body (plain text, at most 120 words, fact-bank text only): `Dear <first
name>,` (or `Hello <company> team,`), `I have just applied for <role> (<req id if the ad has one>).`, the job's hook letter line word for
word, one ask for 15 minutes about the first 90 days, your signature (name, phone, LinkedIn, GitHub from `applicant.yaml`) and an opt-out
line. It passes the letter claim guard (no "we" / "our", no bullet tied to another bullet's tool) and is refused if it holds a number the
fact bank lacks or another company's name. Attachments: the CV PDF made for the job, and the letter PDF when one was chosen. The Sheet gets
`Outreach status` (`drafted <n>`, or `no draft: <why>`) and `Follow-up due` (applied + `outreach.follow_up_days`, 7).
`careerops outreach draft JOB --to you@gmail.com` makes ONE test draft to that address (nothing recorded, no limit used).
**Review screen:** with `outreach.mode: drafts`, after `[a]pprove` the contacts found are listed with their source and domain check, and
`[c]ontact add` lets you add the person you found before you apply.

### Adding a job from its link (`careerops add-url`, `careerops live --url`)

`careerops add-url <link>` fetches one job ad and writes `data/jds/inbox/<company>-<title>-<id>.txt` with `Company:`, `Title:`,
`URL:` and `Location:` header lines filled in, then the ad's text. It adds nothing to the database: `careerops live` does that, with
you at every gate. `careerops live --url <link>` is the same fetch followed by a live session on that one file (the rest of the
inbox is left alone; a rerun finds the file already there). `--company` / `--title` replace what the page or API gives. A Greenhouse link on the EU
hosts (`job-boards.eu.greenhouse.io`, `boards.eu.greenhouse.io`) is read from the same public API as a US one (`boards-api.greenhouse.io`: there is no
`boards-api.eu` host); the company and title come from the job reply, the company's name from `/v1/boards/<board>` when the reply has none.
`careerops live --id <job id>` is the same session for a job already in the database (no fetch).

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
`careerops live` takes the files best first (freshness tier, then the **estimate** of P = F x S x L x R; it prints the order), up to `--max`
(default 5). It works in two passes. **Pass 1** tailors the selected jobs, nothing else is asked but the rule-skip question. The
estimate (F is only the triage-time ceiling) cannot tell which tailoring will be good (an estimate of 0.86 became 0.25), so **pass 2**
sorts the tailored jobs by freshness tier, then the **final P**, prints the table (company, role, age, tier, `LEVEL:` flag, ATS ceiling,
P, and an `Apply:` line each) and only then reviews them. Nothing is dropped; a spent LLM budget in pass 1 still lets you review what was
tailored. For each job:

1. checks the file (missing, under 500 bytes, not UTF-8 → moved to `rejected/` with a `.reason.txt` beside it, loop continues);
2. snapshots the archives (`paths.applications_auto`, where the job's one new folder may appear, and `paths.applications`, your own folder, which must not change at all; read-only `os.walk`: path, size, mtime), in each pass;
3. adds the job (the internal `add-jd` function; the job id is passed on as a value, never read from printed output) and triages it;
4. tailors exactly that id (pass 1), then asks you to review it (pass 2): **approve / edit / skip** (plus `l` to pick the letter A / B /
   none, `n` to open the note, `o` to open the PDFs, `w` to open the apply page in your default Windows browser through `wslview`, else
   `explorer.exe`: it only opens the page, never clicks, fills or submits, and the tool sends no request to it); the review screen prints
   `Apply: <kind> <apply_url>` and, for a title with a level word, `LEVEL: manager | lead | senior`; skip leaves the job `skipped`
   (see "Skip and applied are final" below);
5. unless `--dry-run`, and only after approval: you confirm *Open the application form?*, the existing apply assist fills the
   basics (it never submits), every `CHECK VISA ANSWER` line is printed, and *Did YOU submit the form? [y/N]* decides:
   `y` → mark-applied (archive + `applied`); anything else → status `ready_not_submitted`, nothing recorded as sent;
6. snapshots the archive again. A removed or changed path, or an addition outside this job's one new `<Company>/<Role>` folder,
   stops the whole loop (exit code 3) and prints the paths;
7. moves the JD out of the inbox: a skipped or applied job to `data/jds_done/skipped/` or `data/jds_done/applied/` (`paths.jds_done`), any other
   finished job to `done/<job id>-<name>.txt` (`:` in the id becomes `_`).

**Skip and applied are final** (v2.5). Every decision is appended to `data/state/decisions.jsonl` with the job's keys: its id, the ATS key
`ats:<vendor>:<board>:<job id>` and the normalised URL decide on their own; company + title + country (the fingerprint) never do: a job that only
shares it is shown labelled `possible repost of a job you skipped on <date>`, and `s` skips it. One decision covers every copy of the same LISTING
(same id, ATS key or URL): skipping or applying to one copy skips the others in the database (a copy of an applied job is marked `skipped` with the
reason "same job as …", it was not applied to) and moves its files in the inbox and in `data/jds/approved/<date>/` into `data/jds_done/skipped/` or
`applied/`. `[s]` in `careerops review` stores the skip too, and `careerops skip --id JOB` does it for a job you are not reviewing;
`careerops why <id|url|ats:key>` shows every record and the key that matched. JD files are never deleted or overwritten (a name already there
gets `-2`). `careerops retriage --decided` does the same for what is in the inbox now (a dry run; `--apply` moves the files and skips the
database copies), `careerops unskip --id JOB` undoes a skip (the job and its copies return to `needs_review` if tailored, else `new`, and their
files to the inbox; live works on them again).

**ATS bands** (`apply.bands` in `config/settings.yaml`: `auto: 70`, `review: 50`, `rescue_ceiling: 60`). Right after tailoring, the STRICT CV score
S (the CV's own text, negation-aware) and the honest ceiling C decide what happens next; `live`, `tailor` and `retriage --ats` print the line
`ATS band: strict 63, ceiling 78 -> review (50 to under 70)` for every job.

| S (strict) and C (ceiling) | outcome |
|---|---|
| S >= 70 | the auto-apply gates run (with `--auto`), the letter is drafted |
| 50 <= S < 70 | the review prompt, the letter is drafted |
| S < 50 and C >= 60 | the review prompt with the red flag `CV under-uses the fact bank (strict S, ceiling C)`, the letter is drafted |
| S < 50 and C < 60 | status `skipped (ats)`: **no letter call, no form, no prompt**; the JD goes to `data/jds_done/skipped/` with `<name>.reason.txt` (`ATS S < 50`) |

The letter is drafted only AFTER this check, so a skipped job costs no letter tokens. `live` prints one line per skipped job, `SKIPPED (ATS 32 < 50):
Valarian | MLOps Engineer`, and `Skipped by the ATS band: N` at the end. Only you edit the bands; no code lowers them. After the fact bank grows,
`careerops retriage --ats` tailors the `skipped (ats)` jobs again (a dry run into a scratch folder by default: it reports `ATS 32 -> 55 ... would come back
to review`); `--apply` keeps the new tailoring and moves every job that now reaches 50 (or the rescue line) back to `needs_review` and its JD back to the inbox.

**Hand-off.** A job whose form cannot be submitted ends `handed_off` (see "Submit failures and the hand-off" under Auto-apply); once you have submitted it
in your own browser, `careerops mark-applied --id JOB` records it as always.

There is no `--yes`: every gate is answered by you. `--mock` runs everything on the isolated `data/mock` root with MockLLM
and implies `--dry-run`. **Ctrl-C** (or an unanswered prompt) prints the stage it stopped at and the command to resume (exit 130);
the JD stays in `inbox/`, and a rerun continues the same job at the status it reached. A finished JD is remembered by its
content hash in `data/live/state.json`, so the same text dropped in again is not redone. A dry run, a pending letter and a refused
apply also leave the JD in `inbox/`. Each session ends with a summary (id, company, role, status, sponsor-register match,
visa lines, archive check), printed and written to `data/live/<timestamp>.md`.

### Auto-apply (`careerops live --auto`, Greenhouse / Ashby / Lever only)

`careerops live` still works as above. With `--auto`, a tailored job meets a **gate** first; a job that passes every line of it is filled
and submitted by `careerops/autoapply.py` (the only module that may click a submit control) with no review prompt; any other job gets
`AUTO BLOCKED: ...` printed and goes to the normal review and assist, where you decide. Nothing else changes: `careerops apply` and the
assist still never submit.

**Modes** (`apply.mode` in `config/settings.yaml`; the shipped default is `assist`, you set `auto` yourself):

| `apply.mode` | what `careerops live` does |
|---|---|
| `off` | tailors and reviews, opens no application form (as `--dry-run`); `--auto` is refused |
| `assist` | the assist fills the form, you press Submit. `--auto --no-submit` is allowed; `--auto` that could submit is refused |
| `auto` | `--auto` submits the jobs that pass every gate; everything else is as in `assist` |

**The gate** (`careerops/apply/autogate.py`; all must pass, each failure is named in the `AUTO BLOCKED:` line):

| Gate | Rule | Setting |
|---|---|---|
| ATS | strict CV score (the CV's own text, negation-aware) at least the minimum; the claimable ceiling does not count | `min_ats_strict: 70` |
| Format | exactly 2 pages; every font embedded; no two words overlap and none sits outside the margins (`pdftotext -bbox`); the PDF's text holds the name, the e-mail and every section heading, in order | |
| Faithfulness | the final CV has no claim-guard violation and no warning (a rewrite the guard BLOCKED never reached the CV: it is counted apart, as `blocked rewrites`, and printed, but does not gate); a letter that exists passed its own check (`ok`, not `fallback` / `pending`, no problems) | |
| Level | L = 1.0: no manager / lead / senior word in the title | `priority.level_factor` |
| Sponsorship | the JD has no "no sponsorship" line | |
| Kind | `apply_kind` is one of the listed kinds (Workday and every other site: no) | `kinds: [greenhouse, ashby, lever]` |
| Duplicates | not applied to before (the company is its family: "Graphcore Early Careers" is "Graphcore"): the database (this job, or the same company family and title), the archive (this job's folder, or a `<Company>/<Role>` folder), the AIApply export, `applied_auto.csv`, and the tracker Sheet. An unreadable Sheet blocks a real submit (a duplicate cannot be ruled out); `--no-submit` goes on without it | |
| Daily cap | fewer than N auto-submits today (rows of `applied_auto.csv` whose button was clicked) | `max_auto_per_day: 5` |

**Filling the form** (a headed Chromium with its profile in `data/secrets/browser/`, one request per second per host):

- Name, e-mail, phone, LinkedIn, GitHub, location: `applicant.yaml`. The CV PDF of this job is uploaded, and the letter PDF only if one exists.
- Right to work and sponsorship: the booleans of `applicant.yaml` `visa.by_country` for the job's work country (as in the assist).
- **Every other question is answered only from `config/profile/answers.yaml`**, a file you write (`answer.yaml` is found too, if that is the name
  you gave it). A question matches an entry when it holds one of its `match` texts as whole words, case and punctuation ignored (so `sex` never
  matches "Essex"); the longest matching text wins. `answer_numeric` is used when the field only takes a number; `overrides` gives one company a
  different answer; `options` are the drop-down or radio texts to pick (an option equal to one of them, else one holding it as whole words, so "Male" is
  never "Female"); `{source}`, `{company}` and `{role}` are filled per job. A blank `answer` means unknown.
- **A required question with no exact answer stops the run**: it prints the question, asks in the terminal, uses what you type on this form only, and
  appends it to `answers.yaml` only if you then type `save`. A blank reply stops the job (`needs_user`). An optional question with no answer is left empty.
  Nothing is invented, and an answer that matches none of a drop-down's options is not rounded to the nearest one.
- **A CAPTCHA, a bot check or a sign-in wall is a hand-off, never something to get past.** A **visible** reCAPTCHA / hCaptcha checkbox or puzzle frame
  (displayed and at least 100×60 px), a displayed Turnstile widget, "verify you are human", the message "error verifying your application", or a
  Google or Microsoft sign-in page or popup stops the run (assist and auto-apply alike) before
  anything on it is touched, and no credential is ever typed. Then the apply URL is opened in your normal Windows browser (`explorer.exe <url>`), the CV
  PDF path is printed, every answer that can be prepared from your own files is written to `answers_to_paste.txt` in the job's output folder (your details,
  the visa answers for the job's work country, equal-opportunities options, the `answers.yaml` answers for this job, the free-text answers you approved,
  the letter text, whatever was filled before the stop; a blank answer is listed as blank), the automated browser is closed, and the job is `needs_user`.
  The host is recorded in `data/live/handoff_hosts.json`: the next job on it goes straight to the hand-off without opening an automated browser
  (delete the host's entry from that file to let the program try it again). An **invisible** reCAPTCHA (v3) frame or badge (`size=invisible`, a
  hidden challenge frame, a submit button that carries the site key) is not a trigger: the form is filled as usual, and the program only asks whether
  an element is displayed and how big it is, never clicks, focuses or types into it.
- **Submit failures and the hand-off.** Four things count as a failed attempt on a job, each one row in `data/live/submit_failures.csv` (time, job id,
  host, kind, detail): a verification error ("error verifying your application"), a field `LOST` after the re-fill, an upload the form refused, and
  `FORM NOT FOUND`. After **two** failures on one job (`apply.max_submit_failures`), the assist and the auto-apply offer `[h] hand off`: your normal Windows
  browser opens the apply page (`explorer.exe`), `answers_to_paste.txt` is written in the job's output folder (name, e-mail, phone, links, location,
  salary and notice from `answers.yaml`, the visa text, the cover letter, the `answers.yaml` answers, the free-text answers you approved), the first
  answer (your full name) is put on the clipboard with `clip.exe`, and the CV and letter paths are printed. The job ends `handed_off`. A host in
  `apply.handoff_hosts` (`jobs.lever.co`) or one that handed a job over before goes **straight** to the hand-off, with no automated browser. There is
  **never a third attempt**, never a changed browser profile, user agent or proxy, and nothing is ever signed in to. (To let the program try a job again,
  delete its rows from `submit_failures.csv`; a host's entry in `handoff_hosts.json` works the same way, and `apply.handoff_hosts` is yours to edit.)
- **Finding the form.** Ashby: `<job url>/application`. Lever: `<job url>/apply`. Greenhouse and others: the job page itself, waiting up to 15 s for the
  form; an "Apply" link on it is followed once. If no form field has appeared after 15 s the assist and the auto-apply print `FORM NOT FOUND <url>` (and no
  `Filled:` line); the job is left to you.
- **Fill order and read-back.** Files first: the CV goes only to the real Resume / CV field, never to an "Autofill from resume" control (printed `skipped the
  autofill upload control`); then the form is left alone until it has not changed for 2 s (an Ashby form re-renders after an upload and wipes what was typed);
  then the text fields. Afterwards every field that was filled is read back: an empty one is filled once more and printed `VERIFIED` (`(filled again)`) or
  `LOST`. The assist lists a LOST field under `REQUIRED + EMPTY`; the auto-apply stops (`needs_user`) instead of submitting.
- **The form loop** (`apply/formloop.py`). Before anything is filled, every visible input, textarea, select, radio group and check-box group is printed in a
  table (`FORM FIELDS (n)`: type, required, maxlength, label, options), required or not; a label is the `<label>`, `aria-label`, or the nearest text before the field.
  After the standard fields and `answers.yaml`, each free-text field still empty (a text area, or a text box over 100 characters) goes through
  observe → classify → retrieve → draft → check → fill → verify, at most 3 passes, and the form is scanned again after each fill (an answer can reveal a new
  question). Retrieval is only `answers.yaml`, the answer library, the fact bank and the job ad. An answer you approved for a question at least 0.85 similar
  (same kind) is **filled directly**, no draft, no question (`LIBRARY` in the table). Any other question gets a draft, **filled into the field at once** and marked
  `NEW DRAFT`. If the question asks for something the fact bank never mentions (a compound like "non-engineering", a name like "Salesforce", the words after
  "with" / "for" / "in"), the draft uses the closest true entry, never claims the missing part (the guard rejects a draft that does), and is marked
  `WEAK EVIDENCE: <what was asked> vs <entry ids>`. An **optional** question of no kind a draft can answer ("Anything else?") is skipped; a required one is asked.
  Each new draft is then shown with `[y] use · [e] edit · [r] redraft · [s] I'll write it`: `y` or your edit stores it in the library. In the **auto-apply** a
  draft nobody approved stops the run (`needs_user`) before any submit; in the assist it stays in the field, outlined orange, listed under `YOU ANSWER`.
  Every step of every field is appended to `formloop.jsonl` in the job's output folder. The drafts also go into `answers_to_paste.txt` on a hand-off.
- **A spam flag after a submit.** If the page says "flagged as possible spam" or "couldn't submit your application" after the auto-apply pressed Submit, the job
  goes to the hand-off **at once** (it does not use up one of the two allowed failures): the row `spam_flag` is written to `data/live/submit_failures.csv`,
  the host (e.g. `jobs.ashbyhq.com`) is recorded in `data/live/handoff_hosts.json` so later jobs there skip the automated browser, and the page is never
  refreshed or submitted again from automation. No host is put on `apply.handoff_hosts` in advance.

**Submitting**: a full-page screenshot of the filled form is saved in the job's output folder first. The first `confirm_first_n` (3) auto-submits ever,
or every one with `--confirm`, show its path and ask `Submit? [y/N]`. Then the button is clicked and the confirmation page awaited for 30 seconds; its
screenshot is saved. A confirmation runs `mark-applied` (the archive, the outcome log, the tracker row, the outreach drafts if switched on). No
confirmation in 30 s is `submit_unconfirmed`: the job is not marked applied, is never retried, and you check the form and your e-mail. Every run, whatever
its outcome, appends a row to `data/live/applied_auto.csv` (time, job id, company, role, ATS strict, screenshots, result).

**Run order**

1. `careerops live --auto --no-submit --max 3`: fills the forms of the jobs that pass every gate and screenshots them (look at the PNGs in each job's
   output folder), never clicks Submit; prints `AUTO BLOCKED: ...` for the rest, which go to your review. Works with `apply.mode: assist`.
2. Set `apply: mode: auto` in `config/settings.yaml`, then `careerops live --auto --max 3`. Add `--confirm` to be asked before every submit.
3. `careerops live --auto --dry-run` evaluates the gates and prints `AUTO READY` / `AUTO BLOCKED` without opening a browser.

**Pre-flight**: `live` checks at its start that the Google sign-in may edit the tracker Sheet (the `spreadsheets` scope in the token file; no network). Without it
one line says to run `careerops discover --gmail-auth`, and the run goes on in assist mode only: `--auto` is switched off, because the duplicate gate cannot
read the Sheet and a duplicate cannot be ruled out. The assist fills the same fields as the auto-apply (applicant.yaml: full name, first and last name, e-mail,
phone, links, location; then `answers.yaml`; free-text questions go through the form loop below). If the browser is closed (or the program dies) after the form was opened,
the confirmation question is still asked, and the next `live` starts with `Did you submit <company> <role>? [y/N]` for that job.

**Free-text questions** (a required text area, or a text box with room for over 100 characters, that `answers.yaml` does not answer): `apply/freetext.py`
reads the limit from the label and the field ("1-2 sentences", "max 200 words", `maxlength`; none stated = 120 words) and the kind of question (project,
challenge, why company, why role; anything else is asked, never drafted). An answer you approved for a similar question (same kind, similarity 0.85) is reused
as it is. Otherwise it picks the 1-3 fact-bank entries that fit (the hook proof of this job's CV first), drafts from them in the first person following
`answers.yaml -> style`, and checks the draft like the CV: every number, tool, organisation and name must be in those entries, no deny-listed term, no style-avoid
word, the limit, no "we/our" and no pasted list of terms. A "Why {company}?" claim must be words the job ad itself contains, and without `motivations` in
`answers.yaml` you are asked. One redraft, then you; then it shows the question, limit, sources and draft: `[y] use · [e] edit · [r] redraft · [s] I'll write
it`. Only `y` (or text you typed) is used and kept in `data/answers/library.jsonl` (`paths.answers_library`; runtime data, never `answers.yaml`). A new draft is
therefore never submitted without your `y`; a "why company" answer is only reused for the same company (a "why role" one only for the same job).

**What stays manual**: a CAPTCHA; a required question `answers.yaml` cannot answer; Workday, a company's own site and every other ATS; a job under the
ATS minimum, with a senior / lead / manager title, saying no sponsorship, already applied to, or whose CV fails the format or faithfulness check; the
sixth auto-submit of a day.

**Measured** (`python scripts/auto_census.py`, read only, 2026-10-21, on the 9 jobs in `data/careerops.db`; it opens the database with `mode=ro` and writes nothing):

| What | Number |
|---|---|
| jobs on a Greenhouse, Ashby or Lever apply page | 6 of 9 (Greenhouse 3, Ashby 2, Lever 1); the other 3: employer site 1, job board 1, no URL 1 |
| tailored jobs with a strict ATS score recorded | 4 of 9 (20.0, 35.7, 56.0, 58.1); the other 5 reports pre-date the score |
| strict ATS at least 70 | 0 of 4 |
| CVs that pass the format gate | 5 of 9; the 4 that fail are the CVs made on 3, 10, 10 and 15 October |
| jobs failing each per-job gate | ATS 9, format 4, faithfulness 5, kind 3, sponsorship 1 |
| jobs passing all six per-job gates (duplicates and the cap are not in the census) | 0 of 9 |

So with today's CVs no job in the database would be auto-submitted: the ATS gate alone stops all of them. Tests added: 138 new tests (`test_answers` 20, `test_autogate` 63, `test_autoapply` 30, `test_live_auto` 17, `test_live_auto_cli` 8); the full suite then ran as 133 files on WSL's own disk on 2026-10-21 (3,176 tests, 0 failures).

**Not verified**: no real Greenhouse, Ashby or Lever page has been opened. The form logic ran against local fixture forms in headless Chromium and the
gate against fixture reports and PDFs. How those three sites label their fields, whether their drop-downs are real `<select>` elements, how their
confirmation pages read and whether a form's reCAPTCHA is invisible are for `--no-submit` to show on a real job. The tracker duplicate check, the Sheet
read, has only run against a fake sheet.

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

### Letters (`letter.mode: off | draft | hook | company`, default `off`)

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

**`hook` with no hook** (no `hook_library` entry fits the ad): sentence 1 is the role family's profile line, word for word (the fact bank's
`families[*].profile_body`, the same text as profile line 1 of the CV); the two bullet sentences and the call to action follow as above (the problem
phrase is the ad's two heaviest keywords the bank can claim). No model writes any of it; the same checks apply. A family with no `profile_body` has no
line to open with: the letters are **none**, as before. The apply assist uploads the letter PDF to a cover-letter **file** field (printed
`Uploaded: ['Cover Letter ← letter', ...]`); a form with a cover-letter **text box** instead gets the letter text (and with both, the file only).

**`company`** writes ONE short letter of **200 to 320 body words** in up to seven paragraphs, no bullets (`tailoring/company_letter.py`, v2.3). Code writes P1, P2, P6
and P7; the model (the existing `cover_letter` task, Groq `openai/gpt-oss-120b`) writes P3, P4 and P5 under two guards. The greeting, sign-off and name are not counted.

| | paragraph | source |
|---|---|---|
| P1 | hook | `<Company> is hiring for "<a clause of the ad>"; <the hook's letter_line>.` The clause is the ad's own words (else the company's own careers page); the hook's line carries a number the fact bank holds and ends with how it was validated; a hook with no number is skipped for the next one, else the strongest numbered bullet and the family's profile line |
| P2 | what it really shows | the hook fact's `limits` field, else its `result` field, **word for word** (the hook entry first, then its proof bullet). Left out when the fact bank has neither. Add `limits:` / `result:` to a `hook_library` entry or its proof bullet to get a P2 |
| P3 | JD-matched skills | the ad's 2 or 3 heaviest skills the CV evidences, shown through a second bullet or project (never the hook's own); P3 must name two of them |
| P4 | experience roll-up | one sentence per role, the most relevant role first (the hook's), then the next: the employer, what you owned, one number of that role's bullet |
| P5 | why this team | **one sentence** that quotes or closely follows an About / role sentence of the ad (else of the company page); every claim must be backed by the ad |
| P6 | one honest gap (optional) | `One honest gap: I have used <X> only, never <Y>, so <Y> is where I will learn.` Y = the heaviest **required** ad keyword the fact bank cannot claim (a tool, platform, language or framework; never a visa, a degree or years; never a deny-listed term); X = a neighbour skill the bank really says (no neighbour: `One honest gap: I have never used <Y>, so it is where I will learn.`) |
| P7 | the ask | `Could we spend 20 minutes on the first problem the <role> would own, around <the ad's problem>?` (`letter.ask`; with an assessment named in the ad: `, and what the technical assessment covers`) |

The greeting is `Hi <first name>,` when the job has a contact with a name (`careerops contact add`), else `Hi <Company> team,` (`letter.greeting_hook`); the sign-off is `letter.signoff`
(`Regards,`), a blank line and your full name. `letter.company_words` (`[200, 320]`: `[low, high]`, two whole numbers, 200 <= low <= high <= 320; anything else is refused) is the range
the **body** must come to (the model is asked for the middle of it; `scoring.letter_max_words_company` can lower the top), so 199 and 321 are rejected. `scoring.letter_max_words` (250) stays the cap of the
other modes. **Memory, RAM and GB figures** are allowed in the body only when the ad talks about memory, hardware, efficiency, GPUs or edge deployment; they are never in the header or the hook headline.
**P6** is in the letter only when `letter.honest_gap: true` (the shipped default) or you keep it in review: review shows P6 on its own line with `[k]eep` and `[d]rop`, which re-render the letter
without asking the model; "drop P6", "keep P6" and "drop P2" in the `l` feedback are read in code, anything else ("shorten P4") goes to the model by name (its prompt carries the paragraph table P1 to P7).
Beside the PDF the letter is also written as **`letter.txt`** (greeting, paragraphs, sign-off and name as plain text) for a cover-letter text box. The letter's text goes through `render.letter_escape`
(`% & $ # _ { }` escaped, `~` is `$\sim$`, `+5 pp` is `+5\,pp`).
A draft is checked and, if a guard rejects it, drafted **once more** with every rejection named; a second rejection means **no letter** and the review
screen says why (`Letter draft 1 / 2 rejected: ...`, the `NO LETTER` banner and its reason; approving needs `no letter`, as for any lost letter).

*User guard* (P3, P4): every number, tool and organisation named must be in the four chosen bullets (or in P1); each sentence is checked against its own
bullets (no name, sector or client word they do not say); a **capability or skill word** ("production", "deployment", "pipelines", "end-to-end", "monitoring")
passes when `fact_bank.yaml` says it anywhere (bullets, skills, profile); each paragraph names the requirement its bullets evidence; no filler (robust, seamless,
cutting-edge, passionate, leverage, synergy), no pasted bullet (0.7 overlap), no deny-listed term, no keyword list, no "we" / "our", no "I help <Company>",
and no noun phrase that links one bullet's object to another bullet's tool (a number or a tool named without a chosen bullet saying it still fails).
*Company guard* (P2): the ad is split into sentences; every clause of P2 that makes a claim (product, customers, size, funding, markets, mission) needs at
least 60 % of its content words in one run of up to three consecutive ad sentences (a very short clause: all of them somewhere in the ad); any number
about the company must be in the ad; any capitalised name (a person, a team, a product) must be in the ad, the fact bank or P1. Outside knowledge about the
company therefore cannot get in; a faithful paraphrase of the ad can.
P2 is written from the ad's About / role sentences (the prompt numbers them; it must quote or closely follow 2 or 3), and up to three drafts are tried before
"no letter" (the reasons are shown). The review screen shows the **word count** against the accepted range and, for P2, **each sentence with the ad sentences it drew from**
and which About / role sentences it followed. The apply
assist uploads the letter PDF to the cover-letter file field, or pastes the text into a cover-letter text box.

With `off` no letter is asked for: no letter call, no letter PDF, review says `Letter: off` and approving needs no extra words, and
the application is archived without one. With `draft` the model drafts one from the evidence **as fields** (`method`, `data`,
`outcome`), never as the bullet sentence, so it has to write its own; every letter check is unchanged. Your fact bank has no such
fields yet, so the fields are the bullet's `method` / `data` / `outcome` keys if you add them (the pipeline never writes your fact
bank), else `method` = its `terms`, `outcome` = its `result`, and `data` = `(not stated)`, which the prompt tells the model to leave
alone.

#### Review keys for a company letter, and the tokens

- **`l` (regenerate)** asks `What should change? (enter = just retry)`. The next draft is handed the previous draft, every guard rejection rewritten as an
  instruction (`remove 'robust'`, `the letter is 288 words, 42 short of the 330 minimum: use more of the detail of bullet p1a`) and what you typed, word for
  word. At most three drafts per `l`. What you typed is kept in `data/letters/feedback.jsonl` (`paths.letter_feedback`: job id, text, date).
- **`e` (edit)** asks `Edit the [c]v or the [l]etter? (enter = cv)`. `l` opens the letter text in `$EDITOR` (`nano` if unset): one paragraph under each `[P1]` ... `[P7]` label
  (P2 and P6 may be left out). On save the same guards run. A letter that passes is used. One that fails shows its reasons and is used only if you type `use anyway` in full;
  Enter edits again and `discard` keeps the old letter. A text without P1, P3, P4, P5 and P7, or with another label, is never installed.
- **Tokens.** After tailoring, after `l` and at the end of `live` the tokens used today for `cover_letter` are printed per model, and a `WARNING` for every
  provider or model whose daily token budget (`tpd`) has less than `llm.warn_budget_below` (0.2 = 20 %) left. The counts are kept in the database table
  `llm_task_usage`.

### The gap report and typed facts (a job in the review band)

A job whose strict CV score S is 50 to under 70 (or the "CV under-uses the fact bank" rescue) does not get the plain ATS header. It gets a **gap
report**, computed from the scorer's own numbers:

```
ATS strict 56.3 | ceiling 78.8 | to reach 70 you need +13.7 points
Missing from the CV (on CV? no), most important first; the gain is the points it adds if it gets onto the CV:
  +6.3  Airflow (required, w=2)  gap
  +3.1  dbt (preferred, w=1)  claimable: skills "SQL", bullet av3 (left off: page full)
claimable total: +9.4 points (re-tailoring can win them)
gap total: +12.5 points (each needs a new fact)
```

Every JD keyword the CV lacks, with its weight w and its gain g = 100 x w / sum(w), most important first (see the tiers below). `claimable`: the fact bank evidences it; the skill
or bullet is named and so is why it is not on the CV (`page full`, `rank #n` or `rank, <bullet> replaces it`, `guard`: the bullet's `terms:` say it
but its text does not, and a rewording may not add a word). `gap`: the fact bank has nothing, so only a new fact can add the points (a term on your
`deny_terms` is a gap too and is never asked about). The two totals add up to the points still to win; the "Possible evidence" lines are kept.

**Importance tiers.** Each row ends with a tier read from the ad's own text (`careerops/review/tiers.py`, no model): `[stated]` = the ad says it with must-have
wording (must, required, essential, mandatory, minimum, "you will need"; nice-to-have wording never counts), `[structural]` = it sits in the ad's Requirements-type
section ("Requirements", "What you'll need", "Qualifications", "About you" ...) or the ad says it more than once, `[inferred]` = anything else: our extraction
and weighting guessed it matters. Rows are ranked stated, then structural, then inferred (largest gain first inside a tier), so **an inferred keyword is never
ranked first**; only a stated or structural one is marked `<- top priority`, and when every missing keyword is inferred no row is. `f` asks in the same order.

```
[a] apply as is   [s] skip   [r] re-tailor with the claimable points forced in
[f] add facts     [o] open PDF   [q] quit            (also: [e]dit [l]etter [n]ote [w] apply page)
```

- `r` tailors the job once more with the claimable keywords ranked first (4x weight when the first attempt picks bullets; the score keeps the JD's
  own weights) and shows the new S and C. It cannot add a word a bullet does not say.
- `s` ends the job as `skipped (user)`: its JD goes to `jds_done/skipped/` like any skip.
- `f` asks about the top 5 gap keywords one by one (`Airflow (+6.3): have you used it? Describe it in one line (where, what, result), or press enter to
  skip, or n if you cannot confirm it (never asked again).`). Enter skips and stores nothing. `n` stores the keyword as `user-cannot-confirm` in
  `proposed.yaml`, for good: no later job is asked about it, no fact for it is stored, and a fact already typed for it is not used. A line you type is stored word for word in `data/facts/proposed.yaml` with the keyword, the role or
  project you name, the job id and the date, never in the fact bank, and the job is tailored again with the bank plus these facts, marked
  `[provisional]` on the review screen. A provisional fact is a source for **its own job only** (every other job is tailored with a bank that does not
  have it), is on the CV as your own words (never reworded) and is not used by the free-text answers of the application form.
- **After the job** (applied, or not: your `x`, a hand-off, a skip, a dry run) every typed fact the CV used is offered: `Add to fact_bank.yaml under
  <role/project>? "<text>" [y/N/e(dit)/c(annot confirm)]`. `y`: `careerops/factbank_append.py` writes `config/profile/backups/fact_bank.<YYYYMMDD-HHMMSS>.yaml`, appends
  the bullet (`<prefix><next number>`, `source: user_<date>`, `terms: [<keyword>]`; every other line of the file stays byte-identical), reloads the file
  and puts the backup back if it does not validate (a duplicate id, a deny-listed term). `e`: you retype it, then the same question. `N`: it stays in
  `proposed.yaml` marked `declined` and is never used again (for that wording; another job may ask about the keyword again). `c`: "I cannot confirm this": the
  keyword is marked `user-cannot-confirm`, permanently (see `f`). When the CV was not sent the question says so (`NOT SENT: this CV was not sent`). A
  `--mock` run never writes your real fact bank. `careerops mark-applied` offers the same after a hand-off.

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
  (`MSc Coursework`; a project with a GitHub link keeps its name and shows the link instead), else its `github:` URL itself, `\url{https://github.com/...}`
  in the default `\url` font (since 18 Oct; the `\gh` macro stays in the preamble, which is the reference file line for line, but nothing calls
  it). When the bold name and the URL do not fit one line the URL moves to the stack line, right-aligned, and when the stack and the URL do not fit
  together it gets a line of its own: TeX measures both with boxes, so they never overlap, and `layout.link_problems` re-checks the rendered
  PDF's word boxes (`pdftotext -bbox`): every URL is on the page, inside the margin, touching no other word (`report.json` → `lengths.links`).
  A skills group is `\skillgroup`: the navy bold label on a line of its own and its items below it (the first item of each group
  starts with a capital). There are four groups, in this order: Machine Learning, Deep Learning & Statistics · Python, Data Quality &
  Cloud · NLP, LLMs & Evaluation · Client Delivery & Communication (`cv.skill_groups` in `config/settings.yaml`: each fact-bank skill
  goes to one group by name or by the fact-bank group it sits in; `skills.core` and `cv.skills_always` are never cut; inside a group
  the skills are ordered by the JD keyword weight they carry, then by the fact bank's order, a pinned skill has no edge; a skill appears once:
  "Kubernetes" and "Kubernetes (AWS EKS)" are one (the qualified one stays), and an umbrella name from `cv.skill_umbrellas` (AWS) goes when a
  specific skill of it (AWS Lambda) is listed; TypeScript and JavaScript (`cv.skills_when_asked`) are shown only when the ad names them; a group
  with no skill row of its own but `from_terms` shows the matching `terms:` you wrote on your bullets, e.g. Client Delivery & Communication).
  A group the fact bank has nothing for is left off and the review
  screen says so. **Role families (18 Oct):** when the fact bank has `families:` (each `id`, `match` terms, `skill_lines` of `label` + `items`), the
  family whose `match` terms carry the most JD keyword weight (ties to the first, none above zero: the job's own role family if the bank has one of
  that id, else the groups above) gives the Skills section: its `skill_lines` label for label and item for item, as one line of text each, no
  model and no regrouping, in the same `\skillgroup` layout. The bank has `skill_lines` for `data_scientist` and `ai_engineer` only; every
  other role family in settings has `skills_from:` naming one of the two (data_scientist, data_consultant, computer_vision_engineer,
  ml_research_scientist, data_analyst, data_engineer → `data_scientist`; the rest → `ai_engineer`) and prints that family's lines word for word,
  whatever the ad says. A bank family with the same id as the role family replaces the mapping. The CV headline is the hook library's, word for
  word; the role family's `headline` is only the fallback and no model writes it. `skills_always` is still checked: a standing skill the bank lists but the lines do
  not mention is a `skills:` warning, never added to your words (`report.json` → `skills.family`). A project with an `award:` that the JD does not rank in its top three moves to Certifications & Achievements as a
  bold first line. An experience or project with `situation:` in the fact bank opens its first bullet with "<situation>: " (STAR);
  the model never writes it. Bullet flags in the fact bank (18 Oct): `verbatim: true` = the bullet is used word for word (never reworded by
  `rephrase_bullets`, the wording pass or the shorten pass, never split, never given an opener; one that prints over three lines is reported, not
  trimmed); `replaces: [ids]` = the bullet stands in for those older ones: when it is chosen they are never on the CV, and where one of them would
  have been chosen the replacer is chosen instead if it scores at least 0.8 x the best bullet it replaces (a replacer below that is left out; the
  hook's proof bullet is handed over to a replacer that says the hook's number, else it stays and the replacer goes); `situation_included: true` =
  the bullet starts with its own situation: at most one per role, and then the role's `situation:` text is not added. A role dated within the last
  two years (`scoring.cv_recent_years`) keeps at least two bullets (`scoring.cv_recent_min_bullets`) if the bank has them: the shrink loop trims
  the projects (to one bullet, then whole projects) before such a role loses its second bullet. Length rules, measured on the PDF: a bullet is at most 3 lines (longer: back to its fact-bank wording,
  else split at a sentence or semicolon, else reworded shorter by the model, else reported), the profile at most 7 lines (the hook
  sentence first), the 12pt headline exactly one line (its trailing stack list is dropped first). The contact line shows the
  portfolio as just "Portfolio". Education is `\roleNoLoc` (school with its location, dates, the degree with its grade and note) and, below, a muted
  small line with the modules and `details`. The header is the 22pt navy name, a 10pt contact line with short links (no
  `https://`) and an italic muted status line, all joined with ` · `. The certifications are a bulleted list, one item each,
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
  3–5 (`scoring.cv_bullets`: `[4, 6]` and `[3, 5]`), 3 projects (`scoring.cv_projects`) with 2–3 bullets each. A bullet whose words overlap a chosen
  one (Jaccard ≥ 0.6) is never added. Then content is added until page 2 is at least `scoring.min_fill_page2` (0.96, within two lines of the bottom margin) full,
  measured from the PDF (y of the last text line on page 2 over the text area), in this order: **experience bullets** (every role up to
  its maximum, by relevance), then **projects** (never more than 3 in all, `scoring.cv_projects_max`; 1–3 bullets each), then the **optional fact-bank sections** (`publications`, `languages`, `research_focus`, in that order), and last
  the **linespread**, raised from the reference's 1.02 in steps of 0.02 up to `scoring.cv_linespread_max` (1.10) and stopped at the first value
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
the **sum of keyword weight x [the keyword is one of its `terms`]**, but an entry is **eligible only when its `terms` hit at least 2 of the ad's top 10
weighted keywords**; code picks the highest-scoring eligible one (a tie goes to the earlier entry), then uses its strings **word for word**: the `headline` as the **12pt navy line under the name**, the `profile_line` as
**profile sentence 1**, the `proof` bullet **moved to the top of its role** (or project) with the entry's `situation:` opener, and the `letter_line`
as **sentence 1 of the letter** when letters are on. Nothing is typeset or reworded. An entry whose proof bullet is not in the bank, or that names
a deny-listed term, is skipped and `hook_notes` says why. The number is the first count-like string in the proof bullet's `result:` or `text`
(a model version like "Haiku 4.5" is not a result), never invented; a proof with none carries the flag `hook has no number`. With **no
library, or no eligible entry**, there is **no hook** and nothing is written in its place: the CV headline is the **role family's headline** (e.g.
"Data Scientist") with no number, no hook line, no proof bullet moved and no hook letter; the report's `hook_notes` and the review screen name the closest
entry. The review screen prints **why the hook won** (the overlapping top keywords and their weights) and a **lint warning** (never a rewrite) when its
headline is over 60 characters, has more than one number, or names a hardware or resource unit (GB, MB, RAM, GPU, ms, CPU); `careerops doctor` lints every
`hook_library` headline the same way. A reworded proof bullet that
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
`priority.py` ranks, lexicographically: **the freshness tier first, then P = F x S x L x R inside it**.

    tier 0  posted at most 24 h ago      tier 1  at most 72 h      tier 2  at most 7 days      tier 3  older (or undated)
    P = F x S x L x R      F = honest ATS ceiling (0-1)
    S = 1.00 exact register match · 0.90 fuzzy · 0.75 not on the register / agency / unknown · 0.65 the JD says no sponsorship
    L = 0.3 manager|director|head · 0.4 lead|principal|staff|architect · 0.6 senior|sr · 1.0 any other title
    R = max(0.5, 1 - role_step x (family priority - 1))      role_step 0.03: priority 1 -> 1.00 · 5 -> 0.88 · 12 -> 0.67 · 15 -> 0.58

(the lowest applicable S; the weights are `priority.sponsor_weights` in `config/settings.yaml`. L is the level fit: the ATS score cannot see
seniority, so the title words do; the highest level word wins, the values are `priority.level_factor` and yours to edit. Like S it only
moves a job down: nothing is dropped, and `LEVEL: manager` etc. is shown in the review screen and the final table.)
**R is the role fit** (`priority.role_step` in settings): each family in `roles.families` has a `priority` from 1 to 15, your ranking of the role, and the
title's family sets R. It is shown as `x R 0.88 (forward_deployed_engineer, #5)` in the inbox estimate, the final-P table, the review screen, `discover`'s
listing and `status`; a title with no family has no R (it counts as 1). **Matching and ranking are separate:** the family list is matched in **list
order, first match wins**, and it is ordered most specific first on purpose (a "Data Science Consultant" must reach `data_consultant` before `data scien`
catches it), so do not re-sort it by priority or tier. The `priority` key is never used for matching. `careerops discover --report` lists the family of
each ad the run wrote. (The old recency factor, also called R, was exp(-days / 14) and is
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

### Fewer, better-matched applications: `shortlist`, the caps, `patterns`

Passing the title filter is not a reason to apply. `careerops shortlist` scores every inbox ad with a **zero-token triage score T** (1 to 5) and offers the best few; **you
promote** the ones worth tailoring, and `careerops live` reads only those (`data/jds/approved/<date>/`). Nothing is tailored, sent or changed by `shortlist` except filing
ads away and moving the ones you approve.

    T = 0.30 A + 0.25 E + 0.15 V + 0.10 L + 0.10 Fr + 0.10 P          shown as   T 4.1 = A5 E3.8 V3.5 L5 Fr4 P3

- **A role**: the family's `priority` (1-3 -> 5, 4-7 -> 4, 8-11 -> 3, 12-15 -> 2, no family -> 1). **E evidence** = 1 + 4c, c = the share of the ad's *required* keywords
  that appear anywhere in your fact bank (found the way the scorer finds them; a required keyword the bank lacks stays in the denominator, so the ad cannot inflate E; no
  required keyword found: 3). **V visa**: exact register match 5, fuzzy 4.5, unknown (or an agency) 3.5, the ad says no sponsorship 1.5, work outside your home country 1 (the
  lowest that applies). **L level**: no level word 5, senior 2, lead or manager 1. **Fr freshness**: <= 3 days 5, 7 days 4, 30 days 3, 120 days 2, older 1 (no date: 3).
  **P pay**: the top of the ad's band at or above your `answers.yaml` salary figure 5, no band (or no figure on file) 3, below it 1. A missing salary is never a penalty.
- **Hard stops** (zero tokens; the ad is filed in `jds_done/skipped/` with the reason beside it and never offered), in this order: liveness (asked only for the ads that would otherwise be
  offered, best T first; a closed posting goes to `jds_done/closed/`), your blacklist, the same role under another channel or an agency ad that hides the employer, the level exclude
  list, then `targeting.dealbreakers`. **A dealbreaker needs an ad sentence that states the fact, and it is quoted**: `DEALBREAKER "people management" <- "You will line-manage a team of 4"`
  (a few ideas are read by what an ad says: people management, clearance, "N days on-site", "N+ years", "salary below £N"; any other phrase is matched as words). A sentence that denies
  it ("no people management") or calls it optional is not a hit; a senior-sounding title with no stated years, or a missing salary, breaks nothing.
- **Offered**: T >= `min_triage_score` (3.5), best T first then freshest, at most `shortlist_size` (8). An ad older than `max_posting_age_days` (60) needs T >= 4.5. A company with
  `per_company_max_open` (2) applications and no reply yet, or inside its `per_company_cooldown_days` (30) after a rejection or withdrawal, is left out. An application counts as open
  for 28 days (then the funnel counts it as no response); the count includes the tracker's jobs (the database) and the archive folders for the same company.
- **Choices**: `a` approves all, `1 3 5` approves those, `s 2` skips job 2 for good (filed in `jds_done/skipped/` as `skipped (user, shortlist)`, so `discover` does not bring it back), `x` skips every
  job left in the list that you did not approve (the same), `q` quits. `careerops unskip --shortlist` lists what was skipped this way and the parked ads; `--match TEXT` or `--all` brings them back.
  `shortlist --dry-run` prints the list and moves and counts nothing; without a terminal it only prints (and counts nothing).
- **No repeats**: an ad whose job is already approved (waiting in `data/jds/approved/`), skipped, applied to, closed or parked is never offered. Each list you were asked about is logged in
  `data/jds/shortlist_log.jsonl` (`times_shown` per job). The list prints `new since last shortlist: N` and marks a job shown before with `(shown 2×)`. A job shown 3 times with no decision is
  **parked**: it stays in the inbox, is never in the list, and is only counted in a `parked: N` line (`careerops unskip --shortlist` brings it back, counted from zero). With nothing new since the
  last list it prints `No new jobs since <time>. Run careerops discover --ats --gmail-alerts.` instead of the old list. `live --url` and `live --clipboard` are your own act for one ad, so they need no approval.
- **Volume caps are enforced where the action happens**: `live` stops with the count when `daily_max_applications` (5) submits (yours or auto's) have been made today, before it tailors and again
  right before a form opens; the per-company limit and cooldown are checked again right before a form opens (a held job stays `approved`). `live --auto` never takes unapproved ads.
- **`careerops patterns`** (weekly) reads the Sheet's Status column and prints, per role family, ATS kind and T band, the applications, replies, interviews, rejections and the reply rate. It
  may suggest ("T >= 4.0 had 3/9 replies; T 3.5-4.0 had 0/7: consider `min_triage_score: 4.0`") and **never changes a setting or a weight**. Not taken from the reference project: its LLM
  1-5 score (the ATS bands stay deterministic), its immigration templates.

### Region and company size (v2.4)

**Region.** Most non-UK jobs come from company boards (Greenhouse, Lever, Ashby, Workday), which return every place the company hires, so the region is a **filter after fetching**, not
only a question. `discover`, `shortlist` and `live` take `--region uk` (`uk,ie`, `eu`, `us`, `any`, or a two-letter country code such as `de`); `config/regions.yaml` defines the regions (code-owned;
it is not a setting of yours). The answer comes from, in order: the flag, today's / the last choice in `data/state/targeting.json`, `targeting.region` in `settings.yaml` (a name or a list), then `uk`.
At a terminal you are asked **once a day**:

```
Target region (Enter = UK, last used):
  [1] UK   [2] UK + Ireland   [3] EU   [4] US   [5] All regions   [6] type codes (e.g. uk,ie)
```

**No question ever appears without a terminal, or with `--yes`, `--auto` or `--mock`** (a scheduled run prints `region: uk (from last choice)` and goes on). A flag is for that command only and is never remembered.

Every job goes through one deterministic match (`discovery/region.py`, zero tokens): ISO codes become names (`GB`, `GBR`); `homonym_block` ("Cambridge, MA", "London, Ontario") is tested before `always_allow`; a city or a country the
place names is in, on word boundaries ("India" is not "Indianapolis", "Dublin, OH" is Ohio, "Toronto, CA" is Canada); a multi-place text ("London or New York") passes when any place does. A place that
does not say where the work is ("Remote", "EMEA", no place) is decided by **the ad's own text**: "right to work in the UK", "UK-based", "UK time zone", "open to UK residents only" -> in; "must be based in
the US", "US-based candidates only", "Remote within the United States only", "EU work authorisation required" -> out; both, or neither -> **`region?`** (kept and flagged: missing data is not a penalty).
The answer is stored as `region_match` (`in` / `out` / `unknown`) and `region_evidence` (the quoted words) on the job and on the inbox ad's score.

- **Out of the region = parked, never skipped.** Parked ads are not scored and not offered (`parked (region): N outside United Kingdom`); jobs in the database that wait with status `new`, `triaged`,
  `needs_jd` or `needs_country` get the status `parked (region)` (the old status is kept, in `jobs.parked_from`), and a later `--region` brings them back. Only your `[s]` makes a skip permanent.
- **Sources** carry a `regions:` list in `config/sources.yaml` (`uk`, `ie`, `eu`, `us`, `au`, `ca`, `de` ..., or `any`); a source for other regions only does not run (SEEK -> `au`, Job Bank -> `ca`, StepStone -> `de`,
  IrishJobs -> `ie`). A missing tag means `any`. The tool never edits `sources.yaml`: `careerops doctor` prints the lines to add.
- **Sponsorship.** The sponsor register is UK-only, so for a job in a non-UK region S is 0.75 ("unknown") and V is 3.5, not "not home"; the run prints `non-UK region: visa/right-to-work not checked`.
- **One ad you name** (`live --url`, `--clipboard`) outside the region asks `This job is outside <region>: continue? [y/N]`; in auto mode, or without a terminal, the answer is no.

**Company size.** UK Companies Act thresholds (checked 2026-10-31): small = 2 of 3 of up to 50 employees, GBP 15m turnover, GBP 7.5m balance sheet; medium = 2 of 3 of 250, GBP 54m, GBP 27m; SME = micro (up to 10),
small or medium; large = above. Shown as `SME`, `Large` or `Unknown`, with the label (`SME·small`). Evidence, in this order, cached per company in `data/company_size.jsonl` for 90 days:

1. **Your overrides**, `data/company_size_overrides.yaml` (`Graphcore: large`; also `sme`, `small`, `medium`, `micro`). Always wins; the tool never writes it.
2. **The ad's own text**: an explicit headcount ("team of 40", "1,200 employees", "500+ people"; a range counts its upper value) by the 50 / 250 limits; "Series A/B", seed, "founding team" -> SME; "FTSE 100/250",
   "Fortune 500", NYSE / NASDAQ / LSE-listed, offices in 10+ countries, "10,000+" -> Large; both at once -> unknown.
3. **Companies House** (free; `COMPANIES_HOUSE_API_KEY` in `.env`; GET only; one request a second; at most 60 new lookups a command): search by name, accept only if **exactly one active company** has the same normalised name
   (lower case, "Ltd", "Limited", "PLC", "UK" and punctuation removed), then `accounts.last_accounts.type`: `micro-entity`, `small`, `total-exemption-small`, `total-exemption-full`, `audited-abridged`,
   `unaudited-abridged`, `dormant` -> SME; `medium` -> SME (medium); `group` -> Large; `full` -> **Unknown** (a small company may file full accounts, kept as `full (large?)`). Without a key it is skipped
   (`careerops doctor` says so) and sizes come from 1 and 2 only.
4. Nothing found -> Unknown. Never LinkedIn or a data-broker site.

`careerops shortlist` counts the sizes of the queue (the ads that passed every other filter) **before** it asks:

```
Company size to target (Enter = Both, last used):
  [1] SME (<250 staff)   [2] Large (250+)   [3] Both, SME first   [4] Both, large first   [5] Both, no preference
  sizes in queue: SME 14 · Large 22 · Unknown 31
```

`--size sme|large|both|sme-first|large-first`, else the last choice, else `targeting.size`, else `both`; asked once a day at a terminal only. `sme` / `large` keep that bucket; the size-unknown ads are **not dropped**: they
are listed under `Size unknown (N): [u] include them` and use the daily slots only when you press `u`. `sme-first` / `large-first` order by T + 0.3 for that bucket and drop nobody. The daily cap, the per-company cap and
the cooldown are unchanged. Each line ends `[SME·small]`, `[Large]` or `[size?]` (and `[region?]`); `[i]` prints the evidence. `live` works only on an in-region ad of the chosen bucket (an ad of unknown size passes: you approved
it). Picking `sme`, `sme-first` or `both` prints once a day: *Note: UK small sponsors pay a lower Immigration Skills Charge (GBP 480 vs GBP 1,320 for the first year, rates from 16 Dec 2025), but fewer SMEs hold a sponsor licence.
The sponsor check still applies to every job.*

**Reports.** The tracker Sheet gets `Region` and `Size` columns appended on the right (columns AK and AL; an older Sheet gets them added after its last column, nothing is reordered); `careerops patterns` adds
**By region** and **By company size** with each reply rate; `discover --report` ends with `in region 41 · out 112 (parked) · unknown 9 | SME 14 · Large 22 · size? 31`; `careerops doctor` reports the Companies House key
(present or absent), the sources with no `regions:` tag (and the lines to add) and the share of the queue whose size is unknown.

### The document rules: `DOC CHECK`

Every CV and letter is held to the "Document rules" of `.claude/CLAUDE.md` by `tailoring/doccheck.py`, which reads the finished `.tex` and PDF text and prints
`DOC CHECK: pass` or the list of failures (on the review screen, after `tailor`, and in `live`). A failure blocks `[a]pprove` (you may type `override doc check` in full, and the
failures are logged) and the judge gate's auto-approval, and it **always** blocks `live --auto` (gate `doc check`). No setting turns it off.

- **Hook** the headline, profile sentence 1 and the first bullet of the hook's role all come from one `hook_library` entry (a headline the length fit cut short still counts), or, with no
  hook, are the role family's headline and profile line.
- **Profile** 100 to 150 words (`scoring.profile_words`), assembled in code: the hook's `profile_line`, the family's `profile_body`, then `profile.sentences` (the older `profile_facts` are read
  too), best keyword overlap first, stopping before 150 and trimmed at a sentence boundary. Under 100 the check says `PROFILE 84/100 words: add profile sentences to fact_bank.yaml →
  profile.sentences`; no model writes profile text and nothing is padded.
- **Skills** no skill twice: the canonical key is lower case with hyphens, dots and plurals stripped and a few aliases (`AWS Lambda` = `Lambda`, `TensorFlow/Keras` covers `TensorFlow`,
  `Kubernetes (AWS EKS)` covers `Kubernetes`). Generated groups keep the first occurrence; a repeat in your verbatim family `skill_lines` is reported, never rewritten:
  `SKILLS repeat "Lambda" in <A> and <B>: fix in fact_bank.yaml families.<name>.skill_lines`.
- **Projects** at most 3, each its own sub-heading (name on the left and its URL on the right, an italic stack line, 1–3 bullets). A URL prints only when the project has `github:` and is not `public: false` (the key is optional;
  `careerops doctor` warns when it is missing). With `scoring.cv_projects_need_url: true` (the shipped value, v2.3) a project that prints no URL is **left off** the CV; `false` shows it without a link.
- **Work experience first** the fill raises every role to its maximum bullets, then adds projects (up to 3), then the optional sections, then linespread. A short page 2 says `fact bank has no
  more experience bullets`; nothing is invented.
- **Certifications** a bulleted list, one item per certification.
- **Letter** (`letter.mode: company`) 200 to 320 body words (`letter.company_words`); the CV's name, contact line (full URLs) and hook headline in its header; P3/P4 only from bullets this CV prints, a skill it
  names must be in the CV's Skills or bullets, at least 60 % of the sentences of P1 to P4 cite a bullet (an internal tag, `report.json` → `letter.cites`, never printed); P1 is a quoted clause of the ad, a semicolon and the hook's
  fact (with a number from your bank), the clause from the ad, else from the employer's own careers page (one fetch of `companies.careers_url`, robots.txt obeyed, stored as
  `company_page.txt` with the URL and date; never news, LinkedIn or a job board); "I am writing to apply" and the like are rejected; the body is compared with every earlier letter in
  `outputs` and the archive folders (P2 to P5; 5-word shingles, Jaccard): 0.30 or more is redrafted once with `do not reuse these sentences: ...`, a second copy is no letter.
- **`careerops doctor`** reports profile words available in each family, skill repeats in the family lines, and projects with `github:` but no `public:`.

### After you apply: replies, follow-ups, interview prep

**Reply watch** (`careerops/reporting/replies.py`, runs at the end of `careerops discover --gmail-alerts`). It searches your mailbox, with the same
read-only Gmail scope, for the last 30 days of mail from the domains of the companies you applied to (and from applicant-tracking mailers such as
`greenhouse-mail.io` or `hire.lever.co` when the sender's name or the subject names the company), and classifies each as `interview | assessment |
rejection | info | noise` by rules (no model, no cost). The mail text is data: nothing in it is ever obeyed, and a mail that talks to the tool is flagged.
Each interview, assessment or rejection is **proposed**, one line at a time:

    Trainline: rejection (22 Oct mail) → set Status "Rejected"? [y/N]

Only `y` writes: the **Status** cell of that job's row in the Sheet (that cell alone; Interview / Assessment / Rejected) and the outcome in the database
(`interview`, `screen` for an assessment, `rejected`), so the funnel and `followups` know. Anything else, or Ctrl-C, writes nothing. A mail you answered is
not asked about again (`data/discover/replies_seen.json`). Without a terminal (cron, `--dry-run`) the proposals are printed and not asked. A Sheet that
cannot be written (no sign-in, no row yet) is one line; the outcome is still logged. It never replies, never sends, never labels or deletes a mail.
The rules have not met a real mailbox: read each proposal before you press `y`.

**`careerops followups`** lists the applications past `Follow-up due` (the day you applied plus `outreach.follow_up_days`, 7; for a job with no draft the same
day) that have no reply, oldest first, with the best stored contact or `no contact known`. It only reads. A follow-up message is drafted by `careerops outreach
draft JOB` under outreach's own rules and is never sent.

**Interview prep pack.** `mark-applied` (and `apply` / `live`, which end the same way) writes `prep.md` into the job's archive folder
(`paths.applications_auto/<Company>/<Role>/`, never over an existing file, never in your own `paths.applications`): the 8 to 10 questions the ad is most
likely to lead to (its keywords, required and heaviest first, then why-this-company and the project you are proudest of); for each, up to two fact-bank
entries to answer with, in situation → task and action → result order, the bullet quoted word for word with its id; the gaps from the gap report with a
one-line learn task; and three questions to ask them. **Facts about you come only from the fact bank.** A question with no supporting bullet, or on a term
on the fact bank's deny list, says `no evidence: don't claim`; a result is the bullet's own `result:` or the numbers in its text, never a new one. No model.

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
| Gated auto-apply on local fixture forms in headless Chromium: fills, screenshots, submits, confirms, stops at a CAPTCHA and at an unknown question; the gate on fixture reports and real rendered PDFs | a real Greenhouse / Ashby / Lever page: field labels, script-built drop-downs, confirmation text, an invisible reCAPTCHA; the tracker Sheet read |
| Hook, negation-aware ATS, letters A / B, note, freshness tiers, scheduler script (fixture bank, MockLLM, fake network, a fake `careerops` for the script) | the real CV's strict ATS against the golden 72.5, a real Anaplan run, the Windows task itself |

`career-ops` and this pipeline stay off every CV and letter — both are on the deny list.
