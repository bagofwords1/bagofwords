#!/bin/bash
# Start the backend as a brand-new replica: an empty uploads/ (tmpfs, like a
# pod's emptyDir) in a private mount namespace, same database and env.
#   sudo unshare -m --propagation private docs/feedback-loops/object-storage/run_fresh_pod.sh <env-file>
set -e
BACKEND="$(cd "$(dirname "$0")/../../../backend" && pwd)"
mount -t tmpfs -o size=2g tmpfs "$BACKEND/uploads"
cd "$BACKEND"
set -a; . "$1"; set +a
echo "uploads at start: $(find uploads -type f | wc -l) files"
exec uv run uvicorn main:app --host 0.0.0.0 --port 8000
