# career-ops v2

Local pipeline for UK DS/AI applications: **discover → triage → tailor (fact-bank only) → you review → assisted apply**.
Design rationale and the diff against the original flowchart: [`docs/PLAN.md`](docs/PLAN.md).

## Setup (WSL2 Ubuntu)

```bash
sudo apt install texlive-xetex fonts-crosextra-carlito poppler-utils   # xelatex, Carlito, pdftotext/pdfinfo
cd ~/career-ops && python3 -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]" && playwright install chromium
cp .env.example .env            # GROQ_API_KEY (console.groq.com), optional ADZUNA_APP_ID/KEY
ollama pull qwen2.5:7b-instruct # local fallback; fits the 4060's 8 GB VRAM
pytest -q                       # 28 tests, no network or model needed
```

## Before the first real run

1. **Fact bank** — `config/profile/fact_bank.yaml`. Seeded from facts you've stated; fill every `TODO`, delete anything untrue.
   Each bullet's `terms` list is what that bullet is *allowed* to claim when reworded. If a JD keyword shows up as a
   "genuine gap" but you really have it (e.g. *prompt engineering*, *CI/CD* aren't written anywhere yet), add it to the
   right bullet's `terms` — that is the only way it can ever appear on a CV.
2. **Boards** — `config/settings.yaml → sources`. Add Greenhouse/Lever/Ashby slugs from companies' careers URLs
   (`job-boards.greenhouse.io/<slug>`, `jobs.lever.co/<slug>`, `jobs.ashbyhq.com/<slug>`). Your 80-company sponsor
   tracker is the obvious seed list.
3. **Gates** — title/seniority/location regexes and the JD rules (no-sponsorship, SRE, line management, clearance).
4. **Applicant basics** — `config/profile/applicant.yaml`. Visa, salary, EEO and "why us" are deliberately never filled.

## Daily use

```bash
careerops sponsors                 # refresh the gov.uk register (cached 3 days)
careerops discover                 # pull + triage; skipped jobs keep their reason
careerops add-jd --company X --title Y --url U --file jd.txt   # LinkedIn/email/agency roles
careerops add-jd --id adzuna:123 --file full_jd.txt            # replace a truncated Adzuna snippet
careerops tailor --limit 15        # CV + letter per job → data/outputs/<job>/ (+ report.json)
careerops review                   # approve / edit (opens YAML of bullets) / reject
careerops apply                    # fills basics + uploads PDFs, YOU press submit, paste the reference
careerops export                   # data/companies.xlsx
careerops export-prefs             # your edits → DPO pairs (chosen = yours, rejected = model's)
```

`careerops run --limit 25` (discover + tailor + export) is what `scripts/crontab.example` schedules.
`--mock` runs the whole thing with a deterministic stand-in model — use it after editing templates or the fact bank.

## What the review screen tells you

- **ATS literal** — conservative substring match on the PDF's text layer; the 70–85 % band applies to this.
- **ATS stemmed** — also equates *communicating/communicate*, *modelling/modeling*, *end-to-end/end to end*.
  Literal → stemmed gap = wording, not substance.
- **Honest ceiling** — best score the fact bank can support. Below 70 % → the loop doesn't waste calls; you decide.
- **Genuine gaps** — JD keywords with zero evidence. They stay off the page.
- **Guard blocked** — model claims that were reverted to the canonical bullet (new numbers, deny-listed tools,
  unevidenced keywords). Frequent blocks on one bullet = add honest `terms` or tighten the prompt.

## Verified here vs not

| Verified (tests / local run) | Not verifiable from the build sandbox — check on first live run |
|---|---|
| Sponsor CSV parsing, T/A splitting, exact/fuzzy/prefix matching | gov.uk page still exposes the CSV link in the same form |
| Greenhouse/Lever parsers on recorded payload shapes | Ashby field names (coded defensively) |
| Gates, scorer, stemming, line-wrap/quote normalisation | Groq model id + free-tier caps (change often) |
| Faithfulness guard blocks invented numbers/tools/keywords | Real-LLM output quality on your JDs |
| Full tailor loop → 2-page xelatex PDF, no hyphenated words | Live ATS forms (Greenhouse/Lever/Ashby markup varies) |
| Router: JSON mode, auth header, 429 → fallback, daily quota | Adzuna quota and description length |
| Playwright fills a test form, uploads both PDFs, never submits | |

`career-ops` and this pipeline stay off every CV and letter — both are on the deny list.
