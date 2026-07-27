# Sandbox Feedback Loop — Snyk high-severity frontend transitive deps (postcss, brace-expansion)

Validates a Snyk scan across all four surfaces (frontend npm, backend pip,
Dockerfile base image, Snyk Code SAST) and the remediation of the only
**high**-severity findings: two transitive frontend packages. The loop shows
the scan flipping from **2 high → 0 high**, and proves the app still builds and
serves (frontend SSR + backend `/health`) after the resolution bumps.

No critical findings existed on any surface. Backend deps, the Docker base
image, and Snyk Code produced no high/critical.

## Root cause (validated)

Two transitive dependencies in `frontend/yarn.lock` resolved to vulnerable
versions:

- **postcss@8.5.14** — `SNYK-JS-POSTCSS-18313038`, Directory Traversal (High).
  Fixed in `8.5.18`.
- **brace-expansion@5.0.7** — `SNYK-JS-BRACEEXPANSION-18313044` /
  `CVE-2026-14257`, Allocation of Resources Without Limits or Throttling
  (ReDoS, High). Fixed in `5.0.8`.

Both are pulled in transitively (build tooling / Nuxt toolchain), so the fix is
a Yarn `resolutions` floor bump in `frontend/package.json`, not a direct
dependency change. Both already had resolution entries that were one patch
behind the fix:

```
"brace-expansion": ">=5.0.7",   ->  ">=5.0.8 <6"
"postcss":         ">=8.5.10",  ->  ">=8.5.18 <9"
```

The major is capped (`<6`, `<9`) per the `security-scan` skill's linkify-it
lesson — a bare `>=` floor can let Yarn 1 jump a package to a breaking next
major.

## Loop A — deterministic reproduction (Snyk scan, no app runtime)

Setup (token via env var only — never committed or echoed):

```bash
npm install -g snyk
export SNYK_TOKEN="$SNYK_PAT"
snyk auth "$SNYK_TOKEN"
ORG=35b24828-3089-449e-9bd3-d2b9f0b7f77a   # yochze
```

Scan the frontend from `yarn.lock` (no install needed):

```bash
cd frontend && snyk test --json --org="$ORG" > /tmp/fe.json
```

**Observed BEFORE the fix:** 16 findings — **12 high** (across dependency
paths) + 4 medium; unique high packages: `postcss@8.5.14`,
`brace-expansion@5.0.7`.

Other surfaces (same run), for completeness:

```bash
# backend pip — export pinned reqs into a py3.12 venv, scan as pip
cd backend
uv export --format requirements-txt --no-hashes --no-emit-project -o /tmp/req.txt
uv venv --python 3.12 /tmp/bvenv
uv pip install --python /tmp/bvenv/bin/python -r /tmp/req.txt
uv pip install --python /tmp/bvenv/bin/python pip
cd /tmp/bvenv && cp /tmp/req.txt requirements.txt && source bin/activate
snyk test --file=requirements.txt --package-manager=pip --org="$ORG" --json   # -> 0 vulns

# Docker base image
snyk container test ubuntu:24.04 --file=Dockerfile --org="$ORG" --json       # -> 0 high/crit; "most secure base image"

# Snyk Code SAST (honors .snyk exclude.code)
snyk code test --org="$ORG" --json                                            # -> 0 error-level (3 medium, 25 low)
```

## The fix

`frontend/package.json` resolutions bumped (see diff above), then:

```bash
cd frontend
yarn install --ignore-scripts     # regenerates yarn.lock
snyk test --json --org="$ORG" > /tmp/fe2.json
```

Resolved versions in the regenerated lock: `brace-expansion 5.0.8`,
`postcss 8.5.23`.

**Observed AFTER the fix:** 4 findings — **0 high / 0 critical**, 4 medium
remain (deferred — see below). The high count flipped `12 → 0`.

## Loop B — app still loads (build + runtime health)

Resolutions can break module resolution in ways the scan can't see, so build
and boot for real.

```bash
# Frontend production build (CI's playwright-tests job runs this first)
cd frontend && NODE_OPTIONS="--max-old-space-size=4096" yarn build   # -> Build complete, exit 0
node .output/server/index.mjs                                        # SSR server -> GET / => HTTP 200

# Backend boot + liveness probe
cd backend
export BOW_DATABASE_URL="sqlite:///db/app.db"
uv run alembic upgrade head          # build schema on the fresh sqlite db
uv run uvicorn main:app --port 8000  # (ANTHROPIC_API_KEY + BOW_ENCRYPTION_KEY set via env)
curl -s localhost:8000/health        # -> {"status":"ok"}  (HTTP 200)
```

`/health` (`backend/main.py:245`) is the k8s / docker / CI liveness probe.

## What this proves / regression notes

- The only high-severity findings (both frontend transitive) are resolved,
  verified by re-scan (`12 high → 0 high`, `0 critical` throughout).
- The change is build-tooling-only; the frontend production build and SSR
  server, and the backend app + `/health`, all come up clean after it.
- **Deferred (out of scope for a security PR):** the 4 remaining mediums are
  either major app-dep bumps that carry breaking changes (`@nuxt/ui` 2→4,
  `echarts` 5→6) or low-priority transitive patches (`svgo`, `tar`); the
  `security-scan` skill says to flag these for a dedicated change, not fold
  them into a CVE PR.
- **Won't-fix:** the Docker base image is already on the most secure
  `ubuntu:24.04` tag (Snyk's own advice); the 85 low/medium OS-package
  findings are distro won't-fix / patched at build time by the runtime stage's
  `apt-get upgrade`.
