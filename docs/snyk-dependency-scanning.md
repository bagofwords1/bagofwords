---
name: snyk-dependency-scanning
description: Scan the bagofwords backend (uv) and frontend (yarn) for known dependency vulnerabilities with the Snyk CLI, interpret the results, and apply version-bump fixes. Use when asked to run a Snyk scan, reproduce Snyk dashboard findings locally, or fix High/Critical CVEs in dependencies.
---

# Snyk Dependency Scanning (uv backend + yarn frontend)

This skill explains how to scan this repo's dependencies with the **Snyk CLI**
and how to fix the findings. The backend uses **uv** (`backend/pyproject.toml` +
`backend/uv.lock`); the frontend uses **yarn** (`frontend/package.json` +
`frontend/yarn.lock`).

> Snyk's SCM/UI import cannot natively parse `uv.lock`. The "Enable uv CLI
> Support" org toggle plus the Snyk CLI is the supported path — but on plans
> without the SBOM-convert entitlement, `snyk test --file=uv.lock` fails with a
> **403** (the SBOM-convert API is forbidden). The reliable method below scans
> the **installed** uv environment via Snyk's pip path instead, which every plan
> supports and which produces results that exactly match `uv.lock`.

## Prerequisites

- **Snyk CLI ≥ 1.1304.0** — `npm install -g snyk` (uv support was added here).
- **uv ≥ 0.9.29** — required by Snyk's uv tooling; `uv self update` if older.
- **Auth token** — use the *classic* Auth Token (a UUID from
  https://app.snyk.io/account), exported as `SNYK_TOKEN`. The newer scoped
  `snyk_uat.*` Personal Access Tokens 403 on org-scoped resources unless the
  right scopes are selected. Never commit the token; pass it via env only.

```bash
export SNYK_TOKEN=<classic-uuid-token>
snyk --version   # must be >= 1.1304.0
uv --version     # must be >= 0.9.29
```

`--org=<org-uuid>` may be required if the token can't auto-resolve a default org.

## Backend (uv) — the reliable method

Native `snyk test --file=uv.lock` is blocked on this org (SBOM-convert 403), and
Snyk's *remote* pip resolver ignores transitive pins (it re-resolves its own tree
and reports **false positives** on wrong versions). So scan the **installed**
environment instead — Snyk reads the real installed versions from `uv.lock`:

```bash
cd backend

# 1. Install the locked environment (and pip, which Snyk's pip plugin imports)
uv sync --frozen --no-dev
uv pip install pip

# 2. Export a flat, pinned requirements file from the lock.
#    Strip env markers and drop Windows-only packages that aren't installed on
#    Linux (otherwise Snyk reports SNYK-OS-PYTHON-0013 "Missing required packages").
uv export --frozen --no-emit-project --no-hashes --format requirements-txt \
  | grep -E '==' | sed -E 's/ ;.*$//' \
  | grep -ivE '^(colorama|pywin32|sspilib)==' > requirements.txt

# 3. Scan against the installed env so versions match uv.lock exactly
snyk test --file=requirements.txt --package-manager=pip \
  --command=.venv/bin/python --skip-unresolved=true

rm requirements.txt   # do not commit
```

Notes:
- `--command=.venv/bin/python` points Snyk at the synced venv; without it Snyk
  re-resolves remotely and the versions (and findings) are wrong.
- A trailing `403 Forbidden` printed *after* the results is just post-test
  telemetry on entitlement-limited plans — the scan output above it is valid.

## Frontend (yarn)

Yarn is natively supported — the lockfile carries the full tree, so no install is
needed:

```bash
cd frontend
snyk test                              # full report
snyk test --severity-threshold=high    # gate on High + Critical only
```

## Fixing findings

Apply the **smallest version bump** that clears the CVE, then re-lock and
re-scan. Watch for these traps:

- **Transitive caps.** A High in one package may be gated by another. Fixing
  `starlette` here forced bumps to `fastapi` (≥0.133 drops the `starlette<0.51`
  cap), `fastapi-mail` (≥1.6.5 allows `starlette` 1.x, but pulls
  `cryptography≥49` and `aiosmtplib≥5`). Let `uv lock` surface the chain and
  bump each blocker in `pyproject.toml`.
- **Don't over-widen yarn `resolutions`.** `"vite": ">=7.3.5"` silently pulled
  vite **8.x** (a major, incompatible with Nuxt's vite-builder). Constrain to the
  fix line: `"vite": ">=7.3.5 <8"`. For a direct dep like `nuxt`, bump its floor
  in `dependencies` (`"nuxt": "^3.21.7"`) rather than adding a resolution.
- **Re-lock + smoke test.** Run `uv lock` / `yarn install`, then verify imports
  (`python -c "import <module>"` for packages that changed API surface, e.g.
  `fastapi_mail`, `aiosmtplib`) and re-run the scan to confirm 0 High/Critical.

## One-line CI gate

```bash
# backend (from backend/, env synced as above)
snyk test --file=requirements.txt --package-manager=pip --command=.venv/bin/python \
  --skip-unresolved=true --severity-threshold=high
# frontend
( cd frontend && snyk test --severity-threshold=high )
```

Exit code is non-zero when issues at/above the threshold are found — fail the
build on that.
