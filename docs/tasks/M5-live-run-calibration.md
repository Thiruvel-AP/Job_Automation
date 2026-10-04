# Task M5 — Verification gate, first live run, calibration

**Goal.** Prove M1–M4 work on the user's machine, then run the pipeline with a **real LLM** on 5 JDs the user
already tailored by hand, measure it, and tune until it meets the exit criteria below. This is the first time
real model output is judged, so the deliverable is **evidence**, not features.

**Read first:** `CLAUDE.md`, all files in `docs/tasks/`, `docs/IMPLEMENTATION.md`.

**Secrets rule for this task:** never `cat`, print, or log `.env` or any API key. Check keys only via
`careerops doctor` (presence, not value).

---

## Part 0 — Verification gate (do this first; stop if anything is red)

1. For each of M1, M2, M3, M4: read its task file and audit the code against every "What to build" item and
   every listed test. Produce `docs/status.md`:

   | Milestone | Item | Status (done / missing / differs) | Evidence (file:function or test name) |

2. Run and paste raw output into `docs/status.md`:
   - `pytest -q`
   - `careerops doctor`
   - `careerops status`
   - `sqlite3 data/careerops.db "SELECT COUNT(*) FROM companies; SELECT ch_match, COUNT(*) FROM companies GROUP BY 1; SELECT probe_status, COUNT(*) FROM companies GROUP BY 1; SELECT day, rows FROM register_snapshots ORDER BY day;"`
3. If any item is `missing`/`differs` or a test fails: **stop, report, and ask the user** whether to fix now.
   Do not continue to Part 1 with a red gate.

## Part 1 — Readiness tooling (read-only towards the user's profile)

### 1-pre. Calibration folder  (✅ `paths.calibration`, `setup.CALIBRATION_SUBDIRS`, `tests/test_setup.py`)
Add `calibration: data/calibration` to `config/settings.yaml → paths` (comment: "M5 calibration JDs, manifest, reports")
and to `DATA_DIRS` in `careerops/setup.py` so `careerops init` creates `data/calibration/jds/`. Code reads it via
`settings.path("calibration")`, never a hard-coded path. If the user already created the folder by hand, `init` must
leave its contents untouched (idempotent). Extend `tests/test_setup.py` accordingly.

### 1a. `careerops factbank-check`
Lint `config/profile/fact_bank.yaml` **without editing it**. Report:
- every `TODO` (path + line);
- duplicate bullet ids; bullets with empty `terms`;
- selected-often bullets without `result:` (use `report.json` files under `data/outputs/` if present);
- any `deny_terms` entry that appears in a bullet's `text`, `terms` or `skills` (contradiction);
- skills listed in `skills` that no bullet evidences (claimable only as a Skills-line item — fine, but list them).
Exit code 1 if any TODO remains.

### 1b. `careerops llm-check`
For each provider in `settings.llm.providers`: one tiny JSON request
(`{"ok": true}` echo), report reachable / model id / latency / valid JSON / `response_format` accepted.
If a provider rejects `response_format` (HTTP 400), retry once without it and report that
`json_mode: false` should be set for it. Never print headers or keys.

### 1c. Privacy guard test
Add `tests/llm/test_prompt_privacy.py`: run `tailor_job` with a recording `MockLLM` and assert no
`identity.email`, `identity.phone`, or `identity.links` value appears in any system/user prompt sent to the model.
(Contact details belong in the rendered PDF only, never in a prompt that leaves the machine.)

### 1d. JD section detection — bugs found on the user's real JDs (2026-10-02)
`scorer.taxonomy_keywords` decides required (×2) vs preferred (×1) from section headings. On real JDs it fails:
- Lendable: heading `What we are looking for` is **not** recognised (regex only allows `we're`), while the bullet
  `Strong engineering skills and experience building production systems.` **is** taken as a heading (it contains
  "skills and experience" and is < 80 chars).
- Zone & Co: `What You'll Bring:` is not recognised, so every keyword gets the neutral weight 1.5.
- Benefits / "Why join us" / "Interview process" / "Life at …" text is scored like job content.

Fix:
1. A line is a heading only if it is ≤ 60 chars, does **not** end with `.`, and either ends with `:` or matches a heading phrase.
2. Required phrases: `requirements?`, `essential`, `must[- ]have`, `qualifications`, `what you(?:'ll| will)? (?:need|bring)`,
   `what we(?:'re| are) looking for`, `who you are`, `about you`, `your (?:skills|experience|background)`, `skills and experience`.
3. Preferred phrases: existing ones (`nice to have`, `desirable`, `bonus`, `preferred`, `a plus`, `advantageous`, `ideally`).
4. Neutral headings reset the mode: `what you(?:'ll| will) do`, `responsibilities`, `the role`, `about the (?:role|job)`.
5. Skip-sections (keywords there get weight 0, i.e. ignored): `benefits`, `perks`, `why join`, `life at`, `interview process`,
   `equal opportunit`, `employment terms`, `about (?:us|lendable|the company)` — company blurbs are not requirements.
6. Clause-level "nice to have": a line such as `Proficiency in PyTorch …; exposure to … LoRA is a plus.` must keep
   **PyTorch required** and make only **LoRA** preferred. Split the line on `;`, `—`, and ` (also|ideally|bonus)`; apply the
   preferred weight only to the clause containing the preferred phrase.
7. Extra skip-section headings seen on Chattermill: `our hiring process`, `our perks`, `our values`, `diversity`, and
   hashtag-only lines (`#machinelearning #…`). Without this, *healthcare* (from "private healthcare plan") and
   *stakeholders* (from the values blurb) were scored as JD keywords.
8. Tests in `tests/tailoring/test_sections.py` with these exact heading lines:
   Creditspring `What you'll need to succeed` → req, `Desirable experience:` → pref;
   Chattermill `What you’ll need:` (curly apostrophe) → req, `➕It would be a bonus if you:` → pref, `Our Perks` → skip;
   Lendable `What we are looking for` → req, `Strong engineering skills and experience building production systems.` → not a heading,
   `Why join us` → skip; Zone & Co `What You'll Bring:` → req, `What You'll Do:` → neutral, `Benefits` → skip.

**Status: ✅ done** (`scorer.classify_heading`, `tests/tailoring/test_sections.py`). Deviations approved by the user:
(a) skip any `About <X>` heading except role/job/position/team/you, instead of hard-coding `lendable`;
(b) split clauses on `;`, `—`, ` – `, `(` and before `also`/`ideally` — not before `bonus`, which trails its own clause
("containerization experience is a bonus"); (c) a `:`-ended heading with no known phrase resets the mode to neutral.

## Part 2 — Calibration harness: `careerops calibrate`

**Status: ✅ built (`careerops/reporting/calibrate.py`, `tests/reporting/test_calibrate.py`); not run live yet.** Differences from
the text below: jobs are plain dicts (no row in the jobs table, no `calibration` source in the DB) and the CVs go to
`<calibration>/runs/<stamp>/outputs/`, so a calibration run cannot disturb the real queue or `data/outputs/`; the DB is read for the
sponsor index and holds the router's token counters; `questions` / `needs_result` are not in `report.json`, so they are not reported;
extra flags `--only`, `--limit`, `--compare REPORT`, `--out`, `--mock`; the report also gets a `.json` sidecar for `--compare`.
The manifest now lists seven JDs (heathrow, quantcast = the tier-2 slot, trainline = the tier-3 slot were added).

Input: `data/calibration/manifest.yaml` (git-ignored, written by the user):
```yaml
- id: cal-lendable
  company: Lendable
  title: AI Engineer
  jd_file: data/calibration/jds/lendable.txt
  manual_ats: 0.797          # the user's own hand-tailored score, if known
- ...
```
Optional per-entry fields:
- `reference_cv: data/calibration/reference/<id>.pdf` — the CV the user actually submitted for that job. `calibrate`
  scores it with the **same** keyword set as the pipeline CV, giving a like-for-like reference score (unlike `manual_ats`,
  which came from per-role scorers). Report `pipeline − reference` per JD and use reference scores for the rank comparison
  whenever ≥ 4 entries have one.
- `outcome: rejected | no_response | screen_passed | interview | offer` — what happened in reality. One label is not a
  statistic, but these accumulate (see M6) and are the real target; the ATS score is only a proxy.

For each entry:
1. Insert/refresh the job (source `calibration`), run `gates.triage` and **record** the verdict and reasons,
   but tailor **regardless** of the verdict (calibration needs output for every JD).
2. Run the real `tailor_job` with the `Router` (not the mock). Wrap the router to count calls, tokens if the
   API returns `usage`, provider used, and wall time.
3. Collect from `report.json`: ceiling, final literal/stemmed score, band, attempts, guard violations
   (count + list), questions, needs_result, letter score/band/problems.

Write `data/calibration/report-<timestamp>.md` with one row per JD and these aggregates:

| Metric | Definition | Target |
|---|---|---|
| Guard block rate | blocked rephrases ÷ proposed rephrases | report; investigate if > 0.30 |
| Band hit rate | JDs in band ÷ JDs whose ceiling ≥ 0.70 | ≥ 3 of 4 (or equivalent) |
| Headroom left | mean(ceiling − final literal) over in-scope JDs | report |
| Manual agreement | `|pipeline literal − manual_ats|` per JD, and Spearman ρ over JDs with a manual score | report (see note) |
| Cost | LLM calls and seconds per JD | report |

Note for the summary: manual scores came from per-role keyword lists, so absolute differences are not
like-for-like; rank order is the meaningful comparison. With n = 5, ρ is only significant at one-tailed
α = 0.05 when ρ ≥ 0.90 — treat it as a sanity check, not proof.

Also write `data/calibration/rubric.csv` (one row per JD, empty rating columns) for the user:
`id, cv_false_claim(Y/N), cv_relevance(1-5), cv_readability(1-5), letter_false_claim(Y/N), letter_tone(1-5), would_send(Y/N), notes`.
`careerops calibrate --summarise` merges the filled rubric into the latest report.

## Part 3 — Tuning loop (only after the user has filled the rubric)

1. From the report + rubric, propose changes **as a list first** (do not apply): taxonomy additions, gate regex
   changes, prompt wording, `terms` additions the *user* should make to the fact bank (as suggestions only).
2. After the user approves, apply code/config changes (never `fact_bank.yaml`), re-run `careerops calibrate`
   on the same manifest, and show before/after metrics side by side.
3. Maximum 3 tuning rounds; then stop and report.

## Exit criteria for M5
- [ ] Part 0 gate green, `docs/status.md` committed.
- [ ] `factbank-check` exits 0 (user has resolved every TODO).
- [ ] `llm-check` passes for at least one provider.
- [ ] Calibration on the 5 JDs: **zero** `false_claim = Y` in the rubric (CV and letter), band hit rate ≥ 3/4 of
      in-scope JDs, no letter with rule problems, `would_send = Y` for ≥ 3 of 5.
- [ ] Tests: privacy test + unit tests for `factbank-check`, `llm-check` (mocked HTTP), and `calibrate`
      (MockLLM + a fake manifest). Full `pytest -q` green.
- [ ] Commits: (1) status report, (2) factbank-check + llm-check + privacy test, (3) calibrate harness, (4+) one commit per approved tuning round.

## Out of scope
Applying to the calibration jobs (they're for measurement only), new discovery sources, Phase-2 training.