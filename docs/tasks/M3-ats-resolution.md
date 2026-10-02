# Task M3 — ATS resolution: slug probe, verification, manual queue, discover wiring

**Goal.** For each **in-scope** company from M2, find its public job board on Greenhouse, Lever or Ashby
without knowing its website, prove the board really belongs to that company, and feed verified boards into
`careerops discover`. Companies we can't resolve go to a manual queue where the user pastes a careers URL.
**Precision over recall:** a wrong board means applying to the wrong employer.

**Read first:** `CLAUDE.md`, `careerops/discovery/ats.py` (stub, `SIGNATURES`, `Hit`, `ResolveStats`),
`careerops/discovery/sources.py` (existing fetchers + `UA`), `careerops/discovery/register.py` (`normalise`),
`docs/IMPLEMENTATION.md §4`.

**Prerequisite for the live run (not for the tests):** M2 has been run for real, so `companies` has rows with
`sector IS NOT NULL AND ch_status = 'Active'`.

---

## Verified facts about the APIs (checked 2026-10-02 — still code defensively)

| ATS | Endpoint | Company name in response? | Location data |
|---|---|---|---|
| Greenhouse | `GET https://boards-api.greenhouse.io/v1/boards/{slug}` → board; `/v1/boards/{slug}/jobs` → jobs | **Yes** — board endpoint returns `name` | `jobs[].location.name` (free text) |
| Lever | `GET https://api.lever.co/v0/postings/{slug}?mode=json` (EU: `api.eu.lever.co`) | No | `categories.location` (free text); unknown slugs return `{"ok": false, "error": "Document not found"}` |
| Ashby | `GET https://api.ashbyhq.com/posting-api/job-board/{slug}` | No | `jobs[].address.postalAddress.addressCountry`, `location`, `isRemote` |

- Unknown-slug behaviour is **not documented** for Greenhouse or Ashby. Treat as "not found": HTTP 404, any 4xx, a JSON body with an `error` key, or a board with zero jobs **and** no name.
- Lever's `robots.txt` asks for a 1 s crawl delay — the rate limit below already respects it.

## What to build

### 1. `slug_candidates(register_name, ch_name=None) -> list[str]`
From `normalise(legal part of the name)` with tokens `t1 … tk` (repeat for `ch_name` if different), in this order, deduped, max 4:
`joined` (`t1t2…tk`), `hyphen` (`t1-t2-…-tk`), `first` (`t1`, only if `len(t1) ≥ 5` and `k > 1`), `first-two` (`t1t2`, only if `k > 2`).
Skip tokens that are pure stop words after normalisation (`and`, `of`, `the`, `group`, `holdings`, `services`, `solutions`, `technologies`, `technology`, `uk`, `international`, `global`) when building `first` / `first-two` — they produce collisions (`group`, `global`). Example: *Monzo Bank Ltd* → `monzobank`, `monzo-bank`, `monzo`.

### 2. `probe(slug) -> list[Hit]`
Query Greenhouse (board + jobs), Lever, Lever-EU and Ashby for one slug. Fill `Hit(ats, slug, board_name, uk_jobs, total_jobs)`.
- **UK posting** = location text matches `config/settings.yaml → gates.locations_ok` (reuse the regexes) or Ashby `addressCountry` in {`United Kingdom`, `GB`, `UK`}.
- **Rate limiter: per host**, min 1.0 s between requests to the same host (`companies.probe_rate_per_s`). Calls stay sequential; because consecutive calls alternate hosts, the effective rate is ~4 req/s without threads.
- On HTTP 429 or 5xx from a host: mark that host as backed-off for the rest of the run, record the error, keep probing other hosts.
- Timeout 15 s; one retry on connection error.

### 3. `verify(hit, company_norm, slug_is_full_name) -> tuple[bool, str]`
Return `(verified, rule)`; store `rule` as evidence. Decision table, first match wins:

| Rule | Condition | Verified? |
|---|---|---|
| `name_exact` | `board_name` present and `normalise(board_name) == company_norm` | ✅ |
| `name_prefix_uk` | `board_name` present, its tokens are a prefix of the company tokens (or vice versa, e.g. board *Stripe* ↔ *Stripe Payments UK*), and `uk_jobs ≥ 1` | ✅ |
| `full_slug_uk` | no `board_name` (Lever/Ashby), `slug_is_full_name`, `uk_jobs ≥ 1` | ✅ |
| `name_mismatch` | `board_name` present and none of the above | ❌ reject (do not store) |
| `weak` | anything else that returned a real board (e.g. short slug, no UK jobs) | ❌ store as `unverified` for one-click review |

### 4. `resolve(db, settings, limit=300, only_new=False) -> ResolveStats`
- Queue: in-scope companies (`sector IS NOT NULL`, `ch_status` = Active case-insensitive, `removed_on IS NULL`, `public_sector = 0`) where `probe_status IS NULL`, or `probe_status = 'not_found'` and `last_probed` older than `companies.reprobe_after_days`, or `probe_status = 'error'`. Order by `first_seen DESC` (newest sponsors first). `limit` defaults to `companies.probe_limit_per_run`.
- Per company: probe candidates in order; **stop at the first verified hit**. Record the best unverified hit if no verified one.
- Write per company (one transaction per company, so an interrupted run keeps progress): `ats, ats_slug, ats_verified, probe_status ∈ {verified, unverified, not_found, error}, last_probed`, and a new `ats_evidence` JSON column (`{"rule":…, "board_name":…, "uk_jobs":…, "total_jobs":…, "tried": [...]}`). Add the column with an additive `ALTER TABLE` guarded by a `PRAGMA table_info` check.
- Never touch rows with `probe_status = 'manual'` (human decisions win).

### 5. `detect(url) -> tuple[str, str] | None`
One GET of the careers page (UA header, 15 s timeout, follow redirects), run `SIGNATURES` over the HTML **and** the final URL, return the first `(ats, slug)`. No crawling beyond that one page.

### 6. CLI (`careerops companies …`)
- `--resolve [--limit N]` → prints progress every 25 companies and a final line `probed N · verified V · unverified U · not found F · errors E (backed-off hosts: …)`.
- `--queue [--top 30]` → table of companies needing a human: `unverified` first (with evidence), then `not_found` (newest first). Each row prints a ready-to-click search URL `https://www.google.com/search?q=<company>+careers` (a link only — nothing is fetched from Google).
- `--set "<name>" --url <careers url>` → match the company by case-insensitive exact register name, else a unique substring (error listing matches if ambiguous); run `detect`; Greenhouse/Lever/Ashby → `ats_verified = 1, probe_status = 'manual'`; other ATS → store `ats` + `careers_url`, `ats_verified = 0, probe_status = 'manual'`; nothing found → store `careers_url` only.
- `--confirm "<name>"` / `--reject "<name>"` → accept or discard an `unverified` hit (`manual` either way).

### 7. Wire into discovery
- `sources.fetch_all(settings, db=None)`: when `db` is given, add every company with `ats_verified = 1` and `ats ∈ {greenhouse, lever, ashby}` as an extra board (`{"slug": ats_slug, "name": register_name}`), deduped against `settings.sources`. Using the **register name** as the job's company makes the sponsor gate match exactly.
- `cmd_discover` passes `db`.

## Tests — `tests/discovery/test_ats.py` (no network; patch `requests.get` with a URL → response map)
Fixtures in `tests/fixtures/discovery/ats/`: JSON bodies for a Greenhouse board + jobs, a Lever postings list, an Ashby board, a Lever "Document not found" body; careers-page HTML files for Greenhouse iframe embed (`boards.greenhouse.io/embed/job_board?for=acme`), Lever link, Ashby script, Workday link, and a page with no ATS.

1. `slug_candidates`: Monzo example; stop-word handling (`The Data Group Ltd` doesn't yield `group`); ≤4 results; dedupe.
2. `probe`: each ATS parsed into `Hit`; UK counting from text and from Ashby `addressCountry`; unknown slug variants (404, `{"ok": false}`, `{"error": …}`, empty board) → no hit.
3. `verify`: one test per row of the decision table, including the Stripe-style prefix case and a `name_mismatch` that must be rejected.
4. Rate limiter: with a fake clock, two calls to the same host are ≥ 1 s apart; calls to different hosts don't wait.
5. 429 on one host → host backed-off, other hosts still probed, company marked `error` only if no host could answer.
6. `resolve`: stops at first verified hit (assert later endpoints not called); writes evidence JSON; queue order newest-first; skips `manual`, public-sector and out-of-scope rows; re-probes `not_found` only after the configured days.
7. Interrupted run (exception after N companies) keeps the first N results.
8. `detect` finds each signature; returns `None` for the plain page.
9. CLI: `--set` exact and substring matching, ambiguity error; `--confirm` / `--reject` transitions; `--queue` lists unverified before not_found.
10. `fetch_all` with `db` includes verified boards once, uses the register name as company, and ignores unverified ones.

## Done when
- [ ] Tests pass; full `pytest -q` green.
- [ ] `docs/IMPLEMENTATION.md` M3 row ✅ (note that the discover wiring moved from M4 into M3); README "Daily use" lists `--resolve`, `--queue`, `--set`, `--confirm`, `--reject`.
- [ ] Commits: (1) fixtures + failing tests, (2) candidates/probe/verify/resolve, (3) detect + CLI + discover wiring + docs.
- [ ] Summary includes the live-run checklist below.

## Live-run checklist for the user (put this in your final summary)
1. `careerops companies --resolve --limit 50` → note the counts and runtime.
2. **Precision check:** open 20 `verified` boards from `--queue`/Excel and confirm each is the right employer. If all 20 are right, the 95 % upper bound on the false-match rate is about 3/20 = 15 % (rule of three); if any is wrong, report which rule verified it so it can be tightened before a full run.
3. Then the full run: `careerops companies --resolve` (repeat daily via cron until the queue is empty).
4. `careerops discover` → confirm jobs now arrive from resolved boards.

## Out of scope
Fetchers for Workday/SmartRecruiters/etc. (decide after the queue shows how many need them), Google/Bing search APIs, any crawling beyond the single careers page given by the user.
