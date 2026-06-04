#!/bin/bash
# Samples host CPU/mem + uvicorn process stats + Postgres connection-pool state
# once per second to a CSV. Run during a load test, Ctrl-C when done.
#
#   ./sample_metrics.sh metrics.csv
#
OUT="${1:-metrics.csv}"
PGURL="postgresql://bow:bow@127.0.0.1:5432/bow"
export PGPASSWORD=bow

echo "ts,cpu_pct,mem_used_mb,uvicorn_procs,uvicorn_rss_mb,uvicorn_cpu_pct,pg_total,pg_active,pg_idle,pg_idle_in_txn,pg_waiting" > "$OUT"

read_cpu() { awk '/^cpu /{t=$2+$3+$4+$5+$6+$7+$8; i=$5; print t, i}' /proc/stat; }
prev=($(read_cpu)); prev_t=${prev[0]}; prev_i=${prev[1]}

while true; do
  sleep 1
  cur=($(read_cpu)); cur_t=${cur[0]}; cur_i=${cur[1]}
  dt=$((cur_t - prev_t)); di=$((cur_i - prev_i))
  cpu_pct=0; [ "$dt" -gt 0 ] && cpu_pct=$(awk "BEGIN{printf \"%.1f\", (1-$di/$dt)*100}")
  prev_t=$cur_t; prev_i=$cur_i

  mem_used=$(awk '/MemTotal/{t=$2}/MemAvailable/{a=$2}END{printf "%.0f",(t-a)/1024}' /proc/meminfo)

  # uvicorn worker processes
  mapfile -t UVI < <(ps -C uvicorn -o rss=,%cpu= 2>/dev/null)
  uproc=${#UVI[@]}
  urss=$(printf '%s\n' "${UVI[@]}" | awk '{s+=$1}END{printf "%.0f",s/1024}')
  ucpu=$(printf '%s\n' "${UVI[@]}" | awk '{s+=$2}END{printf "%.1f",s}')
  [ -z "$urss" ] && urss=0; [ -z "$ucpu" ] && ucpu=0

  # Postgres connection pool snapshot
  PG=$(psql "$PGURL" -tA -F',' -c "
    SELECT count(*) FILTER (WHERE datname='bow'),
           count(*) FILTER (WHERE datname='bow' AND state='active'),
           count(*) FILTER (WHERE datname='bow' AND state='idle'),
           count(*) FILTER (WHERE datname='bow' AND state='idle in transaction'),
           count(*) FILTER (WHERE datname='bow' AND wait_event_type='Client' AND state='active')
    FROM pg_stat_activity;" 2>/dev/null)
  [ -z "$PG" ] && PG="0,0,0,0,0"

  echo "$(date +%s.%3N),$cpu_pct,$mem_used,$uproc,$urss,$ucpu,$PG" >> "$OUT"
done
