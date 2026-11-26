# Fields of the filled form

| # | label | type | required | value | source | status |
|---|---|---|---|---|---|---|
| 1 | Resume/CV | file | yes | Thiruvel Andagurunathan Pandian CV.pdf | cv | VERIFIED |
| 2 | Cover Letter | file | no | Thiruvel Andagurunathan Pandian Cover Letter.pdf | letter | VERIFIED |
| 3 | First Name | text | yes | Thiruvel | applicant | VERIFIED |
| 4 | Last Name | text | yes | Andagurunathan Pandian | applicant | VERIFIED |
| 5 | Email | text | yes | apthiruvel@gmail.com | applicant | VERIFIED |
| 6 | Country | combobox | yes | +44 | applicant:location | VERIFIED |
| 7 | Phone | tel | yes | +44 7741016954 | applicant | VERIFIED |
| 8 | Location (City) | combobox | yes | Bristol, England, United Kingdom | answers:current_location | VERIFIED |
| 9 | What are your working rights in the country where this role is advertised for? | combobox | yes | Work Visa | applicant:visa.by_country.GB | VERIFIED |
| 10 | If you answered 'Work Visa' in the previous question, please specify the type of visa that you are currently holding. If | text | yes |  | answers:visa_type | NEEDS USER — answers.yaml has no `visa_type` entry (a few words: your visa type, or 'N.A.') |
| 11 | What is your notice period? | text | yes | Immediately | answers:notice_period | VERIFIED |
| 12 | What motivated you to apply for this position? | textarea | yes | I am applying for the AI Engineer role at RightShip. What draws me to this role is RightShip is the maritime industry's… | freetext (DRAFT, not approved) | VERIFIED |
| 13 | Please provide your base salary expectations (in local currency, excluding bonus and commission). | text | yes | I'd welcome the opportunity to discuss salary expectations during the interview process, once there's a clearer picture… | answers:salary_expectation | VERIFIED |
| 14 | How did you hear about us? | combobox | yes | Corporate website | how-heard | VERIFIED — board -> 'Corporate website' |
| 15 | Please include a link to your LinkedIn profile. | text | yes | https://www.linkedin.com/in/thiruvel-a-p/ | applicant | VERIFIED |
| 16 | Inclusion and Diversity is very important to us and we'd like to know what would be your preferred pronoun for us to ref | combobox | yes |  | equal_opportunities | EMPTY — not listed in equal_opportunities: left blank |
| 17 | If you have answered 'Self-describe' in the previous question, please specify: | text | no |  | freetext | EMPTY — free-text question of no kind a draft can answer ("If you have answered 'Self-describe' in the previous question, please |
| 18 | I declare that the information provided by me is true and accurate. Furthermore, I willingly consent to being contacted  | combobox | yes |  | attestation | NEEDS USER — attestation: you tick it yourself, code never does |
| 19 | Inclusion and Diversity is very important to us and we'd like to know what would be your preferred pronoun for us to ref | combobox | yes |  | equal_opportunities | EMPTY — not listed in equal_opportunities: left blank |
| 20 | Gender | combobox | no | Male | equal_opportunities:eo_gender | VERIFIED |
| 21 | Are you Hispanic/Latino? | combobox | no |  | never_fill | EMPTY — listed in applicant.yaml never_fill: yours to answer |
| 22 | Veteran Status | combobox | no |  | equal_opportunities:eo_veteran | EMPTY — no option matched match_any (I am not a protected veteran \| I identify as one or more of the classifications of a protec |
| 23 | By checking this box, I consent to RightShip collecting, storing, and processing my responses to the demographic data su | checkbox | yes |  | attestation | NEEDS USER — attestation: you tick it yourself, code never does |
