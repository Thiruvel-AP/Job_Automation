# Task M2 — Companies House bulk join + sector filter

**Goal.** For every A-rated Skilled Worker sponsor in `companies` (built by M1), find its Companies House
record and attach company number, status, SIC codes and a **sector**. Companies with an allowed sector
and `Active` status become **in scope** for M3 (ATS resolution). Public bodies that aren't companies are
routed to their own list.

**Read first:** `CLAUDE.md`, `careerops/discovery/companies_house.py` (stub + `JoinStats`),
`careerops/discovery/register.py` (`normalise`, M1 code), `config/settings.yaml → companies`,
`docs/IMPLEMENTATION.md §3`.

---

## Verified facts about the source (checked 2026-10-02 — code defensively anyway)

- Download page: `https://download.companieshouse.gov.uk/en_output.html` (already `companies.ch_bulk_page`).
- On 2026-10-02 the page still offers the **September** snapshot: `BasicCompanyDataAsOneFile-2026-09-01.zip` (469 MB) and 7 parts `BasicCompanyData-2026-09-01-partN_7.zip`. A new month appears within ~5 working days of month end, so **parse the page for the filename — never construct it from today's date.**
- Unzipped it is one CSV of roughly 2 GB, ~5 million live companies.
- **Header quirk:** several column names carry a **leading space**, e.g. `" CompanyNumber"`, `" RegAddress.AddressLine2"`, `" PreviousName_1.CompanyName"`. Strip every header name before lookup.
- Columns used here: `CompanyName`, `CompanyNumber`, `RegAddress.PostTown`, `CompanyStatus`, `CompanyCategory`, `SICCode.SicText_1` … `SICCode.SicText_4`, `PreviousName_1.CompanyName` … `PreviousName_10.CompanyName`.
- SIC fields look like `"62012 - Business and domestic software development"`; some rows hold the literal `"None Supplied"`. Parse codes with `^\s*(\d{5})`.
- `CompanyStatus` casing is inconsistent (e.g. `Active` vs all-caps values) — compare case-insensitively.
- Free-text fields contain web-form junk (`--SELECT--`, `COUNTY (OPTIONAL)`); don't rely on County.

## What to build

### 1. `download_bulk(settings) -> list[Path]`
- GET the page, find the `BasicCompanyDataAsOneFile-YYYY-MM-DD.zip` link (regex on the href), derive `YYYY-MM`.
- Target: `paths.companies_house_dir/YYYY-MM/<filename>`. If it exists and opens as a valid zip → return it (no download).
- Otherwise stream-download (1 MB chunks) to `<filename>.part`, then `os.replace` to the final name. Log progress every ~50 MB (it's ~470 MB on a home connection).
- After success, delete older `YYYY-MM/` folders (keep only the newest) — 470 MB/month adds up.
- Return `[path]` (list kept so the multi-part files remain an option later).

### 2. `stream_rows(paths) -> Iterator[dict]`
- Open the CSV **inside the zip** without extracting (`zipfile.ZipFile(...).open(name)` wrapped in `io.TextIOWrapper(encoding="utf-8", errors="replace", newline="")`).
- Use `csv.reader`; build a `{stripped_header: index}` map once; yield only the needed fields as a small dict:
  `name, number, town, status, category, sic_codes: list[str], previous_names: list[str]`.
- If any required column is missing, raise with the header it actually saw.
- Must never hold the file in memory (no `list(reader)`).

### 3. `classify(sic_codes, allow) -> str | None`
First sector (in `settings.yaml` order) whose list contains any of the company's 5-digit codes. Codes are compared as strings; the yaml ints are converted once.

### 4. `join(db, settings, paths) -> JoinStats` — the single pass
1. Load target sponsors: `SELECT … FROM companies WHERE removed_on IS NULL AND rating IN (sponsor_ratings) AND route LIKE '%Skilled Worker%'`.
   For each, the **legal part** of the register name (`"Acme Ltd T/A DataCo"` → `"Acme Ltd"`) → `normalise()` → key.
   Build `targets: dict[norm → list[(norm, town_norm)]]` (several register rows can share a normalised name).
2. Stream Companies House once. For each row compute `normalise(name)`; if it is in `targets`, record the row as a **current-name** candidate. Also check each previous name; record matches as **previous-name** candidates (lower priority). Keep candidates in `dict[norm → list[row]]` — this is bounded by the number of sponsors, not by 5 M.
3. Resolve each register company:
   - exactly one current-name candidate → `ch_match = "exact"`
   - several → keep those whose normalised `town` equals the register's `town_norm` → exactly one: `"town"`; else `"ambiguous"` (store candidate numbers comma-separated in `ch_number`, leave sector empty)
   - none current, exactly one previous-name candidate → `"previous_name"`
   - none at all → `"none"`; if `register_name` matches `companies.public_sector_pattern` → `public_sector = 1`
4. For resolved rows set `ch_number, ch_name, ch_status, sic_codes` (comma-separated codes), `sector = classify(...)`.
5. **All updates in one transaction** (`executemany`). Re-running the same month must give identical results (idempotent).
6. Return `JoinStats` with every count plus `in_scope` = resolved, status `Active` (case-insensitive, exact — not "Active - Proposal to Strike off"), sector not null.

### 5. CLI — `careerops companies --refresh-ch`
Prints the download decision, then one summary line, e.g.
`CH 2026-09: 0000 sponsors · exact 0000 · town 00 · previous-name 00 · ambiguous 00 · none 0000 (public sector 000) · in scope 0000`
plus a per-sector breakdown of the in-scope count.

### 6. Excel (small, do it now)
Add a **Companies** sheet to `reporting/excel.py`: register name, town, rating, first_seen, ch_match, ch_number, ch_status, sector, public_sector, removed_on. Only rows with a sector or `public_sector = 1` (the full register is too big for a useful sheet).

## Tests — `tests/discovery/test_companies_house.py` (no network)
Fixtures in `tests/fixtures/discovery/`:
- `ch_bulk_sample.csv` (~25 rows) **with the real header including leading spaces**, then zipped in the test (`zipfile` in `tmp_path`) so the code path reads from a zip.
- Seed `companies` rows directly in the test DB (don't depend on M1 fixtures).
- `ch_page.html` with a `BasicCompanyDataAsOneFile-2026-09-01.zip` link and a decoy link.

Cases:
1. `stream_rows` strips header spaces, parses SIC codes, skips `None Supplied`, collects previous names; raises a clear error when a required column is missing.
2. Exact match; `Ltd` vs `Limited` normalise to the same key.
3. Two same-name companies split by town → `town`; same town → `ambiguous` with both numbers stored.
4. `T/A` register name matches on the legal part.
5. Previous-name match when the company was renamed.
6. Dissolved/strike-off status → resolved but **not** in scope; `Active` with allowed SIC → in scope; `Active` with non-allowed SIC → not in scope.
7. NHS trust / university with no CH row → `public_sector = 1`.
8. B-rated and removed sponsors are not joined at all.
9. Idempotency: running `join` twice → identical table state.
10. Memory bound: generate a 200k-row CSV in `tmp_path`, wrap `csv.reader` to count rows yielded, assert the join consumed them as a stream (e.g. peak RSS growth via `tracemalloc` < 50 MB) — keep runtime under ~10 s.
11. `download_bulk`: picks the right link from the page, skips download when the zip exists, writes via `.part` then rename, deletes older month folders (all with patched `requests`).
12. Excel export has the Companies sheet with only in-scope/public rows.

`tests/conftest.py`: make sure the `settings` fixture redirects `companies_house_dir` (and `register_dir`, if M1 didn't already) to `tmp_path` so tests never touch the real `data/`.

## Done when
- [ ] All tests pass; full `pytest -q` green.
- [ ] `docs/IMPLEMENTATION.md` M2 row ✅; README "Daily use" lists `companies --refresh-ch`.
- [ ] Commits: (1) fixtures + failing tests, (2) streaming reader + join, (3) download + CLI + Excel + docs.
- [ ] Summary states what needs a **live** run on the user's machine: `careerops companies --refresh-ch` (≈470 MB download, then one pass over ~5 M rows) — report the runtime, peak memory if easy, and the summary line. Expect the numbers to be checked for plausibility before M3.

## Out of scope
ATS probing (M3), Companies House REST API (not needed — bulk file only), fuzzy name matching beyond `normalise` (ambiguous/none rows wait for M3's manual queue).
