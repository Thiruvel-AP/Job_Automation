# Summary: Workday account wall — `manual_apply_account_required`, never `FORM NOT FOUND`

## What was fixed

All three tickets from `Issues_20261124-160100_001-003.md` are implemented in `careerops/apply/`:

| Ticket | Problem | Fix |
|--------|---------|-----|
| **001** | Sign-in / create-account wall on Workday reported as `FORM NOT FOUND` | `formagent.SIGN_IN_NEED` names the reason; `AgentResult.sign_in` property; `assist.py` agent branch raises `AccountRequired` on sign-in instead of printing `FORM NOT FOUND`; `planned.py` calls `route.require_no_auth(page)` before declaring `FORM NOT FOUND` |
| **002** | Workday's Apply button never pressed (plan-first path called `formscan.reach` with no agent) | `planned.py` passes the form agent (built with `assist.make_agent`) to `formscan.reach`, so the agent clicks `Apply` / `Apply Manually` only — never `Sign In` / `Create Account`, types nothing — then stops at the account page |
| **003** | Account-wall hand-off gave too little | `assist.account_required(..., pack=...)` gained an optional `pack` callable; the `except AccountRequired` handler passes `lambda why: hand_off(why, remember=False)`, so `Handoff.run` builds the apply pack (`answers_to_paste.txt`, CV/letter paths, name on clipboard) and opens the listing — host not recorded. `live._account_required` ends the job `manual_apply_account_required` with the listing printed |

## Files touched

- `careerops/apply/formagent.py` — `SIGN_IN_NEED` constant, used at both `need_user` stops; `AgentResult.sign_in` property
- `careerops/apply/assist.py` — `account_required(..., pack=...)`; agent branch raises `AccountRequired` on `reached.sign_in`
- `careerops/apply/planned.py` — agent passed to `formscan.reach`; `route.require_no_auth(page)` before `FORM NOT FOUND`; `pack` on `account_required`
- `tests/apply/test_workday_account.py` — new, 5 tests
- `README.md` — "Submit failures" and "Finding the form" updated
- `docs/IMPLEMENTATION.md` — added v3.9.4 section
- `TicketsRaised/Issues_20261124-160100_001-003.md` — marked 001-003 fixed
- `TicketsRaised/Issues_20261124-160100_001-003/Issues_20261124-160100_001-003_plan.md` — all steps ticked, commands and results logged

## New tests (5, all passing)

1. `planned.run` on Workday → clicks Apply only, hits account wall → `account_required`, `handoff.run` with `remember=False`, no failure row
2. `account_required(..., pack=...)` calls pack once; without pack behavior unchanged
3. Agent branch sign-in → `AccountRequired`, no `not_found`, no failure row
4. Agent branch "no way in" → `FORM NOT FOUND` includes agent's reason, counted once
5. `planned.run` with password field → `account_required`, no failure row

## Verification

- `pytest tests/apply/test_workday_account.py` → 5/5 passed
- `pytest tests/apply/test_handoff.py tests/apply/test_route_nov13.py -m "not slow"` → 73/73 passed (no regressions)
- `careerops run --mock --limit 1` → `Tailoring 1 job(s)` (real data untouched)

## Left unverified (per the plan)

A live Workday page. Check `data/live/submit_failures.csv` gets no new row and the LSEG job status reads `manual_apply_account_required`. You may remove the old LSEG `form_not_found` row if you wish — code never edits it.
