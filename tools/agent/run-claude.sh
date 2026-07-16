#!/usr/bin/env bash
# Launch Claude Code in the bagofwords sandbox with full project context.
#
# Usage:
#   tools/agent/run-claude.sh                                    # interactive session
#   tools/agent/run-claude.sh "fix the login bug"                # headless one-shot
#   tools/agent/run-claude.sh --skill sandbox-feedback-loop ...   # with skill hint
#
# Environment:
#   ANTHROPIC_API_KEY   required
#   CLAUDE_MODEL        model override (default: uses claude's default)
#
# The script auto-detects whether the stack is running and tells Claude.
# It passes project context via --append-system-prompt so Claude knows
# how to boot, test, and develop without a CLAUDE.md file.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT"

# ---------------------------------------------------------------------------
# System prompt — everything Claude needs to know about this project
# ---------------------------------------------------------------------------
read -r -d '' BOW_SYSTEM_PROMPT << 'SYSPROMPT' || true
You are working on the Bag of Words project — a Python/FastAPI + Nuxt 3/Vue analytics platform.

## Stack Management
Check if stack is running: bash tools/agent/boot_stack.sh --status
Start stack (fast, dev mode): bash tools/agent/boot_stack.sh --dev
Start stack (prod-like): bash tools/agent/boot_stack.sh
Stop stack: bash tools/agent/boot_stack.sh --stop
Seed demo data: cd backend && uv run python ../tools/agent/seed_org.py --demo --invite member@example.com
Stack URLs: backend http://localhost:8000, frontend http://localhost:3000
Login credentials: admin@example.com / Password123!

## Install Dependencies (if not already done)
cd backend && uv sync --frozen --extra dev
cd frontend && yarn install --frozen-lockfile
bash scripts/download-vendor-libs.sh frontend/public/libs

## Running Tests
Backend unit: cd backend && TESTING=true uv run pytest tests/unit -v
Backend e2e: cd backend && TESTING=true uv run pytest -m e2e --db=sqlite
Frontend e2e: cd frontend && npx playwright test
Single test file: cd backend && TESTING=true uv run pytest tests/unit/test_<name>.py -v
i18n sweep: cd frontend && npx playwright test --config=playwright.i18n.config.ts

## Key References (Claude auto-discovers these, but here for quick lookup)
- Root agent guide: AGENTS.md
- Backend architecture + feature checklist: backend/AGENTS.md
- Frontend architecture + feature checklist: frontend/AGENTS.md
- Test philosophy + anti-overfit rules: backend/tests/AGENTS.md
- Helm chart guide: k8s/AGENTS.md
- Agent skills (read the matching SKILL.md before starting): .agents/skills/<name>/SKILL.md
- Design docs: docs/design/
- Feedback loop examples: docs/feedback-loops/
- Debug DB: sqlite3 backend/db/agent.db

## Available Agent Skills
- sandbox-feedback-loop: reproduce-fix-verify loop for bugs/features
- qa: full QA pass with functionality mapping
- ui-evidence: mandatory before/after screenshots for UI changes
- docs-update: update docs.bagofwords.com via Mintlify MCP
- localization: i18n architecture, adding strings/locales, RTL
- add-connection-type: add a data source connector end-to-end
- add-llm-provider-or-model: add LLM models/providers to catalog

## Conventions
- Always run relevant tests before reporting work complete
- For UI changes: capture before/after screenshots per .agents/skills/ui-evidence/SKILL.md
- i18n: changes to locale catalogs (locales/*.json) must touch all active locales (en, es, he)
- DB schema changes require alembic migration: cd backend && uv run alembic revision --autogenerate -m "description"
- No Docker-in-Docker in sandbox; use --db=sqlite for backend tests
- When work on a feature or bug fix is complete (tests pass, code is ready), create a git commit with a clear message describing what changed and why. Stage only the relevant files — do not use `git add -A`
- Screenshots tool: node tools/agent/capture.mjs <url> <output.png>
- Stack logs: /tmp/bow-agent/{backend,frontend}.log
SYSPROMPT

# ---------------------------------------------------------------------------
# Detect stack state and append to system prompt
# ---------------------------------------------------------------------------
STACK_NOTE=""
if bash tools/agent/boot_stack.sh --status 2>/dev/null | grep -q "not running"; then
  STACK_NOTE="

## Current Stack State
The stack is NOT running. Boot it with: bash tools/agent/boot_stack.sh --dev
Then seed data: cd backend && uv run python ../tools/agent/seed_org.py --demo --invite member@example.com"
elif bash tools/agent/boot_stack.sh --status 2>/dev/null | grep -q "running"; then
  STACK_NOTE="

## Current Stack State
The stack is already running (backend :8000, frontend :3000) with demo data seeded.
Run 'bash tools/agent/boot_stack.sh --status' to verify before starting work."
fi

FULL_PROMPT="${BOW_SYSTEM_PROMPT}${STACK_NOTE}"

# ---------------------------------------------------------------------------
# Build claude args
# ---------------------------------------------------------------------------
CLAUDE_ARGS=(
  --dangerously-skip-permissions
  --append-system-prompt "$FULL_PROMPT"
  --mcp-config '{"mcpServers":{"bow-k8s":{"type":"sse","url":"http://mcp-server.openshell.svc:8080/sse"}}}'
  --allowedTools "mcp__bow-k8s__*"
)

if [ -n "${CLAUDE_MODEL:-}" ]; then
  CLAUDE_ARGS+=(--model "$CLAUDE_MODEL")
fi

# ---------------------------------------------------------------------------
# Launch: headless (-p) or interactive
# ---------------------------------------------------------------------------
if [ -n "${1:-}" ]; then
  claude "${CLAUDE_ARGS[@]}" -p "$*"
else
  claude "${CLAUDE_ARGS[@]}"
fi
