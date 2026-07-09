# Feedback Loop — "each user should be able to set their own default model"

Previously the default LLM model was a single org-wide flag: every member's
chat resolved to the same `llm_models.is_default` row. This loop validates the
per-user default: a member picks a personal default (from the models *they*
can use) in Account Settings → General, chat without an explicit `model_id`
uses it, other users are unaffected, and a preference that later becomes
unusable (model disabled/restricted, grant revoked) silently falls back to the
org default.

## Root cause (validated)

Not a bug — a missing capability. Before the change:

- Resolution was org-only: `Organization.get_default_llm_model`
  (`backend/app/models/organization.py:48`) picked the row with
  `is_default=True`; the chat paths (`backend/app/services/completion_service.py`
  ~298/502/1881) called it directly, so there was nowhere to store or honor a
  per-user choice.
- No storage: `memberships` (the per-user-per-org record, already home of the
  custom-instructions `note`) had no default-model column.
- No self-service API: `/users/me/*` (`backend/app/routes/user_profile.py`)
  had no default-model endpoints, and `GET /llm/models` had no way to tell a
  client which model the caller had chosen.

## The change

- `memberships.default_llm_model_id` (migration `ud1a2b3c4d5e`) — soft
  reference, resolved leniently at read time.
- `LLMService.get_default_model_for_user` (`backend/app/services/llm_service.py`)
  — membership default if still enabled + provider alive + user still allowed
  (`user_can_use_model`), else org default. Wired into the chat paths in
  `completion_service.py`, `test_run_service.py`, and `app/ai/tools/mcp/context.py`.
- `GET/PUT /api/users/me/default_model` — strict on write: 404 unknown model,
  400 disabled, 403 restricted-without-grant (user/group/role grants all count).
- `GET /api/llm/models` marks the caller's choice via `is_user_default`;
  the profile modal selector, `PromptBoxV2`, and `TestPromptBox` prefer it
  over `is_default`.

## Loop A — deterministic reproduction (no external services)

```bash
cd backend
export TESTING=true BOW_DATABASE_URL="sqlite:///db/app.db"
uv run pytest tests/e2e/rbac/test_user_default_llm_model.py -q
```

Before the change (feature code stashed, test file kept):

```
>       assert resp.status_code == 200, resp.json()
E       assert 404 == 200        # GET /users/me/default_model does not exist
1 failed
```

After the change:

```
5 passed
```

The suite covers: set/clear + `is_user_default` marking, per-user isolation,
404/400 on unknown/disabled models, **group-RBAC** (restricted model → 403
without a grant, allowed after a group grant, under the `enterprise_license`
fixture), and stale-preference fallback (restrict-after-set → org default,
un-restrict → preference effective again — never breaks chat, never leaks a
restricted model).

## Loop B — live confirmation (real Anthropic key)

Needed because the premise is "chat actually runs on the chosen model".
Key passed via `ANTHROPIC_KEY` env var only.

```bash
tools/agent/boot_stack.sh
cd backend && uv run python ../tools/agent/seed_org.py --org-name "Main Org" --invite member@example.com
ANTHROPIC_KEY=... SEED_SUMMARY=/tmp/bow-agent/seed.json uv run python <verify_live.py> db/agent.db
```

The script creates a real Anthropic provider with three models
(`claude-sonnet-5` = org default, `claude-haiku-4-5-20251001`,
`claude-sonnet-4-6`), then observed:

```
PASS  live test_connection  {"success": true, "message": "Successfully connected to LLM"}
PASS  PUT personal default
PASS  is_user_default marked for member  ['claude-sonnet-4-6']
PASS  admin list unaffected
PASS  member completion used PERSONAL default  ('claude-sonnet-4-6', 'system')
PASS  admin completion used ORG default  ('claude-sonnet-5', 'system')
PASS  member falls back to ORG default  ('claude-sonnet-5', 'system')   # after admin disabled the model
PASS  PUT disabled model rejected 400
PASS  PUT unknown model rejected 404
```

The completion checks read `completions.model` for real chat runs issued
without a `model_id` — the resolution chain end-to-end, including the actual
Anthropic API round-trip.

Note: live restriction endpoints require an enterprise license (signed JWT),
so the RBAC leg runs in Loop A under the license fixture; enforcement fails
open in community mode by design (`llm_access_control_active`).

## UI evidence

`media/pr/ai-eager-dirac-oi587p/`: `before-general.png` (no selector),
`after-general.png`, `after-selector-open.png` (PromptBoxV2-style selector
with provider icons + "Organization default" entry), `after-selected.png`
(saved toast), `after-promptbox.png` (prompt box preselecting the personal
default while the org default differs).

## What this proves / regression notes

Personal default is stored per user per org, honored by real chat, invisible
to other users, gated by model access control on write, and self-healing on
read. Pre-existing unrelated failures: `tests/e2e/test_llm_providers.py` (5
tests) fail for missing `OPENAI_API_KEY_TEST` — identical with the change
stashed. `tests/e2e/rbac/` (123 tests) passes fully with the change.
