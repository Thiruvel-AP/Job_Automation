# Summary: careerops live loops on "closed" status jobs

## Issue
Running `careerops live --max 10` repeatedly processes the same jobs (WRITER "AI engineer (UK)" and HealthHero "Senior AI Engineer") which have status `closed` in the database. The jobs never leave the approved folder.

## Root Cause
1. `pretailor._drop()` moves files from inbox only, but `live` reads from approved folder
2. `live.FINISHED` tuple doesn't include "closed", so `_finish_file()` not called
3. File stays in approved folder → reprocessed on every run

## Fix Applied (2026-11-26 13:10 GMT)
**File:** `careerops/apply/live.py`
1. Added `"closed"` to `FINISHED` tuple (line 58-59)
2. Added explicit handling for `"closed"` status in `_advance()` method (lines 919-934):
   - Reads closure reason from job record
   - Moves JD file to `jds_done/closed/` with `.reason.txt` file
   - Returns `"closed"` status

## Verification
- First run: Both closed jobs processed once, moved to `jds_done/closed/` with correct reasons
- Second run: "Nothing approved: No files were processed" — **loop fixed**
- Reason files created:
  - `writer-ai-engineer-uk-*.reason.txt`: "closed: not on the Ashby board of writer any more"
  - `healthhero-senior-ai-engineer-*.reason.txt`: "closed: HTTP 404"

## Files
- `TicketsRaised/Issues_20261126-125531_001/Issues_20261126-125531_001.md` - Full ticket (with fix details)
- `TicketsRaised/Issues_20261126-125531_001/Issues_20261126-125531_001_plan.md` - Implementation plan