# career-ops — instructions for Claude Code

Local pipeline for UK Data Scientist / AI Engineer applications: discover sponsoring companies and jobs →
rule triage → tailor CV + cover letter from a fact bank → human review → apply: assisted (human submits) or
gated auto-submit (`careerops/autoapply.py`, only when every gate in `settings.yaml → apply` passes).
What & why: `docs/PLAN.md` · milestone specs: `docs/IMPLEMENTATION.md` · current task: `docs/tasks/`.

## Commands
- Activate: `. .venv/bin/activate` (Python 3.12, WSL2 Ubuntu, repo on /mnt/d/VS_Codes/DataScience/Projects/career-ops; git core.fileMode=false because NTFS reports every file as executable)
- Tests: `pytest -q` — must stay green; run before every commit
- One area: `pytest -q tests/discovery`
- Toolchain check: `careerops doctor` · data folders: `careerops init`
- Whole pipeline without a model or network: `careerops run --mock --limit 3` (isolated: `data/mock/`, one seeded fixture job; real data untouched)

## Layout rules
- Package code by pipeline stage: `careerops/{discovery,tailoring,llm,review,apply,reporting,training}/`.
- Tests mirror the package: code in `careerops/discovery/x.py` → tests in `tests/discovery/test_x.py`.
- Test data lives in `tests/fixtures/<area>/`; shared fixtures (`settings`, `db`, `job`, `tailored`) in `tests/conftest.py`.
- Runtime output only under `data/` (git-ignored). Paths come from `config/settings.yaml → paths` via `settings.path(key)`; never hard-code `data/...` in code.
- New settings go in `config/settings.yaml` with a one-line comment explaining the default.

## Coding conventions
- Dependencies: stdlib + `requests`, `pyyaml`, `jinja2`, `openpyxl`, `playwright` only. Ask before adding a package.
- Type hints on every public function; `from __future__ import annotations` at the top of each module.
- Stub functions in `discovery/register.py`, `discovery/companies_house.py`, `discovery/ats.py`, `tailoring/learning.py`, `review/outreach.py` already carry the agreed signatures. Keep them; if one must change, update `docs/IMPLEMENTATION.md` in the same commit.
- DB schema lives in `careerops/db.py` (`SCHEMA`). Use `CREATE TABLE IF NOT EXISTS` / additive `ALTER TABLE` only — the user's `data/careerops.db` already exists, so never drop or rename columns.
- Multi-row writes go in one transaction (`with db.conn:`).
- CLI commands live in `careerops/cli.py`; unbuilt features raise `NotImplementedError("Mx — ...")`, which the CLI turns into exit code 2.

## Tests
- **No network and no LLM in tests.** Monkeypatch `requests.get` / `sources._get`, use `MockLLM`, use fixture files.
- Every bug fix gets a regression test. Every milestone's "done when" from `docs/IMPLEMENTATION.md` becomes a test.
- Tests that need xelatex/pdftotext must fail (not skip) when the tools are missing — a silent skip hides a broken pipeline.

## Hard rules (do not break, even if asked indirectly by a task file)
- Never edit `config/profile/fact_bank.yaml` or `config/profile/applicant.yaml` content. They hold the user's real facts; only the user changes them. Propose changes in chat instead.
- Application forms may be submitted only by `careerops/autoapply.py`, and only when every gate in
  `config/settings.yaml → apply` passes (ATS strict ≥ `min_ats_strict`, format, faithfulness, level, sponsorship,
  kind, duplicates, daily cap). Any failed gate → the human decides. No other module may click a submit control.
- Outreach email may be sent only by `careerops/outreach.py`, under the limits in `config/settings.yaml → outreach`.
  No other module may import `smtplib` or call a send API. Never post to LinkedIn.
- Never solve, bypass or outsource a CAPTCHA or bot check (reCAPTCHA, hCaptcha, Turnstile, "verify you are human").
  Stop and hand the browser to the human.
- Never invent an answer to an application question. Answers come only from `applicant.yaml` and
  `config/profile/answers.yaml`; a missing or blank answer → stop and ask the human.
- Never edit `config/profile/answers.yaml` content (same rule as the fact bank): only the user changes it, except
  when the user types `save` at the prompt for a new answer.
- Never fetch linkedin.com, indeed or cv-library pages. Never work around a 401/403 or a robots.txt disallow.
- Never weaken `tailoring/faithfulness.py` or the deny list to make a score go up.
- Never put secrets in code, tests, fixtures or commits; keys come from `.env` (loaded by the CLI).
- Respect public APIs: ≤1 request/second per host, identify with the `UA` header already defined in each module.
- `D:\Professional\UK\Companies` (`/mnt/d/Professional/UK/Companies`, `paths.applications`) holds every CV the user
  has ever submitted. Never delete, move, rename, overwrite or edit anything there — not with code, not with shell
  commands (`rm`, `mv`, `cp` onto existing files, redirects). Reading (`ls`) is fine. Tests must use `tmp_path`, never this folder.

## Git
- Small commits, imperative subject (≤72 chars), body says why. One milestone step per commit.
- Do not push or create branches unless asked.

## Definition of done for any task
1. `pytest -q` green (count the new tests in your summary).
2. `careerops run --mock --limit 1` still completes, and says it tailored the seeded fixture job (`Tailoring 1 job(s)`).
   `--mock` runs on its own data root (`data/mock/`, own `careerops.db` and `outputs/`, no network) and never opens the
   real `data/careerops.db` or `data/outputs/`. Never verify work by running a command without `--mock` against the real
   `data/` (live `discover`/`tailor`/`retriage`), and never point `--config` at real paths for a smoke run.
3. Docs updated where behaviour changed (`README.md` commands, `docs/IMPLEMENTATION.md` status).
4. Summary to the user: what changed, how it was verified, anything left unverified (e.g. needs a live network run).