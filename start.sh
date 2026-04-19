#!/bin/bash

# Set environment variables
export ENVIRONMENT=production

# Generate BOW_ENCRYPTION_KEY if not provided (must happen BEFORE workers fork)
if [ -z "$BOW_ENCRYPTION_KEY" ]; then
    export BOW_ENCRYPTION_KEY=$(python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())")
    echo "⚠️  WARNING: No BOW_ENCRYPTION_KEY provided. Generated a temporary key."
    echo "⚠️  Users will be logged out if the container restarts!"
    echo "⚠️  For production, set: -e BOW_ENCRYPTION_KEY=<your-persistent-key>"
fi

# =============================================================================
# Detect available CPUs (cgroup-aware for containers)
# Works with: K8s, Docker, Docker Compose (with or without CPU limits)
# =============================================================================
get_container_cpus() {
    local cpus=0
    
    # Method 1: cgroups v2 (modern K8s 1.25+, Docker with cgroupv2)
    # File contains "quota period" e.g., "200000 100000" for 2 CPUs, or "max 100000" for unlimited
    if [ -f /sys/fs/cgroup/cpu.max ] 2>/dev/null; then
        local quota period
        read -r quota period < /sys/fs/cgroup/cpu.max 2>/dev/null
        if [ "$quota" != "max" ] && [ -n "$quota" ] && [ -n "$period" ] && [ "$period" -gt 0 ] 2>/dev/null; then
            cpus=$((quota / period))
            if [ "$cpus" -gt 0 ] 2>/dev/null; then
                echo "cgroups-v2:$cpus"
                return
            fi
        fi
    fi
    
    # Method 2: cgroups v1 (older K8s, older Docker)
    # -1 means unlimited
    local cg_base=""
    for path in /sys/fs/cgroup/cpu /sys/fs/cgroup/cpu,cpuacct; do
        if [ -f "$path/cpu.cfs_quota_us" ] 2>/dev/null; then
            cg_base="$path"
            break
        fi
    done
    
    if [ -n "$cg_base" ]; then
        local quota=$(cat "$cg_base/cpu.cfs_quota_us" 2>/dev/null)
        local period=$(cat "$cg_base/cpu.cfs_period_us" 2>/dev/null)
        if [ -n "$quota" ] && [ "$quota" -gt 0 ] && [ -n "$period" ] && [ "$period" -gt 0 ] 2>/dev/null; then
            cpus=$((quota / period))
            if [ "$cpus" -gt 0 ] 2>/dev/null; then
                echo "cgroups-v1:$cpus"
                return
            fi
        fi
    fi
    
    # Method 3: Fallback to nproc (no container CPU limit set)
    cpus=$(nproc 2>/dev/null || echo 1)
    echo "nproc:$cpus"
}

# Detect CPUs and parse result
CPU_RESULT=$(get_container_cpus)
CPU_SOURCE="${CPU_RESULT%%:*}"
CPUS="${CPU_RESULT##*:}"

# Ensure CPUS is a valid number
if ! [[ "$CPUS" =~ ^[0-9]+$ ]] || [ "$CPUS" -le 0 ]; then
    CPUS=1
    CPU_SOURCE="fallback"
fi

# Calculate workers: half of available CPUs
# - Minimum: 1 worker
# - Maximum: 4 workers (safety cap to prevent OOM)
DEFAULT_WORKERS=$(( CPUS > 1 ? CPUS / 2 : 1 ))
DEFAULT_WORKERS=$(( DEFAULT_WORKERS > 4 ? 4 : DEFAULT_WORKERS ))
DEFAULT_WORKERS=$(( DEFAULT_WORKERS < 1 ? 1 : DEFAULT_WORKERS ))

# Allow override via environment variable
WORKERS=${UVICORN_WORKERS:-$DEFAULT_WORKERS}

echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "🔧 CPU Detection: $CPU_SOURCE"
echo "🖥️  Available CPUs: $CPUS"
echo "🚀 Uvicorn Workers: $WORKERS (max 4, override with UVICORN_WORKERS)"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

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

# Start uvicorn as the single foreground process (SPA is served from the
# same process via SERVE_FRONTEND=1). tini reaps it on shutdown.
exec uvicorn main:app \
    --host 0.0.0.0 \
    --port 3000 \
    --ws websockets \
    --log-level info \
    --workers "$WORKERS" \
    --loop uvloop \
    --http httptools
