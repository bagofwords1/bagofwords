#!/bin/bash

# Set environment variables
export ENVIRONMENT=production
export NODE_ENV=production

# Run database migrations
cd /app/backend
alembic upgrade head

# Start the backend service (removed & to run in foreground)
uvicorn main:app --host 0.0.0.0 --port 8000 --ws websockets --log-level debug &

# Wait a moment for the backend to start
sleep 5

# Start the frontend service
cd /app/frontend
exec node .output/server/index.mjs