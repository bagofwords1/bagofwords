#!/bin/bash

# Set environment variables
export ENVIRONMENT=production
export NODE_ENV=production

# Auto-detect workers: use env var or half the CPUs (min 1, leave room for Node.js + Postgres)
CPUS=$(nproc)
DEFAULT_WORKERS=$(( CPUS > 1 ? CPUS / 2 : 1 ))
WORKERS=${UVICORN_WORKERS:-$DEFAULT_WORKERS}
echo "Starting uvicorn with $WORKERS workers (detected $CPUS CPUs)"

# Run database migrations with retries
cd /app/backend
for i in {1..3}; do
    alembic upgrade head && break
    echo "Migration attempt $i failed. Retrying in $((4 * i)) seconds..."
    if [ $i -eq 3 ]; then
        echo "Migration failed after 3 attempts. Exiting."
        exit 1
    fi
    sleep $((4 * i))
done

# Start the backend service
uvicorn main:app --host 0.0.0.0 --port 8000 --ws websockets --log-level warning --workers "$WORKERS" --loop uvloop --http httptools &

# Wait 5s for the backend to start
sleep 5

# Start the frontend service
cd /app/frontend
exec node .output/server/index.mjs
