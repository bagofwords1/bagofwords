#!/usr/bin/env bash
# Real-LLM validation pass.
#
# The scale numbers come from a mock LLM, which is the only way to get a
# repeatable comparison across 30/60/100. This pass re-runs the smaller levels
# against real Claude Haiku to check the mock did not hide anything — in
# particular that the agent still takes the same *shape* of path (tool calls,
# iteration count, block writes) and that the contention findings reproduce
# when token cadence and context sizes are real.
#
# Deliberately limited to L10/L30: at higher concurrency provider rate limits
# would dominate and we would be measuring Anthropic.
set -uo pipefail
cd /home/user/bagofwords/backend
source .sandbox_env
OUT=loadtest/results/real
mkdir -p "$OUT"

: "${ANTHROPIC_KEY:?ANTHROPIC_KEY must be set for the real-LLM pass}"

echo "=== switching the org's default model to real Claude Haiku ==="
uv run python loadtest/setup_real_llm.py || exit 1

bash loadtest/ctl.sh restart >/dev/null 2>&1
sleep 3

for L in 10 30; do
  echo "--- real-LLM level $L ---"
  : > loadtest/logs/phase_trace.jsonl
  bash loadtest/sample_metrics2.sh "$OUT/metrics_L${L}.csv" &
  SAMPLER=$!
  sleep 2
  uv run python loadtest/harness_v2.py --level "$L" --timeout 900 \
      --out "$OUT/results_L${L}.json" > "$OUT/results_L${L}.txt" 2>&1
  sleep 2
  kill "$SAMPLER" 2>/dev/null || true
  cp loadtest/logs/phase_trace.jsonl "$OUT/phase_L${L}.jsonl" 2>/dev/null || true
  echo "level $L done"
  sleep 20
done

echo "=== real-LLM pass complete: $OUT ==="
