# Feedback Loop — "changing the LLM on the report level should override user and org"

Previously the model for a completion resolved as *per-message override
(`prompt.model_id`) > user default > org default*, with no place to store a
per-**report** choice: switching the model in a report's prompt box changed the
next message but was forgotten on reload, and non-interactive runs (a run
issued without an explicit `prompt.model_id`) always fell back to the user/org
default. This loop validates a report-level override that sits one tier above
the user default: **prompt.model_id > report.model_id > user default > org
default**, persisted on the report, honored by the resolution the chat paths
use, and — like the user default — self-healing when it goes stale.

## Root cause (validated)

Not a bug — a missing capability, mirroring `user-default-llm-model.md`. Before
the change:

- No storage: `reports` had no model column
  (`backend/app/models/report.py`) — the prompt box's selection lived only in
  local component state and rode along as `prompt.model_id` on each message.
- Resolution was user-vs-org only: the three chat paths in
  `backend/app/services/completion_service.py` (~313 / ~526 / ~1915) called
  `LLMService.get_default_model_for_user(...)` in the no-override branch, so a
  report could not influence the model.
- No persistence path: `ReportUpdate`
  (`backend/app/schemas/report_schema.py`) had no `model_id`, and the prompt
  box never wrote its selection back to the report.

## The change

- `reports.model_id` (migration `rptllm01`) — soft reference to
  `llm_models.id`, no FK, resolved leniently at read time (same convention as
  `memberships.default_llm_model_id` / `prompts.model_id`).
- `LLMService.get_default_model_for_report(db, org, user, report)`
  (`backend/app/services/llm_service.py`) — report override when it is still
  enabled, its provider is alive, and the user can use it
  (`user_can_use_model`); else delegates to `get_default_model_for_user`
  (user, then org). Wired into the no-override branch of all three chat paths
  in `completion_service.py`.
- `LLMService.validate_model_for_user(...)` — strict write check reused when a
  user *sets* the override: 404 unknown, 400 disabled, 403 restricted-without-
  grant.
- `PUT /reports/{id}` accepts `model_id`
  (`report_service.update_report`): omitted = untouched, `""` = clear back to
  default, an id = set after `validate_model_for_user`. `ReportSchema`
  (and the explicit constructor in `get_report`) returns `model_id` so clients
  hydrate the selector from it.
- Frontend: `PromptBoxV2` persists the selection to the report on change
  (`persistModel`, mirrors `persistMode`) and adopts `report.model_id` when the
  report loads (`initialModel` prop + a `[initialModel, models]` watcher);
  the report page passes `:initialModel="report?.model_id || ''"`.

## Loop A — deterministic reproduction (no external services)

```bash
cd backend
export TESTING=true BOW_DATABASE_URL="sqlite:///db/app.db"
uv run pytest tests/e2e/rbac/test_report_default_llm_model.py -q
```

Before the change (feature code stashed, migration + test kept):

```
6 failed
# e.g. AssertionError: assert None == '<model id>'   (PUT model_id ignored,
#      GET report has no model_id) and AttributeError on
#      get_default_model_for_report (resolution tier absent)
```

After the change:

```
6 passed
```

The suite covers: set/clear + read-back (`""` clears to org default); an
omitted `model_id` on an unrelated PUT (title edit) leaves the override intact;
the **precedence** ladder (only-user-default → user wins over org; add report
override → report wins over user; clear report override → falls back to the
user default, not straight to org); strict write (404 unknown / 400 disabled);
lenient read (model disabled after being set → degrades to org, never raises);
and **group-RBAC** (restricted model → 403 without a grant, allowed after a
group grant, under the `enterprise_license` fixture).

Regression guard — unchanged:

```
uv run pytest tests/e2e/rbac/test_user_default_llm_model.py -q   # 5 passed
uv run pytest tests/e2e/test_report.py -q                        # 11 passed
```

## UI evidence

Full running stack (`tools/agent/boot_stack.sh --dev`, seeded org + demo data
source + an Anthropic provider with three models, Sonnet 5 = org default).
Driven with Playwright; `media/pr/report-llm-precedence-1vpnfe/`:

- `01-before-org-default.png` — fresh report, selector shows the org default
  **Claude Sonnet 5** (`report.model_id` null).
- `02-selector-open.png` — the model popover, checkmark on Sonnet 5.
- `03-after-pick-opus.png` — after picking **Claude Opus 4.8**; the same click
  PUT `model_id` onto the report (`GET /reports/{id}.model_id` == the Opus id,
  asserted in the run).
- `04-after-reload-persisted.png` — after a **full page reload** the selector
  still shows **Claude Opus 4.8**: the choice is stored on the report, not just
  in component state.

## What this proves / regression notes

A report can carry its own model; it is stored on the report row, survives
reload, overrides both the user and org defaults for runs resolved without an
explicit per-message `prompt.model_id`, is gated by model access control on
write, and self-heals on read (stale/restricted → user, then org — never
breaks chat, never leaks a restricted model). The interactive per-message
override (`prompt.model_id`) still wins over everything, unchanged.
Pre-existing unrelated ruff style findings (repo-wide, e.g. in-function imports
matching the sibling test) are not touched by this change.
