# M5 1d — section detection before/after

Before = scorer at 5d385ac, after = 20503b9. Same current fact bank, taxonomy keywords only (no LLM). Keyword counts are total (req/pref/neutral).

| JD | headings before | headings after | keywords before | keywords after | ceiling before | ceiling after |
|---|---:|---:|---|---|---:|---:|
| chattermill | 2 | 8 | 20 (9/5/6) | 18 (10/2/6) | 0.875 | 0.871 |
| creditspring | 2 | 4 | 18 (10/4/4) | 18 (10/4/4) | 0.867 | 0.867 |
| Heathrow | 1 | 1 | 7 (0/0/7) | 7 (0/0/7) | 0.857 | 0.857 |
| lendable | 1 | 6 | 12 (7/0/5) | 8 (5/0/3) | 0.907 | 0.862 |
| quantcast | 0 | 9 | 19 (0/0/19) | 19 (0/0/19) | 0.895 | 0.895 |
| trainline | 1 | 2 | 12 (5/4/3) | 2 (0/0/2) | 0.622 | 1.000 |
| zoneco | 0 | 5 | 17 (0/2/15) | 17 (11/0/6) | 0.653 | 0.661 |

## chattermill

Ceiling 0.875 → **0.871** · keywords 20 (9/5/6) → 18 (10/2/6) (total (req/pref/neutral))


### Headings before (only lines that switched mode)

- L21 `What you’ll need:` → req
- L31 `➕It would be a bonus if you:` → pref

### Headings after

- L5 `🦸‍♀️ The Role 🦸‍♀️` → neutral
- L11 `What you'll be doing:` → neutral
- L21 `What you’ll need:` → req
- L31 `➕It would be a bonus if you:` → pref
- L33 `🔎 Our Hiring Process` → skip
- L39 `Our Perks` → skip
- L57 `💖 Our Values` → skip
- L72 `🌈 Diversity & Inclusion 🌈` → skip

### Keyword weights (changed only)

| term | before | after | in fact bank |
|---|---:|---:|---|
| healthcare | 1.0 | — | yes |
| pytorch | 1.0 | 2.0 | yes |
| stakeholders | 1.0 | — | yes |

Gaps before: statistics, communication  
Gaps after: statistics, communication


## creditspring

Ceiling 0.867 → **0.867** · keywords 18 (10/4/4) → 18 (10/4/4) (total (req/pref/neutral))


### Headings before (only lines that switched mode)

- L33 `What you'll need to succeed` → req
- L45 `Desirable experience:` → pref

### Headings after

- L7 `About the role` → neutral
- L19 `Responsibilities` → neutral
- L33 `What you'll need to succeed` → req
- L45 `Desirable experience:` → pref

### Keyword weights (changed only)

| term | before | after | in fact bank |
|---|---:|---:|---|

Gaps before: feature engineering, communication  
Gaps after: feature engineering, communication


## Heathrow

Ceiling 0.857 → **0.857** · keywords 7 (0/0/7) → 7 (0/0/7) (total (req/pref/neutral))


### Headings before (only lines that switched mode)

- L73 `Desirable` → pref

### Headings after

- L73 `Desirable` → pref

### Keyword weights (changed only)

| term | before | after | in fact bank |
|---|---:|---:|---|

Gaps before: statistical modelling  
Gaps after: statistical modelling


## lendable

Ceiling 0.907 → **0.862** · keywords 12 (7/0/5) → 8 (5/0/3) (total (req/pref/neutral))


### Headings before (only lines that switched mode)

- L43 `Strong engineering skills and experience building production systems.` → req

### Headings after

- L1 `About Lendable` → skip
- L31 `What you will do` → neutral
- L42 `What we are looking for` → req
- L55 `Why join us` → skip
- L60 `Interview process` → skip
- L71 `Life at Lendable` → skip

### Keyword weights (changed only)

| term | before | after | in fact bank |
|---|---:|---:|---|
| agents | 2.0 | — | yes |
| fintech | 2.0 | — | yes |
| machine learning | 1.5 | — | yes |
| python | 1.5 | — | yes |

Gaps before: communication  
Gaps after: communication


## quantcast

Ceiling 0.895 → **0.895** · keywords 19 (0/0/19) → 19 (0/0/19) (total (req/pref/neutral))


### Headings before (only lines that switched mode)

- none

### Headings after

- L11 `What you'll do:` → neutral
- L20 `Who you are:` → req
- L21 `Experience:` → neutral
- L23 `Academic Background:` → neutral
- L25 `Technical Foundation:` → neutral
- L27 `Analytical Rigor:` → neutral
- L29 `ML Knowledge:` → neutral
- L31 `Data & ML Ecosystem:` → neutral
- L33 `Growth Mindset:` → neutral

### Keyword weights (changed only)

| term | before | after | in fact bank |
|---|---:|---:|---|

Gaps before: scalable, statistics  
Gaps after: scalable, statistics


## trainline

Ceiling 0.622 → **1.000** · keywords 12 (5/4/3) → 2 (0/0/2) (total (req/pref/neutral))


### Headings before (only lines that switched mode)

- L24 `We'd love to hear from you if you have... 🔎` → req

### Headings after

- L6 `About us` → skip
- L34 `More information:` → neutral

### Keyword weights (changed only)

| term | before | after | in fact bank |
|---|---:|---:|---|
| cross-functional | 1.5 | — | gap |
| data engineering | 1.5 | — | gap |
| data visualisation | 1.0 | — | gap |
| dbt | 2.0 | — | gap |
| healthcare | 2.0 | 1.5 | yes |
| insurance | 2.0 | 1.5 | yes |
| machine learning | 1.5 | — | yes |
| power bi | 1.0 | — | gap |
| python | 1.0 | — | yes |
| sql | 2.0 | — | yes |
| stakeholders | 2.0 | — | yes |
| tableau | 1.0 | — | yes |

Gaps before: cross-functional, data engineering, data visualisation, power bi, dbt  
Gaps after: —


## zoneco

Ceiling 0.653 → **0.661** · keywords 17 (0/2/15) → 17 (11/0/6) (total (req/pref/neutral))


### Headings before (only lines that switched mode)

- none

### Headings after

- L9 `About the Job:` → neutral
- L13 `What You'll Do:` → neutral
- L29 `What You'll Bring:` → req
- L42 `Benefits` → skip
- L52 `Employment Terms` → skip

### Keyword weights (changed only)

| term | before | after | in fact bank |
|---|---:|---:|---|
| agents | 1.5 | 2.0 | yes |
| aws | 1.0 | 2.0 | yes |
| azure | 1.0 | 2.0 | gap |
| communication | 1.5 | 2.0 | gap |
| javascript | 1.5 | 2.0 | yes |
| langgraph | 1.5 | 2.0 | yes |
| llms | 1.5 | 2.0 | yes |
| problem-solving | 1.5 | 2.0 | gap |
| production | 1.5 | 2.0 | yes |
| python | 1.5 | 2.0 | yes |
| sql | 1.5 | 2.0 | yes |

Gaps before: scalable, end-to-end, experimentation, azure, problem-solving, communication  
Gaps after: scalable, end-to-end, experimentation, azure, problem-solving, communication

