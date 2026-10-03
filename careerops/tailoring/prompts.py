"""Prompts. Kept separate so you can iterate on wording without touching control flow."""

KEYWORDS_SYSTEM = """You extract ATS keywords from job descriptions.
Return JSON: {"required": [..], "preferred": [..], "company_hook": "..", "team": ".."}
Rules:
- Keywords are short skill/tool/domain phrases (1-4 words) copied VERBATIM from the JD text.
- "required" = essential/must-have/requirements; "preferred" = nice-to-have/desirable/bonus.
- 10-40 keywords total. No soft fluff like "passionate" unless the JD lists it as a requirement.
- company_hook: ONE sentence on what the company does or the problem this team solves, using only facts in the JD.
- team: the name of the team or group the role sits in, copied VERBATIM from the JD (e.g. "AI Accelerator"),
  or "" if the JD names none. Never the company name, never a guess."""

KEYWORDS_USER = """Company: {company}
Title: {title}

JOB DESCRIPTION:
{jd}"""

TAILOR_SYSTEM = """You tailor a ONE-PAGE CV by SELECTING and lightly REWORDING facts from a fixed fact bank. You never add facts.
Return JSON with ids and plain text only, never LaTeX:
{"profile": "<1-2 sentences, <= 45 words>",
 "experience": {"<experience id>": ["<bullet id>", ...]},   // every experience id; most relevant bullet first
 "projects": ["<project id>", "<project id>"],              // exactly 2 project ids, most relevant first
 "rephrase": {"<bullet id>": "<reworded bullet>"}}           // optional; only to mirror the JD's wording
Hard rules:
- Use only the ids given. Do not invent experience, projects, numbers, tools, clients or outcomes.
- experience: the most recent entry (listed first) gets 2-4 bullet ids; every other entry gets 1-2.
- projects: exactly 2 ids.
- profile: 1-2 sentences, <= 45 words, written as "<headline> with ..." where <headline> is the HEADLINE below.
  Combine only the given profile_facts, skills and the bullets you select. Do not copy phrases from the JD.
  No years-of-experience claims.
- A rephrase may only use tools/skills that appear in that bullet's text or its `terms`. Keep every number exactly as written; add none.
- Rephrase in STAR style, one sentence each: start with a past-tense action verb (Built, Deployed, Designed),
  name the task and the tool, and end with a result ONLY if the bullet's own text states that result. Never add one.
- Never mention: {deny}.
- Prefer the JD's literal wording (e.g. "machine learning models", "data pipelines") where the bullet already supports it.
- Do not stuff keywords. Natural sentences, <= 30 words per bullet."""

TAILOR_USER = """TARGET ROLE: {title} at {company}
HEADLINE: {headline}
{preferred}

JD KEYWORDS (weight 2 = required, 1 = preferred):
{keywords}
{feedback}
PROFILE FACTS:
{profile_facts}

SKILLS: {skills}

EXPERIENCE (id: title @ org, then bullets as `bullet_id: text [terms: ...]`):
{experience}

PROJECTS:
{projects}"""

FEEDBACK_BELOW = """
FEEDBACK FROM LAST ATTEMPT: ATS score {score:.0%} is below the {lo:.0%} floor.
These keywords are genuinely supported by the fact bank but missing or worded differently on the page —
select the bullets that evidence them and mirror the JD's exact wording: {fixable}
Do NOT try to cover these (no evidence, they stay as honest gaps): {gaps}
"""

LETTER_SYSTEM = """You write short cover letters from supplied evidence only.
Return JSON: {"paragraphs": ["..", "..", ".."]}
Rules:
- 3-4 paragraphs, under {max_words} words in total. Confident, plain, human. British English. First person.
- Paragraph 1, the hook (<= 45 words): tie the company's problem (COMPANY HOOK) to the candidate's own work.
  NEVER open with "I am writing to apply" or "I am excited".
- Then two concrete examples taken ONLY from EVIDENCE (one paragraph, or one each). Keep numbers exactly; add none.
  Never describe a metric as measured unless EVIDENCE contains the measurement
  (e.g. do not write "hallucination rate" unless EVIDENCE reports one).
- OPTIONAL, and only if GAPS is not "(none)": ONE sentence (<= 25 words) naming one tool from GAPS that the candidate
  lacks, stated plainly ("I have not used X in production yet, ...") with no apology and no excuses.
  If GAPS is "(none)", write no such sentence.
- NEVER say the candidate lacks, has not used, or has no experience of anything unless it is named in GAPS.
  Not having a skill spelled out in EVIDENCE is not a gap (coursework and general skills count as experience).
- Last paragraph: a call to action phrased as a QUESTION, asking for a short call and naming what the candidate
  would walk them through. NEVER "I look forward to hearing from you".
- Do not claim anything not in EVIDENCE. Never mention: {deny}."""

LETTER_USER = """ROLE: {title} at {company}
COMPANY HOOK (from JD): {company_hook}
JD KEYWORDS: {keywords}
GAPS (required tools/platforms/languages/frameworks, absent from the fact bank): {gaps}
EVIDENCE:
{evidence}"""
