#!/bin/bash

# Set environment variables
export ENVIRONMENT=production

# Resolve BOW_ENCRYPTION_KEY (must happen BEFORE workers fork so every worker
# shares the same key). Resolution order:
#   1. BOW_ENCRYPTION_KEY env var (explicit key management: k8s Secrets, vaults)
#   2. Keyfile at BOW_ENCRYPTION_KEY_FILE (default /app/backend/data/encryption.key,
#      a docker-compose volume) — also works with Docker secrets via
#      BOW_ENCRYPTION_KEY_FILE=/run/secrets/bow_encryption_key
#   3. Generate a key and persist it to the keyfile so it survives restarts
KEY_FILE="${BOW_ENCRYPTION_KEY_FILE:-/app/backend/data/encryption.key}"
KEY_DIR="$(dirname "$KEY_FILE")"

# Succeeds only when $1 is a well-formed Fernet key. A malformed key would
# otherwise boot fine and fail on every encrypt/decrypt at request time.
is_fernet_key() {
    python3 -c 'import sys; from cryptography.fernet import Fernet; Fernet(sys.argv[1])' "$1" 2>/dev/null
}

# Keyfile contents without trailing CR/LF (editors, Windows checkouts).
read_keyfile() {
    { tr -d '\r\n' < "$KEY_FILE"; } 2>/dev/null
}

# Persist $1 to $KEY_FILE unless a key already landed there. Several
# first-boot containers may share the volume, so writers serialize on a
# mkdir lock (atomic on shared filesystems), re-read under the lock and only
# then write via temp file + rename. Exactly one key ends up on disk and every
# container re-reads and uses that one. Prints errors to stderr.
persist_key() {
    local lock="$KEY_DIR/.encryption.key.lock" tmp i rc=0
    mkdir -p "$KEY_DIR" || return 1
    if [ ! -w "$KEY_DIR" ]; then
        echo "$KEY_DIR is not writable by $(id -un)" >&2
        return 1
    fi
    for i in $(seq 1 50); do            # ~10s, then assume a stale lock from a crashed container
        mkdir "$lock" 2>/dev/null && break
        sleep 0.2
    done
    if [ -z "$(read_keyfile)" ]; then
        if tmp="$(umask 077 && mktemp "$KEY_DIR/.encryption.key.XXXXXX")" \
           && printf '%s\n' "$1" > "$tmp" && mv -f "$tmp" "$KEY_FILE"; then
            # umask only applies to files we create; a pre-seeded empty keyfile
            # would keep its mode across the rename, so tighten it explicitly.
            chmod 600 "$KEY_FILE" 2>/dev/null || true
        else
            rc=1
            [ -n "$tmp" ] && rm -f "$tmp"
        fi
    fi
    rmdir "$lock" 2>/dev/null
    return $rc
}

if [ -n "$BOW_ENCRYPTION_KEY" ]; then
    if ! is_fernet_key "$BOW_ENCRYPTION_KEY"; then
        echo "❌ BOW_ENCRYPTION_KEY is not a valid Fernet key (44 url-safe base64 chars ending in '=')." >&2
        echo "   Generate one with: python3 -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())'" >&2
        exit 1
    fi
    echo "Using BOW_ENCRYPTION_KEY from the environment."
    KEY_ON_DISK="$(read_keyfile)"
    if [ -n "$KEY_ON_DISK" ] && [ "$KEY_ON_DISK" != "$BOW_ENCRYPTION_KEY" ]; then
        echo "⚠️  WARNING: $KEY_FILE holds a different key than BOW_ENCRYPTION_KEY. The environment variable wins."
        echo "⚠️  Anything encrypted with the keyfile's key will NOT decrypt. Unset BOW_ENCRYPTION_KEY to go back to the keyfile."
    fi
elif [ -e "$KEY_FILE" ] && [ ! -r "$KEY_FILE" ]; then
    echo "❌ $KEY_FILE exists but is not readable by $(id -un). Fix its permissions; refusing to start with a new key." >&2
    exit 1
elif KEY_FROM_FILE="$(read_keyfile)" && [ -n "$KEY_FROM_FILE" ]; then
    if ! is_fernet_key "$KEY_FROM_FILE"; then
        echo "❌ $KEY_FILE does not contain a valid Fernet key. Refusing to start." >&2
        echo "   Restore it from backup, or delete it to generate a new key (stored credentials will be lost)." >&2
        exit 1
    fi
    export BOW_ENCRYPTION_KEY="$KEY_FROM_FILE"
    echo "Loaded BOW_ENCRYPTION_KEY from $KEY_FILE"
else
    NEW_KEY="$(python3 -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())')"
    PERSIST_ERR="$(persist_key "$NEW_KEY" 2>&1)"
    KEY_ON_DISK="$(read_keyfile)"
    if [ -n "$KEY_ON_DISK" ] && is_fernet_key "$KEY_ON_DISK"; then
        # Use what actually landed on disk: ours, or a sibling container's.
        export BOW_ENCRYPTION_KEY="$KEY_ON_DISK"
        if [ "$KEY_ON_DISK" = "$NEW_KEY" ]; then
            echo "Generated a new BOW_ENCRYPTION_KEY and saved it to $KEY_FILE."
        else
            echo "Another container generated $KEY_FILE concurrently; using that key."
        fi
        if [ "$(stat -c %d "$KEY_DIR" 2>/dev/null)" = "$(stat -c %d / 2>/dev/null)" ]; then
            echo "⚠️  WARNING: $KEY_DIR is on the container's writable layer, not a volume."
            echo "⚠️  The key is lost when the container is recreated (pod restart, docker rm, image upgrade),"
            echo "⚠️  and each replica generates its own key. Stored credentials will then be undecryptable."
            echo "⚠️  Mount a persistent volume at $KEY_DIR or set: -e BOW_ENCRYPTION_KEY=<your-persistent-key>"
        else
            echo "It will be reused on restarts. Back this file up — losing it makes stored credentials undecryptable."
        fi
    else
        export BOW_ENCRYPTION_KEY="$NEW_KEY"
        echo "⚠️  WARNING: No BOW_ENCRYPTION_KEY provided and the key could not be saved to $KEY_FILE."
        [ -n "$PERSIST_ERR" ] && echo "⚠️  $PERSIST_ERR"
        echo "⚠️  Generated a TEMPORARY key: stored credentials become undecryptable after a restart!"
        echo "⚠️  Mount a writable volume at $KEY_DIR or set: -e BOW_ENCRYPTION_KEY=<your-persistent-key>"
    fi
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
