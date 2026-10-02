# Task M1 — UKVI register: daily snapshots + diff

**Goal.** Turn the sponsor register from a cache into a history. Each day: download the register once,
store it, compare it with the previous day, and record which sponsors were added, removed, or changed rating.
This feeds the `companies` table that M2 (Companies House) and M3 (ATS resolution) build on.

**Read first:** `CLAUDE.md`, `careerops/discovery/register.py` (existing parse/match code + M1 stubs),
`careerops/db.py` (`companies`, `company_events`, `register_snapshots` tables), `docs/IMPLEMENTATION.md §2`.

---

## Verified facts about the source (checked 2026-10-02 — do not re-derive, but code defensively)

- Page: `https://www.gov.uk/government/publications/register-of-licensed-sponsors-workers` (already in `settings.yaml → sponsors.page_url`). Updated almost every day.
- Current CSV link: `https://assets.publishing.service.gov.uk/media/6abe33d2efad0f1df6722889/SP_-_Worker_and_Temporary_Worker_Web_Register_-_2026-10-01.csv` (~10.4 MB).
- The **filename pattern has changed before** (2024 files were `2024-10-10_-_Worker_and_Temporary_Worker.csv`). Extract the date with `\d{4}-\d{2}-\d{2}` anywhere in the filename; if none is found, use today's UTC date and log a warning.
- Exact header: `Organisation Name, Town/City, County, Type & Rating, Route`.
- Real values: County is often the literal string `Not set` (treat as empty); Type & Rating looks like `Worker (A rating)`; Route like `Skilled Worker`. The same organisation can appear on several rows (different routes, or `X Ltd T/A Y`).
- ~127k sponsors in total; Skilled Worker is a subset.

## What to build

### 1. `register.load_snapshot(path, route="Skilled Worker") -> dict[(norm, town_norm), tuple]`
- Accept `.csv` and `.csv.gz` (open with `gzip.open` when the suffix is `.gz`), UTF-8 with BOM tolerance (reuse the decoding in `parse`).
- Filter to `route` (case-insensitive substring, as `parse` does).
- Key = `(normalise(full legal name), normalise(town))` using the existing `normalise`. Do **not** split `T/A` names for the key (the `SponsorIndex` used by gates keeps splitting — leave it as is).
- Value = `(register_name, town, county_or_empty, rating_letter_or_raw, route)`; rating letter via `\(([AB])\s*rating\)`, else the raw text.
- Duplicate keys: keep the first row, count duplicates, return the count somewhere observable (e.g. a module-level logger message or a second return value in a helper — your choice, but test it).

### 2. `register.diff(prev, cur) -> Diff`
Pure set algebra on keys: `added = K_cur − K_prev`, `removed = K_prev − K_cur`,
`rating_changed = [(norm, town, old, new) for k in K_prev ∩ K_cur if rating differs]`. Sorted output (deterministic tests).

### 3. `register.snapshot(db, settings) -> Path | None`
1. Fetch the page, find the CSV URL (reuse `find_csv_url`), extract the date (rule above).
2. If `register_snapshots` already has a row with this `csv_url` → return `None` (no download). This makes the daily cron idempotent.
3. Stream-download to a temp file in `paths.register_dir`, then:
   - write `register_dir/YYYY-MM-DD.csv.gz` (gzip),
   - atomically replace `paths.sponsors_csv` (`latest.csv`) with the plain CSV (write temp + `os.replace`),
   - refresh the `sponsors` table from `latest.csv` with the existing `parse` + `db.replace_sponsors` so gates use today's register.
4. Load the previous snapshot (latest `register_snapshots.day` before this one). Diff, then `apply_diff`.
5. Insert the `register_snapshots` row (`day, csv_url, path, rows`) **in the same transaction** as `apply_diff`, so a crash never leaves a half-recorded day.
6. Return the `.csv.gz` path.

### 4. `register.apply_diff(db, day, d, cur)` — one transaction
- **First-ever snapshot (no previous):** upsert every company with `first_seen = last_seen = day`; write **one** `company_events` row of kind `baseline` (detail = row count) instead of 100k `added` events — otherwise the "new sponsors this week" view is flooded on day one.
- **Added:** insert with `first_seen = last_seen = day`, event `added`. If the key already exists with `removed_on` set (licence restored), clear `removed_on`, event `readded`.
- **Still present:** `last_seen = day` (one `UPDATE … WHERE` per key is fine; or `executemany`).
- **Removed:** set `removed_on = day`, event `removed`. Then flag affected jobs: `jobs.sponsor_name == register_name` and `status IN ('triaged','needs_review','approved')` → append `"Sponsor licence no longer on register (since <day>)"` to the job's `flags` JSON list (no duplicates).
- **Rating changed:** update `rating`, event `rating` with detail `"A→B"`; if the new rating is not A, flag affected jobs the same way (`"Sponsor rating now B (since <day>)"`).

### 5. Retention — `register.prune(settings, keep_days=30)`
Keep the last `keep_days` daily files plus the first file of every month; delete the rest. Add `register: {keep_days: 30}` to `settings.yaml` (with comment). Call it at the end of `snapshot`.

### 6. CLI
- `careerops companies --snapshot` → runs `snapshot` + prints one line: `2026-10-01: <n> Skilled Worker sponsors · +<a> added · −<r> removed · <c> rating changes` or `Register unchanged (already have 2026-10-01)`.
- `careerops sponsors` (existing) → delegate to the same code path so there is one way to refresh.
- Update `scripts/crontab.example` only if the command line changes.

## Tests — `tests/discovery/test_register_snapshots.py` (no network)
Create fixtures in `tests/fixtures/discovery/`:
- `register_2026-09-30.csv` and `register_2026-10-01.csv` (~12 rows each) planting: one added org, one removed org, one A→B rating change, a `T/A` row, `County = Not set`, a non-Skilled-Worker row, a duplicate row, a UTF-8 BOM in the first file.
- `register_page.html` containing an `assets.publishing.service.gov.uk/.../SP_-_Worker_and_Temporary_Worker_Web_Register_-_2026-10-01.csv` link.

Cases (each its own test):
1. `load_snapshot` on `.csv` and `.csv.gz` gives identical dicts; route filter, `Not set` → empty, rating letter parsed, duplicate counted.
2. `diff` finds exactly the planted added / removed / rating change.
3. First `snapshot` with no history → `baseline` event only, companies populated, `latest.csv` and `.csv.gz` written, `sponsors` table refreshed.
4. Second `snapshot` with the next day's file → correct events; `first_seen` of old companies unchanged; `last_seen` advanced.
5. Running `snapshot` again with the same URL → returns `None`, no download (assert the patched `requests.get` for the CSV was not called), no new rows anywhere.
6. Removed sponsor + rating drop flag the right jobs only (seed jobs in `triaged`, `skipped`, `applied`; only `triaged` gets the flag; no duplicate flag on re-run).
7. Re-added sponsor clears `removed_on` and logs `readded`.
8. Date extraction: new-style name, old-style name, and no date (falls back to today).
9. `prune` keeps the right files (build 70 fake dated files in `tmp_path`).
10. Transaction safety: make `apply_diff` raise mid-way (monkeypatch) → no `register_snapshots` row and no partial events.

## Done when
- [ ] All 10 tests pass; full suite `pytest -q` green.
- [ ] `careerops companies --snapshot` works offline in tests and prints the summary line.
- [ ] `docs/IMPLEMENTATION.md` M1 row marked ✅; README "Daily use" mentions `companies --snapshot`.
- [ ] Commits: (1) fixtures + failing tests, (2) implementation, (3) CLI + docs.
- [ ] Summary lists what still needs a **live** check on the user's machine: one real run (`careerops companies --snapshot`) — report the Skilled Worker row count it prints (the total register is ~127k; the Skilled Worker share is not yet known); a second run the same day must say "unchanged".

## Out of scope (do not build now)
Companies House join (M2), ATS probing (M3), Excel "Companies" sheet (M4).
