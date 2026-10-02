# Status — M5 Part 0 verification gate

Audit date: 2026-10-02 · HEAD `2356466` (+ uncommitted change to `config/settings.yaml`) · environment: WSL2 Ubuntu, `.venv` Python 3.12.3.
Task specs audited: `.claude/M1-register-snapshots.md`, `.claude/M2-companies-house.md`, `.claude/M3-ats-resolution.md`,
`.claude/M4-prompts-learning-outreach.md` (the M5 spec refers to `docs/tasks/`; the files actually live in `.claude/`).

**Gate: 🔴 RED.** M1, M2 and M3 are unimplemented stubs; M4 is unimplemented apart from CLI shells. None of the
milestone test files exist. The 34 passing tests are the pre-sprint baseline only.

## 1. Audit

| Milestone | Item | Status | Evidence |
|---|---|---|---|
| M1 | 1. `register.load_snapshot` | missing | `careerops/discovery/register.py:142` raises `NotImplementedError` |
| M1 | 2. `register.diff` | missing | `register.py:147` stub |
| M1 | 3. `register.snapshot` | missing | `register.py:137` stub |
| M1 | 4. `register.apply_diff` | missing | `register.py:152` stub |
| M1 | 5. `register.prune` + `register.keep_days` setting | missing | no `prune` in `register.py`; no `keep_days` in `config/settings.yaml` |
| M1 | 6. CLI `companies --snapshot` (+ summary line) | differs | `cli.py:207` wired to `sponsors.snapshot` (stub); no summary line; `careerops sponsors` still calls old `register.refresh` (`cli.py:70`) |
| M1 | DB tables `companies`, `company_events`, `register_snapshots` | done | `careerops/db.py` `SCHEMA` |
| M1 | Fixtures `register_2026-09-30.csv`, `register_2026-10-01.csv`, `register_page.html` | missing | `tests/fixtures/discovery/` has only `sponsors.csv` |
| M1 | Tests 1–10 `tests/discovery/test_register_snapshots.py` | missing | file does not exist |
| M1 | Docs: IMPLEMENTATION M1 ✅, README `companies --snapshot` | missing | not marked |
| M2 | 1. `download_bulk` | missing | `careerops/discovery/companies_house.py:30` stub |
| M2 | 2. `stream_rows` | missing | `companies_house.py:35` stub |
| M2 | 3. `classify` | missing | `companies_house.py:40` stub |
| M2 | 4. `join` → `JoinStats` | missing | `companies_house.py:45` stub (`JoinStats` dataclass exists) |
| M2 | 5. CLI `companies --refresh-ch` | differs | arg + dispatch at `cli.py:209`; calls stubs; no summary/per-sector breakdown |
| M2 | 6. Excel Companies sheet | missing | not in `careerops/reporting/excel.py` |
| M2 | Fixtures `ch_bulk_sample.csv`, `ch_page.html`; conftest redirect of `companies_house_dir` | missing | not present |
| M2 | Tests 1–12 `tests/discovery/test_companies_house.py` | missing | file does not exist |
| M3 | 1. `slug_candidates` | missing | `careerops/discovery/ats.py:49` stub |
| M3 | 2. `probe` (+ per-host rate limiter, back-off) | missing | `ats.py:54` stub |
| M3 | 3. `verify` | missing | `ats.py:59` stub |
| M3 | 4. `resolve` + `ats_evidence` column | missing | `ats.py:64` stub; no `ats_evidence` in `db.py` |
| M3 | 5. `detect` | missing | `ats.py:69` stub (`SIGNATURES` exists) |
| M3 | 6. CLI `--resolve`, `--queue`, `--set` | differs | args at `cli.py:253-255`; `--queue` raises `NotImplementedError` (`cli.py:216`); `--set` calls stub `detect` |
| M3 | 6. CLI `--confirm`, `--reject`, `--top`, `--limit` progress | missing | not in `cli.py` |
| M3 | 7. `sources.fetch_all(settings, db=None)` + `cmd_discover` passes `db` | missing | `sources.py:150` signature is `fetch_all(settings)` |
| M3 | Fixtures `tests/fixtures/discovery/ats/` | missing | folder does not exist |
| M3 | Tests 1–10 `tests/discovery/test_ats.py` | missing | file does not exist |
| M4 | A. `result:` field as evidence; STAR `TAILOR_SYSTEM`; `needs_result` in report + review | missing | no `result` handling in `tailor.py`/`faithfulness.py`; `prompts.py:17` prompt unchanged |
| M4 | B. Near-miss review questions | missing | no `questions` anywhere in `careerops/` |
| M4 | C. Learning list `per_job` / `aggregate` | missing | `careerops/tailoring/learning.py:26,31` stubs |
| M4 | C. CLI `careerops learning`, Excel Learning sheet | differs | CLI shell `cli.py:219` calls stub; no Excel sheet |
| M4 | D. Letter keyword set (top 30) + band + regeneration | missing | `tailor._letter` checks only word count, banned opener/closer (`tailor.py:183-188`) |
| M4 | D. Hook check, CTA check | missing | not in `tailor._letter` |
| M4 | E. `needs_company` status, agency regex | missing | not in `careerops/discovery/gates.py` |
| M4 | E. CLI `set-company` | missing | not in `cli.py` |
| M4 | F. `outreach.draft` / `validate` | missing | `careerops/review/outreach.py:26,31` stubs |
| M4 | F. `OUTREACH_SYSTEM/USER` prompts, `MockLLM` outreach handler | missing | not in `prompts.py` / `llm/mock.py` |
| M4 | F. CLI `careerops outreach` + preferences log | differs | CLI shell `cli.py:227` calls stub; no log |
| M4 | Tests `test_star`, `test_questions`, `test_learning`, `test_letter_rules`, `test_gates_company`, `test_outreach` | missing | none exist |
| M4 | `fact_bank.yaml` unchanged | done | `git diff config/profile/` empty |

Other findings (not M1–M4 items, but they affect M5):
- ✅ **Secret in config (resolved 2026-10-02):** `config/settings.yaml → llm.providers[groq].api_key_env` held an API-key value instead of the variable name `GROQ_API_KEY` (uncommitted, never in git history). Restored to `GROQ_API_KEY`; key now in `.env`; re-run `doctor` shows `[ok] groq key (GROQ_API_KEY)`.
- `doctor`: 3 fact-bank TODOs remain; Ollama not running.
- `data/careerops.db`: all tables empty (0 jobs, 0 sponsors, 0 companies, no snapshots).
- No `sqlite3` CLI in WSL; the queries below were run with Python's `sqlite3` module (same SQL).

## 2. Raw outputs

### `pytest -q`
```
..................................                                       [100%]
34 passed in 26.54s
exit=0
```

### `careerops doctor`  (one value replaced with `[REDACTED]`)
```
  [ok] xelatex
  [ok] pdftotext
  [ok] pdfinfo
  [ok] Carlito font
  [ok] playwright chromium
  [!!] groq key ([REDACTED])  → add [REDACTED]=... to .env
  [!!] ollama at http://localhost:11434/v1  → start it, then: ollama pull qwen2.5:7b-instruct
  [!!] fact bank TODOs (3)  → fill or delete every TODO in fact_bank.yaml
exit=1
```

Re-run after the key fix:
```
  [ok] xelatex
  [ok] pdftotext
  [ok] pdfinfo
  [ok] Carlito font
  [ok] playwright chromium
  [ok] groq key (GROQ_API_KEY)
  [!!] ollama at http://localhost:11434/v1  → start it, then: ollama pull qwen2.5:7b-instruct
  [!!] fact bank TODOs (3)  → fill or delete every TODO in fact_bank.yaml
exit=1
```

### `careerops status`
```
exit=0
```
(no output: the `jobs` table is empty)

### SQL queries (`python3 -c` with `sqlite3`, on `data/careerops.db`)
```
sql> SELECT COUNT(*) FROM companies
0
sql> SELECT ch_match, COUNT(*) FROM companies GROUP BY 1
sql> SELECT probe_status, COUNT(*) FROM companies GROUP BY 1
sql> SELECT day, rows FROM register_snapshots ORDER BY day
sql> SELECT COUNT(*) FROM jobs
0
sql> SELECT COUNT(*) FROM sponsors
0
```
