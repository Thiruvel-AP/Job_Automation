#!/usr/bin/env bash
# v3.5a operate mode: Claude Code decides the careerops decision queue through the CLI, with the locked-down settings.
# Run it in WSL from the career-ops folder:   scripts/operate.sh            (at most 10 rows)
#                                             scripts/operate.sh --max 3
# Claude may only run `careerops queue / show / decide / why` and read data/outputs and data/jds; Edit, Write, the web, MCP,
# `careerops live` and anything with autoapply are denied (.claude/operate-settings.json). Every gate stays in code.
set -euo pipefail
cd "$(dirname "$0")/.."
if [ -f .venv/bin/activate ]; then
  . .venv/bin/activate                     # `careerops` on PATH for the session's commands
fi
command -v claude >/dev/null || { echo "claude is not installed here (WSL): https://code.claude.com/docs/en/setup, then run claude once to log in" >&2; exit 1; }
command -v careerops >/dev/null || { echo "careerops is not on PATH: activate .venv first" >&2; exit 1; }
exec claude --settings .claude/operate-settings.json "/careerops-review $*"
