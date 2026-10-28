# Feedback Loop — "do a Snyk scan; if high/critical, fix"

Scheduled security sweep of all four Snyk surfaces (frontend yarn deps,
backend uv deps, Dockerfile base image, Snyk Code SAST) at
`--severity-threshold=high`, fixing the remediable High/Critical findings and
triaging the rest. Follows `.agents/skills/security-scan/SKILL.md` and
`docs/snyk-dependency-scanning.md`.

## Scope of the scan

`SNYK_TOKEN="$SNYK_PAT" snyk ...` against `HEAD` (`c0b0d92`). Backend scanned
against the **installed** locked venv (Snyk's pip path needs the real env — a
bare pinned `requirements.txt` returns `Missing required packages`), per the
docs.

## Findings → actions (High/Critical only)

| Surface | Finding | Severity | Action |
|---|---|---|---|
| Backend deps | `pymongo@4.18.2` — Out-of-bounds Read (`SNYK-PYTHON-PYMONGO-21756829`), fix `4.18.3` | High | **Fixed** — floor bumped `>=4.18.3,<4.19`, re-locked |
| Snyk Code | XSS in `docs/feedback-loops/2026-10-26-agents-push-to-git.git_server.py:31` (`python/XSS`, CWE-79) | High | **Excluded** — dev-only harness, added to `.snyk` `exclude.code` |
| Snyk Code | Path Traversal in `backend/app/routes/dashboard.py:685` (`python/PT`, CWE-23) | High | **False positive** — see below; needs a Snyk web-UI ignore |
| Frontend deps | — | — | 0 High/Critical (28 medium/low, out of scope) |
| Container (`ubuntu:24.04`) | — | — | 0 High/Critical; runtime stage already `apt-get upgrade`s |

## Loop A — deterministic reproduction + fix (backend dep High)

```bash
cd backend
uv sync --frozen --no-dev && uv pip install pip
uv export --frozen --no-emit-project --no-hashes --format requirements-txt \
  | grep -E '==' | sed -E 's/ ;.*$//' | grep -ivE '^(colorama|pywin32|sspilib)==' > requirements.txt
SNYK_TOKEN="$SNYK_PAT" snyk test --file=requirements.txt --package-manager=pip \
  --command=.venv/bin/python --skip-unresolved=true --severity-threshold=high
# BEFORE: ✗ High  pymongo@4.18.2  SNYK-PYTHON-PYMONGO-21756829 (Out-of-bounds Read)
```

Fix: `pymongo>=4.18.2,<4.19` → `>=4.18.3,<4.19` in `backend/pyproject.toml`,
then `uv lock --upgrade-package pymongo` (4.18.2 → 4.18.3; the only package the
lock diff touches). Re-sync and re-scan:

```bash
# AFTER: exit 0 — backend High/Critical: 0
```

## The Path Traversal false positive (dashboard.py:685)

`GET /d/{ref}/previews/{index}` (`get_published_preview`):

```python
own_dir = Path(__file__).parent.parent.parent / "uploads" / "pptx_previews" / f"release-{release.id}"
path = safe_join(own_dir, str(images[index]).removeprefix(f"pptx_previews/release-{release.id}/"))
if not path.is_file() or path.suffix.lower() != ".png":
    raise dashboard_data.not_found()
return FileResponse(path=str(path), media_type="image/png")
```

The only HTTP-controlled input Snyk taints is `index`, which is an **`int`**
bounds-checked (`index < 0 or index >= len(images)`) and used solely as a list
index. The filename is server-stored content (`images[index]` from
`release.content`), joined through `safe_join` (`app/core/path_safety.py`),
which resolves both base and candidate and raises `UnsafePathError` unless the
result stays under `own_dir` — plus a trailing `.is_file()` / `.png` guard.
No attacker-controlled string reaches the path. This is a **false positive**;
the code is correctly guarded.

Per this repo's `.snyk` note, Snyk **Code** issue-level ignores are managed in
the Snyk **web UI** (the `.snyk` file does not support them), not committed
here — so this finding is left for a web-UI ignore rather than churning correct
code. No code change.

## Verification — "all loads well" + health endpoints

Booted the full stack exactly as CI does (`tools/agent/boot_stack.sh`, prod
frontend build) on the patched tree:

- Backend `GET /health` → `200 {"status":"ok"}`
- Frontend `GET /` (`:3000`, prod `.output` server) → `200`
- `backend.log`: migrations applied, APScheduler ticking, no import/boot errors
- `.venv` imports `pymongo 4.18.3`; `import main` loads the FastAPI app cleanly

## What this proves / regression notes

Bumping `pymongo` to the patched `4.18.3` clears the only remediable High in
dependencies and does not break backend import or stack boot. Excluding the
`docs/feedback-loops` dev harnesses drops a non-shipped SAST High without
touching product code. Remaining: the dashboard.py PT false positive (web-UI
ignore) and 28 frontend + 11 backend medium/low dep findings, all out of scope
for a High/Critical sweep.
