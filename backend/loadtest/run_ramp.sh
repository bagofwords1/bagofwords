#!/usr/bin/env bash
# Runs the concurrency ramp end to end.
#
# Per level: reset pg_stat_statements, start the 1 Hz resource sampler, run the
# mixed-workload harness, snapshot the top queries, stop the sampler, then let
# the box settle so the next level starts from idle rather than inheriting the
# previous level's backlog.
#
# The quiet baseline (cohort B alone) runs first so interference can be stated
# as a ratio against a measured number rather than an assumption.
set -uo pipefail
cd /home/user/bagofwords/backend
source .sandbox_env

LEVELS="${1:-1,10,30,60,100}"
TAG="${2:-mock}"
OUTDIR="loadtest/results/$TAG"
mkdir -p "$OUTDIR"
PG="postgresql://bow:bow@127.0.0.1:5432/bow"

snapshot_queries() { # $1=outfile
  psql "$PG" -tA -F'|' -c "
    SELECT round(total_exec_time)::text, calls::text, round(mean_exec_time,2)::text,
           round(rows/GREATEST(calls,1)) ::text, left(regexp_replace(query,'\s+',' ','g'),160)
    FROM pg_stat_statements
    WHERE query NOT LIKE '%pg_stat_statements%'
    ORDER BY total_exec_time DESC LIMIT 25;" > "$1" 2>/dev/null
}

echo "=== ramp start: levels=$LEVELS tag=$TAG ==="

# ---- quiet baseline: browsing cohort with no agent traffic ----
echo "--- baseline (browse only, no agents) ---"
: > loadtest/logs/phase_trace.jsonl
uv run python loadtest/harness_v2.py --level 10 --baseline-only --duration 45 \
    --out "$OUTDIR/baseline.json" > "$OUTDIR/baseline.txt" 2>&1
echo "baseline done"

for L in ${LEVELS//,/ }; do
  echo "--- level $L ---"
  psql "$PG" -tAc "SELECT pg_stat_statements_reset();" >/dev/null 2>&1
  : > loadtest/logs/phase_trace.jsonl
  : > loadtest/logs/mock_trace.jsonl

  bash loadtest/sample_metrics.sh "$OUTDIR/metrics_L${L}.csv" &
  SAMPLER=$!
  sleep 2

  uv run python loadtest/harness_v2.py --level "$L" --timeout 600 \
      --out "$OUTDIR/results_L${L}.json" > "$OUTDIR/results_L${L}.txt" 2>&1
  RC=$?

  sleep 2
  kill "$SAMPLER" 2>/dev/null || true
  snapshot_queries "$OUTDIR/queries_L${L}.txt"
  cp loadtest/logs/phase_trace.jsonl "$OUTDIR/phase_L${L}.jsonl" 2>/dev/null || true
  cp loadtest/logs/mock_trace.jsonl  "$OUTDIR/mock_L${L}.jsonl"  2>/dev/null || true

  # Pool-exhaustion evidence is in the backend log, not the HTTP responses.
  grep -c "QueuePool limit" loadtest/logs/backend.log > "$OUTDIR/queuepool_L${L}.txt" 2>/dev/null || echo 0 > "$OUTDIR/queuepool_L${L}.txt"

  echo "level $L done (rc=$RC)"
  sleep 20   # settle
done

echo "=== ramp complete: $OUTDIR ==="
