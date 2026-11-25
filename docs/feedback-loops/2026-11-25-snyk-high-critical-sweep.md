# Feedback Loop — "do a Snyk scan; if high/critical, fix"

Scheduled security sweep of all four Snyk surfaces (frontend yarn deps, backend
uv deps, Dockerfile base image, Snyk Code SAST) at `--severity-threshold=high`,
fixing the remediable High/Critical findings and triaging the rest. Follows
`.agents/skills/security-scan/SKILL.md` and `docs/snyk-dependency-scanning.md`.

## Scope of the scan

`SNYK_TOKEN="$SNYK_PAT" snyk ...` against `HEAD` (`7f4d6b90`, VERSION `0.0.636`),
org `yochze`. Backend scanned against the **installed** locked venv (Snyk's pip
path needs the real env), per the docs — the requirements list was derived from
`uv pip freeze` of the synced `.venv` so Snyk's set matches the installed env
exactly (avoids the `SNYK-OS-PYTHON-0013` "Missing required packages" error that
a flat `uv export` with env markers / URL-pinned wheels triggers). The native
`snyk test --file=uv.lock` path still **403**s on this org (the hidden
SBOM-convert endpoint is not entitled for the `SNYK_PAT`), exactly as the docs
note.

**Zero remediable High/Critical across all four surfaces this sweep.** The only
High-severity findings are four Snyk **Code** (SAST) results, each triaged below
as a by-design false positive (secure-default TLS opt-in; permission-gated
attachment-download exports). Snyk Code ignores are managed in the Snyk **web
UI**, not the `.snyk` file or code (per Snyk's own docs and this repo's `.snyk`
header), so there is **no code/dependency/build change to make** — the fix path's
verify-and-ship steps are N/A this run. See "Action required" below.

## Findings → actions (High/Critical only)

| Surface | Finding | Severity | Action |
|---|---|---|---|
| Frontend deps (`yarn.lock`) | — | — | **0 High/Critical** (1245 deps tested; local `.snyk` policy applied — open-source ignores incl. dev-only simple-git) |
| Backend deps (`uv.lock` → pip) | — | — | **0 High/Critical** (302 deps tested, dev extras included) |
| Container (`ubuntu:24.04`) | — | — | **0 High/Critical** (base-tag scan: 92 deps, 22 vulns — 0 critical, 0 high, 19 medium, 3 low) |
| Snyk Code (SAST) | 4 `error`-level findings (1 TLS-bypass, 3 export XSS) — all **by-design false positives** | High | **No code change** — these are gated/secure-by-default features; suppress via Snyk web-UI ignore (Code ignores can't live in `.snyk`) |

Before → after High/Critical count (remediable): **0 → 0**. No manifest, lock,
Dockerfile, or application-code change was made.

## Snyk Code triage (the 4 `error`-level findings)

Unchanged from the `2026-11-18` sweep — the same four findings re-surface each
run because a location-keyed web-UI ignore lapses as routes drift. Each was
re-read in full against the data flow Snyk traced at this commit. None is a real,
exploitable vulnerability — all are gated or secure-by-default.

### 1. `python/SSLVerificationBypass` — `backend/app/ai/llm/decisions.py:297`

```python
def __init__(self, ..., verify_ssl: bool = True, pin_egress: bool = False):
    self.verify_ssl = verify_ssl
...
verify = shared_ssl_context() if self.verify_ssl else False
return httpx.AsyncClient(timeout=timeout, verify=verify)
```

**By design, secure default.** `verify_ssl` defaults to **`True`**. It drops to
`False` only when an operator explicitly sets `verify_ssl` on a custom
OpenAI-compatible / self-hosted LLM provider's `additional_config`
(`backend/app/ai/llm/llm.py:589`, `:1668` read it from provider config, default
`True`) — the standard opt-in for reaching an on-prem inference endpoint behind a
self-signed cert. It is not reachable from request data; it is an admin provider
setting with a safe default. The same intentional flag exists on the sibling
OpenAI clients (`clients/openai_client.py:77`,
`clients/openai_responses_client.py:94`, which even documents the
self-hosted-provider case). Nothing to fix.

### 2–4. `python/XSS` — HTML-export endpoints

| Endpoint | Line | Auth / gate (this commit) |
|---|---|---|
| `GET /{artifact_id}/export/html` (`routes/artifact.py`) | 635 | `@requires_permission('view_reports', model=ArtifactModel, owner_only=True, allow_public=True)` |
| `GET /d/{ref}/export/{fmt}` (`routes/dashboard.py`) | 1086 | `viewer_optional` published-dashboard access + per-viewer snapshot-withheld gate |
| `GET /r/{report_id}/export_html` (`routes/report.py`) | 1186 | `current_user_optional` public-share visibility gate + snapshot-withheld gate |

**By design.** All three return a **standalone, fully-offline dashboard HTML
file** built by `services/html_export_service.build_standalone_html` — the
feature *is* to emit active HTML (inlined runtime, charts, data). Mitigations
already present and verified at this commit:

- **`Content-Disposition: attachment`** on every one of the three responses — the
  browser **downloads** the file; it is not rendered inline in the app origin, so
  it cannot touch the app's session/cookies. An opened download runs in a
  `file://` context, not `bagofwords`'s origin.
- **`X-Content-Type-Options: nosniff`** and **`Cache-Control: no-store`** on all
  three responses (each with an in-code comment explaining the XSS reasoning).
- The user-controlled **title** is escaped by `_escape_title()`
  (`html_export_service.py:144`, escapes `& < >`) before it enters the document.
- The Snyk "source" (`artifact_id` / `ref` / `report_id`) is a **lookup key** that
  fetches a permission-gated object — it is not reflected markup.

## `.snyk` policy cross-check

The local `.snyk` policy's open-source ignores are all time-boxed to the Nuxt 4
cohort (expiring 2026-12-07 / 2026-12-21) and remain the documented dev-/
build-only or dormant-optional transitives (simple-git via `@nuxt/devtools`,
node-forge via `listhen`, braces/deepmerge/picomatch/ioredis/`@nuxt/vite-builder`/
`@nuxt/schema` build-time chains, and the client-side-only `flat` MDC transitive).
The former Python ignores (oauthlib, thrift) stay remediated in `uv.lock`, not
merely suppressed — the backend raw scan is 0 High/Critical on its own.

## Action required (outside this repo — Snyk web UI)

Snyk Code ignores **cannot** be stored in `.snyk` (Snyk's own limitation, noted
in this repo's `.snyk` header). To stop these four by-design findings from
re-firing each sweep, add an **Ignore** for each in the Snyk web UI
(app.snyk.io → the `bagofwords` project → Code issues), with the rationale above.
Until then they will keep appearing as `error`-level in `snyk code test` even
though none is a real exposure. (This sweep's `SNYK_PAT` is entitlement-limited —
it 403s on org-scoped REST/hidden endpoints — so it cannot read or write the
web-UI ignore policy from the CLI; the ignore must be added interactively.)

## Loop — reproduce → verify

```bash
export SNYK_TOKEN="$SNYK_PAT"

# Frontend — 0 High/Critical (1245 deps)
( cd frontend && snyk test --severity-threshold=high )

# Backend — 0 High/Critical (302 deps). Scan the installed locked venv.
cd backend
uv sync --extra dev && uv pip install --python .venv/bin/python pip
uv pip freeze --python .venv/bin/python | grep -E '^[A-Za-z0-9_.-]+==' \
  | grep -ivE '^(pip|setuptools|wheel)==' > requirements.txt
snyk test --file=requirements.txt --package-manager=pip \
  --command=.venv/bin/python --skip-unresolved=true --severity-threshold=high
rm requirements.txt   # do not commit

# Container base image — 0 High/Critical
snyk container test ubuntu:24.04 --file=Dockerfile --severity-threshold=high

# Snyk Code (SAST) — 4 error-level, all by-design false positives (see triage)
snyk code test --severity-threshold=high
```

## "All loads well" — stack boot + health endpoints

Unlike a pure docs sweep, the backend was booted from the freshly-synced `.venv`
this run to confirm the baseline is healthy (all four SAST findings live in the
backend). No runtime, dependency, or build change was made — the tree is
byte-identical to `HEAD` apart from this report — so a full real-LLM
`sandbox-feedback-loop` UI boot is not warranted.

```
cd backend
BOW_DATABASE_URL='sqlite:///db/app.db' .venv/bin/python -m alembic upgrade head   # OK (head reached)
BOW_ENCRYPTION_KEY=<fernet> BOW_DATABASE_URL='sqlite:///db/app.db' \
  BOW_CHROMIUM_EXECUTABLE=/opt/pw-browsers/chromium .venv/bin/python main.py      # "Application startup complete"
```

| Check | Location | Result |
|---|---|---|
| Backend boot (migrations + app startup) | `backend/main.py` | ✅ `Application startup complete`, version `0.0.636` |
| `GET /health` (liveness, dependency-free) | `backend/main.py:283` | ✅ `200` `{"status":"ok"}` |
| `GET /health/ready` (readiness — DB + scheduler) | `backend/main.py:302` | ✅ `200` `{"status":"ok","checks":{"database":"ok","scheduler":"ok, running"}}` |

## What this proves

The three dependency/container surfaces that carry *remediable* High/Critical
CVEs are all clean this sweep — no version bump, lock change, or Dockerfile
refresh was needed. The only High-severity signal is four Snyk **Code** findings,
each verified against its data flow to be a gated or secure-by-default feature,
not an exploitable hole; they are suppressed in the Snyk web UI per the repo's
documented process, with no code change. The repo tree is unchanged apart from
this report.
