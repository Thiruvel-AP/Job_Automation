## Document rules (checked in code before any PDF is accepted; a failed check blocks approval and auto-submit)
CV
- Header: name, then the hook headline (hook library entry chosen for this job, or the role-family headline). The
  same hook is profile sentence 1 and the first bullet of the most relevant role.
- Profile: 100–150 words, assembled only from fact-bank text (hook profile_line, family profile_body, profile
  sentences). Never padded by a model; if the fact bank cannot reach 100 words, the check fails with the shortfall.
- Skills: grouped under labelled sub-headings; no skill appears twice anywhere in the Skills section (case-, hyphen-
  and plural-insensitive, "AWS Lambda" and "Lambda" count as one).
- Projects: at most 3, each as its own sub-heading (name on the left and its public GitHub URL on the right, an italic
  stack line, 1–3 bullets). A project with no public URL in the fact bank is left off the CV (`scoring.cv_projects_need_url`,
  true in `config/settings.yaml`; false shows it without a link), never with a guessed link.
- Space goes to work experience first: projects are added only after every role has its maximum bullets.
- Certifications: a bulleted list, one certification per item.
Cover letter (v2.3: up to seven short paragraphs, no bullets)
- 200–320 body words (the greeting, sign-off and name are not counted).
- Header: the same name, contact line (full URLs) and hook headline as the CV. Greeting "Hi <first name>," or "Hi <Company> team,";
  sign-off "Regards," then a blank line, then the full name.
- P1: the company's problem first (a clause of the job ad, else of the company's own page), a semicolon, then the hook: one
  verified fact from the fact bank, with a real number, ending with how it was validated. Never "I am writing to apply".
- P2: the hook fact's `limits`, else its `result`, word for word (left out when there is neither). P3: the ad's 2 or 3 skills
  through a second bullet or project. P4: one sentence per role (employer, what I owned, one number). P5: why this team, one
  sentence quoting the ad. P6 (optional, `letter.honest_gap`): one honest gap, a required ad keyword the bank cannot claim, never a
  visa, degree or years. P7: "Could we spend 20 minutes on ...?".
- Evidence: only projects and bullets that are on this job's CV, and only skills listed on this job's CV.
- At least 60% of the sentences of P1–P4 cite a bullet or project id.
- Memory, RAM and GB figures only in the body, and only when the ad is about memory, hardware, efficiency, GPUs or edge.
- Never reuse another letter's body: 5-word-shingle overlap of P2–P5 with any earlier letter's < 0.30.
