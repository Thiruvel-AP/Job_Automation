# career-ops: plan v4 (hooks, fresh postings first, interview route)

Supersedes the ordering rules in v3. v3's discovery tracks stay as they are.
Status key: ✅ done · 🔨 Claude Code round · 👤 you · 🧪 live check

## 0. Decisions locked on 15 Oct 2026

| Decision | Why |
|---|---|
| **Every CV and letter carries one hook:** the problem the ad hires for, plus your strongest verified proof, plus one real number. It sits in the headline, profile line 1, the first bullet of the most relevant role, and sentence 1 of the letter. | The goal is interviews. The hook is what a 7–10 second skim lands on. |
| **Fresh postings go first. Nothing is dropped.** Ordering is (age tier, then P = F × S). Tiers: ≤24 h, ≤72 h, ≤7 d, older. | Your rule: apply within 24 h, prioritise by posting time, omit nothing. The old R = exp(−days/14) drove 64-day-old open jobs down to 0.01. |
| **Posting time comes from the source.** Greenhouse `first_published`, Lever `createdAt`, Ashby `publishedAt`, Reed `date`, Adzuna `created`. Gmail alerts use the time the email arrived. | `updated_at` resets when a job is reposted, so the Anaplan role would look 10 days old instead of 69. |
| **ATS ignores negated matches.** Three scores: strict CV, claimable ceiling, CV + letter. The letter alone is never gated. | "I have not yet used LightGBM" was counted as a hit, which inflated CV + letter B from 72.5 to 82.5. |
| **Every job gets letters A (strengths) and B (one honest gap).** Both ≤250 words; you pick in review. | Tested on Anaplan. B's gap pre-empts the first screening question. |
| **A note per job:** hook, three ATS scores, learn list, a contact to find, one question, flags. | Every application also gets a way to reach a person directly. |
| **A ceiling below 70 marks the job "stretch". It is never dropped.** | Honest ceiling and your no-omission rule. |
| **The bot never submits. LinkedIn is never fetched. The fact bank and applicant.yaml are edited only by you.** | Unchanged hard rules. |

## 1. Who does what

| Step | Who | How often |
|---|---|---|
| Discovery: ATS boards, Reed, Adzuna, Gmail alerts | scheduled script on your laptop | 07:45 and 17:45 |
| Tailor CV, letters A/B, note | pipeline (Groq, Ollama judge in shadow) | when you run `careerops live` |
| Approve, edit or skip each job; pick letter A/B/none | 👤 you | 2 sittings a day |
| Find the contact on LinkedIn and send the note | 👤 you (LinkedIn can't be automated) | per approved job |
| Fill the form (apply assist), answer eligibility questions, press Submit | 👤 you | per approved job |
| Fact-bank and applicant.yaml changes | 👤 you | when facts change |
| Plan, prompts, Claude Code instructions, checking outputs | Claude, in this Project | on request |

**Time per job:** about 8 min (review 3 + form 3 + contact 2).
- At 25 jobs a day that is 200 min, about 3.3 h. This is the real limit on volume. Discovery is not.

## 2. Daily routine, once the round is built
1. **Morning:** `careerops discover --report` (or let the 07:45 task run). Read the tier counts.
2. `careerops live --fresh`. Review tier 0 (≤24 h) first: approve, pick the letter, open the note.
3. **Per approved job:**
   - send the LinkedIn note to the contact;
   - run apply assist and fill the form;
   - you submit;
   - `mark-applied` (archives to `D:\Professional\UK\Companies\<Company>\<Role>`).
4. **Evening:** after the 17:45 discovery, run `careerops live --fresh` again, then `careerops live --max 10` for older tiers if you have time.
5. **Weekly:**
   - re-run `--gmail-auth` if the token is more than 6 days old;
   - check `data/discover/filtered_<date>.csv` for wrongly filtered roles.

## 3. Today, before the round: Anaplan by hand
1. Download from the chat: the CV PDF, letter B PDF, the note, and `golden_anaplan.zip`.
2. Unzip into `docs/golden/anaplan/` in the repo. The round turns it into the test fixture.
3. On LinkedIn, check whether Ferdinand Stockmann is still at Anaplan. Send the note (250 characters), or find the Functional Analytics manager instead.
4. Apply on Greenhouse with the CV and letter B. Answer the eligibility, relocation and equal-opportunities questions yourself.
5. Run `careerops mark-applied` for the Anaplan job so it is archived and logged.

## 4. Your edits (👤, user-only files)

```yaml
# fact_bank.yaml → experience dc180 → bullets (add)
- id: dc5
  text: "Reviewed pull requests in the client's production repository alongside the client's CTO, the engagement's point of contact."
  terms: [code review, pull requests, git, collaboration, stakeholder]

# fact_bank.yaml → projects (add; from your own hand-made CV)
- id: cnn
  name: Image Classification — CNN and Transfer Learning (MSc coursework)
  stack: Python, PyTorch, torchvision
  github: null
  bullets:
    - id: p_cn1
      text: "Trained a PyTorch CNN on CIFAR-10 and compared it with ImageNet-pretrained ResNet50 transfer learning (frozen backbone, new 10-class head), judged by confusion matrices and validation loss rather than accuracy alone."
      terms: [pytorch, deep learning, cnn, transfer learning, computer vision, model evaluation]

# applicant.yaml (add)
relocation: {willing: true, scope: anywhere, remote_ok: true, letter_line: "I am ready to relocate to {city}."}
```

On 24 Nov 2026 (MSc results), replace "Distinction (Expected)" in the fact bank with your actual grade.

## 5. Milestones

| # | Milestone | Done when | Status |
|---|---|---|---|
| H0 | Manual hook test on Anaplan | CV 2 pages, ATS 72.5 = ceiling, letters ≤250 words, note | ✅ |
| H1 | Golden fixture plus hook module | golden test passes; hook in all 4 places | 🔨 |
| H2 | Negation-aware ATS plus 3 scores | "I have not used X" does not count as a hit | 🔨 |
| H3 | Freshness tiers and `posted_at` from the source | Anaplan shows 69 days and tier 3; no R factor left | 🔨 |
| H4 | Letters A/B on, `[l]` cycles A/B/none | 2 letters ≤250 words; paste-overlap check passes | 🔨 |
| H5 | `note.md` per job, `[n]` in review | all 6 sections present | 🔨 |
| H6 | Twice-daily discovery script plus scheduler doc | logs written; you create the Windows task | 🔨👤 |
| H7 | First live day | 10 fresh-tier jobs reviewed, ≥5 submitted, each with a contact note | 🧪👤 |
| H8 | Calibrate after 60 reviews | judge false approves = 0 → judge may gate | 🧪 |

## 6. Risks
- **Volume versus quality.** At 8 min a job, 100–200 a day is 13–27 h. Stay at 20–30.
- **Stretch roles.** Anaplan's honest ceiling is 72.5: the role centres on dbt, Snowflake and LightGBM. Expect a lower interview rate on stretch jobs.
- **Repost dates.** Some boards refresh dates when a job is reposted. Store `posted_at_source` and trust `first_published` over `updated_at`.
- **The Gmail token expires about every 7 days** while the OAuth app is in Testing. The doctor warns at 6 days.
- **Technical traps in generated text,** for example proposing TimeSeriesSplit for cold-start validation. The review sitting is where these are caught, so never skip it.