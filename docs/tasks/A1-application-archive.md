# Task A1 — Application archive: `applications/<Company>/<Role>/` after applying

**Goal.** When a job is marked **applied**, keep a permanent, self-contained record of exactly what was sent,
organised the way the user browses it:

```
/mnt/d/Professional/UK/Companies/        (= D:\Professional\UK\Companies)
├── 83zero Ltd/                           hand-made folder → adopted, contents untouched
│   └── Python Developer - AI/              hand-made role folder (never written into)
├── Checkout.com/
│   ├── Data Scientist/                     first DS application (pipeline-made, indexed in archive_index)
│   └── Data Scientist – 2026-10-05/        second, different job → date suffix
└── Lendable/
    └── AI Engineer/
        ├── <name> CV.pdf                    ← byte-identical to the file uploaded (name from identity.name)
        ├── <name> CV.tex
        ├── <name> Cover Letter.pdf
        ├── <name> Cover Letter.tex
        ├── JD.txt                           snapshot at application time (postings get taken down)
        ├── job.json                         url, apply_url, source, tier/family, sponsor match, scores, applied_at, confirmation, no_letter
        └── report.json                      copy of the tailoring report
```

**Read first:** `CLAUDE.md`, `careerops/cli.py` (`cmd_apply`), `careerops/apply/assist.py`,
`careerops/tailoring/{tailor,render}.py`, `careerops/discovery/register.py` (`normalise`), `config/settings.yaml → paths`.

**When:** after M4b is committed. Independent of M1–M3.

---

## Rule 0 — the archive root is APPEND-ONLY (user's explicit requirement)
`paths.applications` holds every CV the user has submitted since the start of the search. The code may **only create
new folders and new files** there. It must never delete, rename, move, overwrite or modify anything that already exists —
not files, not folders, not hand-made or pipeline-made ones.
- **No marker files inside the user's tree.** Company keys and job→folder links live in a new SQLite table
  `archive_index (folder TEXT PRIMARY KEY, kind TEXT CHECK(kind IN ('company','role')), company_key TEXT, job_id TEXT,
  created_at TEXT)`, not in `.company.json` / `.job.json`. Existing folders are matched by reading their names only.
  (Wherever this spec mentions `.company.json` or `.job.json`, read: a row in `archive_index`.)
- **One guarded writer.** All writes under the archive root go through `archive/safe_io.py`:
  `safe_mkdir(path)` (fails if it exists) and `safe_copy(src, dst)` (fails if `dst` exists; opens with mode `"xb"`).
  `safe_io` exposes no delete/rename/replace function, and a test greps `careerops/` to ensure no `unlink`, `rmtree`,
  `os.remove`, `os.rename`, `os.replace`, `shutil.move` call takes a path under the archive root.
- **Crash safety without deleting:** build the role folder's files in `data/tmp/archive-<job>/` (inside the repo), verify
  hashes, then copy them in with `safe_copy` into a freshly `safe_mkdir`-ed role folder. If anything fails, the error
  is reported and the half-made role folder is **left in place and flagged** for the user — the code does not clean up
  inside the archive root.
- `v2/` re-submissions are new folders, so nothing earlier is ever touched.
- **The archive root must already exist.** `record_application` refuses (clear error, nothing created, nothing staged)
  when `paths.applications` is missing. Only `careerops init` may create it.

## Rules

### 1. Company folder — "create if missing, else reuse"
- Match existing company folders with a dedicated **folder key**, not exact strings and **not** `register.normalise()`
  (that one strips the token `co` anywhere, so `Co-op` → `op` while `COOP` → `coop` — verified on the user's real folders):
  `folder_key(name)` = lower-case → `&`→` and ` → drop `(…)` → strip **trailing** legal suffixes only
  (`ltd|limited|plc|llp|lp|inc|incorporated|llc|corp|corporation|gmbh|sa|bv|ag`, optional final `.`) → keep `[a-z0-9]` only.
  Verified equal pairs from the user's archive: `COOP`/`Co-op`, `Adria Solution LTD`/`Adria Solution Ltd.`,
  `BrainLabs`/`Brainlabs`, `83zero Ltd`/`83zero`, `Arvora ltd`/`Arvora Limited`, `Zone & Co`/`Zone and Co`,
  `Checkout.com`/`Checkout.com Ltd`.
- **Near-miss, don't guess:** if no folder key is equal but one has `SequenceMatcher` ratio ≥ 0.85 (e.g. existing
  `Blending Spoons` vs `Bending Spoons` = 0.96, `BCG X` vs `BCG` = 0.86), create the new folder **and** flag the job
  `"Possible existing folder: <name> — merge by hand if same company"`. Never auto-merge on similarity.
- The match key is stored in `archive_index` (`company_key`) and looked up there — never by re-parsing folder names.
- Folder display name = the job's company as first seen, sanitised (rule 3).
- **Adopt existing hand-made folders.** The archive root already contains company folders the user created by hand
  (no `archive_index` row). On every lookup, also scan the top-level folders: if `folder_key(folder name)` equals the
  company key, **adopt** that folder (insert a `company` row in `archive_index`; nothing is written to disk) instead of
  creating a new one. Never rename, move or modify anything already inside an adopted folder. If two or more folders
  have the same key, do not guess: archive into the first one alphabetically and add a flag to the job
  (`"Ambiguous company folders: A, B"`) so the user can merge them by hand.

### 2. Role folder — the user's convention: plain role title; never overwrite
- The user's existing archive uses `<Company>/<Role title>/` (e.g. `83zero Ltd/Python Developer - AI/`). Keep that:
  role folder = sanitised job title, **no date** by default.
- Each pipeline-made role folder gets an `archive_index` row (`kind='role'`, `job_id`); no marker file.
- Collision: the folder exists and is hand-made (no marker) or belongs to a **different** job id → append
  ` – <YYYY-MM-DD>`; if that also exists, append ` – <id6>`, where `id6` = first 6 hex chars of `sha1(job_id)`
  (job ids contain colons, e.g. `greenhouse:acme:7`, which NTFS forbids); if that also exists, `-2`, `-3`, … Never write into a hand-made role folder.
- The same job id re-archived → reuse its own folder (found via `archive_index`).
- Re-applying to the same job (e.g. resubmitting) → write into a `v2/`, `v3/` subfolder; earlier files are never touched.

### 3. Safe names on the D: drive (NTFS, reached from WSL)
- Replace `< > : " / \ | ? *` and control characters with `-`; collapse whitespace; strip trailing dots/spaces.
  `Data Scientist / ML Engineer` must become **one** folder, not two nested ones.
- Reserved Windows names (`CON`, `PRN`, `AUX`, `NUL`, `COM1-9`, `LPT1-9`) get a trailing `_`.
- Cap each component at 80 characters and the full path at 240 (Windows MAX_PATH is 260).

### 4. File names a recruiter sees
The PDF filename is visible to whoever opens the upload. It carries the applicant, nothing else — the
`<Company>/<Role>/` folder carries company, role and date.
- CV: `<name> CV.pdf` / `<name> CV.tex`; letter: `<name> Cover Letter.pdf` / `<name> Cover Letter.tex`. Spaces are kept.
- `<name>` is never hard-coded. `config/settings.yaml → files.name_style`: `full` = `identity.name`,
  `short` = first word of `identity.name`.
- Produced **already in `data/outputs/<job>/`** under these names, so the uploaded file and the archived file match in
  name and bytes. xelatex compiles under fixed internal stems (`cv.tex`, `letter.tex`) — never a name with spaces — and
  the outputs are then renamed to the final names inside `data/outputs/<job>/`.
- Final names go through the same NTFS sanitiser as folders (rule 3) and are ≤ 80 characters including the extension.

### 4b. Wrong-file guard
Every CV is now named alike, so a wrong file would not be visible in its name. Before `fill_page` uploads and again before
archiving: the CV and letter paths must lie inside this job's `out_dir`, and the letter's `pdftotext` must contain the
job's company name. If either check fails the command refuses with a clear error; nothing is uploaded or archived.

### 4c. Every job gets a cover letter
- `tailor_job` always writes a letter; no code path skips it. `report["letter"]["status"]` is `"ok"` or `"fallback"`
  (fallback = the LLM failed or broke the letter rules twice).
- `review`: a fallback letter shows `LETTER IS A FALLBACK — regenerate (l) before approving` (`l`, because `r` already
  means reject). Approving anyway requires typing `send fallback`; that is logged as an `events` row.
- `apply` / `mark-applied` exit 1 if the letter PDF is missing. `--no-letter` overrides; it is recorded in `job.json`
  and in an `events` row.
- If the form has a cover-letter file field the letter is uploaded; if it has a cover-letter text box it is filled with
  the letter text. Never submitted. If the form has neither, the letter is still archived.

### 5. When archiving happens
- `careerops apply`: after the user confirms submission (not `x`), archive, then set status `applied`.
- New `careerops mark-applied --id <job> [--ref <confirmation>] [--date YYYY-MM-DD]`: for applications made
  outside the assist (by hand, by email, on a portal Playwright can't drive). Same archive + status update.
- Archive = **copy**, not move: `data/outputs/<job>/` stays as the working copy.
- Staging happens in `data/tmp/` (see Rule 0); nothing is ever replaced or removed in the archive root.
- Record `jobs.application_dir` (additive column) and log an `events` row.
- If copying fails after the application was submitted, the job is still set to `applied` and flagged
  `Archive incomplete: <folder> — finish by hand`; `mark-applied` exits 1.

### 6. Visibility
- Excel Jobs sheet: `Application folder` column (path), only for applied rows.
- `careerops status`: count of archived applications per company (top 10).

### 7. Config
`config/settings.yaml → paths.applications: /mnt/d/Professional/UK/Companies` (comment: "archive of submitted
applications — the user's own folder D:\Professional\UK\Companies, outside the repo"). `Settings.path()` already
accepts absolute paths. Add to `init`'s `DATA_DIRS` (creates it only if missing; never touches existing contents).
**Tests must never write there:** the `settings` fixture in `tests/conftest.py` redirects `applications` to `tmp_path`.

## Tests — `tests/apply/test_archive.py` (no network, MockLLM, tmp paths)
1. New company → creates company folder, role folder, all 7 files, `archive_index` rows, and no marker files in the tree.
2. Existing company under a different spelling (`Checkout.com Ltd` vs `checkout.com`) → reuses the folder.
3. Role naming: first application → `Data Scientist/`; different job, same title → `Data Scientist – <date>/`; same
   title and date again → `… – <id6>`; existing hand-made `Python Developer - AI/` is never written into.
4. Same job archived twice → `v2/` created; original files unchanged (compare hashes).
5. `Data Scientist / ML Engineer` and `AI: R&D?` → single valid folder; `CON` → `CON_`; 200-char title capped.
6. The archived CV PDF is byte-identical to the one passed to `fill_page` for upload (sha256).
7. `mark-applied` without the assist archives and sets status; `--date` controls the folder date.
8. Interrupted archive (exception mid-copy) → nothing pre-existing changed; the partial new folder stays and the job is flagged.
9. Excel shows the folder for applied rows only.
10. Adoption: a pre-existing unmarked folder `Checkout.com` (containing a hand-made file) is adopted for company
    `Checkout.com Ltd`; the hand-made file is untouched (hash); an `archive_index` row is written, no file.
11. Two unmarked folders `Zone & Co` and `Zone and Co` → archive goes to the first alphabetically and the job gets
    the "Ambiguous company folders" flag.
13. **Append-only guarantee:** build a fixture archive tree (several company folders, hand-made role folders, PDFs).
    Record `{relative path: (size, mtime_ns, sha256)}` for every entry. Run every archive scenario above against it.
    Afterwards every recorded entry must still exist with identical size, mtime and hash; the only differences allowed
    are new paths.
14. `safe_copy` onto an existing file raises; `safe_mkdir` on an existing folder raises; the grep test finds no
    destructive call targeting the archive root.
12. `folder_key` table test with every verified pair in rule 1 (equal) plus `BCG X`/`BCG`, `Blending Spoons`/`Bending
    Spoons` (not equal → near-miss flag) and `CGI`/`CGI Group` (not equal, ratio < 0.85 → no flag).

15. Archive root missing → refused, nothing created. `id6` with a multi-colon job id has no `:`.
16. File names: `full`/`short` styles give `<fixture name> CV.pdf` / `<first word> CV.pdf`; `/` and `:` sanitised;
    archived name == uploaded name and sha256 equal.
17. Wrong-file guard: another job's letter raises; nothing uploaded or archived.
18. Letters: LLM failure → letter exists with status `fallback`; review refuses a plain approve on a fallback;
    `mark-applied` without a letter exits 1, with `--no-letter` succeeds and records it.

## Done when
- [ ] Tests pass; full `pytest -q` green; `careerops run --mock --limit 1` still works.
- [ ] README documents the folder layout, `mark-applied`, and `paths.applications`.
- [ ] Commits: (1) tests, (2) naming + archive + `mark-applied`, (3) visibility + docs.
