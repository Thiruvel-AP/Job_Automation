# Fields of the filled form

| # | label | type | required | value | source | status |
|---|---|---|---|---|---|---|
| 1 | Resume/CV | file | yes | Thiruvel Andagurunathan Pandian CV.pdf | cv | VERIFIED |
| 2 | Cover Letter | file | no | Thiruvel Andagurunathan Pandian Cover Letter.pdf | letter | VERIFIED |
| 3 | First Name | text | yes | Thiruvel | applicant | VERIFIED |
| 4 | Last Name | text | yes | Andagurunathan Pandian | applicant | VERIFIED |
| 5 | Email | text | yes | apthiruvel@gmail.com | applicant | VERIFIED |
| 6 | Phone | text | yes | +44 7741016954 | applicant | VERIFIED |
| 7 | What are your working rights in the country where this role is advertised for? | combobox | yes | Work Visa | applicant:visa.by_country.GB | VERIFIED |
| 8 | If you answered 'Work Visa' in the previous question, please specify the type of visa that you are currently holding. If | text | yes | Graduate visa | answers:visa_type | VERIFIED |
| 9 | What is your notice period? | text | yes | Immediately | answers:notice_period | VERIFIED |
| 10 | What motivated you to apply for this position? | textarea | yes | The AI Engineer role at RightShip stands out to me for its focus on safety and risk intelligence. At AstraZeneca, I wro… | freetext (DRAFT, not approved) | VERIFIED |
| 11 | Please provide your base salary expectations (in local currency, excluding bonus and commission). | text | yes | I'd welcome the opportunity to discuss salary expectations during the interview process, once there's a clearer picture… | answers:salary_expectation | VERIFIED |
| 12 | How did you hear about us? | combobox | yes | Corporate website | how-heard | VERIFIED — board -> 'Corporate website' |
| 13 | Please include a link to your LinkedIn profile. | text | yes | https://www.linkedin.com/in/thiruvel-a-p/ | applicant | VERIFIED |
| 14 | Inclusion and Diversity is very important to us and we'd like to know what would be your preferred pronoun for us to ref | combobox | yes |  | equal_opportunities | EMPTY — not listed in equal_opportunities: left blank |
| 15 | If you have answered 'Self-describe' in the previous question, please specify: | text | no |  |  | EMPTY — no answer in applicant.yaml or answers.yaml (type it with [e N], `save` stores it) |
| 16 | I declare that the information provided by me is true and accurate. Furthermore, I willingly consent to being contacted  | combobox | yes |  | attestation | NEEDS USER — attestation: you tick it yourself, code never does |
| 17 | Location | text | yes |  | answers:current_location | COVERED — not on the page; 'Location (City)' holds the answer (VERIFIED) |
| 18 | VeteranStatus | combobox | no |  | equal_opportunities | EMPTY — not listed in equal_opportunities: left blank |
| 19 | Race | combobox | no |  | equal_opportunities:ethnicity | EMPTY — no such field on the page |
| 20 | Gender | combobox | no | Male | equal_opportunities:eo_gender | VERIFIED |
| 21 | Country | combobox | yes | +44 | applicant:location | VERIFIED |
| 22 | Location (City) | combobox | yes | Bristol, England, United Kingdom | answers:current_location | VERIFIED |
| 23 | Inclusion and Diversity is very important to us and we'd like to know what would be your preferred pronoun for us to ref | combobox | yes |  | equal_opportunities | EMPTY — not listed in equal_opportunities: left blank |
| 24 | Are you Hispanic/Latino? | combobox | no |  | never_fill | EMPTY — listed in applicant.yaml never_fill: yours to answer |
| 25 | By checking this box, I consent to RightShip collecting, storing, and processing my responses to the demographic data su | checkbox | yes |  | attestation | NEEDS USER — attestation: you tick it yourself, code never does |

## Options of the empty choice fields

- #14 Inclusion and Diversity is very important to us and we'd like to know what would be your preferred pronoun for us to ref (required): She / Her / Hers · He / Him / His · They / Them / Theirs · Self-describe, please specify in the next question
- #18 VeteranStatus: I don't wish to answer · I identify as one or more of the classifications of a protected veteran · I am not a protected veteran
- #19 Race: Decline To Self Identify · Two or More Races · Native Hawaiian or Other Pacific Islander · White · Hispanic or Latino · Black or African American · Asian · American Indian or Alaskan Native
