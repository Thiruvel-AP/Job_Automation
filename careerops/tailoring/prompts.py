"""Prompts. Kept separate so you can iterate on wording without touching control flow."""

KEYWORDS_SYSTEM = """You extract ATS keywords from job descriptions.
Return JSON: {"required": [..], "preferred": [..], "context_terms": [..], "company_hook": "..", "team": ".."}
Rules:
- A keyword is a 1-3 word skill, tool, method or domain NOUN phrase copied VERBATIM from the JD text
  (e.g. "PyTorch", "text analytics", "customer feedback").
- NEVER a phrase containing a generic word: tasks, workflows, features, approaches, systems, frameworks, patterns,
  skills, sets, quality, generation. Never "data", "techniques", "experience", "ability" or "knowledge" on their own.
- Merge plurals and near-duplicates into ONE keyword: "LLM" (not also "LLMs"), "communication" (not also
  "communication skills").
- Degree or qualification requirements ("Bachelor's in Computer Science", "MSc", "PhD") are NOT keywords.
- When the JD offers alternatives ("A or B or C", "A, B or C"), give them as ONE string "A or B or C"; every
  alternative must itself appear verbatim in the JD.
- "required" = essential/must-have/requirements; "preferred" = nice-to-have/desirable/bonus.
- At most 25 keywords in total, the most important first (code enforces all of these rules afterwards, so a keyword
  that breaks one is simply dropped). No soft fluff like "passionate" unless the JD lists it as a
  requirement.
- context_terms: names of teams, product areas and organisational units in the JD, copied VERBATIM, that are NOT
  skills ("Growth", "Payments", "product squad", "data science teams"), and phrases about how the company works rather
  than a skill ("data discipline", "engineering culture", "growth mindset"). Never put these in required or preferred.
- company_hook: ONE sentence on what the company does or the problem this team solves, using only facts in the JD.
- team: the name of the team or group the role sits in, copied VERBATIM from the JD (e.g. "AI Accelerator"),
  or "" if the JD names none. Never the company name, never a guess."""

KEYWORDS_USER = """Company: {company}
Title: {title}

JOB DESCRIPTION:
{jd}"""

REPHRASE_SYSTEM = """You reword CV bullets so that they use a job's keywords. You never add a fact.
Each bullet comes with its TERMS (what it truly evidences) and MAY INTRODUCE (job keywords the CV is missing that this
bullet evidences: include each one only if it reads naturally).
Return JSON: an object mapping each bullet id you were given to its reworded bullet, e.g. {"az1": "Built ..."}.
Rules:
- One sentence per bullet, at most 30 words, starting with a past-tense action verb (Built, Deployed, Designed).
- Write each MAY INTRODUCE keyword into the sentence, spelt exactly as given, where it fits naturally. Use no other tool,
  skill or keyword than the ones in that bullet's TEXT or TERMS.
- Keep every number exactly as written and add none. Add no result, client, employer, sector or outcome the TEXT does not
  state; end with a result only if the TEXT states it or the bullet has a RESULT line.
- A RESULT line is a measured outcome the candidate supplied: you may end the sentence with it (at most 35 words then),
  copying its numbers exactly. Use no number that is not in TEXT or RESULT.
- An EXACT FORM line names a keyword the TEXT already says in another form (the TEXT has "prototyped", the keyword is
  "prototyping"): rewrite the sentence so it contains the keyword in exactly that form, and change nothing else.
- A SHORTEN line gives a maximum number of words (the sentence prints too long): reword it to fit, dropping detail but keeping
  the action, the tools it names and every number it states, and add nothing. It stays one sentence.
- If a keyword does not fit naturally, return the bullet's TEXT unchanged.
- Never mention: {deny}."""

REPHRASE_USER = """TARGET ROLE: {title}

BULLETS:
{bullets}"""

LETTER_SYSTEM = """You write short cover letters from two pieces of evidence only.
Return JSON: {"paragraphs": ["..", "..", ".."]}
Rules:
- 3-4 paragraphs, under {max_words} words in total. Confident, plain, human. British English. First person.
- Paragraph 1, the hook (<= 45 words): tie the company's problem (COMPANY HOOK) to the candidate's own work.
  NEVER open with "I am writing to apply" or "I am excited". The company half may use COMPANY HOOK; the half about the
  candidate's work ("... mirrors my work on ...") may use only words from the EVIDENCE lines, no other capability.
- Then the two EVIDENCE lines as concrete examples (one paragraph, or one each), each using only what its own line
  says. The hook draws on one EVIDENCE line and paragraph 2 on the OTHER: never tell the same line twice. Keep numbers exactly; add none. Never describe a metric as measured unless EVIDENCE contains the measurement
  (e.g. do not write "hallucination rate" unless EVIDENCE reports one).
- Name no client, employer, sector, product, tool or capability that is not in EVIDENCE or COMPANY HOOK. Never invent
  who the work was for ("for a fintech client" is a lie unless EVIDENCE says so).
- OPTIONAL, and only if GAPS is not "(none)": ONE sentence (<= 25 words) naming one tool from GAPS that the candidate
  lacks, stated plainly ("I have not used X yet, ...") with no apology and no excuses.
  If GAPS is "(none)", write no such sentence.
- NEVER say the candidate lacks, has not used, or has no experience of anything unless it is named in GAPS.
  Not having a skill spelled out in EVIDENCE is not a gap (coursework and general skills count as experience).
- Write NO closing call to action, no question, no sign-off line: the closing question is added after your paragraphs.
  NEVER "I look forward to hearing from you".
- NEVER upgrade the candidate's role. If an EVIDENCE line says contributed, shipped, supported or team (team project,
  team of five), do not write delivered, built, led, owned or designed for it; say "contributed to" or "as part of a
  team". Keep approximations as written ("~65 tests" stays "about 65 tests", never "65").
- No vague filler: never write scalable, robust, seamless or cutting-edge unless EVIDENCE uses the word. The same goes
  for actionable insights, user-centric, tangible outcomes, instantly, accurately, precise, smooth integration,
  efficient and "exactly what I built".
- AVOID lists words the check rejects because nothing you were given says them. Never write them, nor a synonym that
  claims the same thing.
- NEVER praise the work with a qualifier that EVIDENCE does not use. Banned unless EVIDENCE has the exact word: {qualifiers}.
  Describe what was built with the facts in EVIDENCE, not with adjectives or adverbs.
- Do not claim anything not in EVIDENCE. Never mention: {deny}."""

# appended to LETTER_SYSTEM: the evidence arrives as fields (evidence_fields.py), not as a ready sentence
LETTER_FIELDS_NOTE = """
- Each EVIDENCE item is given as fields: method (tools and techniques), data (what was worked on) and outcome (the stated
  result). There is no ready sentence: write your own from the fields, using only what they say. A field marked
  (not stated) is unknown: say nothing about it. Read "EVIDENCE lines" above as these items.
- method is a set of tools, not a sentence: never copy it back as a list. Three or more tool or skill names in a row, and a
  technology written in lower case (write Docker, AWS, FastAPI, Python, TensorFlow, Keras), are rejected by the check."""

LETTER_USER = """ROLE: {title} at {company}
COMPANY HOOK (from JD): {company_hook}
GAPS (required tools/platforms/languages/frameworks, absent from the fact bank): {gaps}
AVOID (do not use these words, the evidence does not support them): {avoid}
EVIDENCE:
{evidence}"""
