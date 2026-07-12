---
name: security-scan
description: Run a Snyk security scan of the repo (frontend npm deps, backend pip deps, Dockerfile/base image, and Snyk Code SAST), triage findings, and remediate the real ones with verified fixes. Use for "scan with Snyk", "resolve the security issues", dependency-CVE cleanup, or a pre-release security pass.
---

# Security scan — scan with Snyk, then fix what's real

Four independent surfaces, each with its own scan command and remediation
style. Scan all four, triage (many findings are dev-only or false positives),
fix the real ones, and **re-scan to prove the fix** before committing.

The repo already has a curated `.snyk` policy (path exclusions for tests,
migrations, vendored bundles, dev helpers) — extend it, don't fight it.

## Setup

The Snyk API token lives in the **`SNYK_PAT`** environment variable (a secret —
never echo it into a committed file). The Snyk CLI itself reads `SNYK_TOKEN`,
so map one to the other:

```bash
npm install -g snyk
export SNYK_TOKEN="$SNYK_PAT"     # CLI reads SNYK_TOKEN; the repo secret is SNYK_PAT
snyk auth "$SNYK_TOKEN"
```

Find the org id (needed for some commands) from the REST API:

```bash
curl -sS --cacert /root/.ccr/ca-bundle.crt -H "Authorization: token $SNYK_PAT" \
  "https://api.snyk.io/rest/orgs?version=2024-10-15&limit=20"
```

Work off a scratch dir for JSON output; parse it with `python3` (the `--json`
payloads carry `fixedIn`, `from` paths, `severity`, `isUpgradable`).

## 1. Frontend deps (npm / `frontend/yarn.lock`)

```bash
cd frontend && snyk test --json > /tmp/fe.json    # builds the graph from yarn.lock, no install needed
```

Almost everything here is **transitive** — remediate via yarn
`resolutions` in `frontend/package.json` (there is already a large block).
For each vulnerable package, compute the max `fixedIn` across its findings and
set the floor to it.

**Gotchas (learned the hard way):**
- This is **yarn 1 (classic)**. Path-scoped resolution keys like
  `"tailwindcss/postcss-selector-parser"` are silently ignored (that's a Yarn
  Berry feature). Use a **global** key: `"postcss-selector-parser": ">=7.1.2"`.
- **Cap the major** when the fix is on the current major. A bare `>=5.0.2`
  let yarn jump `linkify-it` to `6.0.0`, whose ESM build dropped the default
  export `markdown-it@14` imports → `nuxt build` died with a Rollup error. Pin
  `">=5.0.2 <6"` to stay patched *and* compatible.
- Leave **major-version app-dep bumps** (e.g. `echarts` 5→6, `@nuxt/ui` 2→4)
  out of a security PR — they carry breaking changes; flag them for a
  dedicated change.

After editing resolutions:
```bash
yarn install --ignore-scripts        # regenerates yarn.lock
snyk test --json > /tmp/fe2.json     # confirm the drop
```

**Always run the real build before pushing** — resolutions can break module
resolution in ways the scan can't see:
```bash
NODE_OPTIONS="--max-old-space-size=4096" yarn install && yarn build
```
CI's `playwright-tests` job runs `yarn build` first; if the build breaks, the
job fails. (This is not optional — it's how the linkify-it regression was
caught locally instead of in CI.)

## 2. Backend deps (pip / `backend/uv.lock`)

Snyk supports `uv.lock` natively but needs `uv >= 0.9.29`, and even then the
uv dep-graph endpoint may **403** for the org. Reliable path — export a fully
pinned requirements set, install it into a venv, and scan as pip (this uses
Snyk's working v1 dep-graph endpoint, same as npm):

```bash
cd backend
uv export --format requirements-txt --no-hashes --no-emit-project -o /tmp/req.txt
uv venv /tmp/bvenv                                  # python 3.12 to match the lock
uv pip install --python /tmp/bvenv/bin/python -r /tmp/req.txt
uv pip install --python /tmp/bvenv/bin/python pip   # ← Snyk's pip_resolve needs `pip` present; uv venvs omit it
cd /tmp/bvenv && cp /tmp/req.txt requirements.txt && source bin/activate
snyk test --file=requirements.txt --package-manager=pip --json > /tmp/be.json
```

Remediate in `backend/pyproject.toml` + `backend/uv.lock`:
- If the fix version falls **outside** the pinned constraint (e.g.
  `pillow>=12.2.0,<12.3` but fix is `12.3.0`), widen the ceiling
  (`<12.4`). If it fits, just bump the floor.
- Transitive-only packages (e.g. `httplib2`, `setuptools`) need no
  `pyproject.toml` change — just upgrade in the lock.

```bash
cd backend
uv lock --upgrade-package pillow --upgrade-package pypdf --upgrade-package httplib2   # etc.
uv lock --check                                     # pyproject <-> lock consistent
# re-export, reinstall the changed pkgs into /tmp/bvenv, re-scan -> expect 0
```

## 3. Dockerfile / base image

```bash
snyk container test ubuntu:24.04 --file=Dockerfile --json > /tmp/ct.json
```

Snyk fetches image metadata directly (no docker daemon needed). Usually
**nothing to do**: check `docker.baseImageRemediation.advice` — if it says
"most secure version of the selected base image", there's no better tag. The
runtime stage already runs `apt-get upgrade -y`, which patches the fixable OS
packages at build time (Snyk scans the base *tag*, so it can't see that); the
rest are distro won't-fix. Don't churn the Dockerfile for these.

## 4. Snyk Code (SAST)

```bash
snyk code test --json > /tmp/code.json     # honors .snyk exclude.code paths
```

Triage by location:
- **Dev-only code** (`backend/scripts`, `backend/email_sandbox`, `tools`,
  `frontend/.repro`, `frontend/pages/old_agents`, tests, evals): these are
  standalone verification/seed harnesses that log in to `localhost` with
  throwaway test credentials, not imported by `backend/app`, never shipped.
  Add their paths to `exclude.code` in `.snyk` (match the existing
  `dir` + `dir/**` style).
- **Application code** (`backend/app`, shipped `frontend/**`): read each one.
  Common false positives here — path "traversal" from operator-set **env
  vars**, `hashlib.sha1` used for **content/dedup hashing** (not security),
  enum/status string literals flagged as "hardcoded credential/password"
  (`auth: str = "oauth"`, `pass: 'evals.run.rulePass'`). Per this repo's
  `.snyk` note, genuine issue-level ignores are managed in the Snyk **web UI**,
  not the file. Only change code for a *real* finding.

## Before you commit

- Re-scan every surface you touched; confirm the count dropped.
- `uv lock --check` passes; `frontend/package.json` is valid JSON.
- `yarn build` succeeds locally.
- Commit only the manifests/locks/policy (`.snyk`, `pyproject.toml`,
  `uv.lock`, `package.json`, `yarn.lock`) — no venvs, `node_modules`, or
  scratch JSON. Never commit the Snyk token.
- Summarize what was fixed vs. deferred (major bumps) vs. won't-fix (base OS)
  vs. false-positive, with before→after Critical/High/Medium counts.
