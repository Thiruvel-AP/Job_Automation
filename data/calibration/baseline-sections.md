# Calibration baseline — section detection (before M5 step 1d)

Generated 2026-10-02 from the current `careerops/tailoring/scorer.py` (`taxonomy_keywords`, weights req=2.0 / pref=1.0, neutral=1.5). Taxonomy keywords only — no LLM keywords, so these ceilings exclude anything the LLM extractor would add.

Heading rule today: a line counts as a heading candidate if it is < 80 chars and does not start with `-`. `PREF_HEAD` wins over `REQ_HEAD`; a `none` candidate leaves the mode unchanged.


## cal-chattermill — Chattermill, Data Scientist

File `data/calibration/jds/chattermill.txt` · 74 lines · **ceiling 0.875** (literal-in-fact-bank 0.797) · 20 keywords (9 req, 5 pref, 6 neutral)


### Heading candidates

| line | detected as | mode after | text |
|---:|---|---|---|
| 1 | none | neutral | Data Scientist |
| 2 | none | neutral | 🌍 UK (Remote or Hybrid, it’s up to you!) |
| 3 | none | neutral | 💰 Dependent on experience |
| 4 | none | neutral | 📈 Be part of our success with the opportunity to join our company equity scheme |
| 5 | none | neutral | 🦸‍♀️ The Role 🦸‍♀️ |
| 7 | none | neutral | We're hiring a |
| 8 | none | neutral | Data Scientist |
| 9 | none | neutral | to join the team and help build and ship the next generation of that stack. |
| 10 | none | neutral | 👉 |
| 11 | none | neutral | What you'll be doing: |
| 20 | none | neutral | 🧰 |
| 21 | required | req | What you’ll need: |
| 23 | inline-pref (long line, mode unchanged) | req | Proficiency in PyTorch, including training loops and standard model fine-tuning workflows;… |
| 29 | none | req | Genuine curiosity about AI and a habit of experimenting — you learn by doing. |
| 31 | preferred | pref | ➕It would be a bonus if you: |
| 33 | none | pref | 🔎 Our Hiring Process |
| 36 | none | pref | Show us how you work – you'll complete a short take home assignment |
| 39 | none | pref | Our Perks |
| 40 | none | pref | 🤸🏽‍♂️ Flexibility & Work Setup |
| 42 | none | pref | Work-from-home allowance to set up your ideal workspace |
| 43 | none | pref | 🌴 Time Off |
| 45 | none | pref | Your birthday off |
| 46 | none | pref | 🌱 Growth & Ownership |
| 48 | none | pref | Equity options — share in the company’s success |
| 49 | none | pref | 💚 Health & Wellbeing |
| 50 | none | pref | Monthly health & wellbeing budget, increasing with length of service |
| 51 | none | pref | Optional private healthcare plan |
| 52 | none | pref | Life assurance & income protection (location dependent) |
| 54 | none | pref | Enhanced family leave (location dependent), plus fertility and neonatal leave |
| 55 | none | pref | 🌆 Office Perks |
| 57 | none | pref | 💖 Our Values |
| 58 | none | pref | We are obsessed with experience |
| 60 | none | pref | We believe in the power of trust |
| 62 | none | pref | We act as responsible owners |
| 64 | none | pref | We share a passion for growth & progress |
| 66 | none | pref | We set our ambitions high but stay humble |
| 68 | none | pref | We believe the right team is the key to success |
| 70 | none | pref | you |
| 71 | none | pref | to apply today! |
| 72 | none | pref | 🌈 Diversity & Inclusion 🌈 |

### Keywords

| term | weight | class | in fact bank |
|---|---:|---|---|
| agentic | 2 | req | literal |
| classification | 2 | req | literal |
| communication | 2 | req | **gap** |
| embeddings | 2 | req | literal |
| fine-tuning | 2 | req | literal |
| genai | 2 | req | literal |
| nlp | 2 | req | literal |
| production | 2 | req | literal |
| statistics | 2 | req | **gap** |
| data pipelines | 1.5 | neutral | stemmed |
| evaluation | 1.5 | neutral | literal |
| llm | 1.5 | neutral | literal |
| llms | 1.5 | neutral | literal |
| research | 1.5 | neutral | literal |
| retrieval | 1.5 | neutral | literal |
| healthcare | 1 | pref | literal |
| lora | 1 | pref | literal |
| machine learning | 1 | pref | literal |
| pytorch | 1 | pref | literal |
| stakeholders | 1 | pref | stemmed |

Genuine gaps: statistics, communication


## cal-creditspring — Creditspring, Data Scientist

File `data/calibration/jds/creditspring.txt` · 55 lines · **ceiling 0.867** (literal-in-fact-bank 0.750) · 18 keywords (10 req, 4 pref, 4 neutral)


### Heading candidates

| line | detected as | mode after | text |
|---:|---|---|---|
| 7 | none | neutral | About the role |
| 19 | none | neutral | Responsibilities |
| 33 | required | req | What you'll need to succeed |
| 45 | preferred | pref | Desirable experience: |
| 47 | none | pref | Lending, fintech and regulated sectors work experience |

### Keywords

| term | weight | class | in fact bank |
|---|---:|---|---|
| communication | 2 | req | **gap** |
| feature engineering | 2 | req | **gap** |
| machine learning | 2 | req | literal |
| numpy | 2 | req | literal |
| pandas | 2 | req | literal |
| python | 2 | req | literal |
| regression | 2 | req | literal |
| scikit-learn | 2 | req | literal |
| sql | 2 | req | literal |
| stakeholders | 2 | req | stemmed |
| data pipelines | 1.5 | neutral | stemmed |
| model deployment | 1.5 | neutral | literal |
| production | 1.5 | neutral | literal |
| testing | 1.5 | neutral | literal |
| agentic | 1 | pref | literal |
| aws | 1 | pref | literal |
| fintech | 1 | pref | literal |
| research | 1 | pref | literal |

Genuine gaps: feature engineering, communication


## cal-lendable — Lendable, AI Engineer

File `data/calibration/jds/lendable.txt` · 90 lines · **ceiling 0.907** (literal-in-fact-bank 0.744) · 12 keywords (7 req, 0 pref, 5 neutral)


### Heading candidates

| line | detected as | mode after | text |
|---:|---|---|---|
| 1 | none | neutral | About Lendable |
| 4 | none | neutral | One of the UK’s most exciting unicorns with a team of over 800 people |
| 6 | none | neutral | Among the fastest-growing tech companies in the UK |
| 8 | none | neutral | Profitable since 2017 |
| 10 | none | neutral | Backed by top investors including Balderton Capital and Goldman Sachs |
| 18 | none | neutral | Join us if you want to |
| 31 | none | neutral | What you will do |
| 36 | none | neutral | Design LLM-driven workflows that simplify case management. |
| 38 | none | neutral | Find new opportunities to replace manual work with effective AI systems. |
| 42 | none | neutral | What we are looking for |
| 43 | required | req | Strong engineering skills and experience building production systems. |
| 45 | none | req | Hands-on experience using LLMs in real workflows. |
| 55 | none | req | Why join us |
| 56 | none | req | Work with LLMs, retrieval systems, agents and automation pipelines every day. |
| 60 | none | req | Interview process |
| 61 | none | req | Introductory call |
| 63 | none | req | Take-home exercise |
| 65 | none | req | Take home debrief |
| 67 | none | req | Case study |
| 69 | none | req | Onsite final interview |
| 71 | none | req | Life at Lendable |
| 82 | inline-pref (long line, mode unchanged) | req | Employee referral programme: earn a competitive bonus when you refer successful new team m… |
| 90 | none | req | Check out our blog! |

### Keywords

| term | weight | class | in fact bank |
|---|---:|---|---|
| agents | 2 | req | literal |
| communication | 2 | req | **gap** |
| fintech | 2 | req | literal |
| llms | 2 | req | literal |
| production | 2 | req | literal |
| retrieval | 2 | req | literal |
| stakeholders | 2 | req | stemmed |
| evaluation | 1.5 | neutral | literal |
| llm | 1.5 | neutral | literal |
| machine learning | 1.5 | neutral | literal |
| prototyping | 1.5 | neutral | stemmed |
| python | 1.5 | neutral | literal |

Genuine gaps: communication


## cal-zoneco — Zone & Co, AI Software Engineer

File `data/calibration/jds/zoneco.txt` · 60 lines · **ceiling 0.653** (literal-in-fact-bank 0.592) · 17 keywords (0 req, 2 pref, 15 neutral)


### Heading candidates

| line | detected as | mode after | text |
|---:|---|---|---|
| 1 | none | neutral | AI Software Engineer |
| 2 | none | neutral | United Kingdom |
| 9 | none | neutral | About the Job: |
| 13 | none | neutral | What You'll Do: |
| 16 | none | neutral | Build and deploy end-to-end AI/ML pipelines for key product features. |
| 19 | none | neutral | Integrate AI/ML models into backend and frontend applications. |
| 20 | none | neutral | Analyze large datasets to extract insights and support data-driven decisions. |
| 23 | none | neutral | Lead experimentation and prototyping efforts to test new ideas and algorithms. |
| 24 | none | neutral | Participate in code reviews and mentor junior engineers. |
| 27 | none | neutral | Stay current with emerging AI research, frameworks, and methodologies. |
| 28 | none | neutral | Contribute to infrastructure and cloud service decisions for AI workflows. |
| 29 | none | neutral | What You'll Bring: |
| 31 | none | neutral | 2+ years of software engineering experience with a focus on AI/ML applications. |
| 32 | inline-pref (long line, mode unchanged) | neutral | Bachelor’s degree in Computer Science, AI, Data Science, or related field (Master’s prefer… |
| 34 | inline-pref (long line, mode unchanged) | neutral | Proficient in Python with familiarity in JavaScript or similar languages; knowledge of sta… |
| 35 | none | neutral | Strong understanding of both relational (SQL) and non-relational databases. |
| 36 | none | neutral | Experience deploying AI systems in production environments. |
| 37 | inline-pref (long line, mode unchanged) | neutral | Familiarity with cloud platforms (AWS or Azure); containerization experience is a bonus.… |
| 38 | none | neutral | Excellent problem-solving, analytical, and critical-thinking abilities. |
| 39 | none | neutral | Strong communication skills in English—both written and verbal. |
| 40 | none | neutral | Passion for innovation and using technology to create business value. |
| 41 | none | neutral | Commitment to ethical AI development and continuous professional growth. |
| 42 | none | neutral | Benefits |
| 48 | none | neutral | Join Our Global Mission |
| 52 | none | neutral | Employment Terms |
| 54 | none | neutral | United States |
| 58 | none | neutral | Non-US Jurisdictions |

### Keywords

| term | weight | class | in fact bank |
|---|---:|---|---|
| agents | 1.5 | neutral | literal |
| communication | 1.5 | neutral | **gap** |
| data quality | 1.5 | neutral | literal |
| end-to-end | 1.5 | neutral | **gap** |
| experimentation | 1.5 | neutral | **gap** |
| javascript | 1.5 | neutral | literal |
| langgraph | 1.5 | neutral | literal |
| llms | 1.5 | neutral | literal |
| problem-solving | 1.5 | neutral | **gap** |
| production | 1.5 | neutral | literal |
| prototyping | 1.5 | neutral | stemmed |
| python | 1.5 | neutral | literal |
| research | 1.5 | neutral | literal |
| scalable | 1.5 | neutral | **gap** |
| sql | 1.5 | neutral | literal |
| aws | 1 | pref | literal |
| azure | 1 | pref | **gap** |

Genuine gaps: scalable, end-to-end, experimentation, azure, problem-solving, communication

