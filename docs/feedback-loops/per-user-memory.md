# Feedback Loop — "add per-user memory (Hermes-style): the agent remembers a user's preferences across sessions"

Per-user, per-org **agent memory**: a small, curated document the agent writes
via an `update_user_memory` tool and that is injected into every future
conversation with that user as `<user_memory>`. Mirrors Hermes' `MEMORY.md`
(bounded, always-injected, agent-curated) and sits alongside the existing
org-scoped **instructions** (reviewed, shared) and per-report **notes**
(ephemeral scratchpad). This loop validates the whole path: tool → DB → prompt
injection → profile UI, plus a live agent actually choosing to call the tool.

## What was built (file:line)

- **Storage**: `Membership.memory` column (`backend/app/models/membership.py:17`);
  migration `backend/alembic/versions/usermem01_add_membership_memory.py`. Lives
  on the membership (not the user) row because, like `note`, it is org-scoped —
  a user's memory in one org must never leak into another.
- **Bound**: `MEMBERSHIP_MEMORY_MAX_LENGTH = 2000`
  (`backend/app/schemas/organization_schema.py`). A full-document rewrite, not an
  append log — the cap forces curation.
- **Tool**: `update_user_memory`
  (`backend/app/ai/tools/implementations/update_user_memory.py`), full-rewrite,
  `allowed_modes=["chat", "deep"]` so it is hidden in training.
- **Injection**: `PromptBuilderV3._format_user_memory`
  (`backend/app/ai/agents/planner/prompt_builder_v3.py`) renders `<user_memory>`
  in the per-turn user message (not the cached system prefix); loaded by
  `agent_v2._resolve_user_profile`. A COMMUNICATION rule tells the model memory
  is subordinate to org instructions.
- **UI**: `Membership.memory` surfaced self-service via
  `GET/PUT /users/me/instructions`
  (`backend/app/routes/user_profile.py`); rendered in the profile modal's
  "Instructions & Memory" tab (`frontend/components/UserProfileModal.vue`). The
  tool renders as a light, single-line, non-expandable status in the report
  stream (`frontend/components/tools/UpdateUserMemoryTool.vue`).

## Loop A — deterministic (no external services)

Backend contracts. The reproduction survives as a regression test.

```bash
cd backend
export BOW_DATABASE_URL="sqlite:///db/app.db" && mkdir -p db
uv run alembic upgrade head          # applies usermem01
uv run pytest tests/e2e/test_user_memory_tool.py \
              tests/unit/test_prompt_builder_v3_user_profile.py -p no:warnings -q
```

Observed (after the change): **16 passed** — persistence, full-rewrite (not
append), over-cap rejection leaves prior memory intact, empty clears, mode
gating (chat/deep expose the tool, training does not), the `/users/me/instructions`
round-trip carries `memory`, and `<user_memory>` renders in the user turn (never
the cached system prefix) and is omitted when empty.

Boundary stubbed: no LLM is involved in Loop A — the tool is driven directly
with a `runtime_ctx` of `{db, user, organization}`.

## Loop B — live confirmation (real OpenAI credentials)

Proves the agent actually *chooses* to call the tool and that the UI renders it.
Secrets via env only.

```bash
tools/agent/boot_stack.sh --dev
cd backend && export BOW_DATABASE_URL="sqlite:///db/agent.db"
uv run python ../tools/agent/seed_org.py --demo   # org + admin + a data source
# register an OpenAI provider (real key via env) with a small model, then:
#   POST /api/reports/{id}/completions  {"prompt": {"content":
#     "Please remember for all future sessions: answers in Hebrew, amounts in
#      shekels. Save this as my preference, then confirm."}}
```

Observed:
- `membership.memory` went from NULL to
  `"- All answers to be written in Hebrew.\n- All monetary amounts shown in shekels."`
- one `tool_executions` row `update_user_memory` / `status=success`.
- In the report UI the call renders as a single line "🔖 Saved user preferences"
  (the agent-supplied title), and the agent's reply came back **in Hebrew** —
  i.e. it acted on what it had just saved.
- The profile modal "Instructions & Memory" tab loaded the stored memory, and an
  edit-and-save round-tripped back to `membership.memory`.

Note: driving the completion over the dev-server websocket from Playwright was
flaky (intermittent `ERR_CONNECTION_RESET` from the `--reload` proxy); driving it
through `POST /completions` is the reliable path and is what the observations
above use.

## What this proves / regression notes

The full tier is wired end to end: a bounded, agent-curated, per-user/per-org
memory that the model writes with `update_user_memory`, that is injected into
subsequent turns as `<user_memory>` subordinate to org instructions, that is
hidden in training mode, and that the user can view/edit in their profile.
`Note` (per-report scratchpad) and the org instruction pipeline are untouched.
