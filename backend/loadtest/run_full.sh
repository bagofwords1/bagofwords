#!/usr/bin/env bash
# Authoritative run: all three cohorts at once, corrected resource attribution,
# and server-side verification of every completion.
#
# Differences from run_ramp.sh (which produced the first pass):
#   - sample_metrics2.sh attributes CPU per role instead of host-wide
#   - the Playwright cohort runs *during* the level, not separately
#   - the harness re-reads the completions table afterwards, so "success" means
#     the server agrees, not just that the SSE stream closed politely
set -uo pipefail
cd /home/user/bagofwords/backend
source .sandbox_env

LEVELS="${1:-30,60,100}"
TAG="${2:-full}"
UI_USERS="${UI_USERS:-4}"
OUTDIR="loadtest/results/$TAG"
mkdir -p "$OUTDIR"
PG="postgresql://bow:bow@127.0.0.1:5432/bow"

echo "=== full run: levels=$LEVELS tag=$TAG ui_users=$UI_USERS ==="

# UI baseline with no other load, for the interference ratio.
echo "--- UI baseline ---"
(cd loadtest/ui && node ui_driver.js --users "$UI_USERS" --role mixed \
    --duration 90 --out "../results/$TAG/ui_baseline.json") \
    > "$OUTDIR/ui_baseline.txt" 2>&1
echo "ui baseline done"

for L in ${LEVELS//,/ }; do
  echo "--- level $L ---"
  psql "$PG" -tAc "SELECT pg_stat_statements_reset();" >/dev/null 2>&1
  : > loadtest/logs/phase_trace.jsonl
  : > loadtest/logs/mock_trace.jsonl
  # grep -c prints "0" AND exits 1 when there are no matches, so `|| echo 0`
  # appends a second line and the later arithmetic blows up on "0\n0".
  BEFORE_QP=$(grep -c "QueuePool limit" loadtest/logs/backend.log 2>/dev/null) || BEFORE_QP=0
  BEFORE_TC=$(grep -c "too many clients" loadtest/logs/backend.log 2>/dev/null) || BEFORE_TC=0

  bash loadtest/sample_metrics2.sh "$OUTDIR/metrics_L${L}.csv" &
  SAMPLER=$!
  sleep 2

  # UI cohort runs concurrently for the whole level.
  (cd loadtest/ui && node ui_driver.js --users "$UI_USERS" --role mixed \
      --duration 150 --out "../results/$TAG/ui_L${L}.json") \
      > "$OUTDIR/ui_L${L}.txt" 2>&1 &
  UIPID=$!

  uv run python loadtest/harness_v2.py --level "$L" --timeout 600 \
      --out "$OUTDIR/results_L${L}.json" > "$OUTDIR/results_L${L}.txt" 2>&1

  wait "$UIPID" 2>/dev/null || true
  sleep 2
  kill "$SAMPLER" 2>/dev/null || true

  psql "$PG" -tA -F'|' -c "
    SELECT round(total_exec_time::numeric)::text, calls::text,
           round(mean_exec_time::numeric,2)::text,
           left(regexp_replace(query,'\s+',' ','g'),150)
    FROM pg_stat_statements WHERE query NOT LIKE '%pg_stat_statements%'
    ORDER BY total_exec_time DESC LIMIT 25;" > "$OUTDIR/queries_L${L}.txt" 2>/dev/null

  cp loadtest/logs/phase_trace.jsonl "$OUTDIR/phase_L${L}.jsonl" 2>/dev/null || true
  cp loadtest/logs/mock_trace.jsonl  "$OUTDIR/mock_L${L}.jsonl"  2>/dev/null || true

  AFTER_QP=$(grep -c "QueuePool limit" loadtest/logs/backend.log 2>/dev/null) || AFTER_QP=0
  AFTER_TC=$(grep -c "too many clients" loadtest/logs/backend.log 2>/dev/null) || AFTER_TC=0
  echo "$((AFTER_QP - BEFORE_QP))" > "$OUTDIR/queuepool_L${L}.txt"
  echo "$((AFTER_TC - BEFORE_TC))" > "$OUTDIR/toomanyclients_L${L}.txt"

  echo "level $L done"
  sleep 25
done

echo "=== full run complete: $OUTDIR ==="
