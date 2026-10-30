# Feedback loop — built-in chatbot authoring skill

The catalog previously offered general data-app design, but no focused workflow
for creating an agent chatbot artifact or embedding it in a host application.
This change adds `create-chatbot` as a default, chat-only skill. It does not change
the artifact runtime, permissions, identity flows, or available tools.

## Reproduce and verify

In a fresh checkout, use Python 3.13 and the existing backend test setup:

```bash
cd backend
uv sync --frozen --extra dev
mkdir -p db
export BOW_DATABASE_URL=sqlite:///db/app.db
export TESTING=true
uv run pytest tests/unit/test_skill_catalog_library.py tests/e2e/test_skill_catalog.py --db=sqlite -q --tb=short --disable-warnings --show-capture=no
```

Before adding the skill, the updated curated-order expectation failed both
`test_catalog_leads_with_the_curated_entries_in_order` and
`test_curated_entries_are_enabled_by_default_and_nothing_else_is`:
`At index 3 diff: 'complex-dashboard' != 'create-chatbot'`.

After adding the Markdown file: **44 passed** (22.91 seconds locally).
The existing tests cover catalog parsing/order, default installation, published
instruction contents, backfill, disable/customization preservation, and skill
discovery through the instruction context. No exact prose assertions were added.

## Scope and reference checks

- `backend/app/ai/skills/library/create-chatbot.md`: workflow and discovery metadata.
- `backend/app/ai/skills/catalog.py`: automatically loads the Markdown catalog.
- `backend/app/services/skill_catalog_service.py`: default installation/backfill.
- `backend/app/ai/agents/planner/artifact_sdk_reference.py`: current conversation
  API. Ready-made chat components are conditional on their presence in this
  reference; they are introduced in PR #1523 and are not required by this PR.
- `backend/app/services/artifact_conversations.py`: viewer-owned conversations,
  allowed agents, and model resolution/fallback.
- `backend/app/ai/agent_v2.py` (`ARTIFACT_CHAT_TOOL_ALLOWLIST`): the read/query
  subset; the skill must not promise arbitrary MCP or write tools.

This loop validates catalog integration, not generated chatbot quality. No live
model authoring, browser chat flow, ERP/ServiceNow host, downstream OBO,
PostgreSQL run, or air-gapped deployment was exercised for this content change.
