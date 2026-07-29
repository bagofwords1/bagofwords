#!/usr/bin/env bash
# Per-role resource sampler.
#
# The original sampler used `ps -C uvicorn`, which matches nothing here because
# the workers are `uv run`-spawned multiprocessing children whose comm is
# "python". That made host-wide CPU the only available number — and host CPU on
# this box also includes the load harness, the mock LLM, the frontend and
# Postgres, all of which are test scaffolding. Reporting that as "the app is
# CPU-bound" would be wrong.
#
# Here each role is matched on its full command line and summed separately, so
# the report can say what the *backend* actually consumed.
#
# CPU percentages are per-core-sum (400% = 4 saturated vCPU).
OUT="${1:-metrics2.csv}"
PGURL="postgresql://bow:bow@127.0.0.1:5432/bow"
export PGPASSWORD=bow

echo "ts,host_cpu_pct,mem_used_mb,backend_cpu,backend_rss_mb,backend_procs,mock_cpu,harness_cpu,pg_cpu,front_cpu,pg_total,pg_active,pg_idle,pg_idle_in_txn,pg_lock_waits" > "$OUT"

HZ=$(getconf CLK_TCK 2>/dev/null || echo 100)
read_cpu() { awk '/^cpu /{t=$2+$3+$4+$5+$6+$7+$8; i=$5; print t, i}' /proc/stat; }
prev=($(read_cpu)); prev_t=${prev[0]}; prev_i=${prev[1]}

# $1 = grep -E pattern over full args; prints "cpu_sum rss_sum_mb count"
#
# ps's %cpu is CPU averaged over the process's whole lifetime, so a worker that
# was idle for ten minutes then saturated reads low. We want what it is doing
# *now*, so jiffies are diffed out of /proc/<pid>/stat between samples.
# State lives in a file, not a shell variable: `role` is invoked inside $( ),
# which runs it in a subshell, so any in-memory jiffy table would be discarded
# every tick and every sample would read 0.
STATEDIR=$(mktemp -d)
trap 'rm -rf "$STATEDIR"' EXIT

role() {
  local pat="$1" tag="$2" pids c=0 r=0 n=0 sf="$STATEDIR/$2"
  pids=$(ps -eo pid,args --no-headers 2>/dev/null | grep -E "$pat" | grep -v grep | awk '{print $1}')
  local new="$sf.new"; : > "$new"
  for p in $pids; do
    [ -r "/proc/$p/stat" ] || continue
    # fields 14,15 = utime,stime in clock ticks
    local j; j=$(awk '{print $14+$15}' "/proc/$p/stat" 2>/dev/null) || continue
    echo "$p $j" >> "$new"
    local prev=""
    [ -f "$sf" ] && prev=$(awk -v pid="$p" '$1==pid{print $2}' "$sf")
    if [ -n "$prev" ]; then
      c=$(awk "BEGIN{print $c + ($j - $prev) * 100 / $HZ}")
    fi
    local rss; rss=$(awk '/^VmRSS/{print $2}' "/proc/$p/status" 2>/dev/null)
    [ -n "$rss" ] && r=$((r + rss))
    n=$((n + 1))
  done
  [ -f "$new" ] && mv "$new" "$sf"
  printf "%.1f %.0f %d" "$c" "$((r / 1024))" "$n"
}


while true; do
  sleep 1
  cur=($(read_cpu)); cur_t=${cur[0]}; cur_i=${cur[1]}
  dt=$((cur_t - prev_t)); di=$((cur_i - prev_i))
  host_cpu=0; [ "$dt" -gt 0 ] && host_cpu=$(awk "BEGIN{printf \"%.1f\", (1-$di/$dt)*100}")
  prev_t=$cur_t; prev_i=$cur_i

  mem_used=$(awk '/MemTotal/{t=$2}/MemAvailable/{a=$2}END{printf "%.0f",(t-a)/1024}' /proc/meminfo)

  # uvicorn parent + its spawned workers, but NOT the mock LLM (also python)
  BACK=($(role 'multiprocessing.spawn|uvicorn main:app' back))
  MOCK=($(role 'loadtest/mock_llm.py' mock))
  HARN=($(role 'loadtest/harness_v2.py|ui_driver.js' harn))
  PGP=($(role 'postgres:|bin/postgres' pg))
  FRONT=($(role '\.output/server/index\.mjs|nuxt' front))

  PG=$(psql "$PGURL" -tA -F',' -c "
    SELECT count(*) FILTER (WHERE datname='bow'),
           count(*) FILTER (WHERE datname='bow' AND state='active'),
           count(*) FILTER (WHERE datname='bow' AND state='idle'),
           count(*) FILTER (WHERE datname='bow' AND state='idle in transaction'),
           count(*) FILTER (WHERE datname='bow' AND wait_event_type='Lock')
    FROM pg_stat_activity;" 2>/dev/null)
  [ -z "$PG" ] && PG="0,0,0,0,0"

  echo "$(date +%s.%3N),$host_cpu,$mem_used,${BACK[0]},${BACK[1]},${BACK[2]},${MOCK[0]},${HARN[0]},${PGP[0]},${FRONT[0]},$PG" >> "$OUT"
done
