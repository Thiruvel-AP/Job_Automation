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
# put GROQ_API_KEY (console.groq.com) and optional ADZUNA_APP_ID/KEY in .env
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
│   │   ├── intake.py             `add-url` / `live --url`: one job ad from its link into the inbox
│   │   ├── gates.py              rule triage (title, seniority, sponsorship, SRE, line mgmt…)
│   │   └── manual.py             hand-added jobs: read_jd (validation), add_jd, triage_job
│   ├── tailoring/                JD → CV + cover letter
│   │   ├── scorer.py             weighted ATS score (literal + stemmed), honest ceiling, keywords
│   │   ├── faithfulness.py       guard: no new numbers, deny-listed tools, unevidenced keywords
│   │   ├── prompts.py            all LLM prompts in one place
│   │   ├── selection.py          which bullets and projects go on the CV (code, not the model)
│   │   ├── tailor.py             analyse → select → rephrase → guard → render → band loop → profile, letter
│   │   ├── grounding.py          cover-letter check: every sentence against its evidence bullet and the JD
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
3. **Gates** — title/seniority/location regexes and the JD rules (no-sponsorship, SRE, line management, clearance).
4. **Applicant basics** — `config/profile/applicant.yaml`. Salary, EEO and "why us" are deliberately never filled; visa
   questions are answered from the `visa:` block (below), only when they are recognisable.

   ```yaml
   visa:
     summary: >-            # free-text visa/right-to-work boxes get this verbatim
       I am on the UK Graduate visa and won't need sponsorship for 2 years; afterwards I'd welcome Skilled Worker sponsorship.
     answers:               # Yes/No and dropdown questions, by type; quote them ("Yes"/"No"), or write "ask" to leave one to you
       sponsorship_now: "No"          # "require sponsorship?" / "... currently ..."
       sponsorship_future: "Yes"      # "now or in the future" / "in the future"
       right_to_work_now: "Yes"       # "currently/now have the right to work"
   ```

   A field is a visa field when its label mentions *right to work, visa, sponsor, immigration, work permit, work
   authorisation* or *share code*. Every one is outlined magenta and printed as `CHECK VISA ANSWER: <question> -> <value or
   EMPTY>`. A question whose type is unclear (e.g. "Do you have the right to work?" with no *now/currently*), a value of
   `ask` and any share-code box are left empty for you. Every other `never_fill` label stays empty and orange. Nothing is
   submitted. `careerops doctor` fails on an `@example.com/.org/.net` email, a `+44 0…` phone and a `location` with a UK
   postcode.

## Daily use

```bash
careerops sponsors                 # gov.uk register: download only if its link changed, store data/register/YYYY-MM-DD.csv.gz, diff vs the last snapshot
careerops discover                 # pull + triage; skipped jobs keep their reason
careerops retriage                 # re-run triage on all non-applied jobs (after changing roles/gates)
careerops add-jd --company X --title Y --url U --file jd.txt   # LinkedIn/email/agency roles (--file: UTF-8, at least 500 bytes, else a one-line error)
careerops add-jd --id adzuna:123 --file full_jd.txt            # replace a truncated Adzuna snippet
careerops add-url https://job-boards.greenhouse.io/monzo/jobs/123   # fetch one job ad into data/jds/inbox/ (see "Adding a job from its link")
careerops live --url URL           # the same fetch, then live works on that one file
careerops live --clipboard         # a site that blocks scripts: copy the job text, then this (asks Company / Title / URL; Windows clipboard via WSL)
careerops judge-report             # the LLM judge vs your decisions: agreement, kappa, false approves (see "The LLM judge")
careerops tailor --limit 15        # CV (two full pages) + letter per job (letter.mode: draft; with the default `off`, the CV only), in queue order → data/outputs/<job>/ (+ report.json)
careerops tailor --one-page        # the old one-page CV (templates/cv.tex.j2) instead
careerops tailor --id JOB          # exactly this job (it must be 'triaged'; --limit is ignored)
careerops live --max 5             # JD files in data/jds/inbox/, one job at a time, you at every gate (see "Live sessions")
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
careerops export                   # data/exports/companies.xlsx (Jobs, Summary, Outcomes)
careerops export-prefs             # your edits → DPO pairs (chosen = yours, rejected = model's)
careerops status                   # counts per status, plus a tier × status table
careerops calibrate                # tailor the calibration JDs with the real model and measure it (see below)
```

### Adding a job from its link (`careerops add-url`, `careerops live --url`)

`careerops add-url <link>` fetches one job ad and writes `data/jds/inbox/<company>-<title>-<id>.txt` with `Company:`, `Title:`,
`URL:` and `Location:` header lines filled in, then the ad's text. It adds nothing to the database: `careerops live` does that, with
you at every gate. `careerops live --url <link>` is the same fetch followed by a live session on that one file (the rest of the
inbox is left alone; a rerun finds the file already there). `--company` / `--title` replace what the page or API gives.

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
`careerops live` takes the files in name order, up to `--max` (default 5), and for each one:

1. checks the file (missing, under 500 bytes, not UTF-8 → moved to `rejected/` with a `.reason.txt` beside it, loop continues);
2. snapshots the archive (`paths.applications`; read-only `os.walk`: path, size, mtime);
3. adds the job (the internal `add-jd` function; the job id is passed on as a value, never read from printed output) and triages it;
4. tailors exactly that id, then asks you to review it: **approve / edit / skip** (plus open PDFs and regenerate the letter);
   skip leaves the job `skipped` and files the JD in `done/`;
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

`careerops doctor` sends one tiny request to each (provider, model) a task can be routed to: `extract_keywords`, `tailor_profile`,
`rephrase_bullets`, `cover_letter` and `judge` (per `llm.task_providers` / `llm.models`), once per model, and reports a line **per
task**. A failure says its kind: `model_not_found`, `auth`, `timeout` (60 s), `unreachable`, `rate_limited`, `bad_json`, and what to
change. A task whose feature is off (`letter.mode`, `review.judge`) asks nothing; `--no-ping` sends nothing.

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

### Letters (`letter.mode: off | draft`, default `off`)

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

Every CV is two full A4 pages (`templates/cv2.tex.j2`: 11pt Carlito, 0.6in margins, navy uppercase headings with a rule).
Order: Profile → Skills → Experience → Projects → Education → Certifications & Awards.

- **Skills** — `skills.core` in `fact_bank.yaml` is the first row ("Core"), in your order, and is never dropped. A skill
  appears once (case-insensitive, first row wins); the other groups list every skill with the JD's first; at most
  `scoring.max_skill_groups` (5) of them, the groups the JD matches least are left off. `careerops doctor` fails, naming the
  skill, if a core skill is on `deny_terms` or no bullet / skill row / project stack mentions it literally; the CV refuses to
  render a denied core skill.
- **Bullets** — chosen in code by greedy marginal keyword gain (ties to the more recent entry): newest entry 4–5, the others
  3–4 (`scoring.cv_bullets`), 3 projects with 2–3 bullets each. A bullet whose words overlap a chosen one (Jaccard ≥ 0.6) is
  never added. Then bullets are added until page 2 is at least `scoring.min_fill_page2` (0.65) full, measured from the PDF
  (y of the last text line on page 2 over the text area), or none are left: more experience bullets and more bullets of the
  three projects first; a 4th project only with `scoring.cv_fill_extra_projects: true` (off: the CV never shows more than 3).
  The shrink step runs only when the CV is over 2 pages. `cv_pages` and `page2_fill` go into `report.json` and the `jobs` table;
  the tailor line says when the fact bank ran out of bullets before page 2 filled.
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
- **CV profile** — with more than two `profile_facts`, code picks the two most relevant to the JD (the same ranking) and the
  model only words them. A project whose `award` text is already in its `name` prints it once.
- **Letter opening** — the claim half of the opening sentence (after "mirrors", a dash, ":" or ";") may use only the two
  evidence bullets; `actionable insights, user-centric, tangible outcomes, instantly, accurately, precise, smooth
  integration, efficient, exactly what I built` are vague filler unless the evidence says them. Rejected → one retry naming
  the phrase → any remaining problem → the template fallback above.
- **No-regression rule** — a rephrased or wording-pass bullet is kept only if it still hits every keyword (literal) the text it
  replaces hit; otherwise the earlier wording stays and `report.json` warns `no-regression: kept the earlier wording …`.
- **Wording pass** — after rephrasing, a keyword the CV has only in another form ("prototyped" for "prototyping") gets its
  exact form in the one bullet that carries its stem; the guard checks it like any rephrase and it is kept only if the score
  does not drop.

### Sponsor register

`careerops sponsors` reads the gov.uk page, finds the CSV link and downloads only if the link (or its date) is new; the file
is stored as `data/register/YYYY-MM-DD.csv.gz` with a `register_snapshots` row. Column names come from the file's own header
(a missing field stops the run, naming it). `companies` / `company_events` record added, removed, readded and rating changes
(one transaction; a same-day rerun does nothing); jobs of a sponsor that lost its licence or dropped from A get a flag.
Triage matches a job's company against the register (case, punctuation, Ltd/Limited/PLC/LLP, `&`/`and`, both sides of `T/A`)
and stores `sponsor_match` = `exact` | `fuzzy(0.95)` | `none` plus route and rating. **Not on the register is a flag
("Not on sponsor register"), never a skip.** "Register not loaded" appears only when no snapshot exists.

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

- **ATS literal** — conservative whole-word match on the PDF's text layer, singular/plural (trailing *s*/*es*) taken as
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

`career-ops` and this pipeline stay off every CV and letter — both are on the deny list.
