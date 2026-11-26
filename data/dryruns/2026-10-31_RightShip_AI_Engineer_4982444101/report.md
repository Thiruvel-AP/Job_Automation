# RightShip, AI Engineer: v2.4.1 dry-run report (31 Oct 2026)

**Status: Phase 3 is NOT complete.** The job was re-tailored with `live --retailor --url ... --record` and stopped at the review prompt, because approving
a job (here: with no letter) is your decision (`live never answers a gate for you`), and the form is only opened after it. So `fields.md`, `fields.json`,
`form_full.png`, `motivation.txt` and the file copies do NOT exist yet; they are written by `--record` when the form has been filled. Nothing was submitted.

## Command for you (resumes at the review prompt; no `--retailor`, so no more tokens for the CV)
```
careerops live --url "https://job-boards.eu.greenhouse.io/rightship/jobs/4982444101" --company RightShip --title "AI Engineer" --record
```
At the prompt: `a`, then `no letter` if it still has none (the letter is then written when the form shows its Cover Letter block), `y` to open the form. The assist
fills it and writes this folder. At "paste confirmation" type `x` and answer `n` to "Did YOU submit"; to submit by hand instead, press Submit in the browser yourself.

## Phase 1 (what was wrong)
| # | Field / step | Root cause (file:line, before the fix) |
|---|---|---|
| 1 | "no letter" | Three drafts rejected; the last only for "the hook and paragraph 2 use the same evidence bullet". `grounding.repeated_evidence_problems` (hook-mode rule) ran on P3 and P4 (`company_letter.py:414`) and `role_group` (`company_letter.py:598`) gave P4 the bullet P3 used |
| 2 | "What motivated you..." empty | `freetext.classify` had no "motivated" (`freetext.py:138`): class `other`, "it is yours" |
| 3 | Location (City) empty | no answers.yaml pattern matches "location city"; script-built drop-down typed into and lost on blur (`filler.fill_field`, `autoapply._combobox` exact-option only) |
| 4 | How did you hear empty | `{source}` = "Greenhouse", no such option; exact-match only |
| 5 | Visa type got a paragraph | `assist.VISA_FIELD` matched any "visa" label; `_answer_visa` pasted `plan.summary` |
| 6 | Salary got the deflection | by design, unchanged |
| 7 | Greenhouse EU host | `intake.GREENHOUSE_HOSTS` / `ats_vendor.GREENHOUSE_BOARD_HOSTS` had no EU host |

## Fixes applied
- F1 letter: `company_letter.py:598` (`role_group`), `grounding.py:179` (`repeated_evidence_problems`, per-paragraph groups), `tailor.py:1176` (`letter_deferred`, mode `if_field`), `letter_demand.py:145` (`provide`), `cli.py:1071` (`letter_maker_for`), `tailoring/letter_reason.py:10` (exact reasons; note.md, review banner, summary line)
- F2 motivation: `freetext.py:138` (classify), `:608` (`_plan_motivation`), `:729` (`_jd_problem`: JD required, one re-fetch, else hand-off "no JD"), `:763` (`_reuse_checked`), `[l]` in the menu
- F3 short fields: `shortfields.py:299` (`handle`: city, country, how-heard), `:167` (`visa_type_answer`), `:198` (`shortest_answer`, 25-word rule), `:419` (`hear_pick`), `assist.py:49` (`is_visa_question`)
- F4 table: `requiredgate.py:66` (`build`), `autoapply.py:429` (blocks before any submit control), `assist.fill_page` (prints it)
- F5 YAML: `review/workflow.py:605` (`read_edit_file`)
- EU host: `intake.py:45`, `intake.py:676` (`filed_in_approved`), `ats_vendor.py:38`
- Record: `apply/dryrun.py:54`, `live --record`

## Tests
Full suite in the parallel copy: **5322 passed, 0 failed** (225 files); `test_no_real_settings/data/profile` in the real tree: 14 passed. 163 new tests in 10 new files.
`careerops run --mock --limit 1` still tailors the seeded job (`Tailoring 1 job(s)`).

## This job, as re-tailored (report.json)
- ATS strict CV **63.4 %**, claimable-only ceiling **75.3 %**, band `review` (50 to < 70): the human decides; auto-apply gates do not run.
- Hook `h_prod_tests`: "LLM agent shipped to AWS Lambda behind ~65 automated tests" (proof bullet az4).
- Letter: **still none with the real model** (gpt-oss-120b, 3 drafts, temperature 0). The "same evidence bullet" rejection is gone; the drafts were rejected by guards that I did not touch:
  vague filler "robust"; "P4: gives no number from the 180 Degrees Consulting Bristol bullet"; "links 'agentic systems using langchain' to 'aws', but no one bullet says both"; names ('CI', 'APIs', 'Python') in neither the evidence nor the JD; one draft under 200 words.
  At fill time the same writer is tried once more; if it fails again an optional Cover Letter stays empty with these exact reasons printed.
- Groq tokens used by this task's two live attempts: 15,207 + 14,989 = **30,196** (gpt-oss-20b 3,940 in 4 calls; gpt-oss-120b 26,256 in 6 calls). Today's total for gpt-oss-120b: 53,621 tokens in 12 calls.

## Required fields still empty
Not measured on the real form (not opened). On the local replica of its structure (tests/apply/test_greenhouse_form.py) exactly four required fields block: `visa_type` (NEEDS USER: no `visa_type` entry in
answers.yaml), the working-rights drop-down (NEEDS USER: no answer in answers.yaml), the truthfulness declaration and the GDPR consent (NEEDS USER: attestation, never set by code).
