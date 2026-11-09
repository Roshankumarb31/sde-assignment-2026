#!/usr/bin/env bash
# Start the controller against a running simulator and run until the scenario finishes.
#   ./run.sh <SIM_URL> [state.db]
# No Docker, no extra services: Python 3 + requirements.txt, state in one SQLite file.
# Safe to kill and re-run with the same state file: it resumes where it stopped.
set -euo pipefail
SIM_URL="${1:?usage: ./run.sh <SIM_URL> [state.db]}"
STATE_DB="${2:-${STATE_DB:-state.db}}"
cd "$(dirname "$0")"
exec "${PYTHON:-python}" -m controller.main --url "$SIM_URL" --db "$STATE_DB"
