# career-ops — implementation plan (sprint 1: M1–M4 + prompt upgrades)

Companion to [`PLAN.md`](PLAN.md) (what & why). This file is *how*: modules, signatures, schemas, algorithms, tests, acceptance.

**Conventions.** Python ≥3.10, stdlib + `requests/pyyaml/jinja2/openpyxl/playwright` only. Every module gets fixture-driven tests with **no network and no model** (`MockLLM`, monkeypatched HTTP). Every CLI step is idempotent (safe to re-run). Nothing reaches an employer without your click.

---

## 1. Your three prompts → pipeline stages

| Your prompt rule | Where it lives | How it's enforced |
|---|---|---|
| **Resume:** STAR rewrite, JD keywords woven in | `prompts.TAILOR_*` | Bullet shape = *Action verb → what → how (tools) → Result*. Situation/Task compress into the clause "for X / to solve Y". |
| Result part of STAR | `fact_bank.yaml` → new optional `result:` per bullet | The guard already blocks new numbers, so a Result can only come from `result:`. Bullets without one are listed in review as **"needs a result (yours to supply)"** — never invented. |
| Prioritise relevant, cut what doesn't earn its spot | `tailor._proposal` + `_shrink` | ✅ selection by relevance; shrink drops lowest-keyword bullets until `scoring.max_pages` (1, CV-2) pages. |
| ATS 70–85 | `scorer` + band loop | ✅ Keyword weights come from JD sections (`scorer.classify_heading`, M5 1d ✅): required ×2, preferred ×1, before any heading 1.5; benefits/perks/values/hiring-process/"About <company>" sections and hashtag lines are ignored; a "…is a plus" clause is preferred while the rest of its line stays required. |
| LLM provider errors (M5 1b ✅) | `llm/router.py`, `llm/check.py` | HTTP errors keep status + body (≤ 500 chars, keys redacted, no headers) in the `LLMError` and an `events` row (`stage='llm_error'`). 429: wait `Retry-After` (else 2/4/8 s, ≤ 3 retries; waits above `llm.max_retry_wait_s` are skipped) then fall through to the next provider. 400 with `response_format`: retried once without it; if JSON comes back the provider stops sending it and `careerops llm-check` says to set `json_mode: false`. `tailor_job` builds in `<job>.partial` and swaps it in only on success, so a failed re-run keeps the earlier output folder. |
| Check memory / ask before altering; no hallucination | fact bank = the memory; `faithfulness.check` | ✅ guard. New: keywords with *partial* evidence (stem match only in `terms`, not in text) become **review questions**: "Is it true you did X in bullet Y?" |
| "Add all required skills and let me know so I'll keep learning" | **Resolved as:** never added to the CV; written to a per-job and a global **learning list** | Adding an unevidenced skill *is* a hallucination, so the two instructions only reconcile this way. See §5. |
| Keep the structure | `templates/cv.tex.j2` | ✅ CV-2: the user's own one-page layout — Profile → Experience → Projects → Skills → Education → Certifications & Awards. |
| One A4 page (CV) and one page (letter) | `render.page_count` + `_shrink`; `careerops tailor/run` prints both page counts | ✅ CV-2 (`scoring.max_pages: 1`; was 2) |
| Output only the LaTeX | `data/outputs/<job>/CV.tex` (+ PDF) | Learning list and questions go to `report.json`/review, not into the CV. |
| **Cover letter:** company from JD, ask if missing | gates + new status `needs_company` | Agency/unnamed employer (e.g. Adzuna "Recruiter", JD says "our client") → held until you name it. |
| Hook, <250 words, no "I am writing to apply", specific CTA, human tone | `tailor._letter` | ✅ checks. New: hook must reference `company_hook` noun phrase; CTA must name a concrete next step (regex for call/chat/demo/walk-through + timeframe). |
| Letter ATS 70–85 | `scorer` on a **letter keyword set** | New: top-30 JD keywords by weight (your manual letter scorers used ~25–31; a letter can't carry a CV's 70). |
| **LinkedIn:** never-met [title] at [company], referral + team, small ask, direct, not sycophantic, banned opener | new `outreach.py` | See §6 — the 5-sentence cap doesn't fit LinkedIn's note limit, so two artefacts are generated. |

---

## 2. M1 — Register snapshots + diff

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
- Tailoring: CV headline = family headline (rendered under the name); `TAILOR_USER` carries
  `Preferred projects for a <headline> role (use if relevant to the JD): <ids>`; MockLLM breaks overlap ties with
  that list; `report.json` records `family`, `tier`, `headline`.
- Visibility: `status` tier × status table, Excel `Tier` / `Family` / `Ceiling (pre)` columns, review header
  `T1 · Data Scientist`.
- Tests: `tests/discovery/test_roles.py`, `tests/tailoring/test_queue_order.py`, `tests/llm/test_mock.py`,
  two in `tests/review/test_workflow.py`.

---

## 5c. CV-2 — the user's one-page CV and cover-letter format — **done**

- Templates `templates/cv.tex.j2`, `templates/cover_letter.tex.j2` are the user's own layouts; Jinja filters `urlesc` / `urlshow` (`render.env`) escape and shorten link URLs. `identity.address` (optional) is read by the letter template only.
- `settings.yaml`: `scoring.max_pages: 1`; `letter: {greeting, greeting_fallback, signoff}`. `tailor.letter_greeting` picks "Hi {company}{team} team" when the JD names a team (`KEYWORDS_SYSTEM` returns `team`; kept only if it appears verbatim in the JD), else "Dear {company} hiring team".
- `TAILOR_SYSTEM`: profile 1–2 sentences ≤ 45 words starting "<headline> with …"; exactly 2 projects (code cap 4 → 2); most recent experience 2–4 bullets, others 1–2; STAR-style rephrases. The profile is checked against profile_facts + skills + the bullets selected for this CV (still fact-bank only).
- `LETTER_SYSTEM`: 3–4 paragraphs, < 250 words; hook ≤ 45 words; two examples from EVIDENCE; optional ≤ 25-word sentence naming a **required** keyword the fact bank lacks (`GAPS`), only if there is one; CTA as a question. `GAPS` holds only keywords whose `config/taxonomy.txt` category is tool, platform, language or framework (`term | category`); broad skills (statistics, communication, research…) are never gaps, even when their literal term is absent from the fact bank (`report["genuine_gaps"]` still lists them for the review screen). `tailor._lack_problems` rejects any sentence matching `have not|haven't|never used|no experience` unless it names a `GAPS` term and no other tool/platform/language/framework. A deny-listed term may appear in the letter only when it is itself a required gap and the sentence says it is lacking ("I have not used X yet").
- Contact details (address, email, phone, links) never appear in a prompt: `tests/llm/test_prompt_privacy.py`.
- `careerops doctor` counts `# CONFIRM` markers like TODOs (`setup.fact_bank_open_items`).
- The user's header has no headline line, so the family headline (M4b) now reaches the page only as the first words of the profile.

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
