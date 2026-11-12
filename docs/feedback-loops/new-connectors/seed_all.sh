#!/usr/bin/env bash
# Seed every local backing server used by the UI loop (idempotent-ish).
set -u
L=$(cd "$(dirname "$0")" && pwd); cd "$L/../../../backend"
export TESTING=true NO_PROXY=127.0.0.1,localhost no_proxy=127.0.0.1,localhost
wait_port() { for i in $(seq 1 90); do (echo > /dev/tcp/127.0.0.1/$1) 2>/dev/null && return 0; sleep 3; done; echo "port $1 never opened"; }
# Couchbase cluster init (first boot only)
wait_port 8091; sleep 5
docker exec bow-cb couchbase-cli cluster-init -c 127.0.0.1 --cluster-username Administrator --cluster-password password --services data,index,query --cluster-ramsize 512 --cluster-index-ramsize 256 --index-storage-setting default 2>&1 | tail -1
docker exec bow-cb couchbase-cli bucket-create -c 127.0.0.1 -u Administrator -p password --bucket shop --bucket-type couchbase --bucket-ramsize 128 --wait 2>&1 | tail -1
docker exec bow-cb couchbase-cli collection-manage -c 127.0.0.1 -u Administrator -p password --bucket shop --create-scope sales 2>&1 | tail -1
for c in orders customers; do docker exec bow-cb couchbase-cli collection-manage -c 127.0.0.1 -u Administrator -p password --bucket shop --create-collection sales.$c 2>&1 | tail -1; done
wait_port 9042; sleep 20
echo "== nosql"; uv run python "$L/nosql_smoke.py" cassandra couchbase cosmos 2>&1 | grep -E "^(cassandra|couchbase|cosmos):|ALL OK|Error" | head
echo "== ddb"; DDB_EP=http://127.0.0.1:8001 uv run python "$L/ddb_smoke.py" 2>&1 | grep -E "ALL OK|Error" | head -3
echo "== iceberg"; uv run python "$L/iceberg_seed.py" 2>&1 | grep -E "seeded|Error" | head -3
wait_port 9047; for i in $(seq 1 60); do curl -s -o /dev/null -w "%{http_code}" --noproxy '*' http://127.0.0.1:9047/ | grep -q 200 && break; sleep 3; done
echo "== dremio"; uv run python "$L/dremio_seed.py" 2>&1 | grep -E "seeded|FAILED|Error" | head -3
wait_port 10010; sleep 30
echo "== hive"; for i in 1 2 3 4 5; do uv run python "$L/hive_seed.py" 10010 2>&1 | grep -E "seeded" && break; sleep 20; done
echo "SEED DONE"
