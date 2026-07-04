---
name: sandbox-feedback-loop
description: Build a runnable reproduce→fix→verify loop for a bug or feature in a fresh sandbox, and record it as a feedback-loop doc. Use when investigating a reported bug, validating a root cause, or proving a fix works — before and after the change.
---

# Sandbox Feedback Loop

A feedback loop is a **runnable document**: anyone (human or agent) can re-execute
it in a fresh sandbox and observe the same failure, then the same pass after the
fix. Examples of the format live in `docs/feedback-loops/` (e.g.
`save-button.md`, `fabric-obo-second-admin-tables.md`).

## Process

1. **Reproduce first.** Never fix a bug you haven't watched fail. Write the
   smallest deterministic reproduction — a pytest test, a Playwright spec, or a
   static harness — that fails on current code for the reported reason.
2. **Isolate the root cause** and cite it as `file.py:line` references.
3. **Fix**, re-run the same loop, and show the observed output flipping.
4. **Write the doc** to `docs/feedback-loops/<topic>.md` (do NOT add new
   `sandbox-feedback-loop-*.md` files at the repo root).

## Environment setup (fresh sandbox)

The app targets **Python 3.12** (3.12 f-string syntax; the sandbox default
`python` may be 3.11).

```bash
cd backend
pip install uv
uv sync --frozen --extra dev
export BOW_DATABASE_URL="sqlite:///db/app.db"   # required by bow-config.dev.yaml
mkdir -p db
```

- Tests run on SQLite by default; the autouse `run_migrations` fixture builds
  the schema per test (`tests/conftest.py`). Use `--db=postgres` for the
  testcontainers leg, `--db=external` when Docker is unavailable.
- Playwright browsers are pre-provisioned at `/opt/pw-browsers` in cloud
  sandboxes — `export PLAYWRIGHT_BROWSERS_PATH=/opt/pw-browsers`, never
  `playwright install`.
- Full running stack when the loop needs the real UI:
  `tools/agent/boot_stack.sh` then `cd backend && uv run python ../tools/agent/seed_org.py`.

## Doc template

```markdown
# Feedback Loop — <symptom, quoted from the report>

One-paragraph statement of the reported behavior and the claim being validated.

## Root cause (validated)
What actually breaks, citing file:line. Distinguish validated facts from hypotheses.

## Loop A — deterministic reproduction (no external services)
Exact commands + the observed FAIL output. Stub external boundaries (LLMs,
OAuth providers, ODBC) — the loop must run in a clean sandbox.

## Loop B — live confirmation (optional, real credentials)
Only when the premise itself needs a real third party. Secrets via env vars
only — never commit them, never echo them into logs or docs.

## The fix
What changed and where. Re-run Loop A output showing the flip to PASS.

## What this proves / regression notes
What the loop demonstrates; any pre-existing unrelated failures you hit
(verify they reproduce with your change stashed before calling them unrelated).
```

## Rules

- **Loop A must be self-contained** — seeded data, stubbed boundaries, no live
  credentials. Loop B is the exception, not the default.
- **Secrets: env vars only.** Never in the doc, the repo, or command output.
- The reproduction test should survive as a regression test — follow
  `backend/tests/AGENTS.md`: assert the general invariant, not the one magic
  scenario that happened to be reported.
- If the change affects UI/UX, the loop must include before/after evidence —
  invoke the **ui-evidence** skill.
