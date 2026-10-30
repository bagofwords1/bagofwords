#!/usr/bin/env bash
# usage: start.sh <proxy_log>
S=$(cd "$(dirname "$0")" && pwd); cd "$S/../../../backend"
for f in /tmp/bow-agent/backend.pid /tmp/bow-agent/proxy.pid; do [ -f $f ] && kill -- -$(cat $f) 2>/dev/null; done
sleep 2
: > "$1"
setsid uv run python -I $S/count_proxy.py "$1" > /tmp/bow-agent/proxy.log 2>&1 & echo $! > /tmp/bow-agent/proxy.pid
export TESTING=true ENVIRONMENT=production TEST_DATABASE_URL=sqlite:///db/agent.db ANTHROPIC_BASE_URL=http://127.0.0.1:9099
rm -f db/agent.db; uv run alembic upgrade head > /tmp/bow-agent/migrate.log 2>&1
setsid uv run python main.py > /tmp/bow-agent/backend.log 2>&1 & echo $! > /tmp/bow-agent/backend.pid
timeout 120 bash -c 'until curl -sf http://localhost:8000/health >/dev/null; do sleep 1; done' && echo UP
uv run python ../tools/agent/seed_org.py > /tmp/bow-agent/seed.log 2>&1 && echo SEEDED
