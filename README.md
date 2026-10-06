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
careerops doctor                # every line should say [ok]
careerops status                 # job counts, plus LLM tokens and requests used today vs the daily limits
careerops llm-check             # one tiny request per LLM provider: reachable, model, latency, JSON, json_mode (never prints keys)
pytest -q                       # no network or model needed
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
│   │   └── gates.py              rule triage (title, seniority, sponsorship, SRE, line mgmt…)
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
│   ├── review/                   workflow.py (approve/edit/reject → preferences) · outreach.py (M4)
│   ├── apply/assist.py           Playwright: fill basics, upload PDFs, never submit
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
4. **Applicant basics** — `config/profile/applicant.yaml`. Visa, salary, EEO and "why us" are deliberately never filled.

## Daily use

```bash
careerops sponsors                 # gov.uk register: download only if its link changed, store data/register/YYYY-MM-DD.csv.gz, diff vs the last snapshot
careerops discover                 # pull + triage; skipped jobs keep their reason
careerops retriage                 # re-run triage on all non-applied jobs (after changing roles/gates)
careerops add-jd --company X --title Y --url U --file jd.txt   # LinkedIn/email/agency roles
careerops add-jd --id adzuna:123 --file full_jd.txt            # replace a truncated Adzuna snippet
careerops tailor --limit 15        # CV (two full pages) + letter per job, in queue order → data/outputs/<job>/ (+ report.json)
careerops tailor --one-page        # the old one-page CV (templates/cv.tex.j2) instead
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
  never added. Then bullets are added until page 2 is at least `scoring.min_fill_page2` (0.80) full, measured from the PDF
  (y of the last text line on page 2 over the text area), or none are left: more experience bullets and more bullets of the
  three projects first; a 4th project only with `scoring.cv_fill_extra_projects: true` (off: the CV never shows more than 3).
  The shrink step runs only when the CV is over 2 pages. `cv_pages` and `page2_fill` go into `report.json` and the `jobs` table;
  the tailor line says when the fact bank ran out of bullets before page 2 filled.
- **Results** — give a bullet an optional `result:` in the fact bank (a measured outcome). The rephrase step may work it in;
  its numbers count as evidence for that bullet only. `report.json → needs_result` lists the bullets on that CV that have none
  (yours to fill in, never invented).
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

- **ATS literal** — conservative substring match on the PDF's text layer; the 70–85 % band applies to this.
- **ATS stemmed** — also equates *communicating/communicate*, *modelling/modeling*, *end-to-end/end to end*.
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
