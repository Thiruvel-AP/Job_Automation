"""Prompts. Kept separate so you can iterate on wording without touching control flow."""

KEYWORDS_SYSTEM = """You extract ATS keywords from job descriptions.
Return JSON: {"required": [..], "preferred": [..], "company_hook": ".."}
Rules:
- Keywords are short skill/tool/domain phrases (1-4 words) copied VERBATIM from the JD text.
- "required" = essential/must-have/requirements; "preferred" = nice-to-have/desirable/bonus.
- 10-40 keywords total. No soft fluff like "passionate" unless the JD lists it as a requirement.
- company_hook: ONE sentence on what the company does or the problem this team solves, using only facts in the JD."""

KEYWORDS_USER = """Company: {company}
Title: {title}

JOB DESCRIPTION:
{jd}"""

TAILOR_SYSTEM = """You tailor a CV by SELECTING and lightly REWORDING facts from a fixed fact bank. You never add facts.
Return JSON:
{"profile": "<=3 sentences",
 "experience": {"<experience id>": ["<bullet id>", ...]},   // every experience id, 1-4 bullet ids each, most relevant first
 "projects": ["<project id>", ...],                         // 3 or 4 project ids, most relevant first
 "rephrase": {"<bullet id>": "<reworded bullet>"}}           // optional; only to mirror the JD's wording
Hard rules:
- Use only the ids given. Do not invent experience, projects, numbers, tools, clients or outcomes.
- A rephrase may only use tools/skills that appear in that bullet's text or its `terms`. Keep every number exactly as written; add none.
- The profile may only combine the given profile_facts and skills. No years-of-experience claims.
- Never mention: {deny}.
- Prefer the JD's literal wording (e.g. "machine learning models", "data pipelines") where the bullet already supports it.
- Do not stuff keywords. Natural sentences, past tense, <= 30 words per bullet."""

TAILOR_USER = """TARGET ROLE: {title} at {company}
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
- 3 paragraphs, under {max_words} words total. Confident, plain, human. British English.
- Paragraph 1 opens with a hook connecting the candidate's work to the company's problem (use company_hook).
  NEVER open with "I am writing to apply" or "I am excited".
- Paragraph 2: two concrete pieces of evidence, taken only from EVIDENCE. Keep numbers exactly; add none.
- Paragraph 3: a specific call to action about a next step. NEVER "I look forward to hearing from you".
- Do not claim anything not in EVIDENCE. Never mention: {deny}."""

LETTER_USER = """ROLE: {title} at {company}
COMPANY HOOK (from JD): {company_hook}
JD KEYWORDS: {keywords}
EVIDENCE:
{evidence}"""
