# Task M4 — Your application prompts in code: STAR bullets, learning list, letter rules, company hold, outreach

**Goal.** Encode the user's three standing prompts (resume, cover letter, LinkedIn) into the pipeline so every
generated CV/letter/outreach draft obeys them automatically — without ever adding a fact that isn't in
`config/profile/fact_bank.yaml`.

**Read first:** `CLAUDE.md`, `docs/IMPLEMENTATION.md §1, §5, §6`, `careerops/tailoring/{tailor,prompts,scorer,faithfulness}.py`,
`careerops/tailoring/learning.py` (stub), `careerops/review/{workflow,outreach}.py`, `careerops/llm/mock.py`.

**Independent of M2/M3 data** — everything here is offline and testable with `MockLLM`.

---

## Decisions already made (do not reopen)
- "Add all the required skills … and let me know so I'll keep learning" is implemented as a **learning list**. Skills with no evidence are **never** written onto the CV or letter.
- STAR results come **only** from a new optional `result:` field in the fact bank. The code must support the field; it must **not** add `result:` values to `fact_bank.yaml` (CLAUDE.md hard rule). Bullets without one are reported to the user.
- LinkedIn: two drafts — a connection **note ≤ 200 characters** (fits both reported LinkedIn limits) and a **follow-up message ≤ 5 sentences** for after they accept. Nothing is sent.

## What to build

### A. STAR bullets (`prompts.py`, `tailor.py`)
1. Fact-bank bullets may carry `result: "<text>"`. Treat it as part of the bullet's evidence: `check()` source = `text + result`, evidence terms unchanged. Pass `result` into the tailor prompt next to each bullet.
2. Rewrite `TAILOR_SYSTEM` so rephrases follow **Action verb → what → how (tools) → Result**, with Situation/Task compressed into a short "for X / to Y" clause; ≤ 30 words; Result only from that bullet's `result`; if absent, end at the action.
3. `report.json` gains `needs_result: [bullet_id, …]` — selected bullets that have no `result`. Show it in `review.summary` as "Add a measurable result to: az3, fl1 …".

### B. "Check memory or ask" → review questions (`scorer.py` or new `tailoring/questions.py`)
A **near-miss** is a genuine-gap keyword (no evidence) that shares a token with a fact-bank skill or `terms` entry, or has token-Jaccard ≥ 0.5 with one (e.g. JD `CI/CD` vs fact `GitHub Actions CI`). For each, emit a question: `"Do you have evidence for 'CI/CD'? Closest fact: 'GitHub Actions' (az4 / Skills). If yes, add it to that bullet's terms."` Put them in `report.json → questions` and show them in review. Pure function, unit-tested.

### C. Learning list (`tailoring/learning.py`)
- `per_job(report)` → list of `{term, weight, required}` from `genuine_gaps` (look weights up in `report["keywords"]`).
- `aggregate(db)` → `Gap(term, priority, jobs, required_in)` sorted by `priority = Σ_jobs w_{k,j} · 1[k is a gap in j]` (w = 2 required, 1 preferred), ties by `jobs` desc then term. Read `report.json` from `out_dir` of every job with status in `needs_review/approved/applied/rejected`. Normalise terms with `norm_stem` so `LLMs`/`LLM` aggregate together (display the most frequent spelling).
- `careerops learning [--top N]` prints the table and writes `paths.learning` (markdown).
- Excel: new **Learning list** sheet (term, priority, jobs, required in).

### D. Cover-letter rules (`tailor._letter`)
1. **Letter keyword set** = top 30 JD keywords by weight; score the letter against that set and apply the same 70–85 % band → `report.letter.band`. Below band: one regeneration with the missing *evidenced* keywords listed; never add unevidenced ones.
2. **Hook check:** paragraph 1 must share ≥ 1 content word (≥ 5 letters, not a stop word) with `company_hook`, and must not match the banned openers (existing regex).
3. **CTA check:** last paragraph must name a concrete next step — regex for `(call|chat|conversation|demo|walk (you )?through|meet)` — and must not contain "look forward to hearing".
4. Every failure is listed in `report.letter.problems`; after one retry, keep the fallback letter as today.

### E. Company hold (`gates.py`, `cli.py`)
- New status **`needs_company`** when the employer is unknown or an agency: company empty, or matching `(recruit|staffing|talent|search partners|confidential)` in the company name, or the JD contains `(our client|on behalf of (a|our) client|client of ours)`.
- `careerops set-company --id <job> --company "<name>"` sets the company, re-runs triage (sponsor match included).
- Tailoring never processes `needs_company` jobs.

### F. Outreach (`review/outreach.py`, `prompts.py`, `llm/mock.py`, `cli.py`)
- `prompts.OUTREACH_SYSTEM/USER`: inputs = role title, company, team (optional), contact name + title, 2 strongest evidence bullets from the job's report, company_hook. Output JSON `{"note": "...", "message": "..."}`.
- `validate(note, message)` → problems: note > 200 chars; message > 5 sentences (split on `[.!?](\s|$)`); banned openers (`I came across your profile`, `I hope this finds you well`, `I hope you are well`) or flattery (`impressed`, `amazing`, `inspiring`); missing a concrete small ask (`referral|refer|10[- ]minute|quick (call|chat)|pointer|advice`); claims not in the evidence (reuse `faithfulness.numbers` + deny list).
- `draft(...)`: one LLM call, validate, one retry with problems listed, then return `Outreach(note, message, problems)`.
- `MockLLM` gets an `outreach` handler producing a valid pair.
- `careerops outreach --id --person --title [--team] [--mock]` prints both drafts with the note's character count, and logs `{"kind": "outreach", job_id, note, message, problems}` to `preferences.jsonl`.

## Tests (no network, `MockLLM` only)
- `tests/tailoring/test_star.py`: `result` accepted as evidence (a number from `result` passes the guard; a number from nowhere still fails); `needs_result` lists exactly the selected bullets without `result`; prompt contains each bullet's result.
- `tests/tailoring/test_questions.py`: `CI/CD` vs `GitHub Actions CI` → question; `Kubernetes` (no overlap) → stays a plain gap, no question; no question for evidenced keywords.
- `tests/tailoring/test_learning.py`: three fake reports → priorities match a hand-computed table; `LLM`/`LLMs` merged; markdown file written; Excel sheet present.
- `tests/tailoring/test_letter_rules.py`: letter keyword set has ≤ 30 terms; hook check pass/fail; CTA pass/fail; banned closer caught; band computed on the letter set.
- `tests/discovery/test_gates_company.py`: agency name, "our client" JD, empty company → `needs_company`; `set-company` re-triages; tailor skips the status.
- `tests/review/test_outreach.py`: validate catches each rule (201-char note, 6 sentences, banned opener, flattery, no ask, new number); draft retries once then returns problems; CLI logs to preferences.

## Done when
- [ ] All new tests pass; full `pytest -q` green; `careerops run --mock --limit 1` still completes.
- [ ] `fact_bank.yaml` content unchanged (`git diff config/profile/` empty) — schema support only.
- [ ] README: `learning`, `set-company`, `outreach` documented; `docs/IMPLEMENTATION.md` M4 ✅.
- [ ] Commits: (1) STAR + questions, (2) learning list, (3) letter rules + company hold, (4) outreach + docs.
- [ ] Summary lists anything that still needs a real-LLM check (prompt quality can only be judged on real output in M5).
