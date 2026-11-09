#!/usr/bin/env bash
# Full cycle for one scenario: baseline (no-op controller), our controller end to end, grade.
#   scripts/run_scenario.sh scenarios/public_01_dev.yaml [port]
# Writes runs/base_<name>, runs/mine_<name>, runs/mine_<name>.db, reports/<name>.md, reports/<name>.controller.log
set -euo pipefail
SCENARIO="${1:?usage: scripts/run_scenario.sh <scenario.yaml> [port]}"
PORT="${2:-8700}"
PY="${PYTHON:-python}"
NAME="$(basename "$SCENARIO" .yaml)"
cd "$(dirname "$0")/.."
mkdir -p runs reports

[ -d "runs/base_$NAME" ] || $PY -m sim baseline --scenario "$SCENARIO" --run-dir "runs/base_$NAME"

rm -f "runs/mine_$NAME.db"*
$PY -m sim serve --scenario "$SCENARIO" --run-dir "runs/mine_$NAME" --force --port "$PORT" >"runs/sim_$NAME.log" 2>&1 &
SIM_PID=$!
trap 'kill $SIM_PID 2>/dev/null || true' EXIT
until curl -sf "http://127.0.0.1:$PORT/health" >/dev/null; do sleep 0.3; done

PYTHON="$PY" ./run.sh "http://127.0.0.1:$PORT" "runs/mine_$NAME.db" 2>&1 | tee "reports/$NAME.controller.log"
$PY -m sim grade --run "runs/mine_$NAME" --baseline "runs/base_$NAME" --out "reports/$NAME.md"
echo "report: reports/$NAME.md"
