# Feedback Loop — "do a Snyk scan; if high/critical, fix"

Scheduled security sweep of all four Snyk surfaces (frontend yarn deps, backend
uv deps, Dockerfile base image, Snyk Code SAST) at `--severity-threshold=high`,
fixing the remediable High/Critical findings and triaging the rest. Follows
`.agents/skills/security-scan/SKILL.md` and `docs/snyk-dependency-scanning.md`.

## Scope of the scan

`SNYK_TOKEN="$SNYK_PAT" snyk ...` against `HEAD` (`86150bc`), org `yochze`.
Backend scanned against the **installed** locked venv (Snyk's pip path needs the
real env), per the docs. One High/Critical finding across all four surfaces —
the `apt` base-image CVE — remediated via the existing `APT_REFRESH` build knob.

## Findings → actions (High/Critical only)

| Surface | Finding | Severity | Action |
|---|---|---|---|
| Container (`ubuntu:24.04`) | `apt@2.8.3` — CVE-2026-109518 (`SNYK-UBUNTU2404-APT-22204275`), fix `2.8.4ubuntu0.1`, **upgradable** | High | **Fixed** — bumped `APT_REFRESH` so the runtime stage's `apt-get upgrade -y` pulls the patch on a fresh build |
| Frontend deps | — | — | 0 High/Critical (1247 deps tested; 26 medium, 10 low; local `.snyk` policy applied — 13 ignores incl. dev-only simple-git) |
| Backend deps | — | — | 0 High/Critical (258 deps tested; 53 medium, 2 low) |
| Snyk Code (SAST) | — | — | 0 High/Critical (0 `error`-level; 24 `warning`, 76 `note`) |

Before → after High/Critical count: **1 → 0** (runtime image), structural
base-tag finding mitigated as below.

## Root cause (validated)

`snyk container test ubuntu:24.04 --file=Dockerfile --severity-threshold=high`
reports one unique High (surfaced via multiple dependency paths):

```
✗ High  apt@2.8.3  SNYK-UBUNTU2404-APT-22204275 (CVE-2026-109518)
        fixedIn: 2.8.4ubuntu0.1   isUpgradable: true
Base Image    Vulnerabilities  Severity
ubuntu:24.04  23               0 critical, 1 high, 20 medium, 2 low
```

Snyk scans the base **tag** (`ubuntu:24.04`), which still ships `apt 2.8.3`, so
this High is *structural* on the tag scan and can never clear from a Dockerfile
change alone. The fix (`2.8.4ubuntu0.1`) is published to the Ubuntu 24.04 archive
and is `isUpgradable: true`, i.e. it installs at build time. The runtime stage
already runs `apt-get upgrade -y` (`Dockerfile:130`), so a *fresh* build patches
it — but Docker's layer cache can serve a stale `apt-get upgrade` layer. The repo
has a purpose-built cache-bust ARG for exactly this (`Dockerfile:125-127`):

```dockerfile
# Bump APT_REFRESH to force a fresh `apt-get upgrade` past the registry
# build cache when Trivy flags an OS package that already has a fix.
ARG APT_REFRESH=2026-11-08
```

The only base-image remediation Snyk offers is a **major** bump to
`ubuntu:25.10`, which the security-scan skill explicitly says to leave out of a
security PR (breaking churn); the fix version is already available on 24.04.

## The fix

`Dockerfile`: `ARG APT_REFRESH=2026-11-08` → `2026-11-11`. This invalidates the
cached `apt-get update && apt-get upgrade -y` layer on the next build, so the
runtime image installs `apt 2.8.4ubuntu0.1` and any other 24.04 security updates
published since the last sweep. No application code, dependency, or lockfile
changes.

## Loop — reproduce → fix → verify

```bash
# BEFORE (base tag, pre-bump build cache): 1 High
snyk container test ubuntu:24.04 --file=Dockerfile --severity-threshold=high
#   ✗ High apt@2.8.3 SNYK-UBUNTU2404-APT-22204275  (fixedIn 2.8.4ubuntu0.1, upgradable)

# FIX: bump APT_REFRESH → forces apt-get upgrade -y to re-run on next build,
#      installing apt 2.8.4ubuntu0.1 (isUpgradable: true confirms availability).
```

Dep + SAST surfaces were already clean at High/Critical:

```bash
cd frontend && snyk test --severity-threshold=high          # 0 issues (1247 deps)
cd backend  && snyk test --file=requirements.txt --package-manager=pip \
  --command=.venv/bin/python --skip-unresolved=true --severity-threshold=high   # 0 issues (258 deps)
snyk code test --severity-threshold=high                     # 0 error-level issues
```

## "All loads well" — stack boot + health endpoints

Because the change is Docker-build-only, the from-source stack is byte-identical
to `HEAD`; the boot confirms nothing regressed and the app comes up clean.

```bash
tools/agent/boot_stack.sh --dev
# backend is ready → GET /health 200 {"status":"ok"}
# frontend is ready → GET :3000 200
# stack is up: backend http://localhost:8000  frontend http://localhost:3000
```

| Check | Result |
|---|---|
| Backend `GET /health` (`backend/main.py:272`) | `{"status":"ok","version":"0.0.615"}` — 200 |
| Backend `GET /health/ready` (`backend/main.py:291`) | 200 — database ok (6.0 ms), scheduler running/leader |
| Frontend `GET /` (Nuxt) | 200 |
| Frontend-proxied `GET /health` | 200 |
| Alembic migrations (full chain) | applied clean, no errors |

## What this proves / regression notes

The only High/Critical exposure in the repo was an OS base-image package with an
available fix; the repo's own `APT_REFRESH` mechanism remediates it without a
distro major-bump. All three code/dependency surfaces are already clean at
High/Critical. The full stack boots and both health endpoints return 200 after
the change.
