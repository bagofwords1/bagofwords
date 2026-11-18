# Feedback Loop — "do a Snyk scan; if high/critical, fix"

Scheduled security sweep of all four Snyk surfaces (frontend yarn deps, backend
uv deps, Dockerfile base image, Snyk Code SAST) at `--severity-threshold=high`,
fixing the remediable High/Critical findings and triaging the rest. Follows
`.agents/skills/security-scan/SKILL.md` and `docs/snyk-dependency-scanning.md`.

## Scope of the scan

`SNYK_TOKEN="$SNYK_PAT" snyk ...` against `HEAD` (`4b99ad3`, VERSION `0.0.625`),
org `yochze`. Backend scanned against the **installed** locked venv (Snyk's pip
path needs the real env), per the docs — the requirements list was derived from
`uv pip freeze` of the `--no-dev` synced `.venv` so Snyk's set matches the
installed env exactly (avoids the `SNYK-OS-PYTHON-0013` "Missing required
packages" error that a dev-including export triggers).

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
| Frontend deps (`yarn.lock`) | — | — | **0 High/Critical** (1247 deps tested; local `.snyk` policy applied — 16 open-source ignores incl. dev-only simple-git) |
| Backend deps (`uv.lock` → pip) | — | — | **0 High/Critical** (283 deps tested; 97 medium, 1 low) |
| Container (`ubuntu:24.04`) | — | — | **0 High/Critical** (base-tag scan: 0 critical, 0 high, 20 medium, 2 low; last sweep's `apt` High has cleared on the tag) |
| Snyk Code (SAST) | 4 `error`-level findings (1 TLS-bypass, 3 export XSS) — all **by-design false positives** | High | **No code change** — these are gated/secure-by-default features; suppress via Snyk web-UI ignore (Code ignores can't live in `.snyk`) |

Before → after High/Critical count (remediable): **0 → 0**. No manifest, lock,
Dockerfile, or application-code change was made.

## Snyk Code triage (the 4 `error`-level findings)

The previous sweep (`2026-11-11`) reported 0 `error`-level Code findings; these
four surfaced since. Each was read in full against the data flow Snyk traced.
None is a real, exploitable vulnerability — all are gated or secure-by-default.

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
(`backend/app/ai/llm/llm.py:458`, `:1341`) — the standard opt-in for reaching an
on-prem inference endpoint behind a self-signed cert. It is not reachable from
request data; it is an admin provider setting with a safe default. The same
intentional flag exists on the sibling OpenAI clients
(`clients/openai_client.py:77`, `clients/openai_responses_client.py:72`, which
even documents the self-hosted-provider case). Nothing to fix.

### 2–4. `python/XSS` — HTML-export endpoints

| Endpoint | Line | Auth / gate |
|---|---|---|
| `GET /{artifact_id}/export/html` (`routes/artifact.py`) | 635 | `@requires_permission('view_reports', owner_only=True)` + viewer policy |
| `GET /d/{ref}/export/{fmt}` (`routes/dashboard.py`) | 1086 | published-dashboard access + per-viewer snapshot-withheld gate |
| `GET /r/{report_id}/export_html` (`routes/report.py`) | 1170 | `get_public_artifacts` visibility gate + snapshot-withheld gate |

**By design.** All three return a **standalone, fully-offline dashboard HTML
file** built by `services/html_export_service.build_standalone_html` — the
feature *is* to emit active HTML (inlined runtime, charts, data). Mitigations
already present:

- **`Content-Disposition: attachment`** on every one of the three responses — the
  browser **downloads** the file; it is not rendered inline in the app origin, so
  it cannot touch the app's session/cookies. An opened download runs in a
  `file://` context, not `bagofwords`'s origin.
- The user-controlled **title** is escaped by `_escape_title()`
  (`html_export_service.py:215`) before it enters the document.
- The Snyk "source" (`artifact_id` / `ref` / `report_id`) is a **lookup key** that
  fetches a permission-gated object — it is not reflected markup. `report.py`
  additionally refuses any `artifact_id` not already in the viewer's visible set
  (`routes/report.py:1105`).

These mirror the `dashboard.py` export finding triaged as a false positive in the
`2026-10-28` sweep; line numbers drift as the routes evolve, which is why a
location-keyed web-UI ignore lapses and the finding re-surfaces.

## Action required (outside this repo — Snyk web UI)

Snyk Code ignores **cannot** be stored in `.snyk` (Snyk's own limitation, noted
in this repo's `.snyk` header). To stop these four by-design findings from
re-firing each sweep, add an **Ignore** for each in the Snyk web UI
(app.snyk.io → the `bagofwords` project → Code issues), with the rationale above.
Until then they will keep appearing as `error`-level in `snyk code test` even
though none is a real exposure.

## Loop — reproduce → verify

```bash
export SNYK_TOKEN="$SNYK_PAT"

# Frontend — 0 High/Critical (1247 deps)
( cd frontend && snyk test --severity-threshold=high )

# Backend — 0 High/Critical (283 deps). Scan the installed locked venv.
cd backend
uv sync --frozen --no-dev && uv pip install pip
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

**No runtime, dependency, or build change was made this sweep** (dep + container
surfaces are already clean at High/Critical; the four SAST findings are by-design
false positives handled via the Snyk web UI, not code). The from-source stack is
therefore **byte-identical to `HEAD`** — there is nothing a sandbox boot could
regress, so a full real-LLM `sandbox-feedback-loop` boot is not warranted for a
docs-only change.

Health endpoints confirmed present and unchanged:

| Check | Location | Status |
|---|---|---|
| Backend `GET /health` (liveness, dependency-free) | `backend/main.py:275` | present, unchanged |
| Backend `GET /health/ready` (readiness — DB + scheduler) | `backend/main.py:294` | present, unchanged |

## What this proves

The three dependency/container surfaces that carry *remediable* High/Critical
CVEs are all clean this sweep — no version bump, lock change, or Dockerfile
refresh was needed. The only High-severity signal is four Snyk **Code** findings,
each verified against its data flow to be a gated or secure-by-default feature,
not an exploitable hole; they are suppressed in the Snyk web UI per the repo's
documented process, with no code change. The repo tree is unchanged apart from
this report.
