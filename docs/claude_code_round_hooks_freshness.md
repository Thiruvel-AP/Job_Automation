# Claude Code round: hooks, notes, negation-aware ATS, 24-hour freshness

Paste everything below the line into Claude Code, run from the career-ops repository root.

---

Read CLAUDE.md first. Every hard rule in it still applies:
- Never edit `config/profile/fact_bank.yaml` or `config/profile/applicant.yaml`.
- Never add code that submits or sends an application.
- Never weaken `faithfulness.py` or the deny list.
- No secrets in code.
- Never write, move, rename or delete anything under `/mnt/d/Professional/UK/Companies`. Tests use `tmp_path`.
- At most one request per second per host.
- Never fetch linkedin.com, indeed or cv-library.
- Never push, and never add a git remote.

Work in this order. Commit locally after each step, with tests passing.

## Step 1. Golden fixture (do this first)
- Copy `docs/golden/anaplan/` into `tests/golden/anaplan/`. It holds:
  - `cv.tex` and `cv.txt`
  - `letter_A.tex` and `letter_B.tex`
  - `jd.txt` (the Anaplan Data Scientist ad, REQ 26490)
- Add `tests/test_golden_anaplan.py`. It checks four things:
  - the ATS scorer from Step 3 returns strict 72.5 and claimable 72.5 on `cv.txt`, within ±0.1;
  - the hook sentence appears in the headline, in profile line 1 and in the first AstraZeneca bullet;
  - the PDF has 2 pages;
  - after Step 5, the generated letters are ≤ 250 words.
- This is the quality bar. Pipeline output for this job must reach it.

## Step 2. Hook module (`careerops/hook.py`)
Input: the job ad, the extracted keywords and the fact bank. Output:

```python
Hook(problem: str, proof_bullet_id: str, number: str, headline: str, profile_line: str, letter_sentence: str)
```

**How the hook is built:**
- **problem.** One clause naming what the ad hires someone to solve. It must reuse at least 2 nouns that appear in the ad.
- **proof.** Score each fact-bank bullet that has a `result:` or a number:
  - score = Σ(keyword weight × term hit) over the bullet's `terms`;
  - take the highest score; break ties by recency.
- **number.** The string must appear verbatim in that bullet's `text` or `result`. If no bullet has a number, use the best bullet without one and set `note.flags += "hook has no number"`. Never invent one.

**Where the hook is placed:**
- **Headline.** A new template slot under the name, 12pt navy, one line, at most 95 characters.
- **Profile.** Line 1.
- **Experience.** Move the proof bullet to first position inside the most relevant role. The role is the one with the highest summed bullet score. Rephrasing is allowed; facts are not.
- **Letter.** Sentence 1.

**Guard:**
- Every hook string goes through the existing claim check and the paste-overlap check.
- Reject vague adjectives: passionate, proven, dynamic, results-driven, extensive, strong.

## Step 3. Negation-aware ATS (`careerops/ats.py`)
- Keep score = Σ(w × hit) / Σ(w).
- A hit does not count when one of these appears within 6 tokens before the term in the same sentence:

  ```
  not, no, never, haven't, have not, yet to, without, lack, gap, learning
  ```

- Report three numbers:
  - strict CV;
  - claimable ceiling (terms backed by the fact bank, excluding the deny list);
  - CV + letter combined.
- The letter alone is reported but never gated.
- Gate on strict CV:
  - below 70 with ceiling ≥ 70: retry tailoring once;
  - ceiling below 70: label "stretch" and keep it, never drop it;
  - above 85: de-stuff (existing loop).

## Step 4. Freshness priority (fixes the R = 0.01 bug)
**Store `posted_at` per job** from the source:

| Source | Field |
|---|---|
| Greenhouse | `first_published` (not `updated_at`) |
| Lever | `createdAt` |
| Ashby | `publishedAt` |
| Reed | `date` |
| Adzuna | `created` |
| Gmail alert | email received time (an upper bound) |
| Anything else | `first_seen` |

Also store `posted_at_source`.

**Ordering is lexicographic, never a filter:**
- Tier first:
  - tier 0: ≤ 24 h
  - tier 1: ≤ 72 h
  - tier 2: ≤ 7 d
  - tier 3: older
- Then P = F × S within each tier. Remove the R factor from P.
- Old jobs are never dropped.

**Commands and display:**
- Add `careerops live --fresh`, which runs tier 0 only.
- Show the age in hours in the review header.
- `discover --report` prints the counts per tier.

## Step 5. Letters back on, with the hook rules
Set `letter.mode: hook`. Generate two variants:
- **A:** strengths only.
- **B:** A plus exactly one honest gap, taken from the learn list, with a bridge to a fact-bank skill.

Rules for both:
- Sentence 1 is `hook.letter_sentence`.
- 250 words or fewer.
- Salutation: "Dear <Company> <team name if the ad gives one> team". For agencies: "Hi <agency> team".
- Add a relocation line from `applicant.yaml` → `relocation`, if present.
- End with a specific call to action: a 20-minute ask that names one problem from the ad.
- Keep the paste-overlap check.
- No bullet text copied in: overlap of 0.7 or more against any fact-bank bullet means regenerate once, then fail to "letter: none".

In review, the `[l]` key cycles A → B → none, and the chosen variant is uploaded.

## Step 6. Post-CV note (`data/outputs/<job_id>/note.md`)
Sections:
- **Hook:** problem, proof, number.
- **ATS:** the three numbers from Step 3.
- **Learn list:** terms from the ad that are not claimable. Mark deny-list terms "(confirmed absent)".
- **Contact:** leave as `TODO: you find on LinkedIn: <suggested role title, e.g. hiring manager, <team>>`. Never fetch LinkedIn.
- **Question:** one question naming a specific term from the ad.
- **Flags:** visa or sponsorship wording, location or office policy, seniority or years asked, security clearance, IR35, travel, and the posting age.
- **Gap:** the honest gap used in letter B.

The review screen shows the hook and the ATS line. `[n]` opens the note.

## Step 7. Doctor reminders
In `careerops doctor`, warn when:
- today is on or after 2026-11-24 and the fact bank's MSc note still contains "Expected";
- `data/secrets/gmail_token.json` is more than 6 days old.

## Step 8. Twice-daily discovery (no auto-apply)
- Add `scripts/discover.sh`. It runs:

  ```
  careerops discover --ats --reed --adzuna --gmail-alerts --report
  ```

  It logs to `data/logs/discover_<date>.log` and exits non-zero on failure.
- Add `docs/scheduler.md` explaining a Windows Task Scheduler task that runs at 07:45 and 17:45:

  ```
  wsl.exe -e bash -lc "cd /mnt/d/VS_Code/DataScience/Projects/career-ops && scripts/discover.sh"
  ```

- Do not create the task yourself.

## Step 9. Earlier discovery-quality items
- Resolve each Gmail-alert company across Greenhouse, Lever and Ashby (slug probe plus verification).
- Cap the manual queue at the top 10 by priority.
- Write the role-filter audit to `data/discover/filtered_<date>.csv`, with the reason for each row.

## Done when
- All tests pass, including the golden fixture.
- `careerops live --dry-run --max 1 --url https://job-boards.greenhouse.io/anaplan/jobs/8654662002` produces:
  - a 2-page CV with the hook in all 3 places and strict ATS ≥ 70 (golden is 72.5);
  - letters A and B, each ≤ 250 words;
  - `note.md`.
- Report the diff between the pipeline CV and `tests/golden/anaplan/cv.txt`, as lines only in one or only in the other.
