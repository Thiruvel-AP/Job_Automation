#!/usr/bin/env bash
# Twice-daily discovery (Windows Task Scheduler at 07:45 and 17:45, see docs/scheduler.md). It only FINDS jobs: it writes JD files to
# data/jds/inbox/ and the tier counts to its log. It never tailors, never opens a form and never applies: you run `careerops live` yourself.
#
#   careerops discover --ats --reed --adzuna --gmail-alerts --report
#
# Everything it prints goes to data/logs/discover_<YYYY-MM-DD>.log (appended: both runs of a day share one file) and to the terminal.
# The exit code is the command's: non-zero when a source failed (a Gmail sign-in that has lapsed, a board that errored) or the command
# itself failed, so the scheduler shows the task as failed. A missing Reed / Adzuna key is only a note in the log.
set -uo pipefail

cd "$(dirname "$0")/.." || exit 2
if [ -f .venv/bin/activate ]; then
  # shellcheck disable=SC1091
  . .venv/bin/activate
fi

LOG_DIR="${CAREEROPS_LOG_DIR:-data/logs}"
mkdir -p "$LOG_DIR" || exit 2
LOG="$LOG_DIR/discover_$(date +%F).log"

{
  echo "== $(date -Is) discover.sh start"
  careerops discover --ats --reed --adzuna --gmail-alerts --report
} 2>&1 | tee -a "$LOG"
status=${PIPESTATUS[0]}

echo "== $(date -Is) discover.sh end, exit $status" | tee -a "$LOG"
exit "$status"
