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
| Two A4 pages (CV, since 2026-10-06; `--one-page` for one) and one page (letter) | `render.page_count` + `render.page2_fill` + `tailor.fill_page2` + `_shrink`; `careerops tailor/run` prints both page counts and page-2 fill | ✅ §5d (`scoring.max_pages: 2`; CV-2 was 1) |
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

## 5d. Two full pages, outcome log, calibration fix round — **done** (2026-10-06; none of it run live)

**Two pages.** `templates/cv2.tex.j2` is the default (`cv.tex.j2` only with `--one-page`). `scoring.max_pages: 2`,
`min_fill_page2: 0.65` (the reference 2-page CV fills ~70% of page 2 and all experience bullets are already used, so a higher floor would need filler): `render.page2_fill` reads the rendered PDF (`pdftotext -bbox`, y of the lowest word on page 2 over
the text area, `CV2_MARGIN_PT` must match the template's geometry). `selection.select_cv(fb, kws, preferred, skills, layout)`
is greedy marginal gain with `Layout`s (`ONE_PAGE` 2-4 / 1-2 / 2 projects; `TWO_PAGE` from `scoring.cv_bullets`: 4-5 newest,
3-4 others, 3 projects x 2-3 bullets), skips a bullet whose token Jaccard with a chosen one is >= `bullet_dedupe_jaccard`
(0.6) and returns `project_bullets`. `selection.fill_order` lists unused bullets (experience and the chosen projects' bullets by
gain; bullets of a 4th project only with `cv_fill_extra_projects`, default false); `tailor.fill_page2` adds them in batches
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
