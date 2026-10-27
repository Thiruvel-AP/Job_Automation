# career-ops — implementation plan (sprint 1: M1–M4 + prompt upgrades)

Companion to [`PLAN.md`](PLAN.md) (what & why). This file is *how*: modules, signatures, schemas, algorithms, tests, acceptance.

**Conventions.** Python ≥3.10, stdlib + `requests/pyyaml/jinja2/openpyxl/playwright` only. Every module gets fixture-driven tests with **no network and no model** (`MockLLM`, monkeypatched HTTP). Every CLI step is idempotent (safe to re-run). Nothing reaches an employer without your click.

---

## 1. Your three prompts → pipeline stages

| Your prompt rule | Where it lives | How it's enforced |
|---|---|---|
| **Resume:** STAR rewrite, JD keywords woven in | `prompts.REPHRASE_*` | Bullet shape = *Action verb → what → how (tools) → Result*. Situation/Task compress into the clause "for X / to solve Y". |
| Result part of STAR | `fact_bank.yaml` → new optional `result:` per bullet | The guard already blocks new numbers, so a Result can only come from `result:`. Bullets without one are listed in review as **"needs a result (yours to supply)"** — never invented. |
| Prioritise relevant, cut what doesn't earn its spot | `selection.select_cv` + `tailor._shrink` | ✅ selection by relevance, in code; shrink drops lowest-keyword bullets until `scoring.max_pages` (1, CV-2) pages. |
| ATS 70–85 | `scorer` + band loop | ✅ Keyword weights come from JD sections (`scorer.classify_heading`, M5 1d ✅): required ×2, preferred ×1, before any heading 1.5; benefits/perks/values/hiring-process/"About <company>" sections and hashtag lines are ignored; a "…is a plus" clause is preferred while the rest of its line stays required. |
| LLM provider errors (M5 1b ✅) | `llm/router.py`, `llm/check.py` | HTTP errors keep status + body (≤ 500 chars, keys redacted, no headers) in the `LLMError` and an `events` row (`stage='llm_error'`). 429: wait `Retry-After` (else 2/4/8 s, ≤ 3 retries; waits above `llm.max_retry_wait_s` are skipped) then fall through to the next provider. 400 with `response_format`: retried once without it; if JSON comes back the provider stops sending it and `careerops llm-check` says to set `json_mode: false`. `tailor_job` builds in `<job>.partial` and swaps it in only on success, so a failed re-run keeps the earlier output folder. **Reasoning model, call budget, daily budget (M5 8):** `reasoning_effort` (default `low` for gpt-oss) and `include_reasoning` (default `false`) are sent in the Groq body; `max_tokens` is 2000 / 1200 / 600 / 1500 for extract_keywords / rephrase_bullets / tailor_profile / cover_letter. No prompt grows with the fact bank any more (code selects the bullets and a rephrase call carries at most 5), so the old tailor_cv budgeting (`budget.fit`, `llm.top_bullets`, `llm.max_call_tokens`) is gone; `llm_usage` also sums `reasoning_tokens` and has a `by_task` breakdown (calls, tokens, every call's prompt tokens in order; `careerops tailor` prints it). `Router.begin_job` (called by `careerops tailor` before every job) checks today's tokens + `llm.job_token_estimate` (10000; round 1 measured a mean of 6,911 and a max of 8,378 per JD) against each provider's `tpd`: a provider without room is skipped for that job, and if none has room the run stops with a message, the job untouched. `careerops status` lists tokens and requests used today vs tpd/rpd. **Pacing (M5 6–7):** `llm/pacing.py` sleeps before each call until the provider's window admits input (chars ÷ 3.5) + `max_tokens`: Groq's `x-ratelimit-remaining/reset-tokens` and `-requests` headers while present, else a local one-minute window from `providers[].tpm` / `rpm` (defaults 8000 / 30; null = unlimited); `tpd` caps tokens per day (`llm_usage.tokens`). A wait over `max_retry_wait_s` (a spent daily request budget) skips to the next provider. `llm.max_tokens` sets the completion cap per task; a call cut off by it says which entry to raise. **Local Ollama fallback:** the provider's model is `qwen2.5-7b-8k`, built from `config/ollama/Modelfile` (`FROM qwen2.5:7b-instruct`, `PARAMETER num_ctx 8192`) because the OpenAI `/v1` endpoint cannot set `num_ctx` and Ollama's default is 4096; verified 100% GPU, context 8192, ~47 tok/s. Each provider has `context_tokens` (groq 131072, ollama 8192): before every request the router estimates input + `max_tokens` and, if it exceeds that provider's `context_tokens`, skips the provider (reason in the `llm_skip` event and in the final error) so a prompt is never sent to be silently truncated. `careerops doctor` checks that Ollama answers, that the model is listed in `/api/tags`, and that `/api/show` reports `num_ctx` >= `context_tokens` (no `num_ctx` means 4096); on failure it prints `ollama create qwen2.5-7b-8k -f config/ollama/Modelfile`. `Router(only=name)` pins every call to one provider with no fallback. **Determinism:** every task runs at `llm.temperature: 0` with `llm.seed: 7` (a provider entry may set its own `seed`; `null` sends none). `report.json → llm_usage` lists each call's provider-reported prompt/completion/total tokens and finish_reason, with sums. Tests may not open the real `data/` (conftest guard). **Mock isolation:** `--mock` (`run`, `tailor`, `review`, `outreach`) redirects every generated-data path (`careerops/mockroot.py`) under `paths.mock_root` (`data/mock/`); `run`/`tailor` reseed one fixture job (`mock:northwind-ai-engineer`) each time and `run` skips discovery. fact bank and applicant stay read-only. |
| Small-model redesign (M5, after the first real run) | `tailoring/{selection,tailor,grounding,shapes,scorer,budget}.py`, `config/keyword_groups.txt` | The first Chattermill run showed a small model ignoring rules it was only told. Rules that can be code now are. **Keywords:** the model still suggests (`extract_keywords`), code decides: verbatim and whole-word in the JD, 1-3 words, no phrase containing tasks / workflows / features / approaches / systems / frameworks / patterns / skills / sets / quality / generation (and no "data", "experience"... alone), no degree requirements, "A or B" is one any-of keyword (`Keyword.alts`), a keyword whose words sit inside another's is merged into the shorter (`scorer.drop_contained`), wording variants merge (`keyword_groups.txt`), at most `scoring.max_keywords` (25) kept by weight then JD frequency (`scorer.cap_keywords`); each drop is an `analysis_warnings` note. **Selection:** `selection.select_cv` ranks every bullet by weighted keyword overlap (text + terms): the most recent entry keeps 2-4, the others 1-2 (as many as overlap), exactly 2 projects (the family's preferred projects only break exact ties); the shrink loop drops the least relevant bullet until `scoring.max_pages` (1) fits, keeping 2 for the most recent entry until it must not. The model no longer selects (task `tailor_cv` is gone). **Rephrase:** bullets whose terms evidence a JD keyword their text lacks go in batches of <= 5 (`REPHRASE_BATCH`) as (text, terms, `MAY INTRODUCE` keywords) -> `{id: new text}` (task `rephrase_bullets`); every rewrite goes through `faithfulness.check`; answers are cached across attempts; a failed batch keeps the canonical wording; `attempts[].rephrase` and `report.rephrase` log proposed / accepted / blocked / calls, `rephrase_coverage` log eligible / rephrased / ratio. **Profile:** once per job, the model picks two `profile_facts` and writes <= 45 words starting with the headline; the guard sees those two facts (and the headline) only; one retry with the problems, then the two facts themselves (`report.profile`). **Attempts:** a below-band attempt re-ranks with the keywords the fact bank evidences but the CV missed counted double; it stops when that would not change the selection. **Letter:** the model gets the company hook, two evidence bullets chosen in code (the CV's best for the JD, then the best from a different entry; canonical text) and the optional GAPS. `grounding.grounding_problems` matches every sentence to the bullet it is written from and flags a proper noun, client or sector word, or capability word (taxonomy skill, production / real-time / end-to-end ...) that is in neither that bullet (text + terms) nor the JD/hook; a sentence matching no bullet is checked against everything the model was given, and the required-gap sentence may use capability words. Plus the earlier rules: last paragraph ends on a question, no role upgrade (contributed / shipped / team may not become built / led / owned), approximations stay approximate, no scalable / robust / seamless / cutting-edge unless the evidence says it, no deny-listed tool except as a stated gap. A failed draft is retried once with its problems listed, then becomes the fallback letter, flagged (`letter.status`, `letter.problems`, `letter.evidence`). **Text:** LLM output is normalised (U+2010/2011 to "-", curly quotes, NBSP; en-dashes kept) before checking, scoring and rendering. **Gaps:** `genuine_gaps` counts a keyword as evidenced if any any-of alternative or keyword-group synonym is in the fact text, terms or skills, after stemming; synonyms never count for the CV's ATS score. |
| LLM output shapes (M5) | `tailoring/shapes.py`, `tailoring/rawlog.py` | The model's JSON is read, not trusted, and a model that fails never fails the job. **rephrase_bullets:** `{id: text}`, the same inside a wrapper key, a list of `{id, text}` objects, or an id mapped to an object; unknown ids and non-text are dropped (`normalise_rephrase`). **tailor_profile:** facts named by id ("F2"), number or text, at most two kept; too few valid picks are topped up by JD overlap in code (`normalise_profile`). **extract_keywords:** strings or `{term, weight, required}`; drops go to `report["analysis_warnings"]`. **cover_letter:** paragraphs as strings, `{text}` objects or one string; drops go to `report["letter"]["notes"]`. A down or garbage-answering model gives canonical bullets, the two profile facts and the fallback letter, not an error; a job fails only when rendering does. `shapes.clean_proposal` (the old tailor_cv reader, now also reading `{"az1": [...]}` objects keyed by id) is kept and tested but no longer called. **Raw replies:** every call's reply text is saved as `data/outputs/<job>/llm_raw/<task>-<n>.json` (`rephrase_bullets-<attempt>.json`, a second batch in the same attempt gets `-2`; a regenerated letter adds `cover_letter-1-2.json`); a failed job keeps them in `<job>.failed/llm_raw` (its error message says where; the earlier successful folder is untouched). |
| Calibration harness (M5 part 2) ✅ | `reporting/calibrate.py`, `careerops calibrate` | `data/calibration/manifest.yaml` (id, company, title, jd_file; optional manual_ats, reference_cv, outcome) -> per JD: triage verdict (recorded, tailored regardless), the real tailoring run, and keywords, ceiling, final literal/stemmed/band, rephrase coverage and guard blocks, selection, violations, letter status, calls, tokens (largest prompt), seconds. Writes `report-<stamp>.md` + `.json` and an empty-rating `rubric.csv` (never overwritten, only extended). Aggregates: guard block rate (investigate > 30 %), band hit rate over JDs whose ceiling reaches the band floor, headroom, manual agreement with Spearman rho (the reference-CV score replaces manual for the rank comparison when >= 4 entries have one), cost. `--compare <report>` sets two runs side by side; `--summarise` merges the filled rubric into the latest report with the M5 exit criteria; `--only ID ..`, `--limit N`, `--mock` (own data root, MockLLM). `--provider NAME` pins every call to one `llm.providers` entry (no fallback, so a run cannot mix providers; not combinable with `--mock`); `--freeze-keywords` extracts each JD's keywords once (one `extract_keywords` call per JD, no tailoring) into `paths.frozen_keywords` = `config/calibration/keywords/<id>.yaml` (tracked in git: they are curated inputs, not runtime output; term, weight, required, alts, company hook, team, drop notes, JD hash; a failed extraction is not written; an existing file is never overwritten without `--force`, checked before the model is called); every later run reads them whatever the provider, so ceilings are identical across runs and providers. If the folder exists every JD of the run must have its file, and a file whose JD changed since freezing is an error: the run refuses to start (nothing silently goes live for one JD); with no folder the run is live. The header says `keywords: frozen (<file mtime>)` or `keywords: live`, and the Detail lists every dropped keyword with its reason. `--mock` redirects the folder to `<mock_root>/keywords`. **Nothing in calibrate deletes anything** (a snapshot test compares path+sha256 of the calibration and frozen folders before and after a run); `tests/test_deletions.py` lists every delete/move/overwrite call in the package with its justification and fails on a new one. The per-JD console line and the report show a fallback letter's first rejection reason. Keyword hygiene: `scorer.drop_contained` folds a longer keyword into a shorter one only when the extra words are generic heads (pipelines, systems, frameworks, tasks, approaches, workflows, features, architectures, loops, environments), also into an any-of keyword ("retrieval pipelines" into "RAG or retrieval"); "hypothesis testing" and "statistical modelling" survive. `jdtext.strip_benefits` cuts a benefits section (headings: benefits, perks, rewards, what we offer, why join ...; up to the next heading or paragraph of sentences) from the JD before extraction. `cap_keywords` cuts by lowest weight, then lowest JD frequency, then the later in the alphabet, never by list order. Selection (`selection.select_cv`) is greedy by marginal gain: each pick adds the most keyword weight not yet covered by the picked bullets or the skills section, ties to the more recent entry (projects the same way, the family prior breaking ties). The cover letter's closing question comes from `letter.closing` / `closing_fallback` in settings, never from the model (a question the model writes is dropped); `llm.task_providers: {cover_letter: [groq]}` keeps the letter off ollama: when groq cannot take it the CV is still built, the job is `letter_pending` (excluded from review) and the next `careerops tailor` writes the letter when groq has budget; `rephrase_bullets` may still fall back to ollama. The letter's name check strips possessives ("Chattermill's"); the company name and its parts are always allowed. The CV's skills render once per case-insensitive name (`tailor.dedupe_skills`). The report header gives the provider setting and what served every call (flagging MIXED), and each JD row and task line list the provider/model that served its calls. Pacing: the Router sleeps to Groq's 8K TPM window per call, and `begin_job` checks the day's tokens against `llm.job_token_estimate` before every JD: when a job no longer fits the run stops, marks the rest `skipped_budget` and exits 1. **Deviations from the task file:** jobs are plain dicts, never rows in the jobs table, and the CVs go to `<calibration>/runs/<stamp>/outputs/`, so the real queue and `data/outputs/` are untouched (the DB is read for sponsors and holds the router's token counters); `questions` / `needs_result` are not in report.json yet, so they are not reported. Not run live yet. |
| Check memory / ask before altering; no hallucination | fact bank = the memory; `faithfulness.check` | ✅ guard. New: keywords with *partial* evidence (stem match only in `terms`, not in text) become **review questions**: "Is it true you did X in bullet Y?" |
| "Add all required skills and let me know so I'll keep learning" | **Resolved as:** never added to the CV; written to a per-job and a global **learning list** | Adding an unevidenced skill *is* a hallucination, so the two instructions only reconcile this way. See §5. |
| Keep the structure | `templates/cv.tex.j2` | ✅ CV-2: the user's own one-page layout — Profile → Experience → Projects → Skills → Education → Certifications & Awards. |
| Two A4 pages (CV, since 2026-10-06; `--one-page` for one) and one page (letter) | `render.page_count` + `render.page2_fill` + `tailor.fill_page2` + `_shrink`; `careerops tailor/run` prints both page counts and page-2 fill | ✅ §5d (`scoring.max_pages: 2`; CV-2 was 1); page 2 >= 90% full and the reference preamble: §5l |
| Output only the LaTeX | `data/outputs/<job>/CV.tex` (+ PDF) | Learning list and questions go to `report.json`/review, not into the CV. |
| **Cover letter:** company from JD, ask if missing | gates + new status `needs_company` | Agency/unnamed employer (e.g. Adzuna "Recruiter", JD says "our client") → held until you name it. |
| Hook, <250 words, no "I am writing to apply", specific CTA, human tone | `tailor._letter` | ✅ checks. New: hook must reference `company_hook` noun phrase; CTA must name a concrete next step (regex for call/chat/demo/walk-through + timeframe). |
| Letter ATS 70–85 | `scorer` on a **letter keyword set** | New: top-30 JD keywords by weight (your manual letter scorers used ~25–31; a letter can't carry a CV's 70). |
| **LinkedIn:** never-met [title] at [company], referral + team, small ask, direct, not sycophantic, banned opener | new `outreach.py` | See §6 — the 5-sentence cap doesn't fit LinkedIn's note limit, so two artefacts are generated. |

---

## 2. M1 — Register snapshots + diff — **done** (2026-10-06; live GOV.UK download not yet run)

`careerops sponsors` (also `companies --snapshot`) calls `register.take_snapshot`: fetch the publication page, find the CSV
link, stop if that link (or its date) is already in `register_snapshots`, else download, read the **header from the file**
(`register.EXPECTED_FIELDS`: organisation, town, county, rating, route are located by their words; a missing one raises
`RegisterFormatError` naming it and the header seen, before anything is saved), store `data/register/YYYY-MM-DD.csv.gz`
and diff against the register as `companies` holds it. `apply_diff(db, day, d, cur, *, snapshot=None)` is one transaction:
the first snapshot writes one `baseline` event; later ones `added` / `readded` / `removed` / `rating`, stamp
first_seen / last_seen / removed_on, flag in-play jobs of a removed or downgraded sponsor, and insert the
`register_snapshots` row (`snapshot` = (day, csv_url, path, rows), the one addition to the agreed signature). `careerops sponsors` also prints the folder (`paths.register_dir` = `data/register`) and the latest file. A day already
recorded is a no-op. Not built from the task file: `latest.csv`, the `sponsors` table refresh and `register.prune` (nothing in
`discovery/` may move or delete files, see `tests/test_deletions.py`).
**Sponsor check in triage:** `register.CompanyIndex` (the `companies` rows not removed) matches case, punctuation,
Ltd/Limited/PLC/LLP, `&`/`and` and both sides of `T/A`; `jobs.sponsor_match` = `exact` | `fuzzy(0.95)` | `none` (a fuzzy match
is never stored as exact), with `sponsor_route` and `sponsor_rating`. `none` adds the flag "Not on sponsor register" and the job
stays `triaged` (it used to be skipped); "Sponsor register not loaded" only when no snapshot exists (`register.load_index`).

Original spec:


**Files:** `careerops/sponsors.py` (extend), `careerops/db.py` (schema), `tests/test_register.py`, fixtures `register_d1.csv`, `register_d2.csv`.

**Schema**
```sql
CREATE TABLE register_snapshots (day TEXT PRIMARY KEY, csv_url TEXT, path TEXT, rows INTEGER);
CREATE TABLE companies (
  norm TEXT, town_norm TEXT, register_name TEXT, town TEXT, county TEXT, rating TEXT, route TEXT,
  first_seen TEXT, last_seen TEXT, removed_on TEXT,
  ch_number TEXT, ch_name TEXT, ch_status TEXT, sic_codes TEXT, sector TEXT, ch_match TEXT,
  public_sector INTEGER DEFAULT 0,
  ats TEXT, ats_slug TEXT, ats_verified INTEGER, careers_url TEXT,
  probe_status TEXT, last_probed TEXT,
  PRIMARY KEY (norm, town_norm));
CREATE TABLE company_events (norm TEXT, town_norm TEXT, day TEXT, kind TEXT, detail TEXT);  -- added|removed|rating
```

**Functions**
```python
def snapshot(db, settings) -> Snapshot            # download if today's CSV URL is new; gzip to data/register/YYYY-MM-DD.csv.gz
def load_snapshot(path) -> dict[(norm, town_norm), Row]
def diff(prev: dict, cur: dict) -> Diff           # added, removed, rating_changed
def apply_diff(db, day, diff) -> None             # upsert companies; set first_seen/last_seen/removed_on; write company_events
```

**Algorithm.** Key = `(normalise(name), normalise(town))` — name alone collides (e.g. two "Data Lab"). Set algebra on key sets: `added = K₂∖K₁`, `removed = K₁∖K₂`, `rating = {k ∈ K₁∩K₂ : r₁(k) ≠ r₂(k)}`. O(n) with hash sets; ~127k rows → well under a second.

**Side effects on jobs.** `removed` sponsor → any of its jobs in `triaged/needs_review/approved` get flag *"Sponsor licence no longer on register"*. `rating A→B` → flag.

**Tests / acceptance**
- d1→d2 fixture yields exactly the planted added/removed/rating rows; T/A names map to one company.
- Re-running the same day is a no-op (no duplicate events).
- Unchanged CSV URL → no download (monkeypatched `requests`).

---

## 3. M2 — Companies House bulk join + sector filter

**Files:** `careerops/companies_house.py` (new), `config/settings.yaml → companies`, `tests/test_companies_house.py`, fixture `ch_bulk_sample.csv`.

**Settings**
```yaml
companies:
  ch_bulk_page: https://download.companieshouse.gov.uk/en_output.html
  sponsor_ratings: ["A"]          # A-rated only (default agreed)
  sic_allow: {software: [58290, 62011, 62012, 62020, 62030, 62090], data: [63110, 63120],
              finance: [64191, 64999, 66190], insurance: [65110, 65120],
              pharma_research: [21200, 72110, 72190, 72200], consulting: [70229], health: [86101, 86900]}
  public_sector_pattern: '\b(nhs|university|council|trust|authority|agency|college|ministry)\b'
```

**Functions**
```python
def download_bulk(settings) -> list[Path]                 # monthly zip parts; skip if this month's already present
def stream_rows(paths) -> Iterator[dict]                  # csv.DictReader per part; header names read dynamically
def join(db, paths) -> JoinStats                          # single pass, see below
def classify(sic_codes, allow) -> str | None              # first matching sector or None
```

**Algorithm (single streaming pass, O(N_CH) time, O(N_register) memory).**
1. Build `R = {norm → [register rows]}` for A-rated Skilled Worker sponsors (~10⁵ keys).
2. Stream the ~5 M live companies; for each, compute `norm(CompanyName)`; if `norm ∈ R`, collect it as a candidate. Never hold the whole CH file in memory.
3. Resolve per register row:
   - exactly 1 candidate → `ch_match = exact`
   - >1 → keep those whose `RegAddress.PostTown` normalises to the register town → if exactly 1, `town`; else `ambiguous` (all candidate numbers stored, flagged)
   - 0 → `none`; if name matches `public_sector_pattern` → `public_sector = 1`
4. `sector = classify(sic_codes)`; companies with `ch_status = active` and a sector are **in scope**.

**Column names** are read from the header at runtime ([to verify] expected `CompanyName`, `CompanyNumber`, `CompanyStatus`, `RegAddress.PostTown`, `SICCode.SicText_1..4`; SIC text looks like `"62012 - Business and domestic software development"`, so codes are parsed with `^\d{5}`). If expected columns are missing the step fails loudly with the header it saw.

**Tests / acceptance**
- Fixture with: unique match, two same-name companies split by town, unresolvable ambiguity, dissolved company, NHS trust with no CH row, SIC text parsing.
- Memory bound: test streams a generated 200k-row file without materialising it (assert peak via generator use, not `list()`).

---

## 4. M3 — ATS resolution (slug probe, verification, detector, manual queue)

**Files:** `careerops/ats.py` (new), `tests/test_ats.py`, fixtures `careers_*.html`.

**Functions**
```python
def slug_candidates(register_name, ch_name) -> list[str]         # ≤4, deduped
def probe(slug) -> list[Hit]                                       # Greenhouse, Lever, Lever-EU, Ashby
def verify(hit, company) -> bool
def resolve(db, settings, limit, only_new=False) -> ResolveStats
def detect(url) -> tuple[ats, slug] | None                        # from careers-page HTML
```

**Candidate generation.** From `n = normalise(name)` with tokens `t₁…t_k`: `joined = t₁…t_k`, `hyphen = t₁-…-t_k`, `first = t₁` (only if `len(t₁) ≥ 5`), and the same for the CH name if different. Example: *Monzo Bank Ltd* → `monzobank`, `monzo-bank`, `monzo`.

**Verification rule** (precision over recall — a wrong board means applying to the wrong company):
- accept if the board reports a company name (Greenhouse `company_name`) and `ratio(norm(board_name), n) ≥ 0.9`;
- else accept if the slug equals `joined`/`hyphen` (full name, not the short `first`) **and** ≥1 posting is located in the UK;
- otherwise store as `probe_unverified` → shown in review/Excel for one click.

**Budget.** ≤4 slugs × 4 endpoints = 16 GETs/company at 1 req/s ⇒ ≈ 225 companies/hour. Order the queue by `first_seen DESC` (new sponsors first), cap per run (`--limit 300`), re-probe `not_found` after 30 days.

**Detector** (manual queue path): one GET of the careers URL, regexes:
```
greenhouse: (?:job-)?boards(?:\.eu)?\.greenhouse\.io/(?:embed/job_board\?for=)?([\w-]+)
lever:      jobs(?:\.eu)?\.lever\.co/([\w-]+)
ashby:      jobs\.ashbyhq\.com/([\w-]+)
workday:    ([\w-]+)\.wd\d+\.myworkdayjobs\.com
others:     smartrecruiters|apply\.workable\.com|teamtailor|recruitee|pinpointhq|bamboohr
```
Greenhouse/Lever/Ashby → board stored and fetched automatically. Others → `careers_url` stored; counted in the Excel "Companies" sheet so we build a fetcher only when the count justifies it.

**CLI**
```
careerops companies --refresh-ch      # M2
careerops companies --resolve [--limit N]
careerops companies --queue           # list unresolved, newest first
careerops companies --set "<name>" --url <careers url>   # runs detect()
```

**Tests / acceptance**
- Mocked APIs: true hit verified via `company_name`; slug collision (`apex` board with US-only jobs) rejected; 404/timeout handled; rate limiter respected (patched clock).
- Detector finds each signature in fixture HTML (iframe embed, link, script src).

---

## 5. M4 — Wiring, Excel, prompt upgrades, learning list

**Discover reads both sources:** `boards = settings.sources ∪ {companies where ats_verified=1}`. Jobs inherit `sector`, `first_seen` (shown as "new sponsor" badge in review).

**Excel:** new sheets **Companies** (register name, sector, CH match, ATS, verified, careers URL, first seen, status) and **Learning list**.

**Prompt upgrades (`prompts.py`, `fact_bank.yaml`, `tailor.py`)**
- STAR bullet instruction + optional `result:` field per bullet; bullets lacking it are reported, never padded.
- Review questions for partially evidenced keywords.
- Letter keyword subset (top-30 by weight) and band check on the letter; hook/CTA checks; `needs_company` hold.

**Learning list — the maths.** For each JD keyword k that is a genuine gap (zero evidence), accumulate across all triaged jobs J:

  priority(k) = Σ_{j∈J} w_{k,j} · 1[k is a gap in j]

where w = 2 (required) or 1 (preferred). Sorting by priority answers "which single skill would lift the most applications above the 70 % floor?" — e.g. if *Kubernetes* appears as a required gap in 14 jobs it scores 28 and tops the list. Per-job list goes in `report.json`; global list in Excel and `careerops learning`.

---

## 5b. M4b — Role tiers, queue order, run quota — **done** (spec: `docs/tasks/M4b-role-tiers.md`)

- `settings.roles` replaces `gates.title_include/title_exclude`: four families in three tiers, junior/mid level
  exclusion (`intern(ship)?` no longer catches International/Internal), `floor`, `tier3_max_share`.
- `gates.classify_role(title, settings) -> (family, tier) | None`, `gates.load_roles(settings, fb)` (validates
  preferred project ids against the fact bank), `gates.pre_ceiling(jd, fb, settings)`.
- `jobs` gains `family`, `tier`, `ceiling_pre` (additive `ALTER TABLE` via `db.COLUMNS`).
- `careerops/tailoring/queue.py`: `queue_key`, `order(jobs, settings, tailored=False)`, `select_for_run`.
- **Role families, 15 (Oct 21).** `roles.families` is matched in LIST order, first match wins (`gates.match_family`, `classify_role`); it is no longer
  sorted by tier. `priority` (1-15) is the user's ranking and never decides a match. `Family` gains `priority` and `skills_from`.
  `priority.role_factor(title, settings)`: R = max(0.5, 1 - `priority.role_step` x (priority - 1)); P = F x S x L x R; a title with no family has no R
  (`Priority.r is None`). R is printed by `Priority.role_text()` in the live inbox estimate, the final-P table, the review screen, discover's
  listing and status. `skills.pick_family/family_skill_lines(..., skills_from)`: a role family without a bank family of its own uses the bank
  family named by `skills_from` (the bank is never edited); `discover --report` prints the family of each ad the run wrote (`Run.as_json()["written"]`).
  k(j) = (1[c_j < floor], tier_j, −c_j, −posted_or_first_seen_j) with c = `ceiling_pre` for the tailor queue and
  COALESCE(`ceiling`, `ceiling_pre`) for review / apply / Excel. Quota: tier 3 ≤ ⌊share·N⌋ slots, then fills
  whatever tiers 1–2 leave empty.
- `careerops retriage`: pre-tailoring rows get a fresh verdict; `needs_review/approved/rejected/error` keep status,
  get the three columns and one `new rules would skip: <reason>` flag (replaced, never duplicated, on re-run);
  `applied` rows are never touched. One transaction.
- Tailoring: CV headline = family headline (rendered under the name); the family's preferred project ids break exact ties in `selection.select_cv` (they used to be a line in
  the tailor prompt; the redesign removed that prompt); MockLLM no longer selects; `report.json` records `family`, `tier`, `headline`.
- Visibility: `status` tier × status table, Excel `Tier` / `Family` / `Ceiling (pre)` columns, review header
  `T1 · Data Scientist`.
- Tests: `tests/discovery/test_roles.py`, `tests/tailoring/test_queue_order.py`, `tests/llm/test_mock.py`,
  two in `tests/review/test_workflow.py`.

---

## 5c. CV-2 — the user's one-page CV and cover-letter format — **done**

- Templates `templates/cv.tex.j2`, `templates/cover_letter.tex.j2` are the user's own layouts; Jinja filters `urlesc` / `urlshow` (`render.env`) escape and shorten link URLs. `identity.address` (optional) is read by the letter template only.
- `settings.yaml`: `scoring.max_pages: 1`; `letter: {greeting, greeting_fallback, signoff}`. `tailor.letter_greeting` picks "Hi {company}{team} team" when the JD names a team (`KEYWORDS_SYSTEM` returns `team`; kept only if it appears verbatim in the JD), else "Dear {company} hiring team".
- Profile 1–2 sentences ≤ 45 words starting "<headline> with …" (`PROFILE_SYSTEM`); exactly 2 projects, most recent experience 2–4 bullets, others 1–2 (`selection.select_cv`, in code); STAR-style rephrases (`REPHRASE_SYSTEM`). The profile is checked against the two profile_facts the model picked (and the headline) only.
- `LETTER_SYSTEM`: 3–4 paragraphs, < 250 words; hook ≤ 45 words; the two EVIDENCE lines as examples; optional ≤ 25-word sentence naming a **required** keyword the fact bank lacks (`GAPS`), only if there is one; CTA as a question. `GAPS` holds only keywords whose `config/taxonomy.txt` category is tool, platform, language or framework (`term | category`); broad skills (statistics, communication, research…) are never gaps, even when their literal term is absent from the fact bank (`report["genuine_gaps"]` still lists them for the review screen). `tailor._lack_problems` rejects any sentence matching `have not|haven't|never used|no experience` unless it names a `GAPS` term and no other tool/platform/language/framework. A deny-listed term may appear in the letter only when it is itself a required gap and the sentence says it is lacking ("I have not used X yet").
- Contact details (address, email, phone, links) never appear in a prompt: `tests/llm/test_prompt_privacy.py`.
- `careerops doctor` counts `# CONFIRM` markers like TODOs (`setup.fact_bank_open_items`).
- The user's header has no headline line, so the family headline (M4b) now reaches the page only as the first words of the profile.

## 5l. CV template round (2026-10-11) — reference preamble, fill to 90%, LinkedIn import — **done** (offline; never run live)

**Template.** `templates/cv2.tex.j2` reuses the preamble of the user's `config/reference/resume.tex` verbatim (a test pins every
line: 11pt, Carlito, `margin=0.6in, top=0.5in, bottom=0.5in`, headernavy/linkblue, titlesec 8pt/4pt, enumitem, `\linespread`,
no hyphenation) and its body layout; `\needspace` and the `linespread` variable are the only additions. `\textbar` replaces
`$|$` so Carlito is the only font in the PDF (math mode pulled in CMSY10). Separators use `\nobreak`: a line may end with one
but never starts with one. `CV2_MARGIN_PT` is now 0.5in (the vertical margin; the old template's was 0.6in).
Display helpers live in `render.py` (jinja filters `dates`, `org_line`, `role_line`, `school_line`, `degree_line`,
`publication_line`, `language_line`, `bullet`): `dates_display` ("Mon YYYY – Mon YYYY", no invented months), the freelance
heading (`employment_type: freelance`, or an org starting "Self-employed"/"Freelance"; the title loses a leading "Freelance "),
bold numeric results (`bullet_latex`). New optional fact-bank fields (all read with `.get`): experience `location`,
`employment_type`; education `location`, `modules`, `grade`; top level `publications`, `languages`, `research_focus`.
**Fill.** `scoring.min_fill_page2: 0.90` (was 0.65), `cv_projects: 5`, `cv_projects_max: 6` (replaces `cv_fill_extra_projects`),
`cv_linespread: 1.02`, `cv_linespread_max: 1.08`. `selection.fill_order(..., max_projects)` orders further projects first
(best uncovered JD weight, then rank), then the shown projects' bullets, then experience bullets; `tailor.fill_tail` (after
`fill_page2` ran out of bullets) adds `publications`, `languages`, `research_focus` one at a time (taken back if they push the CV
to a third page), then raises `ctx["linespread"]` in steps of 0.02 to the ceiling, keeping the last value that fits two pages.
`report["fill"]` gains `sections` and `linespread`; the review screen's `Fill:` line (`review.workflow.fill_lines`) shows the
fill, the target, the project count, the optional sections and the linespread. The earlier "layout round" items (5 projects,
bullet caps 4-5 / 3-4, modules, one-line certifications) had not been committed before this round and are part of it; the
profile text is unchanged (45 words, guarded).
**LinkedIn import.** `careerops import-linkedin PDF` (`tailoring/linkedin.py`): pdftotext text → sections (first occurrence of
each heading) → entries found by their date lines; page headers/footers and the debris of a header printed over a line are
dropped; a company with several roles shares its name and location. Writes `paths.fact_bank_proposed`
(`config/profile/fact_bank.proposed.yaml`, git-ignored) and `paths.linkedin_diff` (`data/linkedin_diff.md`); the fact bank is only
read (a test compares its sha256). Sections that LinkedIn prints only in part ("Projects (10)", two shown) give a warning and
*unverified* rows. `.gitignore` gains `config/reference/` and the proposal file.
Tests: `tests/tailoring/test_cv_template.py` (helpers, preamble, fill order, snapshots: 2 pages, page 2 >= 90%, Carlito only,
no line starting with a separator, layout, freelance, bold numbers, optional sections, linespread), `test_linkedin_import.py`
(fictional LinkedIn text `tests/fixtures/tailoring/linkedin_fixture.txt`, the CLI, read-only fact bank). Not verified: a
tailoring run with the real model; the real bank's fill was only previewed with MockLLM (92% with 6 projects).

## 5m. CV typography + selection round (2026-10-11) — **done** (offline; never run live)

**Typography, at render time only** (`tailoring/typography.py`, `config/typography.yaml`, `cv.date_separator`). `render.render_cv`
passes a copy of the context through `typography.typeset` (every prose string except links, e-mail, phone, dates and ids), so the
faithfulness / claim checks and `report["context"]` see the original wording. `clean`: `dashes` (em dash, `--`, spaced hyphen ->
comma / colon / parentheses; a number range keeps an en dash), then the `rewrite` table (whole word, case kept, a plural `s` kept),
with `protected` terms, URLs and e-mails masked first; JD keywords spelt with a hyphen (`hyphen_keywords`) are not rewritten.
`audit` (em dashes, prose en dashes, remaining hyphenated words) feeds `report["typography"]` (with `results_bold`,
`results_unmatched`, `award_repeats`, `date_separator`, `keep`) and the review screen's `Typography:` lines
(`review.workflow.typography_lines`); `apply_edits` re-renders with the same separator and keep list. `dates_display(text, sep)`.
**Type scale** (`cv2.tex.j2`): `\namesize` 19pt, `\headsize` 13.2pt, `\smallsize` 10pt (contact + stack lines), 11pt body.
**Bold / italics**: bold is the employer, school, project name, headings, skill-row labels and the bullet's `result:` text
(`render.result_span`: the result's words in order with at most two extra words between; `ctx["results"]` carries the bank's
results; the numeric auto-bold is gone); italics are the title, locations, degree name, stack line, certification issuer
(`typography.parse_cert`), publication venue and `(Expected)` / `(coursework)` (`render.inline`). `language_line` also reads
`{language, level}` (the real bank's spelling).
**Selection**: `domain.TOPIC_GROUPS` (behaviour/segmentation, validation, statistics) join the company domains for ranking only
(`jd_rank_weights`, `rank_overlap`; the letter's hook check still sees `DOMAIN_GROUPS` alone); `budget.relevance_parts` /
`relevance_scores` / `rank_projects(..., jd)` / `ranking_table`; `selection._Picker` counts uncovered domain groups in the marginal
gain (`select_cv(..., jd=)`, `fill_order(..., jd=)`); `report["ranking"]` and `review.workflow.ranking_lines` print the top 10.
`tailor._context` and `fill_page2` (`_insert` / `_remove`) keep the chosen bullets of a role in fact-bank order. Award: `unused_result`
strips the project's award from the result offered to the model, `guard_rephrases` refuses a bullet that adds it, and `typeset` takes
a repeat out of a project's bullets and results.
**Profile**: `qualifiers.QUALIFIER_LIST` gains `expert` and `passionate` (`proven`, `extensive` were there);
`qualifiers.unsupported_qualifiers` is now part of `make_profile`'s checks (a retry, then the picked facts) and both profile prompts
list the banned words. **Skills**: the first item of each row is capitalised (`render.skill_row`); `skills.cv_skills(..., jd)` never
drops a Databases row when the JD mentions SQL or databases (`wants_databases`), and a skill is listed once in the first row that is
shown (it used to vanish when only a dropped row had it); no Databases row in the bank adds an analysis warning.
Tests: `test_typography.py`, `test_selection_round.py` (light) and `test_cv_typography.py` (xelatex snapshot: no em dash in the PDF,
type scale measured from `pdftotext -bbox`, bold / italic lists, bank order, award once, Databases row, date separator); older
snapshots in `test_cv_template.py` / `test_relevance_round.py` follow the new bold and size rules. Not verified: a run with the
real model and the real bank (the review screen's hyphen list will show which words to add to `typography.yaml`).

## 5n. Style-reference template round (2026-10-11) — **done** (offline; never run live)

Supersedes the template, type-scale, bold/italic and linespread statements of §5l and §5m (`resume.tex`, 19pt / 13.2pt, `\entry`,
`headernavy`, `cv_linespread: 1.02`).
**Template.** `templates/cv2.tex.j2` renders with the preamble and macros of `config/reference/cv_style_reference.tex` (git-ignored)
line for line; `tests/tailoring/test_cv_template.py::REFERENCE_PREAMBLE` pins the lines and, when the reference file exists, compares
them to it. Additions: `\usepackage{needspace}` and `\linespread{\VAR{linespread|default(1.03)}}`. Body: experience -> `\role` (`\roleNoLoc`
without a location; `render.org_parts`), projects -> `\project{name}{stack}{tag}` (`render.project_parts`: the award, else the project's `badge:` (`render.badge_shown`; same once-only rule), else a trailing
"(MSc coursework)" of the name when the project has no link, as `\badge`; else `\gh`; a project with no stack prints name and tag
without the empty stack line), skills -> `\skillrow`, education -> `\roleNoLoc` plus a muted `\small` line (`render.education_lines`:
degree, grade, note / modules, details), certifications -> one bullet, `render.cert_tex` puts the date in italics (the issuer is plain)
and a trailing year of a plain string is split off. `bullet_latex` / `bullet_at` take `wrap`: cv2 passes `num`, so only a bullet's
`result:` text is in `\num{}` (the one-page template still gets `\textbf`). Separators are `\nobreak\ \,\textperiodcentered\, `, so
a line may end with a dot but never starts with one. Header: 22pt navy name, `\small` contact line (short link text), italic muted status line.
**Type scale**: 22pt name, 13pt headings (`\fontsize{22}{26}` / `\fontsize{13}{15}`), 10pt = `\small` (contact, stack, badge, GitHub
link, modules line), 11pt the rest; the PDF's only font is Carlito (the reference's `$\rightarrow$` is not used).
**Fill settings**: `cv_linespread: 1.03` (the reference's) and `cv_linespread_max: 1.09` (three steps of 0.02, as 1.02 -> 1.08 was);
`tailor.LEADING_PT` and `review.workflow.fill_lines` follow. `CV2_MARGIN_PT` is unchanged (0.5in).
Tests: `test_cv_template.py` (preamble and macros, body macros, `project_parts`, `education_lines`, `cert_tex`, and a snapshot of the
fixture profile that reads section order, font names and the 22 / 13 / 11 / 10pt sizes back from `pdffonts` / `pdftotext -bbox`),
`test_cv_typography.py` (bold / italic lists now check the macro arguments), `test_relevance_round.py`, `test_typography.py`.
Not verified: the real bank's fill (the macros take slightly more vertical space than the old template, so it may pick a different
number of projects), and ATS reading of the letter-spaced headings beyond what `pdftotext` shows in the test.

## 5d. Two full pages, outcome log, calibration fix round — **done** (2026-10-06; none of it run live)

**Two pages.** `templates/cv2.tex.j2` is the default (`cv.tex.j2` only with `--one-page`). `scoring.max_pages: 2`,
`min_fill_page2: 0.65` (raised to 0.90 in §5l): `render.page2_fill` reads the rendered PDF (`pdftotext -bbox`, y of the lowest word on page 2 over
the text area, `CV2_MARGIN_PT` must match the template's geometry). `selection.select_cv(fb, kws, preferred, skills, layout)`
is greedy marginal gain with `Layout`s (`ONE_PAGE` 2-4 / 1-2 / 2 projects; `TWO_PAGE` from `scoring.cv_bullets`: 4-5 newest,
3-4 others, 5 projects x 2-3 bullets since §5l), skips a bullet whose token Jaccard with a chosen one is >= `bullet_dedupe_jaccard`
(0.6) and returns `project_bullets`. `selection.fill_order` lists unused bullets (experience and the chosen projects' bullets by
gain; since §5l further projects first, up to `cv_projects_max`); `tailor.fill_page2` adds them in batches
sized by a line-count guess, renders, takes back what tipped the CV onto page 3, until page 2 is full or none are left; shrink
runs only above `max_pages` and the fill is skipped after a shrink. `tailoring/skills.py`: `skills.core` pinned first row,
case-insensitive dedupe, JD-first order, at most `max_skill_groups` other rows; `core_skill_problems` (doctor: deny-listed, or
not literally mentioned elsewhere in the bank); `cv_skills` raises for a denied core skill. A bullet's optional `result:` is
sent to rephrase (`RESULT:` line, `unused_result`) and evidences its own numbers in `guard_rephrases`; `report["needs_result"]`
lists CV bullets without one. `report.json`: `one_page`, `cv_pages`, `page2_fill`, `fill`, `skills`; `jobs.cv_pages/page2_fill`.
**Wording pass** (`tailor.wording_pass`): per wording miss, the one CV bullet carrying the keyword's stem is re-asked with an
`EXACT FORM:` line, guarded like any rephrase, kept only if the literal score does not drop and the CV still fits.
**Letter:** the prompt lists `grounding.avoid_words` (filler and capability words nothing supports) under AVOID; a rejected
draft is retried once with `grounding.rejected_words` named; `report["letter"]["retry"]` = {count, rejected_words,
first_problems, outcome}.

**Outcome log.** `outcomes` table; `reporting/outcomes.py`: `record`, `record_applied` (called inside the transaction of
`archive.record_application` / `mark_applied_unarchived`), `wilson`, `applied_jobs`, `funnel`, `build_report` (splits family /
tier / cv pages / sponsor match; `no_response` after 28 days is computed), `propose_from_archive` / `apply_proposals` (listing
and stat only; writes the DB only). CLI `outcome`, `outcomes [--import-archive [--apply]]`; Excel "Outcomes" sheet.

**Calibrate.** Per-JD table gains `pages` and `page 2 fill`; the triage log line shows `(flag: Not on sponsor register)`;
the sidecar records `rubric_mtime_at_run` and `--summarise` prints `RUBRIC PREDATES THIS RUN — grades are not for these outputs`
(rubric older than the run, or not saved since the run wrote it) above the table, leaving the false-claim and would_send
criteria unticked.

## 5e. `careerops live` — interactive batch over a JD inbox — **done** (2026-10-07; tested with fakes and MockLLM, never run live)

`careerops live [--max N] [--dry-run] [--mock]`: for each `.txt` in `paths.jds/inbox` (name order) validate → archive snapshot →
`discovery.manual.add_jd` (returns the job id; no stdout parsing) → `manual.triage_job` (that job only) → `cli.tailor_jobs` for that
id → `workflow.review_job` for that id (approve / edit / skip, + open PDFs, regenerate letter, quit) → unless `--dry-run`: preflight,
"Open the application form?", the existing `assist` (never submits), every `CHECK VISA ANSWER` line printed, "Did YOU submit the
form? [y/N]" → `y` only: `_archive` (= mark-applied) → second snapshot → compare → JD to `done/<id>-<name>`. `--yes` is refused.
Code: `apply/live.py` (the loop; the costly steps are `Hooks` that take a job id, so tests inject fakes), `apply/snapshot.py`
(`take` = os.walk + lstat, `compare` → removed / modified / stray; a folder's own size and mtime are ignored since adding a child
changes them; the one allowed addition is the job's `application_dir` pair `<Company>/<Role>`), `discovery/manual.py`
(`read_jd`: missing, < 500 bytes or non-UTF-8 → `JDError`, one line; `add-jd --file` uses it too), `cli.tailor_jobs` (the loop
`tailor` uses, now shared), `tailor --id JOB`. New settings `paths.jds`, `paths.live`. State: `paths.live/state.json`, keyed by the JD
file's sha256: a started job keeps its id, a finished one is skipped; a rerun resumes by DB status (triaged → tailor,
needs_review → review, approved → apply). Exit codes: 0, 1 (daily LLM budget spent, job stays `triaged`), 2 (unreadable state
file), 3 (archive changed: JD not filed, nothing further processed), 130 (Ctrl-C / EOF). Statuses it adds: `skipped` after a
review skip (the job keeps its `out_dir`; `retriage` now treats a `skipped` row with an `out_dir` as post-tailoring so it is
not reset to `triaged`) and `ready_not_submitted` (declined to open the form, or "did you submit" was not `y`).
`add_jd` refuses (`JobExists`) a JD whose job id already exists past triage, so a repeat never resets tailored or applied work.
`tests/test_deletions.py` lists the two file operations in `apply/live.py` (the JD move inside `paths.jds`, the state swap).
**Not verified:** a real assist/browser session and a real Groq tailoring run inside `live`.

## 5f. Live-run fixes (2026-10-07) — context terms, possible evidence, three letter drafts

**Context terms** (`tailoring/context_terms.py`): `analyse` (the one place keywords are made, for live tailoring and for
`calibrate --freeze-keywords` alike) sets team / product-area / org names aside before the cap: `split_context` = organisational noun
as the last word (squad, team, collective, guild, tribe, chapter, pod, department ...), a name in a list of three or more capitalised
names in the JD ("Borrowing, Growth, Payments, Fincrime, Wealth, or Core"), the model's `context_terms` (verbatim in the JD), or the
team's own name; a taxonomy term is never context. They are out of the score's numerator and denominator and the ceiling;
`report["context_terms"]` = [{term, weight, required, why}], a `keywords: 'X' is context (...)` note each, frozen files carry
`context_terms`, the review screen has a "Context terms" line. Monzo (manual:de99f0a98229): 19 stored keywords -> 10 scored + 9 context.
**Possible evidence** (`tailoring/families.py`, `config/evidence_families.txt`): a gap with a family member in the fact bank moves from
`genuine_gaps` to `report["possible_evidence"]` {term: [{via, where}]} and is shown as "Possible evidence — you decide". Not counted
in the ceiling or the CV, and not a letter gap. Matching is on stems and plural-stripped words (the stemmer maps experiment and
experiments differently).
**Letter** (`tailor._letter`, `LETTER_ATTEMPTS = 3`): a rejected draft is retried with every rejected word so far
(`Do not use 'X' unless it appears in the evidence bullets.`) and the full problem texts; `retry` = {count (retries), rejected_words,
first_problems, failures: [{attempt, problems}], outcome}; the review screen prints "Letter draft N rejected: ..." per failure.
The Monzo run fell back because the retry named only 'integrated' and the model then wrote 'specialised'. `llm.letter_token_estimate`
is 5000 (three drafts). Earlier items re-confirmed: `org_label` ("in my freelance AI/ML work", "at AstraZeneca"),
`llm.max_tokens.cover_letter: 3000`, hook and paragraph 2 from different evidence bullets (`grounding.repeated_evidence_problems`).
`careerops live --retailor` sends a started job (not applied) back to `triaged` and tailors it again.

## 5g. Fixture profile, doctor plural match, qualifier repair, generic head words (2026-10-08)

**Tests never read your profile.** `settings` points `paths.fact_bank` / `paths.applicant` at `tests/fixtures/profile/` (a fictional
Ada Lovelace, no Kubernetes; project ids match `settings.yaml roles.families` so `load_roles` accepts it). `conftest` audit-hooks
`open()` of the real `config/profile/*.yaml` (a swallowed error still fails the test in teardown) and
`tests/test_no_real_profile.py` fails if any test source spells that path. `test_ceiling_reports_genuine_gaps` and
`test_tailor_loop_end_to_end` now run on the fixture. **Slow marker:** a test that runs xelatex or the Playwright driver must be
`@pytest.mark.slow` (the conftest fails it otherwise; `CAREEROPS_SLOW_LOG=<file>` lists offenders instead); `pytest -m "not slow"` is the light run.
**Doctor:** `skills.core_skill_problems` matches with the scorer's literal view (`has_literal`: singular/plural in both directions,
"llm" evidences "LLMs", "api" evidences "APIs"); the suffix stemmer is deliberately not used (it would let "plots" evidence "Plotly").
**Letter qualifiers** (`tailoring/qualifiers.py`): if every problem of a draft is `'<w>' (claim)` or `vague filler '<w>'` with `<w>` in
`QUALIFIER_LIST`, the words are deleted from the body (not from the template closing), the whole `draft_problems` check re-runs, and the edit
is accepted only if it passes; `only_removed` verifies deletion-only (a/an aside), and a qualifier inside a skill name ("deep learning"), after
a copula ("is robust") or whose removal leaves a dangling article/conjunction/doubled word is not touched. `report.letter.retry.removed` +
`outcome` "qualifiers removed[ after retry]"; the review screen prints `Letter removed: ...`. `LETTER_SYSTEM` lists the qualifiers up front
({qualifiers}, minus the words the evidence uses). **Generic head words:** `context_terms.FRAMING_HEADS` (discipline, culture, mindset, ethos)
and `X function` after a unit word (data, analytics, business ...) make a term context, never a gap.

## 5h. Relevance round (2026-10-09) — domain-aware evidence, profile facts, tighter paste and hook checks

Review of the Monzo run: the letter was written from the LLM-agent bullets (az1, dc1) for a bank. Nothing is loosened; the rest re-ranks.
**Domain** (`tailoring/domain.py`, new): `DOMAIN_GROUPS` (finance, customer, health, travel, retail, advertising ...), `jd_domain(jd)` =
{group: 1/3, 2/3 or 1 by how often the JD names it}, `domain_groups(text)`, `domain_overlap`. **Ranking** (`budget.letter_scores`, `fact_scores`):
keyword overlap (`bullet_scores`) + `DOMAIN_WEIGHT` (2.0, one required keyword) x the JD weight of every domain group the bullet names in its
text, `terms` or entry (project name, job org/title). `tailor.letter_evidence(fb, kws, chosen_ids, jd)` ranks ALL bullets on the CV (the
past-tense-verb filter is gone), picks the best, then the best from a different entry. A noun-led bullet in the fallback letter reads
`letter_rules.i_clause` = "I worked on ..." (never "built"), and `clause` keeps acronyms' case. **Profile:** with more than
`tailor.PROFILE_FACTS` (2) `profile_facts`, `pick_facts(facts, kws, [], 2, jd)` chooses the top two by `fact_scores` (bank order on ties,
shown in bank order); the model is offered only those two, the guard sees only those two. **Paste check:** `letter_rules.paste_overlap` =
share of a sentence's words (alphanumeric tokens, bracketed text and a leading "I" removed, >= 5 words) that one evidence bullet also has;
`PASTE_OVERLAP` = 0.7 makes `grammar_problems` report "pastes an evidence bullet (N% of its words are the bullet's)"; the exact-match message
stays. `MockLLM`'s paragraph 2 is now a short own-words sentence. **Award:** `render.award_shown` (jinja filter `award`) returns "" when the
award text is in the project's name; `cv.tex.j2` / `cv2.tex.j2` use it (the GitHub link shows instead). **Hook link:**
`grounding.hook_link_problems` (called in `_letter.draft_problems` on paragraph 1): a sentence with mirror/reflect/align(s|ed|ing)[ with] is
rejected when the company half and the work half (claim + the bullet it is written from, text + terms) each name domain groups and share
none. A side naming no domain word is not judged. Tests: `tests/tailoring/test_relevance_round.py`; `test_guard_round.REST` and a few
letter/profile tests were reworded for the stricter paste rule and the code-picked facts. Not run live.

## 5i. Stability round (2026-10-10) — frozen keywords per job, head words, letter model, letter failure policy

Committed first: the relevance round above ("relevance: evidence ranking, paste overlap, award dedupe, hook sanity"); the slow chunk found 7
tests whose "clean" letter drafts were a bullet with "I" in front, which the paste check rightly rejects. `tests/conftest.restate` gives them
real restatements; the check is unchanged. Not run live; `careerops doctor`'s model ping and a real letter from gpt-oss-120b are unverified.
**1. Frozen keywords.** `jobs.analysis` (TEXT, additive column) holds a job's first extraction as JSON: keywords (term, weight, required, alts),
context terms, company hook, team, drop notes, the JD's sha1 and the date (`tailoring/freeze.py`: `record`, `frozen_for`). `cli.tailor_jobs`
passes `frozen_for(job)` to `tailor_job(frozen=...)`, so a job tailored again (`live --retailor`, a retry after `error`) calls no
`extract_keywords` and gets the same keyword list, ceiling and ATS; the output says `keywords: reusing the extraction frozen on <date>`.
`tailor_job(on_extracted=)` stores the record the moment `analyse` returns, before anything else can fail, but never for a failed model call
(taxonomy-only keywords are not frozen) or when a frozen analysis was used. A changed JD (`add-jd --id`) is extracted again, and says so.
`--re-extract` (on `tailor`, `run` and `live`) redoes the extraction and freezes the new one. Calibration keeps its own frozen files.
**2. Head words.** `context_terms.FRAMING_HEADS` gained perspective(s), thinking, attitude(s); `FRAMING_TAILS` = "decision making" /
"decision-making" as the last words ("data-driven decision making"). Only the end of the term counts: "decision trees", "decision making under
uncertainty", "perspective projection" stay keywords; taxonomy terms are never context.
**3. Letter model.** `llm.models: {cover_letter: {provider: groq, model: openai/gpt-oss-120b}}`: that task uses that model on that provider,
every other task and provider is unchanged, and `task_providers` still keeps the letter off Ollama. Groq limits each model on its own, so a
non-default model is counted, paced and budgeted under the key `groq/openai/gpt-oss-120b` (`Provider.key`, `pacer_for`, `available(model)`,
`tokens_left(model)`; the `llm_usage` table gets rows under that name). `Router.begin_job(est, task=)` checks that model's budget (finishing a
`letter_pending` letter uses `task="cover_letter"`). `report.json -> llm_usage` has `by_model` ("groq/openai/gpt-oss-20b": calls, tokens,
tasks) and the review screen prints it under "LLM tokens by model". `careerops doctor` and `llm-check` send each `llm.models` model a tiny
request (`setup.model_checks`, `llm.check.check_task_models`); `doctor --no-ping` sends nothing. A models entry naming a provider that is not
configured is ignored by the router and reported by doctor; a malformed one raises. Every letter check is the same as before.
**4. Letter failure policy.** `letter.on_failure: none | template` (default `none`; `tailor.letter_policy`). After the 3 drafts: `template` = the
fallback letter as before (status `fallback`, approve needs `send fallback`); `none` = no letter: `report.letter.status == "none"`, no PDF/.tex
(`letter_pdf` is null), `reason` ("3 draft(s) rejected" / "the letter model failed"), `retry.failures` as always. The job goes to
`needs_review` (not `letter_pending`, which waits for a budget), review shows `NO LETTER — write your own or apply without` and approving
needs the words `no letter` (`review.NoLetterError`, event `approved_without_letter`); `l` regenerates. `archive._sources` treats status `none`
as an approved no-letter submission (job.json `no_letter: true`), so `apply`/`live` do not also need `--no-letter`. Calibration counts
`none` letters and the M5 "no letter with rule problems" check fails on one. Tests that exercise the fallback letter set `template`.

**5. Entry name in the evidence's domain; evidence ids on the review screen (small fix round).** `budget.entry_where(entry)` = the project's `name`, the job's `org` and `title`; `letter_scores` uses it as before, and `letter_evidence` now also returns it as each evidence dict's `where`. `grounding.hook_link_problems` reads the work side as claim + bullet text + terms + `where`, so a bank hook on "Banking Customer Churn" (bullet: "churn classifier ...") is no longer rejected for a domain only the project's name says; a hook linking a bank to a cancer-research agent is still rejected. The review screen prints `Letter evidence: p_ch1, av1` under the letter score (from `report.letter.evidence`; older reports without it print nothing). Tests (`test_relevance_round.py` sections 6-8): az1/dc1 are never the letter evidence for the Monzo JD with no LLM keywords, whatever the chosen set.

## 5k. Intake automation, sponsorship weighting, local judge (2026-10-10)

Tests: `pytest -q -m "not slow"` was 1273 passed at the start of this round. Parts below are marked **done** when committed.

**A. Judge on local Ollama — done.** `llm.models.judge: {provider: ollama, model: qwen2.5-7b-8k}`, `llm.task_providers.judge: [ollama]`
(Groq's llama-3.3-70b-versatile answered 404 model_not_found). `judge.prepare_input(job, rep, fb, budget)` fits the prompt to
`input_budget` = ollama `context_tokens` (8192) − `llm.max_tokens.judge` (800) − `CTX_MARGIN` (300): `jdtext.role_sections` keeps the
role / requirements sections (drops about-us, benefits, equal-opportunity, how-to-apply; unknown headings stay), then the JD is cut to the
room left, then source facts are dropped from the end before the JD goes under `MIN_JD_CHARS`; if CV + letter alone do not fit → no
verdict. Tokens (estimate and the provider's `prompt_tokens`) are on `Verdict.tokens`, printed by the review screen, in
`log.csv → judge_input_tokens` (an older log keeps its own header) and in an `events` row (`stage = judge`). Failure: the router's two
attempts (unreachable / not JSON) then `Verdict(error=...)` — never raises, logged beside the human decision. Gate rules unchanged.
`check.py`: `classify()` (model_not_found / auth / timeout / unreachable / rate_limited / bad_json / http_error), `Router.serving(task)`,
`check_tasks`; `setup.model_checks` asks every (provider, model) every task can use once and reports per task (a feature that is off
asks nothing; a task restricted to a provider that is not configured fails). Tests: `tests/review/test_judge.py`, `tests/llm/test_router_models.py`.

**B. Clipboard intake — done.** `intake.add_clipboard`: prints "Copy the job ad now, then press Enter" and waits (`ask("")`) before the
clipboard is read; `read_clipboard` refuses < 2000 characters and text whose first word is a shell command (`shell_command_start`:
careerops / powershell(.exe) / explorer(.exe) in any case, `cd` / `git` / `source` only in lowercase so an ad that begins "Source
Digital ..." is kept; a `$ `, `> ` or `PS ...>` prompt mark in front is ignored); `preview` prints the count and the first 3 non-empty
lines; `Use this? [y/N]` — only `y`/`yes` goes on, else `IntakeError` and nothing is written. B2 (company / title ≥ 2 characters, no `<`
`>`) was already `manual.meta_problem` behind `_ask_field`; the tests now cover both fields. Tests: `tests/discovery/test_intake.py`.

**C. Robots-aware URL intake + CV-Library — done (fixtures only; not run against a live site).** `discovery/robots.py`: own RFC 9309
matcher (`urllib.robotparser` ignores the `*`/`$` that CV-Library's `Disallow: /*?` and `Allow: *?jobId=` need), group chosen by token
`career-ops` else `*`, longest rule wins, Allow wins ties; `RobotsCache(dir)` keeps `<host>.json` for `TTL_S` = 24 h (`paths.robots`);
404/410 = allowed, 401/403/429/5xx/no answer = refused ("cannot be checked") except a 401/403 on the three documented API hosts
(`DOCUMENTED_API_HOSTS`: Greenhouse boards-api, Lever, Ashby; observed 2026-10-10: Ashby's `/robots.txt` answers 401 although its posting
API is documented as public; Greenhouse's has `Disallow: /embed/`, Lever's `Allow: /`). `intake._fetch` checks robots.txt before every GET
and follows redirects by hand (`allow_redirects=False`, max 5) so a hop to a disallowed path or to linkedin.com is refused; the robots.txt
request is paced like any other (so the first fetch from a host takes a second longer). `NEVER_FETCH` = linkedin.com, lnkd.in (checked in
`parse_url` and on every hop, before any request). CV-Library: `parse_url` rewrites `/job/apply/<id>[/slug][?q]` and `/job/<id>[/slug][?q]`
to `https://www.cv-library.co.uk/job/<id>` and refuses every other path (search/query); `cvlibrary_ad` reads JSON-LD `JobPosting`
(title, hiringOrganization, jobLocation, baseSalary, datePosted, description) and falls back to `_Fields` (h1 and class/id/itemprop
words; sub-elements named similar/related/recommended/sidebar/share/alert inside the description are skipped). Inbox files now start
`Company / Title / URL / Source` then `Location`, `Salary`, `Posted`, `Agency: yes`; `manual.HEADER_KEYS` reads them and `live` passes
`source`, `posted_at` and the new `jobs.agency` column to `add_jd`. `DB.jobs()` now orders by `first_seen, rowid` (same-second inserts
were returned in arbitrary order). Tests: `tests/discovery/test_robots.py`, `test_intake.py` (sections C1-C3); fixtures
`tests/fixtures/intake/robots_cvlibrary.txt`, `cvlibrary_job*.html` (written by hand from the page structure CV-Library publishes, not
saved from a live page).

**E. Sponsorship is a weight, never a filter — done.** E1: the two sponsorship rules ("without sponsorship", "do not offer sponsorship")
are gone from `gates.jd_rules` (the register miss and agency were already notes only; the nationality rule is not about sponsorship and
stays); a JD that says no sponsorship stays `triaged`. E2: `discovery/priority.py` — `priority(job, settings)` = `Priority(p, f, s, r)`;
S = min of the applicable `priority.sponsor_weights` (exact 1.00, fuzzy 0.90, unknown / not on register / agency 0.75, JD says no
sponsorship 0.65; all > 0 so a job is only moved), R = exp(-days/`priority.recency_days`) from `posted_at` (1.0 unknown), F = `ceiling`
if tailored else `ceiling_pre` (0 when unknown). `order_files` ranks inbox files that are not in the DB yet (headers `Posted:` /
`Agency:`, register lookup, `gates.pre_ceiling`); `Live._by_priority` uses it and prints the order (falls back to file-name order if the
ranking raises); `cli.priority_lines` adds "Top jobs by priority" to `status`. The tailor/review queue keeps its own key
(`tailoring/queue.py`: floor, tier, ceiling, freshness) — E2 names the inbox, discover and status. E3: `priority.sponsor_wording(jd)`
finds the sentences about sponsorship (kinds no_sponsorship / right_to_work / sponsorship_offered) and `workflow.summary` prints
`Sponsorship wording in the JD (information only): ...` and the `Priority P=...` line; `gates.is_sponsor_flag` moves the register
notes from `FLAGS:` to the `Sponsor:` line and the judge gate ignores them. Tests: `tests/discovery/test_priority.py`,
`test_gates.py`. Wording patterns are code (`priority.WORDING`), tested on 14 sentences and 4 near-misses; an unusual phrasing is shown
in no line and costs no weight (it only fails to lower S).

**D. `careerops discover` writes ads to the inbox — done (offline tests only; no live run, no real mailbox).** Modules: `discovery/http.py`
(`Fetcher`: 1 req/s per host, `sources.UA`, robots.txt for pages and the public job-board APIs (`robots=False` only for the credentialed
Reed / Adzuna / Gmail calls), manual redirects, 401/403/429 -> `Blocked` and the host is not asked again, linkedin.com refused, one retry on a
connection error), `candidate.py` (the shape every source produces; `family_of`, `uk_ok`, `dedupe_key`), `ats.py` (M3: `slug_candidates`,
`probe`, `verify`/`verify_rule`, `resolve`, `detect` implemented to docs/tasks/M3; each gained a keyword-only `fetcher` and `settings`
argument, the positional signatures are unchanged; `board_postings`, `configured_boards`, `resolved_boards`, `all_boards`; `companies.ats_evidence`
column added), `reed.py`, `gmail.py` (loopback + PKCE OAuth with `requests`, `gmail.readonly` only, token in `paths.secrets`, alert parsers), `discover.py`
(`Discover`, `Known`, `BoardCache`, `BoardLookup`, `queue_manual`, `format_report`). Settings: `discover:` block, `paths.discover/robots/secrets`.
**D1** APIs checked against the vendors' docs on 2026-10-10: Greenhouse `GET boards-api.greenhouse.io/v1/boards/{token}[/jobs?content=true]` (no auth
for GET), Lever `GET api.lever.co/v0/postings/{site}?mode=json` (EU host `api.eu.lever.co`; no auth for GET), Ashby `GET api.ashbyhq.com/posting-api/job-board/{board}`;
**Workable not implemented**: its API (`/spi/v3/...`) needs an account token and the docs list no public posting endpoint. M3's `sector` filter needs M2
(Companies House join), which is still a stub, so without it `resolve` takes any A-rated register row that does not look like a public body; coverage of
the register is therefore slow until M2 exists (`discover --ats` no longer calls `resolve`: see D8; `careerops resolve` does). Greenhouse's list endpoint gives `updated_at`, not a first-published date, so
R for those jobs could look fresher than the posting is (5r: R now counts from the day we first saw the job, and an open listing keeps R >= 0.5). **D2** Reed: search (`postedByDirectEmployer` / `postedByRecruitmentAgency`, run separately because
the result has no agency field) then `/jobs/{id}`; `expirationDate` passed = closed. **D3** Adzuna is treated as snippets (docs unreachable from here; the
third-party descriptions say "truncated"): a description of >= 1500 characters that does not end in an ellipsis is taken as the ad, anything else is a link
resolved through the employer's board (never by following Adzuna's redirect) or queued. **D4** alerts: LinkedIn / Indeed through the employer's board by
title (>= 0.8 similar since 5r, 0.9 since 5x; was 0.88), CV-Library through `intake.fetch_ad` (C2; removed in 5x: never fetched), Reed through the API; `data/jds/manual_queue.csv` for the rest. **D5** `Known`:
DB + inbox/done/rejected headers + archive folder names (company + role, any place) + this run; closed = Reed `expirationDate`, Ashby `isListed: false`.
**D6** `runs.jsonl` + `discover --report`. **D7** README "Getting a Reed API key", "Getting Gmail access". **D8** `discover --ats` seeds (no random register sampling any more): `discovery/seeds.py` (`seeds`: top-level folder NAMES of `paths.applications`, then `discover.sources`, then register companies only when they match one of those, which adds `on_register` and the slug `resolve` verified, tried first) and `discovery/boards.py` (`BoardIndex`: `paths.discover/boards.json`, one entry per `provider:token`, hit or miss, `discover.ats.board_cache_days` = 30; `find`: up to four tokens per company from `ats.board_tokens`, the first token that is a board for any provider ends the search; `Finding` is the per-company report row). `ats.py` gained `board_tokens`, `PROVIDERS`, `board_request`, `is_board` (200 + `jobs` list; an empty Lever list is not told apart from an unknown site) and `parse_postings` (`board_postings` is now request + parse). A blocked host, a 5xx or a connection error is reported and NOT cached. `Discover.from_boards` reads the `sources:` boards, then one seed at a time; `ats_report()` prints boards found first; `listing()` is the top 10 and shows the URL. The `discover.ats.probe_per_run` setting is gone. Tests: `tests/discovery/test_{ats,discover}.py`. Tests: `tests/discovery/test_{http,ats,reed,gmail,discover}.py`.
Judgement calls: the documented API hosts are exempt from the *unreadable robots.txt* refusal only (Ashby's `/robots.txt` answers 401 while its API is documented as
public), a robots.txt they do serve is obeyed; `discover` with no source flag keeps the old pull-into-the-database job so `run`/cron do not change.

## 5o. Source registry round (2026-10-13) — **done** (offline tests only; no live discover run, no real mailbox)

**1. `config/sources.yaml` + `discovery/registry.py`.** One entry per source (`id`, `name`, `url`, `country` = ISO code or `multi`, `method` =
greenhouse | ashby | lever | reed | jobbank_csv | gmail_alerts | dedupe_only; optional `token`, `domains`, `subject_contains`, `parser`,
`dataset`, `enabled`, `note`). 24 seeded entries. `registry.load(settings)` (`paths.sources`) validates and names the bad entry; a missing
file is an empty registry. Checked on 2026-10-13 against the vendors: OpenAI = Ashby `openai` (HTTP 200, ~13 MB list); **Google DeepMind has no
Greenhouse token that answers** (`deepmind`, `googledeepmind`, `google-deepmind`, `deepmindtechnologies`, `deepmind-technologies` all 404 on
`boards-api.greenhouse.io`, though `job-boards.greenhouse.io/deepmind` pages exist), so it is `gmail_alerts`; BlackRock and JPMorgan have no
public API, so also `gmail_alerts`. **The sender domains of the alert sources are unverified guesses** (no real alert was read) and say so in
their `note`. Registry boards (OpenAI) are read by `discover --ats` and used by the alert-resolution lookup.
**2. Health (`discovery/health.py`).** `careerops sources check` and the end of every `discover` run (also the older no-flag one): per source, method
reachable / robots.txt / credentials / alerts in `discover.health.alert_days` (7) / postings, table (source, method, status, new jobs) and
`paths.discover/health/<date>.csv` (appended; header once). Every check is isolated; `--dry-run` prints without saving. New jobs per source come from
`Run.by_source` (a `Candidate` is credited to its alert, its token board or its method's entry: `Registry.attribute`).
**3. Country gate.** `discovery/countries.py` (canonical names, codes, `detect(place)`: country names, then Canadian/Australian regions, then major
cities, then US state codes; `London, Ontario` is Canada). A role-family match that is not UK is no longer dropped: `Discover.wanted` sets
`Candidate.country/listed_only`, `intake.render` writes `Country:` + `Status: listed_only` (UK ads are unchanged), `discover.listed_only.max_per_run`
(20) is its own limit beside `max_new_per_run`. Priority S = `priority.sponsor_weights.non_uk` (renamed `non_home` in 5p; 0.5, the lowest applicable weight). `live` refuses a
file with a non-UK `Country:` (or `Status: listed_only` without a country) unless `applicant.yaml` has a non-empty `visa.by_country.<country>` block (`default` does not count; this was `visa_by_country` until 5p):
`LISTED ONLY`, the file stays in `inbox/`, `_one` returns None (not counted towards `--max`); the check reads headers only, so removing the `Status:`
line does not let it through. A country with a block also passes the triage location rule (`gates.visa_countries`). `config/profile/applicant.yaml` was
not touched: add the blocks yourself. Not done: the database has no `country` column, so a non-UK job added by hand with `add-jd --location` is not caught.
**4. Gmail alerts.** `gmail.build_query(settings, registry)` names every source with `domains` (a shared sender carries `subject:(...)`);
`discover.gmail.senders: []` (shipped) means all of them, a list of the four older kinds keeps the old fixed query. `gmail.parse_alert(..., link_fn)`
and `gmail.generic_link` read sources without a dedicated parser (job links on the source's own domain, tracking parameters dropped). Resolution to the
employer's Greenhouse/Ashby/Lever board (else `manual_queue.csv`) was already in place and now serves every source; an alert with no recognisable job link
goes to the queue as a link to the email (`gmail.web_link`). A one-country source (SEEK AU) gives the posting that country when its own place does not say one.
**5. AIApply dedupe.** `paths.aiapply_applied` (`data/aiapply_applied.csv`): `discover.read_aiapply`, matched on company + normalised title in `Known`; a file
without the two columns is a note, never a crash; never written.
**Job Bank Canada (`jobbank_csv`, `discover --jobbank`).** Read from the September 2026 file: UTF-16, tab-separated, ~46 MB, columns include
`WIC Job Location Snapshot ID`, `Job Title`, `First Posting Date`, `Province/Territory`, `City`, salary fields; **no employer, link or ad text**. So rows are
only LISTED in `paths.discover/listed_only.csv` (never inbox ads). The file is found through the CKAN API, streamed (`Fetcher.request(stream=True)`), and
`Fetcher` now keeps a host's robots.txt `Crawl-delay` (open.canada.ca asks 20 s; capped at 30).
Tests: `tests/discovery/test_{registry,health,listed_only,jobbank,aiapply,gmail_sources}.py`, additions to `tests/apply/test_live.py`; the settings fixture
points `paths.sources` at a file that is not there (no registry) and `listed_only` is off in `test_discover.py`'s fixture, so the older tests keep their
UK-only meaning. **Unverified:** a live run of any of it (a real Gmail, the Job Bank download, the alert layouts of the generic sources).

## 5p. Per-country visa answers, generic remote (2026-10-13) — **done** (offline tests only; no real form opened)

**W(job)** (`discovery/workcountry.py`, `resolve` / `for_job`), generic around `visa.home`: on-site / hybrid → the office country (the place in `Location:`, else the
one office the ad names); remote with no restriction (no place in the location or in a scope sentence, or "anywhere / worldwide") → `visa.home`; remote restricted
to a place that includes home (the country, or a region that covers it: EMEA / Europe for GB, EU for DE) → home; restricted to one place that excludes home ("US only",
"EU only") → that place (a country code or a region key: EU, EEA, EMEA, Europe, North America, Americas, LATAM, APAC; `countries.REGIONS`); otherwise `None`. Evidence (a header
or one sentence) comes with every answer. Sentences with *not / cannot / outside / except* decide nothing; "join us" is not the US (capital `US` only); a London office is
not remote scope (names and regions only, no cities, in ad sentences). `Country:` in a JD file or `careerops set-country` (a country or region) is W as you gave it
(`work_evidence` starts "set by you", survives a re-triage). Without `visa.home` a remote role cannot be placed (`needs_country`); an office still can.
**The remote label.** For a remote role at home every user-facing string says only `visa.remote.label` (default "Remote"): `Work.line()` / `shown`, the review header
location (`workflow.shown_location`), `visa.review_lines`, `CHECK VISA ANSWER [W=Remote]`, `{country}` in a summary, the Excel Location column (`jobs.work_location`, set at
triage) and `set-country`'s output; the home country's names (name, other names, code in capitals) in the evidence sentence become `[home]`. `visa.remote.summary`
(optional) replaces the home block's summary for such a role; without it the home block's own summary is used verbatim (it is your text). The archive's `job.json`
still records the ad's location as it was. DB `jobs.work_country` (the code) stays internal.
**Answers** (`apply/visa.py`, `plan_for`): `visa.by_country[W]` (key = country name/code or region key), else `.default`; only real booleans (`right_to_work`,
`sponsorship_now`, `sponsorship_future`) fill Yes/No fields. W unknown → status **`needs_country`** (`gates.triage`; after `skipped` and `needs_jd`): not tailored
(`tailor` selects `triaged`), `apply` / `live` refuse it (`require_plan` raises `WorkCountryError`, an `ArchiveError`, from the preflight hook and from `assist` before a
browser opens). DB: `jobs.work_country`, `work_evidence`, `work_location` (additive). **Assist**: `fill_page(..., plan=)`; `visa_value(plan, kind)`; every visa field is
printed `CHECK VISA ANSWER [W=XX]: ...`; a question that names another country than W is left empty and says so. **Review screen**: `Work location: ... — evidence: "..."`
and `Visa answers (source): right to work X · sponsorship now Y · sponsorship in the future Z`. **Priority**: S = `priority.sponsor_weights.non_home` (0.5; `non_uk` is still
read as its old name) whenever W is not `visa.home` (`work_country` beats the listing's `country`; unknown W is not penalised). **Doctor**: fails without `visa.home` (a
country this tool knows) and without `visa.by_country[visa.home]`; warns (`ok == "warn"`: printed `[warn]`, exit code unchanged) without `default`. New command
`careerops set-country --id JOB --country XX`. This replaces the first version of this round, which was UK-specific (`remote_from_uk_counts_as_uk`, `non_uk`, a GB
block that had no default fallback); that key is no longer read.
Judgement calls: (1) a country counts as "your block" for `live`'s listed-only refusal and for the triage location rule only when it has its own key, never through
`default`, so a foreign job is still skipped by the location rule (and `live` asks "Tailor it anyway?") unless you add the country; `default` serves jobs that pass the
rule by other routes (a remote ad restricted to the US from a UK-listed company, a job you chose to tailor). (2) A file given to `live` without a `Location:` header still
gets the location "United Kingdom" (older behaviour), so W is the office country for it. (3) A bare "Remote" with nothing else is unrestricted, so it is home.
Tests: `tests/discovery/test_workcountry.py`, `tests/apply/test_visa_plan.py`, `tests/apply/test_assist_visa.py` (Chromium, slow), additions to `test_live.py`,
`test_applicant_doctor.py`, `test_listed_only.py`. **Unverified:** a real application form; the rules on real ads (they are tested on hand-made sentences).

## 5r. Discovery quality round (2026-10-15) — **done** (offline tests only; no live discover run, no real mailbox)

**1. Resolver.** `BoardLookup.boards_for` no longer probes with `ats.slug_candidates` + `verify_rule`: a company an alert (or Adzuna) names that is in no list is
looked up as `discover --ats` looks up a seed (`boards.find`, `Seed(name, "alert")`): up to four `ats.board_tokens`, each asked of Greenhouse, Lever (global, EU) and Ashby,
every answer cached in `boards.json` for `discover.ats.board_cache_days` (30). `company_boards.json` is gone (`BoardCache.remember` too); `Discover` and `BoardLookup`
share one `BoardIndex`. `discover.alert_probes_per_run` (default now 100) caps the companies looked for per run; one left alone says so in its queue reason. `find` takes the
open posting whose normalised title is >= `discover.title_match` (0.8; was 0.88) similar **and has the same level words** (`candidate.level_words`), preferring the same
place. Because a guessed token can be another company's, a board with origin `alert` must also pass `plausible`: the posting's town is not another town than the alert's
(`candidate.norm_place`), and a Greenhouse board's own name (`GET /boards/{token}`, once per board) is the company's or starts with it. `resolve_link(source_id=, seen=)`
credits `resolved` / `unresolved` to the registry source in `Run.by_source`; `resolution_lines` prints `linkedin  12 of 20 (60%)` after a run and in `discover --report` (last run and all runs).
**2. Recency.** R counts from `seen_on`, the day discover first saw the job, else from `posted_at` (hand-added JDs). `firstseen.FirstSeen` (`paths.discover/first_seen.json`, key =
`candidate.dedupe_key`) keeps the earliest of the alert's date (`gmail.message_date`: `internalDate`, else the `Date` header) and the first run that read the job; a dry run records
nothing. `Discover.accept` sets `Candidate.first_seen` and `open_listing` (read off an employer board) once a job passes the role / place / closed / duplicate checks; the inbox
file gets `First seen:` and `Listed: yes` headers (`intake.render`), `live` keeps them in `jobs.seen_on` / `jobs.open_listing` (additive columns; `manual.add_jd(seen_on=, open_listing=)`),
`priority.file_job` reads them from the file. `priority.open_listing_floor` (0.5) raises R of an open listing, never lowers it; `Priority.floored` says so on the review line.
Test: a 64-day-old posting still open on a board gets R >= 0.5 (it is 1.0 on its first day), and one seen 64 days ago and still listed sits at exactly 0.5.
**3. Manual queue.** `Discover.to_queue` holds the run's unresolved links; `flush_queue` (at the end of every source step, `_finishing`) drops those already in either file, ranks the rest
by `candidate.title_relevance` x S (`priority.sponsor_factor`: register match by company name) with arrival order breaking ties, writes the best `discover.manual_queue_max` (10)
to `manual_queue.csv` and the rest to `manual_queue_overflow.csv` (`score` column). An alert email nothing could be read from counts as relevance 0.5. The cap is per run, not on the
file's total. Counters `to_manual_queue` / `to_manual_queue_overflow`. `queue_manual()` (one row, no cap) is kept for direct use.
**4. Filter audit.** `Discover.filtered_out` collects every title `wanted()` turns away (role family, or place with `listed_only` off) and the alert titles outside the families;
`flush_filtered` appends them to `paths.discover/filtered.csv`, skipping a (company, title, filter) already there so a board read daily adds only new titles. A dry run writes nothing.
**5. AIApply dedupe** is unchanged (CSV, company + title).
Tests: `tests/discovery/test_quality_round.py` (43), one in `tests/apply/test_live.py`; three older tests updated (`boards.json` instead of `company_boards.json`, the new headers, the
`resolved` / `unresolved` counts). **Unverified:** a live run: token guesses on real boards, the Greenhouse name check, real alert dates.

## 5s. Hooks, notes, negation-aware ATS, 24-hour freshness (2026-10-15) — **done** (offline tests only; never run on the real data or the real model)

Spec: `docs/claude_code_round_hooks_freshness.md` (decisions in `docs/Career ops plan v4.md`). It supersedes the ordering rules of 5k and 5r
(P = F x S x R, R from first-seen): there is no R any more.

**Step 1 — golden fixture.** `tests/golden/anaplan/` (copy of `docs/golden/anaplan/`: `cv.tex`, `cv.txt`, `letter_A.tex`, `letter_B.tex`, `jd.txt`,
`reference_ats_weights.py`) plus `keywords.yaml` (the reference's 38 weighted regexes spelt out as terms and alternatives, with `claimable`).
`tests/test_golden_anaplan.py`: strict and claimable ATS 72.5 ±0.1 on `cv.txt`; the keyword file agrees with the reference scorer hit for hit; CV + letter
B is 75.0 once the admitted gap is ignored (the reference reads 82.5); the hook ("prototype to production", "~65") is in the headline, profile line 1 and
the first AstraZeneca bullet; the golden CV is two pages; both golden letters are at most 250 words (greeting and sign-off excluded, as the pipeline
counts); and the pipeline's own output for the Anaplan ad (fixture fact bank, MockLLM) has the hook in all three places, two pages, letters A and B of
at most 250 words, the three ATS numbers and `note.md`. `careerops/golden.py` (`python -m careerops.golden <CV pdf|txt>`) lists the lines only in one CV
or only in the other.

**Step 2 — `careerops/hook.py`.** *(The model-written strings described here were replaced by the user's `hook_library` on 2026-10-17: see 5u.)* `Hook(problem, proof_bullet_id, number, headline, profile_line, letter_sentence, ...)`. `choose_proof`: highest
sum(weight x term hit) among bullets with a number or a `result:` (tie: more recent; a bullet with a deny-listed term is never used; no number anywhere:
the best bullet and the flag `hook has no number`); `find_number` returns a string verbatim from the bullet (a `result:` first, then a count-like number,
never a model version like "Haiku 4.5"). Strings: the `hook` task (prompts `HOOK_SYSTEM`/`HOOK_USER`) writes `problem`, `tail`, `profile_line`,
`letter_sentence`; `problems()` checks each (number verbatim, no new number, deny list, `faithfulness.check` with the problem clause allowed to name the ad's
own keywords, grounding, unsupported qualifiers, the vague adjectives, the em dash, length: headline <= 95 characters including the role title, profile
line <= 35 words, letter sentence <= 45, problem <= 14 words with >= 2 words the ad uses, no 0.7 paste overlap with ANY fact-bank bullet); one retry
naming the problems; then `template_strings` over the same checked pieces; None (no hook, nothing faked) if even that fails. Placement: `ensure_selected`
puts the proof bullet in the selection, `proof_first` moves it to the top of its role (or project), `_shrink` and `fill_page2` keep it first, a rewording that
drops the number is refused (`keeps_number`), `templates/cv2.tex.j2` / `cv.tex.j2` get a 12pt navy headline slot under the name (the number in bold),
the profile is `<profile line> <the usual profile>`. `hook.enabled` (settings, default true); `llm.max_tokens.hook`; MockLLM answers `hook` with `{}`
(the template). A proof bullet from a project goes first in its project, which goes first among the projects.

**Step 3 — `careerops/ats.py`.** `score(text, kws)` = `scorer.score` with negation: a mention is negated when one of `not, no, never, haven't, yet to,
without, lack(s/ing/ed), gap(s), learning` (as a verb: not after machine/deep/transfer/..., not before rate/models/...) stands within 6 tokens before it in the
same sentence (sentences end at `. ! ? ;`, a bullet mark or a blank line); a text with any negated mention of a keyword does not claim it anywhere
(`"I have not yet used LightGBM ... XGBoost shares LightGBM's ideas"` is no hit). `scores()` -> `AtsScores`: strict CV, claimable ceiling (the keywords the bank
backs, deny list excluded), CV + letter (a keyword counts when the CV or the letter has it, each negation-aware), letter alone, `gate()`:
`ok | above | retry | stretch`. Tailor: `ats.score` replaces `scorer.score` on every CV and letter text; the loop's retry is now ONE retry
(`scoring.max_attempts: 2`); the ceiling is the claimable ceiling; `report["ats"]`, `report["stretch"]`, `report["claimable"]`; a stretch job gets the flag
`Stretch: ...` (so the judge gate never auto-approves it) and is never dropped; `Score.negated`. Review: `ats_lines`, `refresh_ats` after edits.

**Step 4 — freshness.** `discovery/freshness.py`: `posted_at` + `posted_at_source` per job (db column `posted_at_source`): Greenhouse `first_published`
(never `updated_at`), Lever `createdAt`, Ashby `publishedAt`, Reed `date`, Adzuna `created`, Gmail alert `gmail.received` (the email's arrival, `gmail.message_time`),
anything else `first_seen`. Only those sources are trusted (`TRUSTED`); an untrusted `Posted:` line counts from `First seen:`. Tier 0 <= 24 h, 1 <= 72 h,
2 <= 7 d, 3 older or undated. `priority.py`: no R, no `recency_days`, no `open_listing_floor`; `Priority(p, f, s, s_reason, hours, tier, posted_via)`;
`sort_key`/`order_files` = (tier, -P, -S, ...). Inbox headers `Posted:` (ISO date or datetime) and `Posted via:`; `Candidate.posted_source`, `Ad.posted_source`,
`intake` records `first_published` / `createdAt` / `publishedAt` for `add-url`/`live --url`. `live --fresh` (tier 0 only; older files stay in the inbox), the review
header `Posted: posted 5 h ago (Greenhouse first_published), tier 0 (<= 24 h)`, `discover --report` prints the tiers of the last run and of the inbox (`Run.tiers`,
`inbox_tiers`), the manual queue is ranked tier first, `status` and the discover listing show `T<tier> <age>`.

**Step 5 — letters.** `letter.mode: hook` (new default): `tailoring/letters.py`, see README "Letters". `make_letters` -> `{"A", "B", "choice"}` stored as
`report["letters"]` / `report["letter_choice"]` (`report["letter"]` is the chosen one); `letter.greeting_hook`, `greeting_agency`, `closing_hook`;
`applicant.yaml -> relocation`; 2 attempts for the body (then "none", no template letter in this mode) and 2 for B's gap sentence (then a template over
the same two terms); `paste_problems` against ALL bullets; PDFs in `<job>/letters/A|B/<name> Cover Letter.pdf` (the same name whichever goes). Review: `l`
cycles A -> B -> none (`review.cycle_letter`, no model call), `g` regenerates, status `declined` (you chose none) needs no confirmation and the archive goes without.
`tailor.regenerate_letter` and `letter_pending` handle both variants. `letter_token_estimate` 7000 (not measured on the real model).

**Step 6 — `careerops/note.py`.** `note.md` per job (sections Hook, ATS, Learn list, Contact, Question, Flags, Gap), written at the end of tailoring and rewritten by
review edits and letter regeneration; `[n]` opens it. The contact is a TODO for you (nothing fetches linkedin.com).

**Step 7 — doctor reminders** (`setup.reminder_checks`): from 2026-11-24 the MSc entry whose `note` still says "Expected"; the Gmail sign-in more than 6 days old
(`gmail.token_age_days`, from `granted_at`, written at sign-in; an older token is aged by its file). Warnings only.

**Step 8 — scheduler.** `scripts/discover.sh` (logs to `data/logs/discover_<date>.log`, exit code passed on), `docs/scheduler.md` (Windows Task Scheduler 07:45 / 17:45;
the task is not created by this repo). `discover ... --report` now runs the sources first and then prints the report; the command exits 1 when a source failed
(`Run.failures`: a lapsed Gmail sign-in, an exception).

**Step 9 — discovery quality.** The token resolver and the capped queue were done in 5r; this round makes the queue tier-first and writes the filter audit to
`data/discover/filtered_<date>.csv` (one file per day: the titles first turned away that day, with the reason).

Not verified: a live `careerops live --dry-run --url https://job-boards.greenhouse.io/anaplan/jobs/8654662002` (real fact bank, real Groq): the hook model's output quality, the strict
ATS of the real CV against the golden 72.5, and the diff against `tests/golden/anaplan/cv.txt` are for you to read on that run (`python -m careerops.golden`).

## 5t. Fix round 2 (2026-10-16) — the Anaplan dry run missed the golden bar — **done** (offline tests only; never run on the real data or the real model)

The dry run of `careerops live --dry-run --max 1 --url https://job-boards.greenhouse.io/anaplan/jobs/8654662002` had no headline, a profile that opened
"Data Scientist skilled in building deployable ML models", a letter that pasted a `terms:` list, rewrites that changed facts and 15 hyphens. The golden test
had passed because `tests/test_golden_anaplan.py` checks the golden files themselves. Each cause below was reproduced first (the real fact bank READ, never
written, with the golden keywords and `MockLLM`), then a failing test, then the fix.

1. **The golden test now tests the pipeline** (`tests/test_golden_pipeline.py`, fixture `tests/fixtures/golden/fact_bank_anaplan.yaml`: the real bank's shape, no real
   person). It runs `tailor_job` on `tests/golden/anaplan/jd.txt` with a deterministic MockLLM and asserts on the generated files: a 12pt navy line under the name with
   the number; sentence 1 of the profile with the number and every ad noun of the problem; the first bullet of the most relevant role is the proof bullet; sentence 1 of
   letters A and B has the number; at most 250 words; no pasted list; B's gap is from the learn list and bridged to a claimable skill; no hyphen outside the exceptions;
   the forecasting project is on the CV. It also runs two failure shapes (no number in the best role; no buildable hook). The old file keeps the golden-file checks.
2. **The hook was lost silently.** Root cause: `hook.choose_proof` took "10" from "new 10-class head" (`_countish` read "class" as a plural) and "3" from "3rd
   place"; the template then named the project with its em dash, the problem clause "deep learning" tripped the qualifier check ("deep"), and `build_hook` returned None
   without a word. With no hook the CV has no headline slot, the profile no hook line, the proof bullet is not moved, and `letter.mode: hook` fell back to the old single
   draft letter (the "old closing line"). Now: ordinals, hyphenated modifiers ("10-class", "5-star") and a first-word verb ("Ran 20 jobs") are handled in `find_number`;
   `proof_candidates` ranks every usable bullet in four tiers (numbered bullet of the MOST RELEVANT ROLE, numbered bullet of another job, numbered project bullet, unnumbered) and `build_hook` tries
   the best `MAX_PROOFS` (4) before giving up (the model is asked for the first only); a project is named without its subtitle or dash; a qualifier that is part of a JD
   keyword the string names ("deep learning") is not praise; the template quotes at most `PHRASE_WORDS` (4) words and never ends on "behind/with/and"; the hook's strings are
   typeset like the CV (hyphens closed) and written with the technologies' proper case (MLflow, not mlflow; `term_dump_problems` is part of `hook.problems`) so the report, the review screen and the PDF say the same. A hook that cannot be built is no longer silent: `report["hook_notes"]`,
   an `analysis_warnings` line starting "hook: none could be built", and in letter mode `hook` both letters are "none" (`letters.none_letters`) instead of the old draft.
   "skilled" is a vague adjective for hook text and a qualifier (`QUALIFIER_LIST`) for the profile and letters; `make_profile` also refuses the vague words.
3. **The letter dumped `terms:`.** Root cause: `evidence_fields.fields()` sent the bullet's raw `terms` as its `method` field when the bank has no `method:` key, and the model
   copied it ("mirrors my work on docker, containerised, containerized, aws lambda, serverless, mlops, cloud, iam, security, production"). No code interpolated `terms` into
   letter text and `letter_rules.fallback_letter` uses bullet text, so there was no such template to delete; the raw-terms method field is gone (method = the terms the
   bullet's own text names, in its spelling, joined by "; ", else "(not stated)"). New guard `letter_rules.term_dump_problems`, applied to the body, the gap sentence and the
   old draft path: three or more comma-separated items in a row that are all some bullet's `terms`, an item repeated inside a list, and a known technology in lower case
   (docker, aws, fastapi, python, tensorflow, keras and the `CANONICAL` / extra names) are refused. The prompts say so. In hook mode a letter that fails is "none"; B's gap sentence
   also has no template any more (`template_gap` is deleted): two failed tries make B "none". `pick_bridge` takes a family neighbour only if the bank has places for it and does not
   deny it (`config/evidence_families.txt` gained `gradient boosting | xgboost | lightgbm ...` and `dbt | sql | ...`); the gap sentence may name its bridge (`_lack_problems(..., bridge)`).
   The gap itself was always the heaviest required, nameable tool of the learn list in the ad's order (here dbt, which is also deny-listed: a confirmed absence is a fine gap).
4. **Rephrasing changed facts.** Root cause: `guard_rephrases` only rejected what a rewording ADDS (new numbers, deny terms, new keywords) and a few qualifiers. New
   `rewrite_rules.must_keep_problems(new, text, result, award)` rejects a rewording that drops: any number (ordinals and the project's award excepted), the approximation mark of
   an approximate number, any word of the bullet's `result:` (stem compared: "imbalance" ~ "imbalanced", "cut" ~ "cutting"), or deterministic / overfitting / held-out /
   validated (any form) / under (or below, less than) / per. The original text is then used. Two earlier tests (`test_placement.py`) used a rewriting that dropped "20" of
   `gpt-oss:20b`; their constant keeps it now, the tests' purpose (the no-regression rule) is unchanged.
5. **Hyphens.** Root cause: `typography.clean` closed only the forms listed in `config/typography.yaml`. Now every word-word becomes word word at render time (after the claim
   checks) except `protected` (scikit-learn, k-means, gpt-oss, faster-whisper, CIFAR-10, TF-IDF, Skip-gram, k-NN, the owner's handles), the handles in the CV's own profile links, number
   ranges, letters-digits names (GPT-4o), a one-letter prefix (t-SNE) and proper-noun pairs (Rolls-Royce), and the JD's own hyphenated keywords. Special spellings in
   `rewrite`: e-commerce -> ecommerce, 5-star -> five star, sign-on -> sign on. `typography.audit` uses the same exemptions.
6. **Project selection.** Root causes: (a) `scorer.keyword_hits` — a multiword keyword had to appear as ONE phrase, so "time-series forecasting" never matched a bullet whose
   `terms` say "forecasting" and "time series" separately; matching is now normalised on both sides (lower case, `-` `_` `/` as spaces, stemmed) and a multiword keyword
   also matches when all its content words are there (used by selection, `budget.bullet_scores` and the hook's scores; the ATS `score` itself is unchanged). (b) a keyword the
   skills line already covers added no gain, so a project that proves it tied with filler; a project now earns `SKILL_ONLY_CREDIT` (0.5) of the weight of such a keyword.

Not verified: the real model's hook and letters, and a real `careerops live --dry-run` (that command opens the real `data/careerops.db`, spends Groq quota and fetches the
page; CLAUDE.md forbids running it as a check). `scripts`-free offline stand-in: the real fact bank READ with `MockLLM` and the golden ad, outputs in a scratch folder (see the
fix-round summary). To get the golden hook (deployment + ~65 tests) from the real bank, az4 or az1 needs a term/number the scorer can see (for example `prototype to production`
in az4's `terms`): the real az1/az3 carry no number and the most relevant role by summed term score is `freelance`, which has none; a bank edit is yours to make.

## 5u. Deterministic text round (2026-10-17) — hook, profile, rephrase, letter and skills without a model — **done** (offline tests only; never run on the real data or the real model)

The Anaplan dry run of 17 Oct 13:56 showed that the deterministic parts work and every failure was in model-written free text: a hook that tied
"~65 tests" to forecasting, two sentences fused into a profile that said "Data Scientist" twice, a bullet that gained "and MLOps practices" from its
`terms:`, a letter that said Docker and AWS Lambda ran "the test suite", and a CV that listed "Kubernetes (AWS EKS)" and "Kubernetes". Each item had a
failing test first (`tests/test_hook_library.py`, `tests/tailoring/test_profile_assembled.py`, `test_rephrase_no_new_claims.py`,
`test_letter_assembled.py`, `test_skill_groups_round.py`); the fix replaces the free text with the user's own text.

1. **Hooks come from `hook_library`** (`hook.py`). `library_scores` = sum of keyword weight x [the keyword is one of the entry's `terms`];
   `pick_library_entry` takes the highest (a tie goes to the earlier entry, an entry that scores 0 is never picked, an entry whose proof bullet is not
   in the bank, with a missing string or with a deny-listed term is skipped with a note); `library_hook` uses `headline`, `profile_line`, `letter_line` word for word
   (no typesetting) and the entry's `proof` bullet first in its role (the existing `ensure_selected` / `proof_first`, with the entry's `situation:` opener).
   `Hook.hook_id` and `source: library`. The problem phrase is the entry's own `terms` that the ad asked for, a lower-case noun phrase
   (`letter_rules.noun_phrase`). **Changed 21 Oct:** an entry is eligible only when its `terms` hit at least 2 of the ad's top 10 weighted keywords (`top_keyword_overlap`, `MIN_OVERLAP`);
   `Hook.overlap` / `Hook.reason` say why it won (shown on the review screen); `headline_lint` warns (review screen, `careerops doctor`; never rewrites) about a
   headline over 60 characters, with more than one number or with GB / MB / RAM / GPU / ms / CPU. No eligible entry (or no library): NO hook, the family headline
   with no number (`report["hook_family_headline"]`), no hook letter; the code template is no longer called by `build_hook` (`template_strings` and `problems` remain as helpers). **Deleted:** the `hook` model call (`_model_strings`,
   `ATTEMPTS`, `prompts.HOOK_*`, MockLLM's `hook`, `llm.max_tokens.hook`).
2. **Profile is assembled** (`tailoring/profile.py`): `profile_line` + one or two `profile_facts` chosen by `fact_scores` (`pick_facts`, bank order on a
   tie), each ending in a full stop; a fact that names a deny-listed term, repeats a role title (`ROLE_PHRASES`: "Data Scientist") or restates the
   sentences before it (0.6 of its content words) is left out. `problems()` flags a title said twice or a sentence without punctuation. **Deleted:**
   `make_profile`, the `tailor_profile` call and prompts, `shapes.normalise_profile`, MockLLM's `tailor_profile`, `llm.max_tokens.tailor_profile`; `careerops
   doctor` / `llm-check` no longer ping that task.
3. **Rephrasing may not add claims** (`rewrite_rules.added_word_problems`, called by `guard_rephrases`): every content word of a reworded bullet must be in the
   original text or `result:` (any inflection: a shared 6-letter start or a doubled last letter counts as one word) or be an alternative spelling of a
   keyword the original already hits. `placement_offers` offers nothing any more (`introduce=False`: the `MAY INTRODUCE` keywords were how az3 was asked for
   "MLOps"; they are reported as unoffered); `introduce=True` keeps the old machinery for its unit tests. A rephrase call is now made only for a bullet with a
   `result:` its text does not say (and by the wording and shorten passes, which only change forms and lengths).
4. **Letter off by default, assembled when on.** `settings letter.mode: "off"`. `hook` mode (`letters.py`): `letter_line` + two sentences built from two selected
   bullets (`assemble_body`: `<lead-in>: <where>, I <the longest cut of the bullet's text that keeps the paste overlap < 0.65>.`) + the call to action
   (`letter.closing_hook` with the hook's problem phrase). Variant B (the model's gap sentence) is gone: always `none`. **The guard** `letters.claim_problems`
   (also in the `draft` letter's checks): "our" / "we", "I help <Company>", and `link_problems` (an object phrase before "on / in / with / using ... <tool>" or after
   "<tool> ran / hosted / served ..." whose words one bullet says and whose tool only a *different* bullet says). **Deleted:** `draft_body`, `draft_gap`,
   `body_problems`, `gap_problems`, `prompts.LETTER_HOOK_*`, `GAP_*`, MockLLM's hook-mode letter answers. `draft` mode is unchanged apart from the guard.
5. **Skills** (`skills.cv_skill_groups(fb, kws, cfg, jd)`): a name and its qualified form are one skill (the qualified one stays); `cv.skill_umbrellas` (AWS, GCP) go when
   a specific skill of them is listed and hand over their pin; inside a group order = JD keyword weight, then the bank's order (a pin is no edge);
   `cv.skills_when_asked` (TypeScript, JavaScript) are placed only when the ad names them; a group with no skill row of its own but `from_terms` (the client group) shows
   the matching `terms:` the user wrote, capitalised, so all four groups render. The two failing tests of the 16 Oct suite
   (`test_a_skill_listed_twice_renders_once_on_the_cv`, `test_the_model_is_never_asked_to_select_and_...`) compared the pipeline with the old one-page skills rows and with the
   bank before award projects moved to Achievements; they now mirror the pipeline's inputs.
6. **Review / note** print the hook id, the headline, profile sentence 1 and letter sentence 1, the CV PDF's creation time and `letter: off`.

**Tests.** 2,800 collected (2,778 before the round): 69 new in six new files (`test_hook_library.py`, `test_profile_assembled.py`, `test_rephrase_no_new_claims.py`, `test_letter_assembled.py`, `test_skill_groups_round.py`, `tests/review/test_review_hook_lines.py`); 2,798 passed and 2 failed when every file ran on a fresh copy of the tree, and the 2 failures there (`test_a_test_that_swallows_the_error_still_fails_in_teardown` in `test_no_real_data.py` and `test_no_real_profile.py`) fail the same way on the tree from before the round and pass in the repo itself (19 passed together with `test_deletions.py`). Rewritten because the behaviour they pinned is gone: `test_hook.py` (model path), `test_profile_source.py`,
`test_hook_letters.py` (run tests), `test_term_dump.py` (letter body / gap), `test_golden_pipeline.py` (library hook), `test_llm_shapes.py`, `test_tailor.py`, `test_rephrase.py`,
`test_placement.py`, `test_budget.py` (they now give the bank a `result:` via `conftest.with_results`), the doctor tests (one task fewer).

**Not verified.** The real model's keyword list for the Anaplan ad (the stand-in run uses MockLLM's taxonomy keywords, so the hook may differ from the live one); the real
`careerops live --dry-run --retailor` (CLAUDE.md forbids running it against the real `data/`; the command to run is in the summary).

## 5v. Tracker, contacts and outreach drafts (2026-10-17) — **done** (offline tests only; never run against the real Sheet, Gmail or data)

Spec: the 17 Oct round "Google Sheet tracker, contact discovery, outreach drafts". Hard rule of the round: **no code sends e-mail**.

- **Auth** (`discovery/gmail.py`): `--gmail-auth` asks for `gmail.readonly`, `gmail.compose` and `spreadsheets` (`gmail.SCOPES`); a token with any other scope, or a sign-in with a box unticked, is refused and not kept. `access_token(settings, need=...)` demands only the scopes a caller uses, so an old read-only token still reads alerts and the tracker / drafts say to sign in again. `gmail.compose` can send as well as draft, so the guard is code: `googleapi.GoogleAPI` (the one paced client, 1 request/s per host, bearer token only in the header, never in an error) refuses a non-Google or non-https URL and any path ending in `/send` or containing `:send`; `tests/test_never_sends.py` greps `careerops/` and `scripts/` for `smtplib`, `SMTP_SSL`, `messages().send`, `drafts().send`, the REST send endpoints, `.sendmail(` and `.send_message(`. Its docstring and `outreach.py`'s were reworded so the grep has no false hit.
- **Tracker** (`tracker.py`): `HEADERS` (the 36 of A1:AJ1), `record()` (job + report.json + fact bank -> {header: value}), `upsert()` (header row read first; missing headers appended at the end after widening the grid; key column found by `Job ID`; a new row is appended, an existing row gets only the changed cells, one range per cell, RAW; Status `Applied` only when empty; Interview date and Notes never written), `sync()`. `SheetsClient` is the only thing that speaks Sheets (GET values, `values:batchUpdate`, `values:append`, `spreadsheets.batchUpdate appendDimension`); tests use `FakeSheet`. `tracker.sheet_id` and `tracker.tab` are in `settings.yaml` (the sheet id was a flat `tracker.sheet_id:` line at the end of the file; it is now the nested block). `cli._archive` is `_archive_files` + `_after_applied`: after any application (archived or not) the drafts (`outreach.mode: drafts`) then the tracker row, each failure printed to stderr and never undoing the application; the mock root never runs them (`mockroot.is_mock`). `careerops tracker sync [--id] [--dry-run]`.
- **Contacts** (`contacts.py`, `contacts` table): sources `jd`, `company_site` (SITE_PATHS fetched once each through `Fetcher`, so robots.txt and 1 req/s), `user`, `hunter`; at most `MAX_CONTACTS` (2) stored by discovery, `contact add` may add more, the best two (user, hunter, jd, company_site) are shown and drafted to. `mx_status` ports `VaidateMailAddress.py`: MX, else A (`ok` / `a_record` / `no_mx` incl. a null MX / `invalid` / `unknown` for a DNS timeout, retried after a pause); the SMTP `RCPT TO` probe is dropped. Hunter Email Finder: one call, key in `X-API-KEY`, status kept, only `valid` is verified. `data/outreach/do_not_contact.csv` (`paths.outreach`; `careerops init` creates it with its header). `review_step` is called after `[a]pprove` (both the keypress and the judge-gate auto-approve) when `outreach.mode: drafts` and not on the mock root. `careerops contact add|list`.
- **Drafts** (`outreach.py`, `outreach_drafts` table): `drafts.create` only; limits 1 per contact, 2 per job; subject, body and attachments as in the README; `body_problems` = at most 120 words, `letters.claim_problems` (we / our / linked bullets), a number the fact bank lacks, another company's name (from the jobs table) unless the fact bank names it. A job with no hook letter line gets no draft. `Result.status` -> the Sheet's `Outreach status`; `follow_up_due` = applied + `outreach.follow_up_days` (7). `careerops outreach draft JOB [--to ADDR]` (the old `outreach --id --person --title` form and `review/outreach.py` are unchanged).
- **New dependency, asked for by the task:** `dnspython>=2.4` (pyproject), used only by `contacts.py`. **New settings:** `tracker.sheet_id`, `tracker.tab`, `outreach.mode` (off), `outreach.follow_up_days` (7), `paths.outreach`; `.env.example` gained `HUNTER_API_KEY`. **DB:** additive tables `contacts` and `outreach_drafts`.
- **Tests** (`tests/test_tracker.py`, `test_contacts.py`, `test_outreach.py`, `test_never_sends.py`, the changed scope tests in `tests/discovery/test_gmail.py`, fakes in `tests/outreach_fakes.py`): no network, no DNS, no xelatex.
- **Not verified:** a real sign-in with the three scopes, a write to the real Sheet, a real Gmail draft, Hunter, real DNS, and the company-site pages of a real employer. The commands for those are in the README ("The tracker, contacts and outreach drafts"); they were not run because they need your browser sign-in and write to your account and real data.

## 5w. The 18 Oct round — links, verbatim, replaces, family skills, recent roles — **done** (offline tests and a MockLLM stand-in on the real bank; never run on the real data or the real model)

The user was happy with the CV layout of the 18 Oct 13:31 dry run, so the layout is untouched; five changes, each with a failing test first
(`tests/tailoring/test_oct18_round.py`, 31 tests; against the 17 Oct code 27 of them fail on their assertions).

1. **Project links are `\url`** (`templates/cv2.tex.j2`, `cv.tex.j2`). A project with a `github:` value prints `\url{<github>}` as its right-hand tag, the full URL in the
   default `\url` font, instead of `\href{..}{GitHub}`; a project without one keeps its award or badge. TeX measures the bold name + URL and the stack + URL with boxes: if the
   name and URL fit one line it is `\project{name}{stack}{\url{..}}` (the existing macro); else the URL goes on the stack line, right-aligned; else on a line of its own under the
   stack. The URL is written through `urlesc` (`#` and `%` as `\#` and `\%`, which `\url` prints as they are) because it sits inside a macro argument. `layout.link_problems`
   re-checks the rendered PDF with `pdftotext -bbox`: every URL is on the page, inside the right margin, and touches no other word (`report.json` -> `lengths.links`).
   The `\gh` macro stays in the preamble (the preamble is the user's reference file line for line) but nothing calls it. `\url` brings in `LMMono10`, so the two font tests
   (`test_cv_template`, `test_cv_typography`) now allow that font next to Carlito.
2. **`verbatim: true` bullets are never rephrased** (`tailor.propose_rephrases`, `wording_targets`, `shorten_pass`, `layout.with_splits/with_openers`): not sent to
   `rephrase_bullets`, not a wording target, not shortened, split or given an opener. One that prints over three lines is a `length:` note, not trimmed.
3. **`replaces:` and one opener per role** (`selection.settle_selection`, `replacers`, `fill_order`, `tailor.reconcile_proof`): a chosen bullet drops the bullets it replaces;
   the replacer is preferred when it scores >= `REPLACE_RATIO` (0.8) x the best it replaces, else it is left out; the fill never adds a bullet next to its replacer/replacee;
   a role has at most one opener: the `situation:` text, or one `situation_included: true` bullet, never both. The hook's proof bullet is handed to a replacer that keeps the hook's
   number (and passes the 0.8 test), otherwise the proof stays and the replacer goes.
4. **Skills from `families[].skill_lines`** (`skills.family_scores/pick_family/family_skill_lines/family_basics_missing`): the family whose `match` terms carry the most keyword
   weight (ties to the first; none above zero: the job's own family id if the bank has it, else the old groups) gives its `skill_lines` label for label and item for item,
   one text line each (`render.skill_row` prints a string as it is). `cv.skills_always` is still checked and a missing standing skill is a `skills:` warning, never added.
5. **Two bullets per recent role** (`selection.role_end/is_recent`, `Layout.recent_min`, `tailor._shrink`): a role whose end date is within `scoring.cv_recent_years` (2) keeps
   at least `scoring.cv_recent_min_bullets` (2) bullets if the bank has them, even against the near-duplicate filter; the shrink loop trims projects (to one bullet, then whole
   projects down to `MIN_PROJECTS_KEPT`) before such a role loses its second bullet.

Also: `list.remove` calls in the new code were rewritten as slice assignments, because `tests/apply/test_safe_io.py` (the AST guard for destructive calls) flags any `.remove(`.

**Stand-in run** (`anaplan_standin.py`: the real fact bank READ, MockLLM with the golden keywords, scratch folder): 2 pages, page-2 fill 0.915, one `\url` link, six verbatim
bullets identical to the bank, no replacing pair anywhere, AstraZeneca with exactly one "Needed" opener, the four `data_scientist` skill lines as written and every standing basic
present, 180 Degrees Consulting 3 bullets, Freelance 2. az4 stays over its replacer az11 there because az11 scores 0.5 x az4 on the golden keywords (needs 0.8); a live
keyword list may differ.

**Not verified.** The real model's keyword list and text, and the real `careerops live --dry-run --retailor` (CLAUDE.md forbids running it against the real `data/`).

## 5x. Round 8 — go-live blockers (2026-10-20) — **done** (offline tests only; no live discover run, no real mailbox, no real form)

114 new tests (`test_level` 28, `test_live_order` 8, `test_board_check` 17, `test_alert_resolution` 41, `test_apply_page` 11, `test_alert_health` 8, one in `test_registry`); the full suite
ran as 128 files on WSL's own disk (3,040 tests, 0 failures).

Evidence from the user's run of `discover --ats --gmail-alerts --report` on 2026-10-20: 8 of 89 alert jobs resolved (indeed-uk 5 of 65,
linkedin 3 of 24), 38 went to the manual queue, the freshest jobs were the hardest to open. Each item had a failing test first.

0. **The one failing test.** `tests/discovery/test_jobbank.py` copied the real `config/sources.yaml`, where `jobbank-ca` is now `enabled: false`
   (9 tests of the file failed, not one), and `test_discover_jobbank_prints_the_counts...` also read the real clock against rows dated October 2026
   (the 5 October row aged out of the 14-day window on 20 October). Fix: `tests/fixtures/discovery/sources_jobbank.yaml` and a frozen clock. No assertion changed.
1. **Alert resolution** (`discover.resolve_link`, `applykind.py`). Read first: the only step that can resolve an Indeed or LinkedIn alert is the employer-board
   lookup; the run record did not say which board it was, so resolutions are now counted per step (`resolved_via_board_known | board_guessed | reed`,
   `discover --report` prints them). Found in the same read: 3 of the 8 resolved alerts were dropped silently because the `ats` step had already used the run's
   40 new-file allowance (`accept() == "full"`, no counter), and the mail was marked seen so they never came back. Now a resolved job with no room goes to
   the manual queue (employer page as its link, its reason says so), and `discover` reads the alerts BEFORE the boards. The CV-Library page fetch (C2) is gone:
   linkedin.com, indeed, cv-library and Workday hosts are never requested (`applykind.NEVER_FETCH`; the CLI's `Fetcher(never=...)` refuses them too). The title
   must match at `discover.title_match` 0.9 (was 0.8) with the same level words and a place that agrees (`candidate.places_agree`). Every candidate and inbox ad
   carries `apply_url` / `apply_kind` (`Apply kind:` / `Apply URL:` headers; `header_fields` reads a header run past line ten); `jobs.apply_kind` is a new additive column;
   `manual_queue.csv` has an `apply_kind` column (an older file's header is extended, rows kept). Review prints `Apply: <kind> <url>`; `[w]` opens it (`review.open_url`).
2. **Order after tailoring** (`live.py`). Pass 1 tailors the selected files (tier, then the estimate, `--max`); pass 2 reviews them sorted by tier and FINAL P and prints the
   table first. The inbox listing says "estimate". `Live._one(f, stop_before_review=..., r=...)`; `--retailor` re-tailors once, not again in pass 2.
   Existing tests that fixed the old one-job-at-a-time order were changed to the new order (the archive guard, snapshot order, quit at review).
3. **Level fit** (`priority.level_of/level_factor`, `priority.level_factor` in settings): P = F x S x L; shown as `LEVEL: ...`; nothing dropped. (Triage still skips a
   senior / manager title with "Tailor it anyway?"; that rule is `roles.levels.exclude`, yours.)
4. **Wrong-company boards** (`boards.verify_shortened`, `ats.full_name_tokens`, `discover.ignore_companies`, `discover.ats.board_name_match`). A board found only
   through a shortened slug is accepted only if the board's own name matches at 0.9. Greenhouse's API gives a name; Lever's and Ashby's do not, so a shortened slug
   found on those is `board_unverified` (the user's evidence cases `ashby/applied`, `ashby/focus`, `ashby/build`, `ashby/arch` are all Ashby). A board you name under
   `sources:` is never name-checked. Applies to the alert lookup too.
5. **Honest alert health** (`health.py`). `alerts`, `jobs parsed`, `new`; `ok` only if jobs were parsed over `discover.health.alert_days`, else `parsed_none` with the first subject.

Also: 13 tests of `test_registry` / `test_gmail_sources` / `test_health` / `test_quality_round` were already failing against the user's uncommitted `config/sources.yaml` (sources
switched off or removed, four added). They now read `tests/fixtures/discovery/sources_registry.yaml`, a frozen copy of the registry as committed on 2026-10-13, and one new test
checks that the live file loads and lists each source once. `append_rows` extends a CSV header in place (a plain rewrite, no `os.replace`: `tests/test_deletions.py` guards moves).

**Stand-ins** (CLAUDE.md forbids the live runs): a scratch script ran the real `cli.main(["discover", "--gmail-alerts", "--ats", "--report"])` over scratch folders, the tests' fake
network and three made-up mails (it printed the board-check lines, `Alerts resolved by step`, the health table with `alerts / jobs parsed / new jobs`, and no banned host in
the hosts asked); `careerops live --mock --max 3` over three mock JDs printed the final-P table with `LEVEL:` flags and an `Apply:` line each.

**Not verified.** Every live behaviour: which boards the real names resolve to, the real resolution rate, a real mailbox's subjects, `wslview` / `explorer.exe` opening a page.

## 5y. Round 9 — gated auto-apply for Greenhouse, Ashby and Lever (2026-10-21) — **done** (fixture forms in headless Chromium and fixture reports; never a real form, the real model or the real data)

The hard rule that no code submits an application was replaced (in `.claude/CLAUDE.md`, by the user) by: forms may be submitted only by `careerops/autoapply.py`, only when every gate in
`settings.yaml → apply` passes; never solve, bypass or outsource a CAPTCHA; never invent an answer. A test (`test_click_on_a_submit_control_appears_only_in_autoapply_py`, and one that finds the single
`_click_submit` call by AST) holds the first part. README, "Auto-apply", is the user's description; this is the build record.

- **Settings** (`apply:` block, new; `paths.answers`, new): `mode: off | assist | auto` (default `assist`), `min_ats_strict: 70`, `max_auto_per_day: 5`, `confirm_first_n: 3`, `kinds`. `apply/autogate.config()` reads them with
  the defaults and refuses a wrong kind of value by name (`GateConfigError`). The mock root keeps `answers` as the user's file (`mockroot.KEEP_AS_IS`).
- **`apply/autogate.py`**: `evaluate(db, settings, job, rep, fb, today=, tracker=, submitting=) -> Gates`, eight `Gate`s in the order of the table, `Gates.line()` = `AUTO BLOCKED: ATS 58 < 70; level: senior`. ATS = `rep["ats"]["strict"]`;
  format = `pdf_problems()` (`pdfinfo` pages == 2, `pdffonts` emb column, `pdftotext -bbox` word boxes for overlap and the margins of `cv2.tex.j2`, `pdftotext -layout` lines for the name, the e-mail and
  `expected_headings(ctx)` in order); faithfulness = the chosen attempt's `violations` and `warnings` empty and the letter status; level = `priority.level_factor` is 1.0; sponsorship =
  `priority.says_no_sponsorship`; kind = `apply_kind`, else `applykind.kind_of(apply_url)`; duplicates = job row / outcomes / `archive_index` / `discover.archive_pairs` / AIApply export / `applied_auto.csv` /
  `tracker.sheet_job_ids` (new, read only; an unreadable Sheet fails closed only when `submitting`); cap = `autolog.clicked_on(today)`.
- **`apply/answers.py`**: reads the user's `answers.yaml` (`answer.yaml` if that is what exists); `AnswerBook.resolve(question, job)`; whole-word, case- and punctuation-insensitive, longest match wins; blank = unknown;
  `answer_numeric`, `overrides`, `options` (`pick_option`: equal, else whole words), `{source}` `{company}` `{role}`; `save()` appends one entry in the file's own list indentation after checking that exactly one
  entry was added (the only write to that file, only on the user's `save`).
- **`autoapply.py`**: `run(job, rep, applicant, settings, submit=, confirm=, ask=, launch=, ...) -> AutoResult`. Order: visa plan and wrong-file guard -> open (`Throttle`: 1 request/s/host) -> `captcha_reason` (selectors and
  text; looks only) -> `fill_form` -> `still_empty` -> `captcha_reason` -> screenshot -> [`submit=False`: stop] -> [first N ever / `confirm`: `Submit? [y/N]`] -> `captcha_reason` -> `_click_submit` -> poll 30 s for
  the confirmation -> screenshot. Every outcome appends one row to `data/live/applied_auto.csv` (`apply/autolog.py`). On `needs_user` the browser stays open until the user presses Enter.
- **`apply/live.py`**: `Hooks.auto_gate` / `Hooks.auto_apply` (both optional); `Live._try_auto` before the review prompt of a `needs_review` job. Statuses added: `needs_user`, `submit_unconfirmed` (finished: the JD is filed,
  never retried), `auto_filled (not submitted)`, `auto_ready (dry run)`. A decline or an `error` falls through to the review prompt.
- **`cli.py`**: `live --auto [--no-submit] [--confirm]`. `--auto` that could submit needs `apply.mode: auto`; `--auto --no-submit` also runs in `assist`; `off` refuses `--auto` and makes `live` a dry run; the check runs
  before the database is opened. `--dry-run` / `--mock` open no browser (the gate runs and prints `AUTO READY` / `AUTO BLOCKED`).
- **`scripts/auto_census.py`**: read-only census of the real database against the per-job gates (the numbers in the README).

Tests: 138 new tests (`test_answers` 20, `test_autogate` 63, `test_autoapply` 30, `test_live_auto` 17, `test_live_auto_cli` 8); the full suite then ran as 133 files on WSL's own disk on 2026-10-21 (3,176 tests, 0 failures).

**Done when.** The full suite has 0 failures (counts above). `careerops live --auto --no-submit --max 3` on the real inbox was NOT run: it tailors with the real model and opens real pages, which the project rules keep
for the user. What was run: `careerops run --mock --limit 1` (tailored the seeded fixture job), `live --mock --auto --no-submit` in the tests (prints `AUTO BLOCKED: ... kind: employer_other`, opens no browser), and
the census script on the real database (read only).

**Not verified.** A real Greenhouse, Ashby or Lever page: field labels, script-built drop-downs (`_combobox` is written for the common `role=combobox` pattern, tested on no real site), confirmation wording, an invisible
reCAPTCHA (it counts as a bot check, so such a form stays manual), the tracker Sheet read.

## 5q. Equal-opportunities answers (2026-10-13) — **done** (offline tests only; no real form opened)

`apply/eo.py` + `assist.fill_page`. Reads `applicant.yaml → equal_opportunities`: `{key: {answer, match_any: [...], question: [regex]}}` (`question` built in for `ethnicity`:
`ethnic | \brace\b | racial`, required for any other key; unknown keys are an error). **Fills only listed questions**: for each `match_any` text in order, the first form option
(select option, radio / checkbox label in form order) whose `eo.norm` equals the text's (casefolded, NFKC, all dash characters and runs of spaces collapsed to one space,
trailing `*`/`:` dropped; **equality, never "contains"**, so "American Indian or Alaska Native" is not "Indian"); no match, a text box, or no `equal_opportunities` block → empty.
`answer` is documentation (never typed, not matched unless it is also in `match_any`). **Nothing is inferred**: no other applicant field, the job or the visa block is read for an EO answer.
**Printing**: every EO field (`eo.EO_FIELD`: ethnic / race / gender / sex / disab / veteran / pronoun / sexual orientation / religio / belief / transgender / marital), listed or
not, is outlined blue and printed `CHECK EO ANSWER [key|not listed]: question -> option|EMPTY (reason; options: ...)`; `report["eo"]` has `{question, key, value, options}`; empty ones
go to `manual`, filled to `filled` (`... = eo`). These fields are handled before `never_fill` (which keeps nationality / citizenship / criminal / salary ...). **Doctor**:
`setup.eo_checks` fails a malformed block (one line, every problem listed) and says nothing when there is no block. Not done: a custom React dropdown (not a `<select>` / radio /
checkbox) is not recognised as having options, so it stays empty and is printed; a free-text box is never typed into (decision: `answer` is not a form option).
Tests: `tests/apply/test_eo.py` (matching, structure, doctor), `tests/apply/test_assist_eo.py` (Chromium: UK-style list, US-style list, no-match list, unlisted questions, text box, order,
invalid block). **Unverified:** a real application form.

## 5j. URL intake, LLM judge, letter mode (2026-10-10) — **done** (tested offline; none of it run live)

Started from a suite that was not green: the stability round (5i) was uncommitted and 18 tests failed (17 expected `fallback` where
`letter.on_failure: none` now gives `none`; the doctor test saw an `llm.models` entry for a provider it had removed). Those tests now set
`letter.on_failure: template` or the new expectation; no check, guard or deny-list entry was touched.
**1. URL intake** (`discovery/intake.py`; `careerops add-url URL [--company --title]`, `careerops live --url URL`). `parse_url` →
Greenhouse (`boards-api.greenhouse.io/v1/boards/{board}/jobs/{id}`, `content` unescaped and stripped with `sources.html_to_text`), Lever
(`api.lever.co/v0/postings/{site}/{id}`, `api.eu.lever.co` for `jobs.eu.lever.co`; verified in Lever's postings-api README), Ashby (the docs
list only the board endpoint `api.ashbyhq.com/posting-api/job-board/{board}` and no single-posting call, so that one request is made and
the posting matched by id / URL) or any other page (`JobPosting` JSON-LD, else `<main>`/`<article>`/body without scripts, nav, header,
footer; under `MIN_PAGE_CHARS` = 2000 → `IntakeError` with a clear message). One request per second per host (`_throttle`), the shared `UA`,
5 MB cap, 404 = "closed or wrong link". `write_inbox` creates `<company>-<title>-<id>.txt` with `open(..., "x")` (never overwrites; an
existing file is reused) and refuses headers `live` would reject. `Live(only=file)` makes `live --url` work on that one file; its resume
command carries `--url`; `--url` with `--mock` is refused (no network). Fixtures: `tests/fixtures/intake/`.
**1b. Clipboard intake and blocked sites** (`intake.add_clipboard`, `live --clipboard`; offline-tested with a stubbed `_run_clipboard`). HTTP
401/403/429 in `_fetch` raise `BlockedError` ("This site blocks automated access — copy the job text and use --clipboard"): one request, no
retry or workaround, exit 1 from `add-url` / `live --url`. `--clipboard` runs `powershell.exe -NoProfile -Command "[Console]::OutputEncoding=
[Text.Encoding]::UTF8; Get-Clipboard"`, strips `\r`/BOM, refuses under `MIN_PAGE_CHARS`, asks Company / Title / URL (`meta_problem`, URL must be
http(s); three bad answers write nothing) and writes `<company>-<title>.txt` via `write_inbox`; `--url` + `--clipboard` is refused, the
resume command does not carry `--clipboard`. Review screen: `workflow.job_board` (host label or source of cv-library / reed / totaljobs /
indeed / linkedin) adds the `BOARD:` line. Not run against a real clipboard.
**2. Inbox validation** (`manual.meta_problem`): `<`/`>` anywhere in company/title/url/location, or a company/title under
`MIN_NAME_CHARS` = 2, is a defect. A header with one moves the file to `rejected/` with `header <reason>` and nothing is asked; a prompt
answer with one is asked again (`Live._ask_valid`, three bad answers = blank = reject); `add_jd` raises `JDError` (also for `add-jd --company o`).
**3. Judge** (`review/judge.py`, `review.judge: off|shadow|gate`, default shadow; spec in README "The LLM judge"). Model =
`llm.models.judge` (`groq/llama-3.3-70b-versatile`), `llm.task_providers.judge: [groq]`, `llm.max_tokens.judge: 800`; `check_config` refuses a
missing model/pin or a model of the generator's family (`model_family`), `check_family` compares with `report.llm_usage.calls` for each
job, `doctor` has a `review.judge` line, `doctor`/`llm-check` ping the model (doctor skips the models of features that are off).
Input: JD (cut at 9000 chars), CV text from `report.context` (no name/contact), letter paragraphs, source bullets with `result`.
Output parsed by `parse_verdict` (a `would_send` with a score under 4 counts as a no). Verdicts are cached by a hash of what was shown in
`paths.judge/verdicts/`; `paths.judge/log.csv` (append-only) gets a row per human decision (`approve`/`reject`; live's `s` = reject via
`review_job(skip_means_no=True)`; gate auto-approvals are `auto`, not labels). `judge-report`: n, agreement, Cohen's kappa (n/a when a side
never varies), false approves, false rejects, rule of three 3/n (and 3/judge-yes) when there are none, Wilson interval otherwise, gate status.
Gate: `GATE_MIN_LABELS` = 60 constant, `require_gate` raises `GateRefused` (cli → exit 1) at the start of `review_job` unless 60 labelled
reviews and 0 false approves; only a judge yes with no `flags` and letter status ok/off is approved in code; fallback / none letters,
judge no, no verdict all go to the prompt. The judge module imports nothing from `apply/`.
**Router fix found on the way:** the provider's `reasoning_effort: low` / `include_reasoning: false` were sent with every model of that
provider; Groq answers 400 to `reasoning_effort` for llama. They now apply to the provider's own model only (`Provider.reasoning_for`); a
task model gets the name-based default (gpt-oss: low/false, others: none). Regression test in `test_router_models.py`.
**4. WSL `o`** (`workflow.open_file` → `wsl_open`): in WSL (`WSL_DISTRO_NAME`/`WSL_INTEROP`/`/proc/version` "microsoft") with `wslpath` and
`explorer.exe` (or `/mnt/c/Windows/explorer.exe`), `explorer.exe $(wslpath -w FILE)`; explorer's exit code (1 on success) is never read;
any missing piece falls through to wslview / open / startfile / xdg-open as before.
**5. Letter mode** (`letter.mode: off|draft`, default off, `tailor.letter_mode`). Off: `_make_letter` returns status `off`
(`reason: letter.mode: off`, no PDF, no call, evidence still recorded); review prints `Letter: off (...)` and approves without
confirmation (`l` explains); `archive._sources` treats `off` like `none` (archived with `no_letter: true`); doctor skips the letter model
ping. Draft: `tailoring/evidence_fields.py` sends `EVIDENCE n / method / data / outcome` instead of the bullet sentence
(`P.LETTER_FIELDS_NOTE` added to the system prompt); the checks still read the full bullet. **Open question for you:** fact-bank bullets
have no method/data/outcome keys and I may not edit the bank, so unless you add them the fields are `terms`, `(not stated)` and `result`.
Tests that need a letter get `letter.mode: draft` from the `settings` fixture (`tests/conftest.py`).
YAML trap: an unquoted `off` loads as `False`; `letter_mode` / `judge_mode` accept it and `settings.yaml` quotes it.

## 6. Outreach (`outreach.py`) — your LinkedIn prompt, adapted to LinkedIn's limits

**Constraint clash.** Sources disagree on the note limit — 200 characters on free accounts (300 on Premium) per one, 300 for all accounts per another — and free accounts get only ~3–5 personalised notes a month. Five sentences is ~400–600 characters, so it can't fit either way.

**Design:** two artefacts per approved job, generated on demand (`careerops outreach --id <job> --person "Name" --title "Senior Data Scientist"`):
1. **Connection note** ≤ 200 characters (safe for both limits): why *them* (same team as the role) + one small ask.
2. **Follow-up message** for after they accept (no note cap on messages to connections) — your full spec: ≤ 5 sentences, direct, referral + team question, small ask, banned openers ("I came across your profile…", "I hope this finds you well") rejected by regex.

Inputs are what you supply (name, title, team); nothing is scraped from LinkedIn and nothing is sent — LinkedIn's terms bar automation, and with 3–5 notes a month each one is worth your judgement. Outreach drafts are logged to `preferences.jsonl` like CV edits.

---

## 7. Sequencing

| Order | Work | Depends on | Exit test |
|---|---|---|---|
| 1 | M1 register snapshots + diff | — | `test_register.py` green |
| 2 | M2 Companies House join | M1 `companies` table | `test_companies_house.py` green |
| 3 | M3 ATS resolution + detector + queue CLI | M2 in-scope set | `test_ats.py` green |
| 4 | M4 wiring + Excel sheets | M3 | `careerops run --mock` uses resolved boards |
| 5 | Prompt upgrades + learning list + `needs_company` | — (parallel) | tailoring tests extended |
| 6 | Outreach | 5 | `test_outreach.py` green |
| 7 | Package → you run M5 live | all | your first real review sitting |

**Sprint done =** all tests green (existing 28 + new), `run --mock` end-to-end on fixtures, README updated, zip delivered.

### Letter template + CV filler round (run 201304 review)
`tailoring/letter_rules.py`: `starts_with_past_verb` (no longer a filter on letter evidence since the relevance round, 5h), `gap_display` (JD casing, else a canonical map), `grammar_problems` (added to `_style_problems`' set in `_letter.draft_problems`: "my work on <verb>", "mirrors my work <noun>", a paragraph starting with a bare past-tense verb, a bullet pasted verbatim) and `fallback_letter` (P1 hook + "I'd like to bring my experience to that work: at <org>, I <clause>.", P2 "At <org 2>, I <clause>.", P3 gap, P4 CTA; a project bullet reads "in my <name> project"). It replaces the old fallback AND the previous round's opening-only `code_hook` (that kept the model's paragraphs, which usually re-described bullet 1 and so pasted it twice). `MockLLM`'s letter now writes "I <bullet clause>". `rewrite_rules`: the trailing-participial pattern joins `FILLER_PATTERNS`; `bare_keyword_problems(new, source, kws)` is added in `guard_rephrases`.

### Guard round (run 190956 review)
`tailoring/rewrite_rules.py`: `qualifier_problems`, `filler_problems` (both added to `guard_rephrases`' violations, `faithfulness.py` untouched) and `canonical_case` (accepted rewrites only). `grounding.opening_claim_problems`: evidence-only check of the opening's claim half (`opening_claim` splits at mirrors/dash/colon/semicolon; names, sector/client words, capabilities and other content words must be in the two bullets' text + terms; `_NEUTRAL` words excepted); `FILLER_WORDS` gained the nine vague phrases (matched singular/plural, case-folded). `tailor._letter`: `draft_problems` separates the opening's problems; after two rejections, if only they remain, `code_hook` replaces the opening sentence (re-checked, `retry["outcome"] == "code-built hook"`). `strip_team_word` stops "team team" in the greeting, closing and `clean_team`. `render.skill_display` + the `skill` template filter show commas outside brackets as "/".

### Tuning round 2: keyword placement, no-regression, plural literal
`tailor.placement_offers(fb, sel, kws, other_text)` (MAX_OFFERS = 2): keywords with no literal hit in the selected bullets + profile + skills go to the first selected bullet whose `terms` evidence them; `propose_rephrases` sends only those as MAY INTRODUCE (prompt: "include each one only if it reads naturally"). `faithfulness.check` already treats a bullet's own `terms` as evidence, so it is untouched. `tailor.placement_report` → `rec["placement"]` / `report["placement"]` / calibration row + detail line: placed / not_placed / blocked / unoffered. `guard_rephrases(..., base=)` adds the no-regression rule (`lost_hits`: literal hits of the replaced text must survive; the wording pass passes the text on the CV as `base`); refusals are `no-regression` warnings and counted in `rephrase.regressed`. `scorer.has_literal` makes the literal view singular/plural-tolerant (scorer, `missing_keywords`); the stemmed view and faithfulness are unchanged.
Lendable 162044 → 190956: `dc1` lost "communication" (a 162044 rewrite wrote "live communication demo"; the 190956 rewrite did not offer/place it — model variance, not a regression of an accepted rewrite) and `az4` lost "evaluations" only as the plural (the bullet says "evaluation"; fixed by the literal plural rule). Both are in `tests/tailoring/test_placement.py`.

### Fix round: assist visa answers + applicant doctor checks
`apply/assist.py`: a field whose label matches `VISA_FIELD` is answered from `applicant.yaml → visa` before `never_fill` is
consulted (free text ← `visa.summary`; Yes/No, dropdown or radio group ← `visa.answers[sponsorship_now | sponsorship_future |
right_to_work_now]` by `classify_visa`; unclear type, `ask` or a share-code box → left empty). Each is outlined magenta and printed as
`CHECK VISA ANSWER: <question> -> <value or EMPTY>`; `fill_page` returns them under `report["visa"]`. Other `never_fill` matches are
unchanged. `setup.applicant_checks` adds three `careerops doctor` lines: placeholder email domain, `+44` followed by 0, a UK postcode in `location`.

### CV template round v2 (16 Oct 2026): `cv_style_reference.tex` v2
`templates/cv2.tex.j2` is the v2 preamble line for line (`REFERENCE_PREAMBLE` in `test_cv_template.py` updated with it) plus `needspace` and the linespread value. New: `\skillgroup{label}{items}` (label alone on a navy bold line), the 12pt headline kept to one line, the contact line's portfolio link text "Portfolio", project links only as `\gh` (never `\url`).
**Skills.** `cv.skill_groups` / `skills_always` / `skill_group_max` in `settings.yaml` -> `skills.cv_skill_groups(fb, kws, cfg)`: four fixed groups; a skill goes to the group whose `skills` names it, else to the first group whose `banks` regex matches its fact-bank group's name, else (core only) to the `default` group; unclaimed fact-bank groups are listed in `report["skills"]["dropped_groups"]`, a configured group with no skills in `analysis_warnings`. The one-page CV and `cv_skills` are unchanged. The real fact bank has no "Client Delivery & Communication" skills group yet, so that group is left off until the user adds one (proposed in chat, not written: `fact_bank.yaml` is the user's).
**Achievements.** `tailor.achievement_projects` (award project outside the JD's top three by `budget.rank_projects`, unless it holds the hook's proof bullet) and `achievement_entries`; the project is taken out of the selection and fill (`fb_sel`) and printed as `\item \textbf{name, award}: bullet` before the certifications bullet.
**STAR.** `layout.with_openers` (render time, two-page only): the first bullet of an entry with `situation:` is "<situation>: " + the action with its first letter lower-cased (not an acronym). Checks and the report see the wording without it.
**Length rules.** `layout.py` measures the rendered PDF (`pdftotext -bbox-layout`): headline lines, profile lines, lines per bullet. `tailor.fit_lengths` wraps every two-page render: headline -> `layout.headline_options` (trailing bracket / stack list, the `problem:` label, clauses, role alone); profile -> whole sentences dropped after the hook sentence; bullet -> fact-bank wording, `ctx["splits"]` (two bullets under one id, `layout.with_splits`), `shorten_pass` (REPHRASE prompt with a SHORTEN line, guarded by `guard_rephrases`), else a `length:` warning. Result in `report["lengths"]`. `scoring.min_fill_page2` 0.90 -> 0.96 (two lines of the 770pt text area).
Tests: `tests/tailoring/test_cv_v2.py` (31), the updated `test_cv_template.py`, `test_two_page.py`, `test_cv_typography.py`; the fictional profile fixture gained `situation:` on `az` and `churn` and a "Client Delivery & Communication" skills group.

## 5y. Round 10 — fixes from the first live run (2026-10-21) — **done** (offline tests only; no live form, no live discover, no real model)

Each item had a failing test first. Evidence: the user's first live run.

1. **A closed browser must not lose the record.** `assist.assist(..., launch=, ask=)`: the launcher's `close()` and the call to it swallow `playwright.Error`; a
   page that fails before the prompt (the window closed) still reaches "paste confirmation". `Live` writes `apply_opened` into `state.json` when the apply page is
   opened and clears it when "Did YOU submit?" is answered; a session that finds it set asks `Did you submit <company> <role>? [y/N]` first (not in a dry run),
   `y` runs the same `mark_applied` hook (archive, outcome log, tracker row), `n` clears it. `tests/apply/test_assist_closed_browser.py`, 4 in `test_live.py`.
2. **One filler.** New `apply/filler.py`: `fill_field` (standard fields from applicant.yaml, including the single "Name" box = first + last; then answers.yaml for
   text, drop-downs, radio groups, check boxes) used by `autoapply.fill_form` and `assist.fill_page`; `Ctx.strict` is the only difference (auto asks for a required
   question nobody answered, the assist leaves it for you and marks it red). The click on a script-built drop-down's option stays in `autoapply._combobox`
   (the "only autoapply.py clicks" test still holds). `assist(book=, history=)`; `cli._assist_answers` passes answers.yaml. `tests/apply/test_assist_filler.py`
   (Ashby fixture `ashby_form.html`).
3. **Output folder names.** `tailor.folder_name`: `<Company>_<Role, cut>_<job id>`, the id never cut (company + role take `60 - len(id) - 1`, at least 24).
   `tailor.swap_in`: a folder rename that raises PermissionError is retried 3 times, 1 s apart, then the files are copied into the folder and the `.partial` deleted;
   the file Windows holds is printed; an earlier output is only given up once the new one is complete. `tests/tailoring/test_output_folder.py`.
4. **Cross-board duplicates.** `candidate.family_key` (strips a trailing Early Careers / Careers / UK / Ltd / ...) is the company part of `dedupe_key`;
   `Known.where` also matches the same family + title under another board's spelling whatever the place says. Discovery keeps the NEWER copy (`posted`): the older
   inbox file gets `Duplicate of: <newer file>` (header, ad untouched; counter `superseded`), `live` moves a marked file to `rejected/`. The auto-apply duplicate
   gate uses the same key. `tests/discovery/test_cross_board_duplicates.py`. No new database column: the mark lives in the inbox file.
5. **Tracker auth pre-flight.** `tracker.scope_problem(settings)` reads the token file; `cmd_live` prints one line and runs `--auto`-less when the scope is missing
   (not on the mock root). `tests/test_live_tracker_auth.py`.
6. **Faithfulness gate.** `autogate.split_violations`: a violation of a rewrite the model proposed and the CV does not carry is a *blocked rewrite* (information,
   `Gate.info`, printed by `live`); every other violation, and one of a rewrite that reached the CV, is an *issue in the final CV* and gates. `faithfulness.py` and the
   deny list are untouched.
7. **CV-Library alerts.** The real alert (`tests/fixtures/discovery/cvlibrary_alert_2026-10-21.html`, personal data and tracking tokens removed) has no `/job/<id>`
   link: every link is on `clicks.cv-library.co.uk/f/a/`. `gmail.job_link` accepts such a link as the job's pointer (never requested); a tracking link counts as a job
   only with a company and a place after it; header/footer links are noise. `tests/discovery/test_cvlibrary_alert.py`.
8. **Ashby 401.** `discovery/blocklog.py` (`data/discover/blocked_hosts.json`): host, status, time, requests before the refusal. `Fetcher(blocklog=)` writes it and,
   for 7 days, keeps 2 s between requests to that host; the health row says when it was last seen refusing. The same-run rule (no request after a 401) was already in
   `http.py` and now has a test. The message in the evidence came from the health probe of the bare Ashby API path, not from a board request.
   `tests/discovery/test_blocked_hosts.py`.

Not verified (needs the real world): a live Ashby form, a real closed browser, a Windows lock on a PDF, the live Gmail token, a second CV-Library alert shape.

## 5z. Round 11 — the agent drafts free-text answers (2026-10-21) — **done** (MockLLM and fixture forms only; no real model, no live form)

New `apply/freetext.py`, called by `apply/filler.fill_field` for a REQUIRED text area (or a text box with `maxlength` over 100) that answers.yaml does not answer;
the same code serves `autoapply.fill_form` (strict: a missing answer stops the run, `needs_user`) and `assist.fill_page` (not strict: it leaves the field for
you). `cli.cmd_live` passes a per-job `FreeText` (job ad, the CV's report: keywords and hook, fact bank, the run's model, `answers.yaml`).

1. **Question.** `read_limits` ("1-2 sentences" -> 2, "max 200 words", `maxlength`, "at least N" ignored, none = 120 words); `classify` -> project | challenge |
   why_company | why_role | other.
2. **Plan, draft.** `FreeText.plan`: bullets scored with the CV's own keyword matching (`scorer.keyword_hits`) on the job's keywords plus the question's words, the hook
   proof first, at most 3; a challenge prefers entries with a `situation` or a `result`; a "why" answer draws on the ad sentences (`ad_snippets`), `answers.yaml ->
   motivations` and the hook. One call of task `free_text_answer` (`llm.max_tokens.free_text_answer`), JSON `{answer, ad_phrases}`.
3. **Guard.** `Guard.problems`: `faithfulness.check` (deny list, new numbers, JD keywords not evidenced), the limit, `style.avoid`, first person, every bank tool / taxonomy
   tool in the draft in the chosen entries, every capitalised name in them, a "why" claim = 4+ words in a row from the ad (each sentence naming the company too),
   `letters.claim_problems` (no we/our, no "I help X") and `term_dump_problems`. Redraft once with the problems; a second failure goes to the person with no draft.
   `faithfulness.py` and the deny list are untouched.
4. **Approve, store, reuse.** `[y] use · [e] edit · [r] redraft · [s] I'll write it`; only `y` or typed text is used. `Library` (`paths.answers_library`, JSON lines,
   append-only): question, normalised question, answer, company, job id, source ids, date, `approved: true` (+ class, origin). Reuse: same class, `similarity` >= 0.85 (words
   after synonyms: shipped/built/deployed are one), still inside the new limit; why_company only for the same company, why_role only for the same job. A file that does
   not exist yet starts with the two Limbic answers approved on 2026-10-21 (job `manual:177a9a1afcc1`), word for word. `answers.AnswerBook` now also reads `motivations` and `style`.
5. **Tests.** `tests/apply/test_freetext.py` (68: limits, classes, similarity, seeds, plan, guard, approval, reuse, why_company/why_role), `tests/test_autoapply_freetext.py`
   (6, Chromium: a new draft is not used without `y` and nothing is submitted; a reused answer goes through).

Not verified: a real model's drafts (the guard may reject more than the mock shows: it then asks you), a live form. `careerops apply` (the old command) does not draft.

## 5af. Round 17 — the 22 Oct loveholidays run (Ashby): fill order and read-back, an extractive P2 (2026-10-22) — **done** (offline tests; Chromium fixture forms, MockLLM; no live form, no real model)
1. **Fill order** (`apply/verify.py`, used by `assist.fill_page` and `autoapply.fill_form`). Pass 1: every file field. The "Autofill from resume" control is never given a file
   (`verify.is_autofill`: its id / name / aria-label / label says autofill, or the text of the smallest container around it that holds no other field and no other file input does;
   printed `skipped the autofill upload control (never used)`, `report["skipped"]`); the CV goes only to the real Resume / CV field, the letter PDF to a cover-letter file field. Then, only when
   a file was set, `verify.wait_quiet`: no DOM mutation (a MutationObserver) for `QUIET_MS = 2000`, at most `QUIET_MAX_MS = 15000`. Pass 2: the text fields, selects, boxes (fresh element
   handles, the form may have replaced its nodes). `fill_page(..., quiet_ms=)` / `fill_form(..., quiet_ms=)` shorten the wait for tests.
2. **Read-back.** Every field a run filled is noted in a `verify.Tracker` (`Ctx.track`, called by `filler` for standard fields, answers.yaml text / select / radio / checkbox and free text, and by
   the assist and the auto-apply for the cover-letter box, visa fields and equal-opportunities fields). After the fill each is read back (`input_value()`; a select by its selected option text; a
   check box / radio by `is_checked()`); an empty one is filled once more and read again. Prints `VERIFIED  <field>` (`(filled again)` when the second fill was needed) or `LOST      <field>`.
   The assist adds every LOST field to `required_empty` (REQUIRED + EMPTY, outlined red; `report["verified"]`, `report["lost"]`). The auto-apply (`autoapply.verify_filled`) stops with
   `needs_user` ("the form emptied field(s) after they were filled") rather than submit. A field with an id or a unique name is found again by that selector (a re-rendered node is not read stale).
3. **Cover letter.** Unchanged and now tested in both callers: a cover-letter text box gets the letter text (unless it holds text, or there is a cover-letter file field and a letter PDF in the
   assist); a cover-letter file field gets the PDF.
4. **Equal opportunities: a single check box.** `assist._answer_eo`: a lone check box ("Person with disability", "Veteran") whose listed answer is No (`eo.answers_no`: the entry's `answer`,
   else its first accepted option) stays unchecked and prints `CHECK EO ANSWER [key]: question -> left unchecked (answer: No)` (`report["eo"]` value `No`); it is not in `manual` or
   `required_empty`. A lone check box with any other answer, or not listed, is unchanged (EMPTY). A group of check boxes still picks the matching option.
5. **Letter, company mode** (`tailoring/company_letter.py`, `prompts.py`). `ATTEMPTS = 3`. P2 is extractive: `about_sentences` offers the ad's About / role sentences (before the first heading and under
   about / role / team / impact headings; not skills, requirements, benefits, diversity, privacy or fraud sections; at most `ABOUT_LIMIT` 10, ranked by company name, ad keywords and cue words), numbered in the
   prompt; `extractive_problems` rejects a P2 that does not quote or closely follow 2 of them (`used_sentences`: a P2 sentence or clause with >= 60 % of its content stems in one offered sentence,
   >= 3 shared). `letter["p2_used"]` and the review lines (`workflow.company_letter_lines`) print which ones it followed. The company guard checks facts (nouns, names, numbers, products, customers):
   the connecting words of a role ("the role involves", "requires", "this team", "you will", responsibilities, candidate ...) are in `_GENERIC` and are not claims. The user guard is unchanged; its
   prompt now lists the ALLOWED VOCABULARY (the chosen bullets' tools and methods + the ad's skill terms, `allowed_vocabulary`) and the filler words (`FILLER`: robust, seamless, cutting-edge,
   passionate, leverage, synergy).
6. **Tests.** `tests/apply/test_fill_verify.py` (20: the autofill control never used, a form that clears its fields after an upload still ends with salary and notice filled, the wipe-once refill, LOST,
   select reset, `wait_quiet`, the auto-apply twin, the cover-letter box and file in both callers), `tests/apply/test_assist_eo.py` (+6: single check box No / Yes / unlisted / required / group),
   `tests/tailoring/test_company_letter.py` (+18, the two-draft test replaced by a three-draft one: "The role involves ..." with ad content passes, "SaaS" not in the ad or the fact bank fails,
   "seamless" fails, P2 following 0 / 1 / 2 offered sentences, the prompt's numbered sentences / vocabulary / filler, the third draft used, the review lines).
   Not verified: a live Ashby form (the fixtures re-render and wipe like it does, but the real timing is unseen), and the real Groq model following the extractive prompt. Stale and left alone:
   the comment above `letter:` in `config/settings.yaml` (and the test copy) still says "one redraft"; those files hold your uncommitted settings.

## 5ae. Round 16 — `letter.mode: company` (2026-10-22) — **done** (offline tests, MockLLM; no real model, no live form)

1. **Settings.** `letter.mode` accepts `company` (`tailor.LETTER_MODES`). `letter.company_words: 400` (allowed 360-420, `company_letter.target_words` refuses anything else) is the length the
   model is asked for. `scoring.letter_max_words_company: 420` is the cap of this mode only (`word_cap`, never over 420); `scoring.letter_max_words` (250) is untouched, so the other modes
   are unchanged. Both keys are in `config/settings.yaml` and in `tests/fixtures/config/settings.yaml`.
2. **`tailoring/company_letter.py`.** `prepare` settles P1 (hook `letter_sentence`, else the family `profile_body` from `tailor._tailor`'s new `report["family_line"]`, which is now set for every job,
   not only a hook-less one), P5 (`letters.closing_text(problem)`: the hook's problem, else `letters.ad_problem` = the ad's two heaviest claimable keywords) and the two evidence bullets with the ad
   requirements each evidences. `make` asks the `cover_letter` task (context `mode: "company"`, prompts `COMPANY_LETTER_SYSTEM/USER`) for `[P2, P3, P4]`, runs `check`, and on a rejection asks
   again with every rejection listed (`ATTEMPTS = 3` since round 17; it was 2). Three rejections: `status "none"`, `reason`, `retry.failures`, no PDF. `TaskUnavailable`: `status "pending"`. No template fallback.
3. **Guards.** `company_problems` (P2: clause-level normalised overlap against windows of up to 3 ad sentences, `COMPANY_SUPPORT` 0.6; numbers must be in the ad), `name_problems` (an invented
   person / team / product name), `user_problems` (P3, P4: numbers, fact-bank tools and organisations must be the chosen bullets', `grounding_problems` per bullet with no ad to lean on, a named
   requirement, no paste, no repeated bullet), plus the existing `claim_problems` (we / our / "I help"), `term_dump_problems`, deny list, style and word-count checks.
4. **Wiring.** `tailor._letters` has a `company` branch (returns one letter, no A / B variants, so review's `l` / `g` regenerate it with the same inputs: `report["family_line"]`, hook, claimable).
   `review.workflow.company_letter_lines`: word count, accepted range, requirements named, and each P2 sentence with the ad sentences it drew from. The report's letter dict carries
   `mode`, `words`, `target_words`, `p2_sources`, `requirements`, `hook_source`.
5. **Upload.** Unchanged from round 15: the PDF goes to a cover-letter file field, the text to a cover-letter text box when there is no file field.
6. **Tests.** `tests/tailoring/test_company_letter.py` (57): a P2 claim missing from the ad, a company number missing from the ad, an invented person / team name, a user number / tool /
   organisation missing from the bullets, 360-420 words pass and 250 / 359 / 421 / 500 fail, the redraft-then-no-letter flow (three drafts since round 17), a no-hook job (with and without a family line), the review lines,
   and `tailor_job` end to end (letter fits one page). Not verified: the real Groq model's drafts (the guards are strict by design and may reject paraphrases: it then redrafts, up to three drafts, and says why).

## 5ad. Round 15 — the 22 Oct live run at Flo Health (Greenhouse) (2026-10-22) — **done** (offline tests; no live form, no real model, no real data)

1. **Crash.** `job.get` on a `sqlite3.Row` (`Handoff.run`, reached from `assist`). `assist()` and `Handoff.run()` now start with `job = dict(job)`. Tests pass a real `sqlite3.Row`
   through `assist` to the hand-off (a bot check, and a host handed over before) and into `Handoff.run`.
2. **Hand-off trigger** (`formfind.captcha_reason` / `challenge_shown`). A reCAPTCHA / hCaptcha / Arkose frame or container hands over only when it is displayed
   (`is_visible`), at least `MIN_CHALLENGE_W x MIN_CHALLENGE_H` (100 x 60) px, not marked invisible (`size=invisible` in its `src`, `data-size="invisible"`) and not a
   `button` / `input` / `a` that merely carries a `data-sitekey` (reCAPTCHA v3). A displayed Turnstile widget (`TURNSTILE_SELECTORS`) hands over at any size. The words
   "verify you are human", "error verifying your application" and the Google / Microsoft sign-in page are unchanged. The v3 badge and its hidden challenge frame no longer stop the
   run; nothing is ever clicked, focused or typed into (only `is_visible`, `bounding_box`, `get_attribute`, `evaluate(tagName)`). `autoapply` uses the same function.
3. **EO text boxes.** `assist._answer_eo`: a text box / text area for an `eo_*` entry of answers.yaml is filled with that entry's `answer` (a box that already has text is left; an
   entry with a blank answer, or a question listed only in applicant.yaml, stays empty as before). Printed `CHECK EO ANSWER [eo_gender]: Gender -> Male`.
4. **Letters without a hook** (`letter.mode: hook`). `letters.family_hook` makes a stand-in `Hook` (`source: "family"`) whose `letter_sentence` is the family's `profile_body` word for word
   (`tailor._letters(..., family_line=head)`; kept in `report["family_line"]` so review's `l` / `g` regenerate it the same way); `make_letters` is unchanged otherwise (two bullet
   sentences from the CV's bullets, closing question from `letter.closing_hook` with the ad's two heaviest claimable keywords as the problem, same checks). No `profile_body` = letters "none".
5. **Upload.** `assist.fill_page`: the letter PDF goes to a cover-letter file field and is reported `Cover Letter ← letter` (was `← cover letter`); the letter text goes to a cover-letter text
   box only when the form has no cover-letter file field for the PDF (the PDF and the text are not both put on one form).
6. **Tests** (+39: `test_handoff.py` +26 = 8 displayed and 8 invisible/hidden blocker cases, 2 combined, 3 `sqlite3.Row`, 5 in Chromium; `test_assist_eo.py` +5 text boxes; `test_assist_letter.py` +5; `test_golden_pipeline.py` +2; `test_autoapply.py` +1).
   `tests/challenge_fakes.py` (fake frames), `tests/fixtures/apply/auto_captcha.html?invisible`. Not verified: a real Greenhouse page with the real reCAPTCHA v3 badge, the real fallback letter text.

## 5ac. Round 14 — fixes from the 22 Oct live run (2026-10-22) — **done** (offline tests only; no live form, no real model, no real data)

1. **Excluded titles never prompt.** `settings rules.ask_on_excluded: false` (the built-in default when the key is absent too): a job whose triage reason is "title not in target
   families" or "level excluded (...)" (`gates.is_excluded_reason`) is `skipped (rules)` with no "Tailor it anyway?" and its JD goes to `jds_done/skipped/` (round 13). Other rule
   skips (a `jd_rules` skip such as a nationality restriction) are still asked. `true` brings the question back for the excluded ones.
2. **The headline is never empty.** With the hook on and no entry qualifying, `tailor._context(family_headline=True)` puts the role family's `headline` under the name
   (`ctx["hook_headline"]`, number empty) and profile line 1 is the bank family's `profile_body` (`families[*].profile_body` in the fact bank, the family `pick_family` chose;
   `assemble_profile(..., head)`), followed by the usual `profile_facts` sentences; `ctx["profile_head"]` is what a length fit never drops (`fit_lengths`). The report has
   `hook_family_profile`. A bank family without `profile_body` leaves profile line 1 as the facts only. Hook off (`hook.enabled: false`) is unchanged: no slot.
3. **Find the form** (`apply/formfind.py`, used by `apply/assist.py` and `autoapply.py`): `form_url` (Ashby `/application`, Lever `/apply`), `wait_for_form` (15 s, polls every 0.5 s,
   an "Apply" link followed once through the caller's own click), `FORM NOT FOUND <url>`. The assist prints no `Filled:` line then; autoapply stops with that reason (`needs_user`).
4. **Hand-off** (`apply/handoff.py`): `formfind.blocker` recognises a bot check (the old `autoapply.captcha_reason`, moved), "error verifying your application", and a Google /
   Microsoft sign-in page or popup (`accounts.google.com`, `login.microsoftonline.com`, `login.live.com`, `login.microsoft.com`, any window of the same browser). `Handoff.run`
   opens the URL with `explorer.exe`, prints the CV path, writes `answers_to_paste.txt` (`paste_text`: only the person's own files and approved answers) and records the host in
   `paths.live/handoff_hosts.json`; a recorded host opens no browser (`assist` and `autoapply.run` check `Handoff.known` first). `cli._assist_answers` and the auto hook pass `Handoff(s)`.
5. **EO answers.** `eo.entries(applicant, book)` adds the `eo_*` entries of answers.yaml (`eo.book_entries`: `match` texts as whole-word question patterns, `options` else `answer` as the options,
   best first), after applicant.yaml's; the assist and the paste file use them.
6. **Tests.** `tests/apply/test_handoff.py` (30: form addresses per ATS, the 15 s wait, FORM NOT FOUND, blockers, hand-off file and host record, the assist without a browser, EO options),
   `tests/test_autoapply.py` (+4 in Chromium: bot check, verify error, known host, no form), `tests/apply/test_live.py` (+5: silent skips), `tests/test_golden_pipeline.py` (+1, the
   headline and profile body without a hook). `tests/test_autoapply.py`'s "the browser is left open" test became the hand-off test.
   Not verified: a real Greenhouse / Ashby / Lever page, a real sign-in popup, `explorer.exe` opening the browser from WSL.

## 5ab. Round 13 — skip and applied are final, and cover every copy of a job (2026-10-22) — **done** (offline tests only)

1. **`apply/decided.py`.** The duplicate key is `(candidate.family_key(company), candidate.norm_title(title))` (round 10; place is not part of it). `settle(db, settings, job_id,
   decision, also=file)` is called by `live._finish_file` for the statuses `skipped`, `skipped (rules)`, `applied`, `applied (archive incomplete)` and by `mark-applied`: every other
   database row with the key becomes `skipped` (`reason: same job as <id>: <decision>`; an applied row is never touched, and a copy of an applied job is not marked applied, so
   the funnel and the daily cap count one application), and every inbox JD with the key moves to `paths.jds_done/<skipped|applied>/` (`move_file`: never over a file, never in or
   out of the archive). Pass 2 of `live` does not review a copy that was filed with its twin.
2. **`paths.jds_done`** (`data/jds_done`; also the built-in default in `config.DEFAULT_PATHS`). Other finished jobs (`ready_not_submitted`, `submit_unconfirmed`) still go to `jds/done/`.
3. **Discovery.** `Discover.decided = decided.load(...)`: job ids and keys of the database rows with status skipped / applied and of the files in `jds_done/`.
   `duplicate()` and the Gmail alert path check it first and count `already_decided` (a new counter in the report) instead of `duplicate`.
4. **`careerops retriage --decided [--apply]`** (`decided.sweep`): dry run by default, lists the inbox files and the database rows that are copies of a decided job; `--apply` skips the rows and
   moves the files. **`careerops unskip --id JOB`** (`decided.restore`): refuses an applied group or a job that is not skipped; sets the job and its skipped copies to `needs_review` (tailored)
   or `new`, moves their files from `jds_done/skipped/` to the inbox and reopens their entries in `live/state.json`.
5. **Tests.** `tests/apply/test_decided.py` (skipping one Graphcore copy removes both; a re-discovered ad with a decided key is not written; `--apply` is needed to move anything;
   unskip restores the job; an applied decision is final; never overwrites). `tests/test_deletions.py` lists the two new file operations. The live tests now look in `jds_done/`.

## 5aa. Round 12 — the write folder, tests that never read your settings, stale answers (2026-10-21) — **done** (offline tests only; no live form, no real model, no real data)

1. **`paths.applications_auto`.** `paths.applications` is the user's own folder of past submissions and is READ only. The pipeline writes its archive (`apply/archive.py`:
   `preflight`, `record_application`) only to `paths.applications_auto`; both are read for the duplicate check (`autogate`, `discover.Known`), the `discover --ats` company
   list (`seeds.archive_names`), `autoapply.default_history` and `outcomes --import-archive`. `config.Settings.archive_roots()` lists both; `Settings.path("applications_auto")`
   falls back to the built-in `data/applications_auto` when the settings file has no such key (`config.DEFAULT_PATHS`), so an old settings file can never send a write to the
   user's folder. `preflight` refuses an `applications_auto` inside or around `applications`. `live` snapshots both folders around each job: one new Company/Role folder may
   appear in `applications_auto`; any change at all in `applications` stops the session (`EXIT_ARCHIVE`). `careerops init` creates `applications_auto`, never `applications`.
   `--mock` redirects `applications_auto` under `data/mock/` even when the key is absent. `config/settings.yaml` is the user's: add `applications_auto: data/applications_auto`
   there by hand (the code works without it).
2. **Tests never read the real `config/settings.yaml`.** `tests/conftest.py` points `CAREEROPS_SETTINGS` (read by `config.load_settings` when no path is given) at
   `tests/fixtures/config/settings.yaml` (a copy; `apply.mode: assist` explicit) and its audit hook fails any test that opens the real file, even if it swallows the error
   (`tests/test_no_real_settings.py`). The two tests that failed when `apply.mode: auto` was set now test the built-in default on a settings file with no `apply` section, and
   the `world` fixture of `tests/test_live_cli.py` writes `apply.mode: assist` itself. A new settings key must be added to the test copy by hand.
3. **Free-text claims and stale answers** (`apply/freetext.py`). Guard: in a "why" answer a sentence that says what the user did or used ("I used X", "my experience") is checked
   against the fact bank, the chosen entries, the company and the title, never the job ad (before this, a tool outside the taxonomy that only the ad names passed). A library
   record keeps a sha256 of each fact-bank entry (with its bullet) and motivation it was built from (`Record.digests`); `Library.find` skips a record whose source changed, was
   removed, or whose digest was never recorded (lines written before this round are asked about once more). The ad and the hook are the job's own and are not digested.
   Tests: `tests/apply/test_freetext.py` (+17), `tests/apply/test_applications_auto.py`, `tests/test_no_real_settings.py`.

## 5ag. Round 18 — ATS bands, a four-bullet letter, `l` with feedback, `e` on a letter, token visibility, submit failures and the hand-off, Lever free text (2026-10-22) — **done** (offline tests; Chromium fixture forms, MockLLM; never run on the real data, the real model or a real form)

1. **ATS bands** (`careerops/bands.py`, settings `apply.bands: {auto: 70, review: 50, rescue_ceiling: 60}`). After the CV is built, `_tailor` classifies the strict CV score S and the
   claimable ceiling C (`ats.scores(cv_text, ...)` with no letter): `auto` (S >= 70), `review` (50 <= S < 70), `rescue` (S < 50, C >= 60: flag `CV under-uses the fact bank (strict S,
   ceiling C)`), `skip` (S < 50, C < 60). The letter is drafted AFTER that check and only when the outcome is not `skip` (a skipped job gets a letter dict with status `off` and the
   reason `ATS S < 50: no letter was drafted`; no model call, no PDF). `report["band"]` holds the verdict. `cli.record_tailoring` stores it: status `skipped (ats)` (reason `ATS S < 50`) or
   `needs_review` / `letter_pending` plus the flag. `live` ends a `skipped (ats)` job in `_ats_skip` (prints `SKIPPED (ATS 32 < 50): Valarian | MLOps Engineer`; the summary says `Skipped by
   the ATS band: N`), files its JD in `jds_done/skipped/` with `<name>.reason.txt` (`Live._file_ats_skip`; only that file moves, copies of the job are not touched) and never asks, reviews or
   opens a form. `careerops retriage --ats [--apply]` (`cli.retriage_ats`): every `skipped (ats)` job is tailored again with `frozen_for` keywords; a dry run goes to a scratch folder under
   `paths.tmp` (a `TemporaryDirectory`, nothing in the database or the inbox changes); `--apply` keeps the tailoring (`record_tailoring`), and a job that now reaches the review band or the
   rescue line goes back to `needs_review` with its JD back in the inbox and its `live/state.json` entry reopened (`decided.bring_back`). `autogate`'s `min_ats_strict` is unchanged
   (70: the auto gates still decide on the same S). Code never lowers a band.
2. **Letter evidence and length** (`company_letter.py`). `requirement_groups`: the ad's heaviest keywords first, each with the two best bullets of the CV (`tailor.ranked_letter_bullets`,
   never the hook's proof bullet, no bullet twice); a requirement with only one bullet left takes it (second pass). `Plan.groups` / `Plan.evidence` (four bullets, flat) / `Plan.reqs` (one
   requirement per paragraph). The prompt shows `P3 EVIDENCE (requirement: X; the two best bullets for it)` and `P4 EVIDENCE ...`. `letter.company_words: [330, 420]` (`word_range`; the
   model is asked for the middle, `word_cap` = the top or `scoring.letter_max_words_company`, never over 420; a scalar or anything else is refused with `letter.company_words must be
   [low, high]`). The letter dict carries `accepted: [lo, cap]` and the review screen prints it.
3. **User guard scope.** `grounding.bank_text(fb)` (every string of `fact_bank.yaml` except `deny_terms`) lets a capability or skill word through `grounding_problems(..., bank=...)`; names,
   sectors, clients, numbers, the tool/organisation check, the cross-bullet link check (`claim_problems`) and the company guard on P2 are unchanged. `company_letter.filler_problems` rejects
   robust, seamless, cutting-edge, passionate, leverage and synergy (unless a chosen bullet says the word).
4. **`l` with feedback** (`tailoring/letter_feedback.py`, `review/letter_edit.py`). `l` on a company letter asks `What should change? (enter = just retry)`; the answer is appended to
   `paths.letter_feedback` (`data/letters/feedback.jsonl`: job id, text, date; also a built-in default in `config.DEFAULT_PATHS`). `make(..., revise=...)` gives every draft of that `l` the
   previous draft (`YOUR PREVIOUS DRAFT`), every rejection rewritten by `letter_feedback.instruction` (`remove 'robust'`, `the letter is 288 words, 42 short of the 330 minimum: use more
   of the detail of bullet p1a`, ...) and the feedback word for word; the redraft inside one call is told the same way. At most `ATTEMPTS` = 3 drafts per `l`; a letter that fails keeps its
   last draft (`draft_body`, `opening`, `closing`) for the next `l`.
5. **`e` on a letter.** In company mode `e` asks `Edit the [c]v or the [l]etter? (enter = cv)`. `letter_edit.edit_letter` opens the five paragraphs in `$EDITOR` (`nano` by default), runs
   `company_letter.edit_problems` (the same `check`) and installs the text (`install_edited`, `tailor.install_letter` / `save_report`) when it passes; a failing text lists its reasons and
   is installed only on `use anyway` typed in full; Enter edits again, `discard` keeps the old letter; a text that is not five paragraphs is never installed.
6. **Token visibility** (`llm/usage.py`). The router files each answered call under (day, task, model) in the new table `llm_task_usage` (`DB.llm_task_add` / `llm_task_today`). After
   tailoring, after `l` and at the end of `live` `usage.report` prints the day's `cover_letter` tokens per model and a `WARNING` for every provider or model whose `tpd` has less than
   `llm.warn_budget_below` (0.2) left.
7. **Submit failures and the hand-off** (`apply/failures.py`, `apply/handoff.py`, `autoapply.run`, `assist.assist`, `live.py`). Four kinds are counted per job in `paths.live/submit_failures.csv`
   (time, job id, host, kind, detail): verification error, field LOST after the re-fill, upload refused (`verify.upload_refused` reads the visible error containers), FORM NOT FOUND. The
   second failure (`apply.max_submit_failures`) offers `[h] hand off`; a job with two failures, a host in `apply.handoff_hosts` (`jobs.lever.co`) and a host that handed a job over before go
   straight to `Handoff.run` with no browser (`Failures.gate`, `Handoff.why`): never a third attempt. `Handoff.run` opens the URL (`explorer.exe`), prints the CV and letter paths, writes
   `answers_to_paste.txt` (now with a QUICK ANSWERS block: salary and notice) and copies the first answer (the full name) with `clip.exe` (`copy_to_clipboard`). `AutoResult.handed_off` and the
   assist's `handoff` key make `live` end the job `handed_off` (finished: its JD goes to `done/`); `mark-applied` has no status gate and works as before. Nothing here fills, clicks, signs in
   to anything or changes a profile, user agent or proxy.
8. **Free text on Lever** (`apply/filler.py`). `LABEL_JS` reads the text ABOVE a textarea that has no label, no `aria-label` and no `aria-labelledby` (Lever's `.application-label`), so the
   free-text drafter gets the real question instead of the placeholder "Type your response". `filler.readable_question` stops a required text box whose label says nothing (a prompt such as
   "Type your response", or a field name) with a printed `free text: the question of this required box cannot be read ...` and `ctx.stop(...)`, never a silent skip.
9. **Tests** (+196 new): `tests/apply/test_bands.py` (29), `test_bands_retriage.py` (9), `test_bands_pipeline.py` (6, xelatex), `test_submit_failures.py` (29), `test_submit_failures_flow.py` (7);
   `tests/tailoring/test_letter_evidence.py` (49), `test_letter_feedback.py` (14), `test_letter_edit.py` (18); `tests/review/test_letter_review_keys.py` (10); `tests/llm/test_usage.py` (12);
   `tests/test_autoapply_lever_freetext.py` (13, Chromium; fixture `tests/fixtures/apply/lever_form.html`). The old `letter.company_words` scalar tests were replaced by the range tests; the
   company-letter tests build P3/P4 from the first bullet of each group; `test_letter_assembled.py` and `test_golden_anaplan.py` (hook-mode letters on a MockLLM CV that sits under the ATS bands)
   set permissive bands, and `test_letter_format.py` expects the range. Full suite, 8 shards by file: 3,814 passed, 0 failed. **Unverified:** a live `careerops live --dry-run --max 3` on the real data and a real Lever form (CLAUDE.md forbids the
   first from a task; the second needs a live site); `clip.exe` and `explorer.exe` are faked in the tests.


## 5ah. Round 19 — the gap report, typed facts and saving them (2026-10-23) — **done** (offline tests; MockLLM; the live dry run on the real data is yours to run)

What and why: a job in the review band (50 <= strict S < 70, or the rescue) used to get a bare "ATS ... ceiling ..." header and the user could only
approve, edit or skip it. Now the review screen says exactly which keywords are missing, what each is worth, which the fact bank already backs and
which need a new fact, and the user can act on it.

1. **Gap report** (`review/gaps.py`, `workflow.summary(..., fb=)`, `gap_lines`). Header `ATS strict S | ceiling C | to reach 70 you need +D points`; a row
   per JD keyword not in `rep["ats"]["hits"]` with w (required 2 / preferred 1, as scored) and g = 100 w / sum(w) (rounded half up: 1.25 prints 1.3),
   largest first (a tie keeps the JD's order); `claimable` (`rep["claimable"]`: the keywords the bank backs) names the skills (`fb.skills`) and bullets (text
   + `terms:`, the ceiling's own matcher with keyword-group synonyms) and why each bullet is not on the CV (`page full` = `fill.rejected_overflow`,
   `rank #n` from `rep["ranking"]`, `rank, <id> replaces it` from `replaces:`, `guard`: it is on the CV but only its `terms:` say it); `gap`: no evidence.
   `claimable total` = C - S, `gap total` = 100 - C, so they add up to 100 - S (a test on a fixture). A deny-listed term is a gap marked
   `deny-listed`, never asked about. `gaps.applies` = band outcome review or rescue (a report without `band` is judged by its numbers). The later
   "Possible evidence" block is part of the report; "Genuine gaps" is not printed twice.
2. **Choices** (`review_job`, `GAP_MENU`). `a` approve, `s` returns `skipped (user)` (live: DB status `skipped`, reason `skipped (user)`, result status
   `skipped (user)`, JD to `jds_done/skipped/`; `careerops review`: `review.skip_user`), `r` re-tailor, `f` add facts, `o`, `q`; the old `e l n w` keys
   still work and are listed on an "also" line. `r` means re-tailor here, not reject. Both re-tailorings call `retailor(job id, the job's bank, keywords)`
   (`cli.retailor_hook` -> `tailor_jobs(..., boost=)` -> `tailor_job(..., boost=)`; the job's frozen keywords are reused, so S and C compare), then print
   `re-tailored: ATS strict A -> B, ceiling C -> D` and the new screen. A re-tailor that lands under the bands, fails or whose letter is pending never skips
   the job on its own: it stays `needs_review` (a pending letter ends the session as `quit`).
3. **`boost`** (`tailor.FORCE_FACTOR` = 4, `_keyword_boost(kws, fixable, factor)`): the first attempt ranks the named keywords at 4x weight; only the
   ranking sees it, `report["keywords"]` and the score keep the JD's weights. `report["boosted"]` names them.
4. **Typed facts** (`review/addfacts.py`, `tailoring/provisional.py`, `paths.facts_proposed` = `data/facts/proposed.yaml`, also a built-in default in
   `config.DEFAULT_PATHS` and redirected by `--mock`). `f` asks `<kw> (+g): have you used it? Describe it in one line (where, what, result), or press
   enter to skip. ` for the top 5 askable gaps (none deny-listed, none that already has a fact for this job), then which role or project (a number or
   an id). An empty answer stores nothing; a sentence naming a deny-listed tool is refused; one that does not contain the keyword is kept with a note
   (the ATS would not count it). Entry = `{keyword, text (as typed), target, job_id, date, status: provisional|declined|saved, saved_as}`.
   `provisional.augment(fb, facts_of_this_job)` returns a COPY of the bank with each fact added as the last bullet of its target (`prov<n>`,
   `provisional: true`, `verbatim: true`, `terms: [keyword]`); only `Store.for_job(job id)` feeds it, so another job's bank never has the fact and the claim
   guard (which checks against the bullets of the bank it is given) has no such source there. `_tailor` forces every provisional bullet into the
   selection (`force_provisional`) and `_shrink` never drops it; `report["provisional"]` = `[{id, keyword, text, on_cv}]`, and the review screen
   labels the ones on the CV `[provisional]`. The free-text answers (`apply/freetext.py`) and the auto-apply gate are given the real bank, so a
   provisional fact is never a source for them.
5. **Saving** (`careerops/factbank_append.py`, `review/factsave.py`, `Hooks.save_facts`, `Live._offer_facts`, `cmd_mark_applied`). `append_fact(path, target,
   text, keyword, confirmed="y")` is the only writer of the fact bank: anything but `y` raises `NotConfirmed` before a file is touched; the old bytes go to
   `config/profile/backups/fact_bank.<stamp>.yaml` first (`xb`, never over an existing file; `-2` for a second in the same second); the block is inserted
   as text after the entry's last bullet (file newline style kept, id = the entry's own prefix + highest number + 1, text as a JSON/YAML double-quoted
   scalar); the result must load and have no NEW problem (`problems`: duplicate ids, a deny-listed term in a bullet, in its terms or in `skills.core`) or
   the old bytes are written back and `AppendError` says why (the backup stays). `tests/test_fact_bank_single_writer.py` greps the package: no other
   function that names the fact bank also writes a file, and `append_fact` is called only from `review/factsave.py` with `confirmed=`. `factsave.offer`
   asks `Add to fact_bank.yaml under <id>: <title>, <org>? "<text>" [y/N/e(dit)]` for each fact the CV used, after `mark-applied`, and at the end of a
   job that was NOT sent (`ready_not_submitted`, hand-off, skip, dry run), saying `NOT SENT: this CV was not sent (...)`; `N` marks it `declined`; it
   prints what was added and the backup path, and refreshes the in-memory bank. A mock run says it keeps them provisional.
6. **Tests** (+115): `tests/review/test_gap_report.py` (14), `test_gap_review.py` (20), `test_factsave.py` (22); `tests/test_factbank_append.py` (27),
   `tests/test_fact_bank_single_writer.py` (3); `tests/tailoring/test_provisional.py` (14), `test_gap_tailoring.py` (7, xelatex); `tests/apply/test_live_facts.py`
   (8). `tests/test_deletions.py` and `tests/apply/test_safe_io.py` list `factbank_append._write_atomic`'s `os.replace`. **Unverified:** a real
   `careerops live --dry-run --max 3` on the real data and the real model (CLAUDE.md forbids running it from a task); the same screen was produced
   on the mock root with a scratch JD (`live --mock`, MockLLM, the real bank read only: its sha256 was the same before and after).


## 5ai. Round 20 — the form-question loop (23 Oct; the task file calls it "Round 17") — **done** (offline tests; Chromium fixture forms, MockLLM; never run on a real form or the real model)

Evidence: on the 23 Oct ElevenLabs Ashby form three OPTIONAL text areas ("Describe an agent or LLM-powered system you have shipped...", "Tell us about an automation or agent you took
from prototype to production...", "Describe a project you built with non-engineering stakeholders...") were never looked at: only REQUIRED free-text questions reached `apply/freetext.py`, the
assist printed `YOU ANSWER: []`. Separately Ashby answered a submit from the automated browser with "flagged as possible spam".

1. **Scanner** (`apply/formloop.py: scan`, `table`). `FormLoop.begin(page)` runs before any file or field is touched and prints `FORM FIELDS (n)`: every visible input, textarea, select, radio group
   and check-box group (a group with one name is one field; a file input counts even when hidden), required or not, with `type`, `req`, `maxlength`, label and options. The label is the `<label>`
   (`el.labels`), `aria-labelledby`, `aria-label`, else the nearest text before the field (siblings of the field, then of each ancestor, stopping at another field), else placeholder / name / id.
   After the run it prints `FORM FIELDS AFTER FILLING` with a status per field (FILLED, PRE-FILLED, LIBRARY, NEW DRAFT; WEAK EVIDENCE: ..., APPROVED, FILE, BLANK, BLANK (required), or why it was skipped).
   The word is BLANK, not EMPTY: tests assert that EMPTY is absent for a "left unchecked (answer: No)" box.
2. **Loop** (`FormLoop.run`, `work`). Called by `assist.fill_page` and `autoapply.fill_form` after their own field pass (the per-element body of each is now a nested `fill_element`, also handed to the
   loop as `handle_new`). `filler.fill_field` no longer drafts: with `Ctx.loop` set a free-text field (textarea, or a text box with `maxlength` > 100) that `answers.yaml` does not answer is
   left to the loop. Per field still empty: observe -> classify (`freetext.classify`) -> retrieve (`answers.yaml`; the answer library at similarity >= 0.85, same class; the fact bank and the job
   ad through `FreeText.plan`) -> draft -> check (the existing guard) -> fill -> read back, up to `MAX_PASSES` = 3 passes (a pass that fails the guard, or whose text the form did not keep,
   tells the next one why); then the form is scanned again, and a field that appeared (`phase: appeared after a fill`) goes to `handle_new` (the caller's own routine) and, if free text, to the loop.
   An OPTIONAL field of class `other` ("anything else?") is skipped (`skip` in the log); a REQUIRED one is asked of the person as before; an optional field no draft passed for stays empty.
   With no `FreeText` (no model) a required free-text box goes through `filler.decide` (answers.yaml, else the person) and an optional one is skipped.
3. **Freetext split** (`apply/freetext.py`). `FreeText.propose(question, label, maxlength, problems=, tries=)` -> `Proposal` does one pass (reuse, plan, draft, guard) and asks nobody;
   `FreeText.review(proposal, strict, on_text=)` is the old `[y] use · [e] edit · [r] redraft · [s] I'll write it` menu and the only thing that stores an answer; `answer()` is `propose` + `review`
   (same prompts and library writes as before). `similarity` also compares the FIRST SENTENCE of a question that goes on (>= 3 meaningful words each side) and `powered / based / driven / enabled`
   are stop words, so "Describe an agent or LLM-powered system you have shipped. What did it do?" matches the seeded Limbic question (1.0). `Plan.fallback`: a question that overlaps no fact-bank
   entry (and has no hook proof) is answered from the closest entry (`order[0]`) instead of being given up.
4. **Weak evidence** (`freetext.asks`, `FreeText.unsupported`, `mentions`, `Guard.problems(missing=)`). `asks(question)` is what a question asks for beyond its kind: the words after with / for /
   using / in / from / ... up to the next small word, a hyphenated compound ("non-engineering", but not "LLM-powered"), a name written with a capital inside the sentence. `unsupported` keeps the
   terms the WHOLE fact bank (bullets, situation, results, stack, skills) never mentions (word-prefix match, so "stakeholders" ~ "stakeholder"). They go to the model as `not_in_bank`, and the guard
   rejects a draft that mentions one (`claims 'x', which no fact-bank entry mentions`). The proposal carries `WEAK EVIDENCE: <asked> vs <entry ids>` (printed in the table, in the `[y]` screen and in
   `answers_to_paste.txt`). This is a heuristic on wording, not understanding: a question phrased without those cues is not flagged, and a flagged term the bank covers under another word is a
   false alarm the person sees and ignores.
5. **Approval.** A draft is filled at once and listed as NEW DRAFT; after the loop `FormLoop.approve` runs `review` for each. `y` / an edit / a typed answer is stored in the library (never
   before) and the filled-list entry becomes `free text (approved|edited|written)`. A library answer is `LIBRARY`: no draft, no question. Unapproved: the gated auto-apply raises `NeedsUser("new draft not
   approved (nothing is submitted): ...")` before the screenshot and the submit; the assist leaves the text in the field outlined orange and lists `<field> (NEW DRAFT: check it)` under `YOU ANSWER`.
   The draft is NOT removed from the field when the run stops: the person who takes over sees it.
6. **Log.** `formloop.jsonl` in the job's output folder (`out_dir`, else the folder of the CV), append-only, one object per step: `observe`, `classify`, `retrieve`, `draft`, `check`, `fill`, `verify`,
   `skip`, `give_up`, `approve`, `status`.
7. **Spam flags** (`formfind.SPAM_FLAG`, `spam_reason`, `blocker`; `failures.SPAM_FLAG`). "flagged as possible spam", "possible / potential / suspected spam", "couldn't / could not / unable to submit your
   application" is a `blocker`: after the click the auto-apply hands the job over at once (`autoapply.run` already did so for any blocker). `failures.kind_of` gives it its own kind `spam_flag`,
   which `Failures.record` writes to `submit_failures.csv` (with the host) but `count` / `kinds` / `gate` ignore: it is not one of the two allowed failures. `Handoff.run` records the host in
   `handoff_hosts.json`, so later jobs there skip the browser; `jobs.ashbyhq.com` is NOT added to `apply.handoff_hosts` (your file). Nothing is refreshed or resubmitted: the button is pressed once.
8. **Hand-off text.** `Handoff.run(..., drafts=)` / `paste_text(..., drafts)`: `answers_to_paste.txt` gains `FREE-TEXT ANSWERS DRAFTED FOR THIS FORM` (question, answer, `[NEW DRAFT]` or
   `[approved by you]`, `[WEAK EVIDENCE: ...]`), skipping an answer that is already in the approved-for-this-job section. Both callers pass `loop.paste_drafts()`. A job whose host goes straight to the
   hand-off never opens the form, so it has no drafts to list (only the answers you approved for it).
9. **Tests** (`tests/apply/test_formloop.py`, 27; fixture `tests/fixtures/apply/ashby_elevenlabs.html`, the ElevenLabs layout: system fields, three optional text areas, a field that appears after
   the first answer, `?spam`). Changed on purpose: `tests/test_autoapply_freetext.py` (the "no y" test now expects the draft in the field, not stored, and `new draft not approved`),
   `tests/test_autoapply_lever_freetext.py` (its stub offers `propose` / `review`), `tests/apply/test_assist_visa.py` (the table lists "Salary"; the check is that no CHECK line does),
   `tests/test_autoapply.py` (`Run.run(failures=)`). **Unverified:** a live Ashby / Lever form and a real model: drafts from a real model may fail the guard more often than MockLLM's, and then the
   person is asked (required) or the optional field stays empty; the wording of the real ElevenLabs questions past their first sentence is unseen (library matching uses the first sentence as well).


## 5aj. Phase A — apply reliability (27 Oct) — **done** (offline tests; Chromium fixture forms, fake pages; never run on a real form, a real model or the real data)

The task file's Phase A (A1 to A5). The reference project `~/ref/career-ops` was not present on this machine, so nothing was copied from it (no `NOTICE.md` entry is needed); every list and
pattern below is this repo's own.

1. **A1 — the real form.** `apply/ats_vendor.py`: `vendor_of(url)` (greenhouse, lever, ashby, workday for myworkdayjobs / myworkdaysite, icims, successfactors for successfactors.com / .eu /
   jobs2web.com, smartrecruiters, workable, bamboohr, taleo, oracle for `oraclecloud*.com`; any other host returns the host itself) and `direct_url(url, kind=, job=, tokens=)`: a Greenhouse job on a company
   domain (`gh_jid=` in the address) opens `https://job-boards.greenhouse.io/<token>/jobs/<id>`; the token is the job's own board (its `board`, or its id `greenhouse:<token>:<posting>`) or a known board that
   the company domain or name spells (`tokens=`); with none the page is left as it is. Lever `/apply`, Ashby `/application` as before. `formfind.form_url` / `open_form` call it.
2. **A2 — the form agent** (`apply/formagent.py`, `apply/submit_guard.py`). Snapshot: every old `data-co-ref` stripped, the interactive elements tagged `e<epoch>-<n>`, their facts read in the page (tag,
   authored type, role, in a form, aria-label, aria-labelledby text, placeholder, text, value, name, title, alt, id, data-testid). `submit_guard.submit_reason(facts)` is the one function the snapshot and
   the executor share: it tests the shapes (`type=submit`, `input type=image`, a `<button>` with no type inside a `<form>`) and EVERY name source separately against a multilingual submit vocabulary
   (submit, send, finish, confirm, absenden, envoyer, enviar, 提交, 送信 ...); an "Apply" word is a submit only inside a form (outside it is the way in that `reach` clicks). A submit control is listed
   `[--] submit "<label>" (the human or the gates submit)` with no ref. Planner actions: `click, type, choose, upload, reached_form, done, need_user` (there is no submit action; `parse_action` turns anything else into
   `need_user`). The executor refuses a ref outside this turn's set, re-reads the element and refuses if it now classifies as submit or changed kind, accepts a value only from `ValueBank` (applicant.yaml,
   answers.yaml, the visa plan, equal-opportunities answers, approved free text) and a file only if it is this job's CV or letter (and the CV only into a resume field). Goals `reach` (click Apply and
   interstitials until Name, Email and Resume are visible) and `fill` (every page, Next / Continue between pages, then `done`); 40 steps per job, 8 per page, every turn in `formagent.jsonl`; page text that gives
   orders is logged as an anomaly; a bot check, a spam flag or a Google / Microsoft sign-in raises `HandOff`. `RulePlanner` decides without a model; `LLMPlanner(ask)` asks one (not wired to the router: nothing
   calls it yet). A free-text question goes to `FreeText.answer` (drafted from the fact bank, the person approves), nowhere else.
   **Order inside `assist`:** the direct ATS address, then the existing filler for Greenhouse / Lever / Ashby, then the form agent when no form is found there (`reach`, then the existing filler on the page it
   reached) or for any other vendor (`reach` then `fill`; the report lists the submit controls it did not press). A page with no host (a file) keeps the old path.
   **Limits, on purpose:** a typeless `<button>` or any `type=submit` button is a submit even when it says "Next", so a multi-page form whose Next is `type=submit` stops there for the human; the agent never ticks a
   check box; it works in the main frame only and does not follow a pop-up window.
3. **A3 — per-ATS quirks.** Lever: `filler.lever_page(page)`; on a Lever host no radio or check box is set by `filler.choice`, the assist's visa and equal-opportunities paths or the auto-apply's visa path:
   they are listed under YOU ANSWER with the recommended answer (`report["recommended"]`), and the auto-apply stops at a required one that is still empty. Ashby: `assist` prints "Ashby merges candidates by
   email; an earlier application to <company> exists." when `history(company)` is true; a spam flag already hands the page over. React-select drop-downs: the option text is typed 50 ms apart, the list read again, the
   option picked by its visible label (`FormAgent._combobox`); a long `<select>` is chosen by label or value without listing it. A box that will not keep its text: asked for again right before the fill, then pasted
   with clip.exe + Ctrl+V, then written to `answers_to_paste.txt` (`assist.write_unpasted`).
4. **A4 — knock-out pre-scan** (`apply/knockout.py`). After `loop.begin(page)` and before any upload or draft: sponsorship / work authorisation (a "now or in the future" question is Yes when either
   `sponsorship_now` or `sponsorship_future` is Yes: `assist.sponsorship_answer`; `classify_visa` itself is unchanged), years of experience (`applicant.yaml years_experience`, else the fact bank's dated roles),
   degree (the fact bank's `education`; a missing or empty section is never evidence of lacking it), salary floor, on-site / relocation, clearance. A hit prints
   `KNOCK-OUT: "<question>" → your answer "<answer>" may auto-reject. [c]ontinue / [s]kip job`; the assist asks, `s` ends the job (`r["skipped"]`, confirmation `x`); the auto-apply always raises `NeedsUser`
   (the human decides). **Consequence:** with `sponsorship_future: true` in your visa block, every "now or in the future" question is a knock-out, so such a job never auto-submits.
5. **A5 — attestations** (`apply/attest.py`). "I acknowledge ... privacy notice", "I confirm all answers are true", terms, consent: never ticked by any code, whatever answers.yaml says; outlined magenta, printed under
   `ATTESTATIONS`, and a required one still empty stops the auto-apply (`still_empty`). **Changed on purpose:** `answers.yaml` entries such as `privacy_consent: Yes` no longer tick anything.
   Next to an upload the PDF is uploaded; "Enter manually", Dropbox and Attach are never pressed.
6. **Tests** (new, 8 files in `tests/apply/`): `test_ats_vendor`, `test_submit_guard`, `test_formagent`, `test_assist_agent`, `test_attestations`, `test_lever_choices`, `test_knockout`, `test_knockout_flow`;
   fixtures `agent_site.html`, `attest_form.html`, `lever_checks.html`, `knock_form.html`, `react_select.html`. Changed on purpose: `tests/test_autoapply.py` (the world patches `knockout.scan` because its fixture asks the
   sponsorship question; the privacy checkbox moved behind `?attest` and has its own test; the "only autoapply clicks" scan allows `formagent.py`), `tests/fixtures/apply/auto_form.html`.
   **Unverified:** the Epic Games ML Engineer `--no-submit` run (a live form), a Lever job on the real site, the form agent on a real Workday / iCIMS / SuccessFactors page, and `LLMPlanner` with a real model.


## 5ak. Phase B — stop wasting tokens on dead or duplicate jobs (27 Oct) — **done** (offline tests: a fake `get`, fixture databases, no network, no model; never run against the live sites or the real data)

Every job now meets one gate before tailoring: `cli.tailor_jobs` calls `discovery/pretailor.py: run` first, so `tailor`, `run` and `live` share it. In order: blacklist, liveness, agency vs
direct, then the repost flags. A stop costs no token. Every question defaults to no (Enter and EOF are a no). `--mock` skips the network and the archive. The job's flags (JSON list on the jobs row)
carry `liveness uncertain: ...`, `possible repost (...)`, `evergreen?`, `blacklisted (you chose to go on)`, `same role via another channel (you chose this one)`; the gate rewrites its own flags
each time, never anyone else's.

1. **B1 — liveness** (`discovery/liveness.py`). `open | closed | uncertain`. Closed: HTTP 404 / 410; a redirect to a generic careers or search page (the final address names no posting while the
   requested one did: Greenhouse's `?error=true`, `/careers`, `/jobs/search?q=`); a long page with no job title (an `<h1>` or a JSON-LD JobPosting) and no apply path; an expired phrase. Phrases are
   normalised first (curly quotes straightened, accents stripped, whitespace collapsed): "no longer available", "no longer accepting applications", "this job has expired", "job posting has expired",
   "this role|position|job is no longer / closed" (not "closed-loop"), "<job noun> ... has been filled" (not "has been filled out / in / up"). A page with almost no text (a script shell) is uncertain, never
   closed. Requests go through the discovery `Fetcher`: robots.txt, one a second per host, a 401 / 403 / 429 backs the host off; LinkedIn, Indeed, CV-Library and the Workday hosts are not fetched at all
   (uncertain). Cache: `paths.discover/liveness.json`, 6 hours. Closed: status `closed`, the job's own inbox file moves to `jds_done/closed/` with `<name>.reason.txt`, no tailoring. Uncertain goes on with
   a flag. `discover --report` (and `--report` after a source run) prints `open N · closed N · uncertain N` for the inbox and lists the closed ones, checking at most 150 ads a run; `--no-liveness` skips it.
   The report moves nothing.
2. **B2 — repost / ghost signal** (`discovery/repost.py`). Information only: no score, no filter. `possible repost (n listings since <date>)`: same company family, same title identity (the SET of title words,
   gender tags dropped, so "Engineer - Munich" is not "Engineer - Berlin"), 2+ URLs, 2+ distinct first-seen dates inside 90 days, span over 0 days. `evergreen?`: posted date over 120 days old. Counts in
   `discover --report` come from the inbox files (First seen / Posted headers) and the jobs table.
3. **B3 — blacklist** (`discovery/blacklist.py`, `config/profile/blacklist.yaml`, beside the fact bank; yours, the module has no write call). Entries `{name, scope: company|domain, since, reason}`. Company: the
   name equals the job's company after folding case, punctuation, "(...)" and a trailing Ltd / plc / GmbH ("Acme" is not "Acme Recruitment"). Domain: the job's address or apply address is on the host or a
   subdomain, never a lookalike. A hit asks `<company> is on your blacklist since <date>: <reason>. Apply anyway? [y/N]`; no = status `skipped` and the file to `jds_done/skipped/`. A blacklist.yaml that cannot
   be read stops the job (nothing is tailored on the guess that nobody is blacklisted) and leaves it as it was. `tests/conftest.py` gained an autouse fixture that gives the gate an offline fetcher (every address answers with an open page) and a throwaway cache, so no existing test reaches a site.
4. **B4 — agency vs direct** (`discovery/channel.py`). The tracker (every job not skipped / rejected / closed / error) and the archive (`discover.archive_pairs`, read only) are checked for the same title
   identity at the same company under another channel: agency vs direct, or two agencies (the employer an agency ad names is read from "Our client, X", "on behalf of X", "Client: X"; two agencies that name
   nobody match on title and town). Found: the job stops and asks `[c]ontinue / [s]kip`; only c goes on; skip marks THIS job skipped (the other channel's job is left alone). An agency ad that hides the employer:
   `... does not name the employer ... Go on with this ad anyway? [y/N]`. Two ads of one company on one channel are a duplicate (apply/decided.py), not another channel.
5. **Tests** (new, 5 files in `tests/discovery/`): `test_liveness` (67: each pattern, "closed-loop" and "filled out" false positives, redirects, cache, Fetcher pacing, never-fetched hosts), `test_repost`,
   `test_blacklist`, `test_channel`, `test_pretailor` (the gate, `tailor_jobs`, the report). **Unverified:** the real sites' closed pages (a live Greenhouse / Lever / Ashby 404 or redirect, an SPA that shows
   "no longer available" only after its scripts run is uncertain here), the real archive's folder names against the channel check, and `discover --report` against a full inbox. **Known limits:** the
   employer behind an agency ad is only found when the ad says so in one of the three forms above; a blacklist.yaml error shows `tailoring did not run (budget?)` in `live` (the real reason is printed above it).


## 5al. Phase C — Workday coverage (27 Oct) — **done** (offline tests: a fake `get` / `post`, no network; never run against a real Workday tenant)

1. **C1 — discovery** (`discovery/workday.py`). `sources.workday: [{company, url}]` in settings.yaml, seeded with `[]` (and in the test copy); an entry may add `queries: [...]` (the `searchText`s,
   default `data scientist`, `machine learning`, `AI engineer`, `generative AI`) and `max_pages`. `parse_url` takes both host forms (`<tenant>.<wdN>.myworkdayjobs.com[/<locale>]/<site>` and
   `<wdN>.myworkdaysite.com[/<locale>]/recruiting/<tenant>/<site>`, no lookalikes) and `Site.api` is `https://<host>/wday/cxs/<tenant>/<site>/jobs`. The list is `POST {appliedFacets:{}, limit:20,
   offset:n, searchText}`; the offset runs until a short page; `max_pages` (10) list requests per tenant per run are shared by the searches. The list carries no description, so a posting that passes
   the role-family and UK-place filters (`BoardCache` hands `keep`) is read once more from `/wday/cxs/<tenant>/<site>/job/...` (at most 40 a run); a posting whose place is "2 Locations" is read first and
   judged by its real places. A board is `Board("workday", slug, company)`, the slug carrying host, tenant, site and locale (`~`-joined, safe in the cache file name `boards/workday-<slug>.json`),
   and it flows through `Discover.from_boards` like the three other ATS boards (same filters, same inbox ad, `apply_kind: workday`).
2. **Manners.** `discovery/http.py` gained `Fetcher(post=, allow_paths=)` and `request(method="POST", json=, follow=False)`. robots.txt of the tenant host is read before the first request (the Fetcher's own
   check, on the `/wday/cxs/...` path): a tenant that disallows `/wday/cxs/` raises `Blocked` and is skipped with a note, the other tenants still run (a tenant that disallows only `/refreshFacet/` is read).
   One request a second per host. `applykind.NEVER_FETCH` still lists the Workday hosts, so careers PAGES, alert resolution and the liveness check do not fetch them; only the `/wday/cxs/` prefix is
   allowed, and only the discover Fetcher built in `cli._discover_inbox` allows it.
3. **Posting time.** `workday.posted_on`: "Posted Today", "Posted Yesterday", "Posted N Days Ago" become the day; "Posted 30+ Days Ago" becomes the day 30 days back, which is freshness tier 3 whatever the
   real day was (a missing date would have fallen back to the first-seen day and made it tier 0). `freshness.SOURCES` trusts `workday.postedOn` for the age.
4. **Outages.** A 422, a redirect to `community.workday.com/maintenance-page` or `/wday/drs/outage`, or a reply that is not the job list raises `Transient`: a note in the run ("retried next run"), nothing is
   cached (no `boards/workday-*.json`), and it is never counted as zero jobs; a real empty list is zero jobs. A 404 says the address is wrong; a 5xx is a `FetchError`; a 401 / 403 / 429 backs the host off.
5. **Tests** (`tests/discovery/test_workday.py`, 80): URL parsing for both host forms and the lookalikes, `postedOn` mapping and its tiers, the request body, paging and the page cap, the shared budget,
   one request a second, postings and details, outages (each redirect form), robots refusal, the never-list with the CXS exception, the configuration, and discover end to end (inbox ads, an outage
   run, a refusing tenant next to a good one, the cache). **Unverified:** a real tenant (the exact `myworkdaysite.com` API path is assumed to be `https://<wdN>.myworkdaysite.com/wday/cxs/<tenant>/<site>/jobs`;
   the reply fields `jobPostings`, `externalPath`, `locationsText`, `postedOn`, `bulletFields`, `jobPostingInfo` are as documented by the pages that call it, not read from a live answer today).
   **Known limits:** Workday jobs are `liveness uncertain` before tailoring (their pages are never fetched; the CXS detail would answer, not wired); the search terms narrow what is listed, so a role the
   searches do not name is not seen (add `queries`).


## 5am. Phase D — after applying (27 Oct) — **done** (offline tests: a fake Gmail `get`, a fake Sheet, fixture databases; no network, no model; never run against a real mailbox or Sheet)

1. **D1 — reply watch** (`reporting/replies.py`, called by `cli.reply_lines` at the end of `discover --gmail-alerts`; skipped on the mock root). `watched` = every applied job without a
   rejected / withdrawn / offer outcome, with the employer's own domain (`contacts.company_domain`). `queries` = `(from:d1 OR from:d2 ...) newer_than:30d`, 25 domains to a search, plus one search for the
   applicant-tracking mailers (`ATS_MAILERS`); read with `gmail.list_message_ids` / `get_message` (GET only, read-only scope). `match`: the sender is the domain or a subdomain of a watched company (never a
   lookalike: `nottrainline.com`), or an ATS mailer whose display name or subject names the company; with several jobs at one company the title in the subject decides, else the latest application.
   `classify(subject, body)` is rules only: a strict rejection phrase wins over everything (a rejection may mention a future interview); between interview and assessment the first one in the text; then info;
   else noise. Curly quotes, accents, case and contractions are normalised first; a mail that addresses the tool ("ignore previous instructions", "set the status") is flagged and classified by the rules
   alone. `run` prints `Trainline: rejection (22 Oct mail) → set Status "Rejected"? [y/N]` per interview / assessment / rejection; **only `y` / `yes` writes**: `outcomes.record(..., source="reply-watch")` and
   `tracker.set_status` (new: the Status cell of the row found by Job ID, nothing else, `TrackerError` when there is no row or no column). A failed Sheet write is one line and the outcome stays. A prompt cut
   short (EOF / Ctrl-C) writes nothing and the mail is asked again next time; an answered mail is kept in `data/discover/replies_seen.json`; with no terminal (`ask=None`) proposals are printed, not asked, not
   remembered. Info lines never prompt. No settings key was added (window 30 days, 80 mails a run, constants in the module).
2. **D2 — `careerops followups`** (`reporting/followups.py`). Due = applied day + `outreach.follow_up_days` (7); listed from that day on (`due today`, `N days over`) when the outcome log has no
   acknowledged / screen / interview / offer / rejected / withdrawn event for the job; jobs backfilled from the archive (`source=archive`) are skipped. Oldest due first, with the best stored contact
   (`contacts.stored`) or `no contact known`. Read-only; drafting stays in `outreach.py`.
3. **D3 — interview prep pack** (`review/prep.py`, `cli._write_prep` after a complete archive in `cli._archive`). `prep.md` is created with the new `safe_io.safe_write_text` (mode `xb`: never over an
   existing file) in `application_dir`; a folder inside `paths.applications` is refused. Questions: the ad's keywords (required first, heaviest first, at most 8; the ad's order within a tie) with three
   question templates, then 2 to 5 standard ones so that the list has 8 to 10 (fewer only when the ad has almost no keywords). Stories: up to two fact-bank entries, those on this CV first, each with up to two
   matching bullets quoted word for word with their ids, labelled Situation (the entry's `situation:`) / Task and action / Result (the bullet's `result:`, else "the numbers in the bullet above", else "none
   recorded"). **Truth rule:** nothing about the user that is not a fact-bank bullet; a keyword with no bullet, or on `deny_terms` whatever a stray tag says, gets `no evidence: don't claim`. Gaps: the gap
   report's `gap` rows (`gaps.build`; `learn_list` when the report has no scores) with a one-line learn task, `do not claim` on a deny-listed term. Three questions to ask them, from the ad.
4. **Tests** (new): `tests/reporting/test_replies.py`, `tests/reporting/test_followups.py`, `tests/review/test_prep.py`, `tests/test_tracker_status.py`, `tests/apply/test_safe_write.py`, helper
   `tests/gmail_fakes.py`. **Unverified:** the classification rules on a real mailbox (hand-made mails only), Gmail's `from:` search on real senders, an ATS mailer's real display names, a real Sheet write.
   **Known limits:** a company whose domain cannot be told (its job link is on an ATS) is found only through an ATS mailer; a reply from a recruiter's personal address is not seen; `assessment` is logged as the
   outcome `screen` (the log has no assessment event).


## 5an. Phase E — honesty details (27 Oct) — **done** (offline tests, no model, no network)

1. **E1 — `user-cannot-confirm`** (`tailoring/provisional.py`, `review/addfacts.py`, `review/factsave.py`). A fourth status of a fact in `data/facts/proposed.yaml`, next to
   provisional / declined / saved. It is about the KEYWORD (compared lower case, one space between words), not the job, and permanent: `Store.mark_cannot_confirm` writes a record with no text
   (nothing was claimed) and marks every stored non-saved fact for the keyword; `asked_rows` never offers the keyword again on any job; `Store.add` refuses a fact for it; `Store.for_job`
   no longer returns a provisional fact for it, so tailoring cannot use one typed earlier. A `saved` fact stays saved (it is in the fact bank, which this code never edits). Two ways in: `n` at the
   review `f` question (the prompt now says so), and `c` at the post-job `Add to fact_bank.yaml ...?` prompt. `declined` is unchanged: that wording was not kept, and another job may ask again.
2. **E2 — importance tiers** (`review/tiers.py`, `review/gaps.py`, `review/workflow.py`). `tiers.classify(jd, keywords)` reads the ad's text: `stated` = a sentence with must-have wording
   (must / required / essential / mandatory / minimum / you will need ...) and no nice-to-have wording, outside a nice-to-have section; `structural` = not stated, but under a Requirements-type
   heading or said in more than one sentence; `inferred` = anything else, or no ad. A keyword is looked for by its own words (term and the ad's alternatives), never through keyword-group synonyms.
   `gaps.build(rep, fb, bands, jd=None)` gives every row a `tier` (shown as `[tier]` at the end of its line, with a one-line legend), ranks rows tier first and gain second, and `GapReport.top`
   is the first row unless it is inferred (the `<- top priority` mark). `workflow` passes the job's description at both call sites; a job without one (or `jd=None`) is all inferred, which keeps the
   old gain order. The tier does not change any score, band or gain.
3. **Tests** (new): `tests/review/test_cannot_confirm.py`, `tests/review/test_gap_tiers.py`; the prompt strings in `test_gap_review.py` and `test_factsave.py` were updated. **Known limits:** the
   section and must-have rules are English phrases tested on hand-made ads, not on a corpus of real ones; a keyword the ad only implies (a synonym) is inferred.


## 5ao. Phase F — fewer, better-matched applications (27 Oct) — **done** (offline tests: fixture ads in tmp folders, a fake register, a fake Sheet; no network, no model; never run on the real inbox)

1. **Settings** (`targeting:` in settings.yaml and the test copy; `careerops/targeting.py` reads it with the defaults and refuses a value that cannot be used, by name): `shortlist_size` 8, `min_triage_score` 3.5,
   `daily_max_applications` 5, `per_company_max_open` 2, `per_company_cooldown_days` 30, `max_posting_age_days` 60 (older needs T >= `OLD_POSTING_MIN_T` 4.5), `dealbreakers` [].
2. **T** (`discovery/triage.py`): the six factors and weights of the spec. Choices where the spec was silent: no date known -> Fr 3; no required keyword found -> E 3; no band or no floor -> P 3; floor = the `answer_numeric` of
   the `salary_expectation` entry of answers.yaml; pay figures under £15,000 (a bonus) or over £1,000,000, and day / hour rates, are not a band; "required" = the keywords under the ad's Requirements heading (the taxonomy scan's
   full weight), and in an ad without one every keyword outside a nice-to-have, as the scorer reads it; E matches the term and the ad's own alternatives, never keyword-group synonyms. `Triage.text()` is
   `T 4.1 = A5 E3.8 V3.5 L5 Fr4 P3`. The score of every scored ad is stored in the new table `triage_scores` (keyed by the duplicate key of company + title) and shown in the `live` inbox listing.
3. **Hard stops** (`triage.disqualify`, `discovery/dealbreakers.py`): liveness (a callable, asked lazily by `shortlist` for the ads that would be offered) -> blacklist -> hidden-employer agency ad / same role under
   another channel -> level exclude list -> dealbreakers. A dealbreaker needs an ad sentence: known ideas by their wording (negation before the match, or optional wording, is not a hit), anything else as words; years
   and pay floors need a stated number / band. The hit is `DEALBREAKER "<phrase>" ← "<sentence>"`.
4. **`careerops shortlist`** (`discovery/shortlist.py`, `cli.cmd_shortlist`): build (score, stop, filter, order by T then freshness, company limits, lazy liveness) -> file the disqualified -> print -> choices. Approved ads move to
   `data/jds/approved/<date>/`; `Live(require_approval=True)` (the command's default) reads only those; `live --include-unapproved` is refused with `--auto`. `--mock` uses the isolated root and no network.
5. **Caps** (`apply/volume.py`, called from `Live._volume_block` right before `_apply` and the auto-fill, and at the start of a session): daily count of jobs with today's `applied_at`; per company, open = no reply
   (acknowledged / screen / interview / offer / rejected / withdrawn) and applied within `outcomes.NO_RESPONSE_DAYS` (28), database jobs plus archive role folders no database job accounts for (by folder date);
   cooldown = days since a rejection or withdrawal at the company. Reading the tracker Sheet is not needed: the Sheet mirrors the database.
6. **`careerops patterns`** (`reporting/patterns.py`): from the Sheet's Status column (Job ID, Status, Role family, Posting URL found by header text; T joined from `triage_scores`), else the outcome log; tallies and
   suggestions only (`MIN_BAND` 5 applications and a 20-point lead); no setting is ever written.
7. **Tests** (new): `tests/discovery/{test_triage,test_dealbreakers,test_shortlist}.py`, `tests/apply/test_volume.py`, `tests/reporting/test_patterns.py`, helper `tests/triage_fakes.py`; `tests/test_live_cli.py` now
   approves its ads into `approved/` and gained the two approval tests. **Unverified:** `shortlist` on the real inbox (never run on real data), liveness on real pages, the dealbreaker sentence rules on real ads (hand-made
   ones only), and the Sheet read of `patterns`. **Known limits:** a company whose name differs between the ad and your archive (a trading name) is not recognised; the cooldown starts from a rejection or withdrawal
   logged in the outcome log (the reply watch or `careerops outcome`), not from a Status typed only into the Sheet.
