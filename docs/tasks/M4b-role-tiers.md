# Task M4b — Role tiers: Data Scientist & AI Engineer first, then MLE, then Data Analyst (junior–mid)

**Goal.** Make the pipeline spend its limited capacity (LLM quota, and above all the user's review time) in the
user's priority order:

| Tier | Family | Examples |
|---|---|---|
| 1 | Data Scientist | Data Scientist, Decision Scientist, Applied Data Scientist |
| 1 | AI Engineer | AI Engineer, AI/ML Engineer, LLM / GenAI Engineer, Applied AI Engineer, AI Software Engineer |
| 2 | ML Engineer | Machine Learning Engineer, ML Engineer, MLOps Engineer, Applied Scientist, NLP Engineer |
| 3 | Data Analyst | Data Analyst, Product / Insight Analyst, BI Analyst, Analytics Engineer, Reporting Analyst |

Levels: **junior and mid only** for every tier. Titles outside these families are skipped with a reason.

**When:** after M5 Part 0 is green, **before** M5 Part 2 (calibration must measure the system the user will actually use).

**Read first:** `CLAUDE.md`, `careerops/discovery/gates.py`, `careerops/cli.py` (`triage_new`, `cmd_tailor`, `cmd_review`),
`careerops/tailoring/{tailor,prompts,scorer}.py`, `careerops/llm/mock.py`, `config/settings.yaml → gates`.

---

## 1. Config — replace `gates.title_include` / `gates.title_exclude` with a `roles` block
```yaml
roles:
  # Lower tier = higher priority. Patterns are case-insensitive regexes on the job TITLE.
  # Families are tried in tier order, so an ambiguous title ("Data Scientist / ML Engineer") takes the higher tier.
  families:
    - {name: data_scientist, tier: 1, headline: Data Scientist,
       title: ['data scien', 'decision scien'],
       projects: [churn, bitcoin, lloyds, gtaigraph]}
    - {name: ai_engineer, tier: 1, headline: AI Engineer,
       title: ['\bai engineer', '\bai/ml engineer', '\b(llm|genai|generative ai) engineer', 'applied ai', '\bai (software )?(engineer|developer)', 'agent(ic)? (ai )?engineer'],
       projects: [agenticfriend, mcpsearch, coverfit, lloyds]}
    - {name: ml_engineer, tier: 2, headline: Machine Learning Engineer,
       title: ['machine learning engineer', '\bml engineer', '\bmlops', 'applied scientist', '\bnlp engineer'],
       projects: [agenticfriend, bitcoin, gtaigraph, coverfit]}
    - {name: data_analyst, tier: 3, headline: Data Analyst,
       title: ['data analyst', 'insights? analyst', 'product analyst', 'analytics engineer', '\bbi analyst', 'business intelligence analyst', 'reporting analyst'],
       projects: [churn, lloyds, bitcoin, coverfit]}
  levels:
    exclude: ['\bsenior\b', '\bsr\.?\b', '\bstaff\b', '\bprincipal\b', '\blead\b', '\bhead of\b', '\bdirector\b',
              '\bmanager\b', '\bvp\b', '\b(iii|iv)\b', '\bintern(ship)?\b']
  floor: 0.70                 # same as scoring.band[0]
  tier3_max_share: 0.20       # tier 3 gets at most 20 % of a run's tailoring slots unless tiers 1–2 run out
```
Notes for implementation:
- The old `intern` pattern also matched "International" and "Internal" — the new `\bintern(ship)?\b` fixes that; add a regression test.
- Project ids must exist in `fact_bank.yaml`; validate at load and fail with a clear message if one doesn't (the user may rename ids).
- Keep `gates.jd_rules`, `locations_ok`, `max_years_required` as they are.

## 2. Classification at triage (`gates.py`)
- `classify_role(title, settings) -> (family, tier) | None`. No family → skip, reason `"title not in target families"`. Level excluded → skip, reason `"level excluded (<pattern>)"`.
- New `jobs` columns (additive `ALTER TABLE`): `family TEXT`, `tier INTEGER`, `ceiling_pre REAL`.
- `ceiling_pre`: honest ceiling computed **at triage with no LLM** — `scorer.ceiling(fact_text(fb), taxonomy_keywords(jd))`. Cheap, deterministic, and good enough to order the queue.

## 3. Queue ordering — one key everywhere (tailor, review, apply, Excel)
Sort ascending by

  k(j) = ( 1[ceiling_pre_j < floor],  tier_j,  −ceiling_pre_j,  −posted_or_first_seen_j )

i.e. every job that can honestly reach the 70 % floor comes before any that can't; within that, tier 1 before 2
before 3; within a tier, best honest fit first; ties go to the freshest posting.

**Run quota** for `careerops tailor --limit N`: take the sorted queue; tier-3 jobs fill at most
`⌊tier3_max_share · N⌋` slots; if tiers 1–2 have fewer than `N − ⌊tier3_max_share · N⌋` eligible jobs, the
remaining slots go to tier 3. (Example: N = 25 → tier 3 is capped at 5 slots while tiers 1–2 can fill 20; if tiers 1–2 have only 8 eligible jobs, tier 3 may take the remaining 17.)

## 4. Tailoring uses the family (`tailor.py`, `prompts.py`, `mock.py`)
- CV headline = the family's `headline` (fallback: `identity.headline`).
- `TAILOR_USER` gets one line: `Preferred projects for a <headline> role (use if relevant to the JD): <ids>`. It is a prior, not an order — relevance to the JD still wins.
- `MockLLM` breaks ties in project selection using the preferred list (deterministic tests).
- `report.json` records `family` and `tier`.

## 5. Visibility
- `careerops status` adds a table: tier × status counts.
- Excel Jobs sheet: `Tier`, `Family`, `Ceiling (pre)` columns; default sort = the key above.
- `review` header line shows `T1 · Data Scientist` etc.

## Tests (no network, MockLLM only)
`tests/discovery/test_roles.py`
1. Each example title in the table above maps to the right family/tier; "Data Scientist / ML Engineer" → tier 1 DS.
2. Excluded levels: Senior, Lead, Staff, Principal, Manager, "Data Scientist III", "Data Science Intern" → skipped with the level reason.
3. Regression: "International Data Analyst" and "Internal Tools AI Engineer" are **not** excluded.
4. Out-of-family titles (Data Engineer, Research Scientist, Software Engineer) → skipped with the family reason.
5. Unknown project id in config → clear load error.

`tests/tailoring/test_queue_order.py`
6. Sorting key: a tier-3 job with ceiling 0.85 comes after a tier-1 job with ceiling 0.72, but before a tier-1 job with ceiling 0.60.
7. Quota: 30 eligible jobs (20 T1/T2, 10 T3), N = 25 → 20 T1/T2 + 5 T3; with only 8 T1/T2 → 8 + 17 T3.
8. `ceiling_pre` is computed at triage without any LLM call (MockLLM call count = 0).
9. Headline and preferred-project prior reach the prompt; `report.json` carries family/tier.

## Done when
- [ ] Tests pass; full `pytest -q` green; `careerops run --mock --limit 3` completes.
- [ ] Existing jobs in `data/careerops.db` get `family/tier/ceiling_pre` back-filled by `careerops retriage` (new command: re-runs triage on every non-applied job; never touches `applied` rows).
- [ ] `fact_bank.yaml` unchanged.
- [ ] README + `docs/IMPLEMENTATION.md` updated (roles block, ordering key, quota).
- [ ] Commits: (1) tests, (2) roles config + classification + retriage, (3) ordering + quota, (4) tailoring prior + visibility + docs.
