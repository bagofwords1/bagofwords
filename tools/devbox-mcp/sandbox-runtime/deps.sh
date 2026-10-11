#!/usr/bin/env bash
# deps.sh - dependency setup for a devbox using the node-wide cache mount.
#
# Sourced by entrypoint.sh. Why this exists: the uv and yarn caches under
# /cache are warm after the first boot, but /cache and the devbox PVC are
# different mounts, so uv cannot hard-link packages into a venv on the PVC and
# copies 2.4 GB instead (~1 min), and yarn unpacks ~1 GB of node_modules
# (~1.5 min). Putting the per-devbox venv and node_modules *on the cache
# mount* turns both into hard-link operations that take seconds:
#
#   /cache/venv/<devbox>                 uv venv, hard-linked from /cache/uv
#   /cache/node_modules/golden-<hash>    one node_modules tree per yarn.lock hash
#   /cache/node_modules/<devbox>         cp -al clone of the golden tree, symlinked
#                                        from <src>/frontend/node_modules
#   /cache/live/<devbox>                 heartbeat (touched every 5 min)
#
# Per-devbox directories are pruned at boot when their heartbeat is older than
# DEPS_PRUNE_AFTER (default 24h): the devbox is gone, and the files are only
# hard links, so the shared caches lose nothing. Golden trees are never pruned.
#
# Hard-linked files are shared between devboxes (that is also how uv's own
# hardlink mode works): package managers replace files rather than edit them
# in place, so this is safe unless someone hand-edits a dependency file.

DEPS_CACHE="${DEPS_CACHE:-/cache}"
DEPS_PRUNE_AFTER="${DEPS_PRUNE_AFTER:-1440}"   # minutes

deps_cache_usable() {
  [[ "${BOW_LINKED_DEPS:-1}" != "0" ]] && [[ -d "$DEPS_CACHE" ]] && [[ -w "$DEPS_CACHE" ]]
}

deps_devbox_name() { printf '%s' "${BOW_SANDBOX_NAME:-${HOSTNAME:-devbox}}"; }

# --- heartbeat ---------------------------------------------------------------
deps_heartbeat_start() {
  local me; me="$(deps_devbox_name)"
  mkdir -p "$DEPS_CACHE/live"
  touch "$DEPS_CACHE/live/$me"
  # Fully detached (no inherited stdio): otherwise anything that invoked this
  # through an exec session keeps that session open forever.
  ( while true; do touch "$DEPS_CACHE/live/$me" 2>/dev/null; sleep 300; done ) >/dev/null 2>&1 </dev/null &
  echo $!
}

# --- prune -------------------------------------------------------------------
# Remove per-devbox dirs whose heartbeat is missing/stale. Only ever touches
# $DEPS_CACHE/venv/<name> and $DEPS_CACHE/node_modules/<name> for names that
# are not "golden-*" and not this devbox.
deps_prune() {
  local me dir name hb n=0; me="$(deps_devbox_name)"
  mkdir -p "$DEPS_CACHE/live"
  for dir in "$DEPS_CACHE"/venv/* "$DEPS_CACHE"/node_modules/*; do
    [[ -d "$dir" ]] || continue
    name="$(basename "$dir")"
    [[ "$name" == "$me" || "$name" == golden-* || "$name" == .* ]] && continue
    hb="$DEPS_CACHE/live/$name"
    if [[ -f "$hb" ]] && [[ -n "$(find "$hb" -mmin -"$DEPS_PRUNE_AFTER" 2>/dev/null)" ]]; then
      continue   # heartbeat fresh: that devbox is alive
    fi
    if [[ -n "$(find "$dir" -maxdepth 0 -mmin -"$DEPS_PRUNE_AFTER" 2>/dev/null)" ]]; then
      continue   # dir itself is recent (e.g. a devbox still booting): leave it
    fi
    echo "  pruning stale $dir"
    rm -rf -- "$dir"; rm -f -- "$hb"; n=$((n+1))
  done
  echo "  pruned ${n} stale dependency dir(s)"
}

# --- python venv -------------------------------------------------------------
# Prints the venv path to use (stdout is the return value: progress goes to stderr). Hard-link mode only works when venv and uv
# cache share a mount, which is the whole point of placing it under /cache.
deps_setup_venv() { # backend_dir
  local backend="$1" me venv
  me="$(deps_devbox_name)"
  if deps_cache_usable; then
    venv="$DEPS_CACHE/venv/$me"
    export UV_LINK_MODE=hardlink UV_CACHE_DIR="${UV_CACHE_DIR:-$DEPS_CACHE/uv}"
  else
    venv="${BOW_VENV:-${BOW_WORKSPACE:-/app/workspace}/venv}"
  fi
  export UV_PROJECT_ENVIRONMENT="$venv"
  mkdir -p "$(dirname "$venv")"
  if [[ "${BOW_SYNC_DEPS:-0}" == "1" || ! -x "$venv/bin/uvicorn" ]]; then
    (cd "$backend" && uv sync --frozen --no-install-project --extra kerberos --extra dev)
  fi
  printf '%s' "$venv"
}

_deps_link_node_modules() { # frontend_dir, target
  if [[ -e "$1/node_modules" && ! -L "$1/node_modules" ]]; then rm -rf -- "$1/node_modules"; fi
  ln -sfn "$2" "$1/node_modules"
}

# --- node_modules ------------------------------------------------------------
# Ensures <frontend>/node_modules is populated; prints the real directory.
# Layout: /cache/node_modules/<name>/node_modules. The inner directory MUST be
# called node_modules: packages' postinstall scripts (esbuild, nuxt prepare)
# resolve modules by walking up to a directory with that exact name.
deps_setup_node_modules() { # frontend_dir
  local frontend="$1" me hash golden mine lock tmp
  me="$(deps_devbox_name)"
  if ! deps_cache_usable; then
    if [[ "${BOW_SYNC_DEPS:-0}" == "1" || ! -d "$frontend/node_modules" ]]; then
      (cd "$frontend" && yarn install --frozen-lockfile)
    fi
    printf '%s' "$frontend/node_modules"; return
  fi
  export YARN_CACHE_FOLDER="${YARN_CACHE_FOLDER:-$DEPS_CACHE/yarn}"
  hash="$(sha256sum "$frontend/yarn.lock" | cut -c1-16)"
  golden="$DEPS_CACHE/node_modules/golden-$hash/node_modules"
  mine="$DEPS_CACHE/node_modules/$me/node_modules"
  lock="$DEPS_CACHE/node_modules/.lock-$hash"
  mkdir -p "$DEPS_CACHE/node_modules/$me"

  if [[ "${BOW_SYNC_DEPS:-0}" == "1" ]]; then rm -rf -- "$mine"; fi
  if [[ ! -d "$mine" ]]; then
    if [[ -d "$golden" ]]; then
      # Clone atomically: hard-link into a temp dir, then move it into place,
      # and only then point the checkout at it.
      echo "  cloning node_modules from golden-$hash (hard links)" >&2
      tmp="$DEPS_CACHE/node_modules/$me/.node_modules.tmp.$$"
      rm -rf -- "$tmp"; cp -al "$golden" "$tmp" && mv "$tmp" "$mine"
      _deps_link_node_modules "$frontend" "$mine"
    else
      # No golden yet: install through the project symlink so yarn and every
      # postinstall script (esbuild, nuxt prepare) see <project>/node_modules.
      echo "  no golden node_modules for yarn.lock $hash: running yarn install" >&2
      mkdir -p "$mine"
      _deps_link_node_modules "$frontend" "$mine"
      (cd "$frontend" && yarn install --frozen-lockfile --mutex file:"$YARN_CACHE_FOLDER/.yarn-mutex")
      # Publish as golden for the next devbox (first one to finish wins),
      # but only a tree whose native postinstalls succeeded.
      if [[ -x "$mine/.bin/esbuild" ]] && "$mine/.bin/esbuild" --version >/dev/null 2>&1; then
        if mkdir "$lock" 2>/dev/null; then
          if [[ ! -d "$golden" ]]; then
            tmp="$(dirname "$golden").tmp.$$"; mkdir -p "$tmp"
            cp -al "$mine" "$tmp/node_modules" && mv "$tmp" "$(dirname "$golden")"
            echo "  published golden-$hash" >&2
          fi
          rmdir "$lock"
        fi
      else
        echo "  WARN: esbuild binary missing after install; not publishing a golden tree" >&2
      fi
    fi
  else
    _deps_link_node_modules "$frontend" "$mine"
  fi
  printf '%s' "$mine"
}
