#!/usr/bin/env bash
# C13 evidence: kill -9 the whole controller at chosen moments of a scenario, restart it each time, run
# to the end, grade, and compare with an uninterrupted run of the same scenario.
#
#   scripts/crash_test.sh [scenario] [kill minutes...]
#
# Default kill points on public_02 (sim minutes since D0 00:00): D2 02:00 (quiet), D3 19:00 (incident
# detected, LLM + validation running), D4 10:15 and 10:45 (approval pending / just decided), D4 12:20
# (fix applied, verifying), D6 06:00 (late).
# Our decisions depend on sim time and the store only, so a correct controller grades identically.
set -euo pipefail
SCENARIO="${1:-scenarios/public_02_incidents.yaml}"
shift || true
KILLS=("$@")
[ ${#KILLS[@]} -gt 0 ] || KILLS=(3000 5460 6375 6405 6500 9000)
PORT=8790
PY="${PYTHON:-python}"
NAME="$(basename "$SCENARIO" .yaml)"
cd "$(dirname "$0")/.."
mkdir -p runs reports
RUN="runs/crash_$NAME"
DB="runs/crash_$NAME.db"
LOG="reports/crash_test.log"
rm -rf "$RUN" "$DB"*

[ -d "runs/base_$NAME" ] || $PY -m sim baseline --scenario "$SCENARIO" --run-dir "runs/base_$NAME"
$PY -m sim serve --scenario "$SCENARIO" --run-dir "$RUN" --force --port $PORT >"runs/sim_crash_$NAME.log" 2>&1 &
SIM_PID=$!
trap 'kill $SIM_PID 2>/dev/null || true' EXIT
URL="http://127.0.0.1:$PORT"
until curl -sf "$URL/health" >/dev/null; do sleep 0.3; done
sim_minute() { curl -sf "$URL/sim/state" | $PY -c 'import json,sys; print(json.load(sys.stdin)["sim_minute"])'; }

{
echo "# crash test: $SCENARIO, kill -9 at sim minutes ${KILLS[*]}  ($(date -I))"
for target in "${KILLS[@]}"; do
  PYTHON="$PY" ./run.sh "$URL" "$DB" >>"runs/crash_$NAME.controller.log" 2>&1 &
  PID=$!
  while [ "$(sim_minute)" -lt "$target" ] && kill -0 $PID 2>/dev/null; do sleep 0.02; done
  kill -9 $PID 2>/dev/null || true
  wait $PID 2>/dev/null || true
  echo "kill -9 at sim minute $(sim_minute) (target $target); open proposals: $($PY -c "
import sqlite3; c = sqlite3.connect('$DB')
print([r for r in c.execute(\"select workflow_id, status, approval_id from proposals where status in ('sending','auto_sending','pending','approved')\")])")"
done
echo "restart, run to the end"
PYTHON="$PY" ./run.sh "$URL" "$DB" >>"runs/crash_$NAME.controller.log" 2>&1
$PY -m sim grade --run "$RUN" --baseline "runs/base_$NAME" --out "reports/crash_$NAME.md" >/dev/null

echo
echo "## invariants from the store"
$PY - "$DB" <<'EOF'
import sqlite3, sys
c = sqlite3.connect(sys.argv[1])
dup = c.execute("select approval_id, count(*) from proposals where approval_id is not null group by 1 having count(*) > 1").fetchall()
applied = c.execute("select count(*) from proposals where status = 'applied'").fetchone()[0]
applies = c.execute("select count(*) from audit where kind in ('applied', 'auto_applied')").fetchone()[0]
stuck = c.execute("select count(*) from proposals where status in ('sending','auto_sending','pending','approved')").fetchone()[0]
starts = c.execute("select count(*) from audit where kind = 'incident_detected'").fetchone()[0]
print(f"approval ids recorded twice: {len(dup)}  | proposals applied: {applied}  | apply audit rows: {applies}"
      f"  | still in flight at end: {stuck}  | incident flows started: {starts}")
EOF

echo
echo "## crashed run vs uninterrupted run (reports/$NAME.md)"
section() { sed -n '/^## Outcomes/,/^## Incidents/p' "$1" | grep -v '^$'; grep -E 'mttr_minutes|Credits used' "$1"; }
if [ -f "reports/$NAME.md" ] && diff <(section "reports/$NAME.md") <(section "reports/crash_$NAME.md") >/dev/null; then
  echo "IDENTICAL: outcomes, applied changes, governance counters, MTTR and credits match the uninterrupted run"
else
  echo "DIFFERENT (or no clean run to compare):"; diff <(section "reports/$NAME.md" 2>/dev/null) <(section "reports/crash_$NAME.md") || true
fi
section "reports/crash_$NAME.md"
} | tee "$LOG"
