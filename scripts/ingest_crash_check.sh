#!/usr/bin/env bash
# C1 evidence: ingest a few simulated days while being kill -9'd repeatedly, then compare the store
# with a clean, never-killed ingest of the same chunks. Identical tables = exactly-once ingestion.
#
#   scripts/ingest_crash_check.sh [scenario] [kills] [until_minute]
set -euo pipefail
SCENARIO="${1:-scenarios/public_01_dev.yaml}"
KILLS="${2:-5}"
UNTIL="${3:-4320}"   # sim minutes to run (default 3 days)
PORT=8720
PY="${PYTHON:-python}"
WORK="$(mktemp -d)"
RUN="$WORK/run"
trap 'kill $SIM_PID 2>/dev/null || true; rm -rf "$WORK"' EXIT

$PY -m sim serve --scenario "$SCENARIO" --run-dir "$RUN" --force --port $PORT >"$WORK/sim.log" 2>&1 &
SIM_PID=$!
until curl -sf "http://127.0.0.1:$PORT/health" >/dev/null; do sleep 0.3; done
URL="http://127.0.0.1:$PORT"

# tiny sqlite query helper (no sqlite3 CLI needed): q <db> <sql>
q() { $PY -c 'import sqlite3,sys
try:
    for r in sqlite3.connect(sys.argv[1]).execute(sys.argv[2]): print(*r, sep="|")
except sqlite3.Error: print("?")' "$1" "$2"; }
dump() {
  q "$1" "select * from cursor"
  q "$1" "select * from stats order by 1,2,3,4,5,6"
  q "$1" "select * from lead_counts order by 1,2,3,4,5,6"
  q "$1" "select * from campaigns order by 1"
}

for i in $(seq 1 "$KILLS"); do
  $PY -m controller.ingest --url "$URL" --db "$WORK/killed.db" --until-minute "$UNTIL" >>"$WORK/ingest.log" 2>&1 &
  PID=$!
  sleep "$(awk -v s=$RANDOM 'BEGIN{srand(s); printf "%.1f", 0.3 + rand()*1.5}')"
  if kill -9 $PID 2>/dev/null; then echo "kill -9 #$i at cursor $(q "$WORK/killed.db" 'select last_chunk, last_event_id from cursor')"; fi
  wait $PID 2>/dev/null || true
done
echo "restart and run to sim minute $UNTIL"
$PY -m controller.ingest --url "$URL" --db "$WORK/killed.db" --until-minute "$UNTIL" 2>&1 | tail -1

echo "clean ingest of the same chunks (no advance, never killed)"
$PY -m controller.ingest --url "$URL" --db "$WORK/clean.db" --no-advance 2>&1 | tail -1

if diff <(dump "$WORK/killed.db") <(dump "$WORK/clean.db") >/dev/null; then
  echo "PASS: killed-and-restarted store is identical to the clean store ($(q "$WORK/clean.db" "select sum(dials) from stats where dim='prompt_version'") dials)"
else
  echo "FAIL: stores differ"; diff <(dump "$WORK/killed.db") <(dump "$WORK/clean.db") | head -20; exit 1
fi
