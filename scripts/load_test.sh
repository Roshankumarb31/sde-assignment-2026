#!/usr/bin/env bash
# Platform/SRE target: ingest AND decide one simulated day of public_03_load in < 10 wall-clock minutes.
# Runs the full controller (./run.sh, decisions on) for DAYS simulated days, then stops it, and reports
# wall time per simulated day, events/s, peak memory (VmHWM) and the ingest lag from the hourly log line.
#
#   scripts/load_test.sh [days=2]      -> reports/load_public_03.txt
set -euo pipefail
DAYS="${1:-2}"
PORT=8795
PY="${PYTHON:-python}"
cd "$(dirname "$0")/.."
mkdir -p runs reports
DB=runs/load_public_03.db
OUT=reports/load_public_03.txt
LOG=runs/load_public_03.controller.log
rm -f "$DB"* "$LOG"

$PY -m sim serve --scenario scenarios/public_03_load.yaml --run-dir runs/load_public_03 --force --port $PORT \
  >runs/sim_load_public_03.log 2>&1 &
SIM_PID=$!
trap 'kill $SIM_PID $CTL_PID 2>/dev/null || true' EXIT
URL="http://127.0.0.1:$PORT"
until curl -sf "$URL/health" >/dev/null; do sleep 0.3; done
field() { curl -sf "$URL/sim/state" | $PY -c "import json,sys; print(json.load(sys.stdin)['$1'])"; }

T0=$(date +%s.%N)
PYTHON="$PY" ./run.sh "$URL" "$DB" >"$LOG" 2>&1 &
CTL_PID=$!
PEAK=0
declare -a MARK
for day in $(seq 1 "$DAYS"); do
  while [ "$(field sim_minute)" -lt $((day * 1440)) ]; do
    kb=$(awk '/VmHWM/ {print $2}' /proc/$CTL_PID/status 2>/dev/null || echo 0)
    [ "${kb:-0}" -gt "$PEAK" ] && PEAK=$kb
    sleep 0.5
  done
  MARK[$day]=$(date +%s.%N)
  EV[$day]=$(field last_event_id)
done
kb=$(awk '/VmHWM/ {print $2}' /proc/$CTL_PID/status 2>/dev/null || echo 0)
[ "${kb:-0}" -gt "$PEAK" ] && PEAK=$kb
sleep 2   # let the controller finish the tick it is in, so its last hourly line is written
kill $CTL_PID 2>/dev/null || true

{
echo "# Load test: full controller (ingest + detectors + governance) on public_03_load, $(date -I)"
echo "# $(nproc) CPUs, $(free -g | awk '/Mem/ {print $2}') GB RAM; simulator and controller on the same machine"
echo "# command: scripts/load_test.sh $DAYS"
echo
prev=$T0; prev_ev=0
for day in $(seq 1 "$DAYS"); do
  wall=$($PY -c "print(round(${MARK[$day]} - $prev, 1))")
  ev=$(( ${EV[$day]} - prev_ev ))
  label=$([ "$day" = 1 ] && echo "D0 (incl. 7 days of history)" || echo "D$((day - 1))")
  echo "$label: ${wall} s wall, $(printf "%'d" $ev) events, $($PY -c "print(f'{$ev / ($wall or 1):,.0f}')") events/s"
  prev=${MARK[$day]}; prev_ev=${EV[$day]}
done
echo "peak controller memory (VmHWM): $((PEAK / 1024)) MB"
echo "ingest lag at the last hourly log lines (events not yet in the store when the hour's decisions ran):"
grep -E "lag=" "$LOG" | tail -3 | sed 's/^/  /'
echo "decisions made: $(grep -c -E 'propose|incident_' "$LOG" || true) log lines with proposals or incident steps"
echo "SRE target: one simulated day in < 600 s"
} | tee "$OUT"
