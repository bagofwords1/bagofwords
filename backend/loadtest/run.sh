#!/usr/bin/env bash
# Orchestrates a load run: starts the metrics sampler, runs the concurrency
# ramp, stops the sampler. Designed to run as a single tracked background task.
cd /home/user/bagofwords/backend
source .venv/bin/activate
source .sandbox_env
LEVELS="${1:-10,30,50}"
PROMPT="${2:-show list of albums}"
TS=$(date +%H%M%S)
MET="loadtest/metrics_${TS}.csv"
RES="loadtest/results_${TS}.json"

echo "=== load run: levels=$LEVELS prompt='$PROMPT' ==="
bash loadtest/sample_metrics.sh "$MET" &
SAMPLER=$!
sleep 2  # capture a little idle baseline

python loadtest/harness.py --concurrency "$LEVELS" --prompt "$PROMPT" \
    --timeout 180 --settle 20 --out "$RES"

sleep 2
kill "$SAMPLER" 2>/dev/null || true
echo "=== DONE === results=$RES metrics=$MET"
echo "RESULTS_FILE=$RES"
echo "METRICS_FILE=$MET"