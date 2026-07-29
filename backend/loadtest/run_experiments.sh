#!/usr/bin/env bash
# Targeted experiments that the ramp cannot answer on its own.
#
# 1. isolate  — is the agent flow slow because of other agents, or because of
#               instruction writes? Same agent count, with and without cohort B.
# 2. semaphore— do the non-streaming completion paths honour the agent cap?
#               Run with a deliberately low cap so the answer is unambiguous.
# 3. logging  — what does DEBUG-level logging (the default whenever ENVIRONMENT
#               is not "production") cost at load?
set -uo pipefail
cd /home/user/bagofwords/backend
source .sandbox_env
OUT=loadtest/results/exp
mkdir -p "$OUT"

echo "=== 1. isolate: agents alone vs agents+browsing ==="
bash loadtest/ctl.sh restart >/dev/null 2>&1
uv run python loadtest/exp_isolate.py --agents 36 --outdir "$OUT/isolate" 2>&1 | tee "$OUT/isolate.txt"

echo
echo "=== 2. semaphore bypass (cap forced to 4) ==="
BOW_MAX_CONCURRENT_AGENTS=4 BOW_WORKERS=1 bash loadtest/ctl.sh restart >/dev/null 2>&1
sleep 3
BOW_MAX_CONCURRENT_AGENTS=4 uv run python loadtest/exp_semaphore_bypass.py \
    --n 16 --out "$OUT/semaphore.json" 2>&1 | tee "$OUT/semaphore.txt"

echo
echo "=== 3. logging cost at L30 ==="
bash loadtest/ctl.sh restart >/dev/null 2>&1
sleep 3
uv run python loadtest/harness_v2.py --level 30 --timeout 600 \
    --out "$OUT/log_debug.json" > "$OUT/log_debug.txt" 2>&1
DBG_BYTES=$(stat -c %s loadtest/logs/backend.log 2>/dev/null || echo 0)
echo "debug-mode backend.log bytes after L30: $DBG_BYTES"
sleep 20

ENVIRONMENT=production BOW_WORKERS=2 bash loadtest/ctl.sh restart >/dev/null 2>&1
sleep 3
uv run python loadtest/harness_v2.py --level 30 --timeout 600 \
    --out "$OUT/log_info.json" > "$OUT/log_info.txt" 2>&1
INFO_BYTES=$(stat -c %s loadtest/logs/backend.log 2>/dev/null || echo 0)
echo "info-mode backend.log bytes after L30: $INFO_BYTES"
echo "$DBG_BYTES $INFO_BYTES" > "$OUT/log_bytes.txt"

echo "=== experiments complete: $OUT ==="
