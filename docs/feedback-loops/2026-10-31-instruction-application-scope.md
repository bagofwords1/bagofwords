# Feedback Loop — "instructions per connected app, picked like Slack/Teams"

An admin who has embedded BOW in an outside system (an ERP, a support portal,
an MCP client) wants rules that apply only to conversations held in that
system: "these users are warehouse managers, two sentences, lead with the
number". The claim: in the instruction editor, the Channels multi-select now
lists the org's connected applications next to Web app / Slack / Teams /
AI mailbox / MCP, and an instruction scoped to an app loads into the agent's
context only for conversations that came through that app.

## Design (what was built)

- **Storage:** no migration. An app is a value in the existing
  `applicable_channels` list: `connected_app:<application id>`
  (`app/models/connected_application.py`, `application_channel()`).
  Empty list still means "everywhere"; any mix of channels and apps works.
- **Which app a conversation belongs to:** the report's origin stamp
  (`reports.origin_app_id`, set by `app/core/request_origin.py`), so
  follow-ups and background runs keep it; MCP tools that build context
  without a report fall back to the request's origin
  (`ContextHub.__init__`, `app/ai/context/context_hub.py`).
- **Matching:** `InstructionContextBuilder._passes_mode_channel` now matches
  if any of the conversation's channels is listed: its delivery channel
  (`app`, `slack`, …) plus `connected_app:<id>` when it came through an app
  (`instruction_context_builder.py`, `_current_channels`). A "Web app" rule
  therefore still loads inside an embed, as before.
- **Picker:** `GET /api/instructions/application-targets` lists the org's
  non-deleted apps (`enabled`, `agent_ids`) to anyone who may edit
  instructions (org-level or per agent); `/api/connected_applications`
  stays behind `manage_settings`. The editor offers an app only when it is
  enabled and can reach one of the instruction's agents (any app for an
  instruction with no agents); apps already selected always stay listed —
  disabled ones read "<name> (disabled)", deleted ones "Deleted application"
  (`frontend/components/KnowledgeExplorer.vue`, `channelOpts`).
- **Validation on save:** an app id from another org (or no app at all) →
  400 `INSTRUCTION_APPLICATIONS_INVALID`; an id of an app since deleted is
  dropped so the instruction still saves
  (`connected_application_service.validate_instruction_channels`).
- **Mid-run rebuild** (`agent_v2.py`, roster refresh) now passes channel and
  app too; before, it passed no channel, so it skipped channel scoping
  entirely.

## Root cause found by the live loop (validated)

The first live run loaded the app rule **nowhere** even though unit-level
tests passed. `ContextHub.prime_static` keeps a process-wide
`_INSTRUCTIONS_CACHE` (2-minute TTL) whose key had org, agents, build,
query, user and scope — but not mode, channel or app
(`context_hub.py`, `instr_key`). A web-app turn and an ERP turn with the same
prompt shared one entry, so whichever ran first decided the other's
instructions. This was already wrong for Slack/Teams-scoped instructions with
identical prompts; app scoping made it visible. Fix: mode, channel and
application id are now part of the key.

## Loop A — deterministic (no external services)

```bash
cd backend
TESTING=true BOW_DATABASE_URL=sqlite:///db/app.db uv run pytest \
  tests/e2e/test_instruction_application_scope.py \
  tests/e2e/rbac/test_instruction_application_targets.py -m e2e -q
```

- `test_app_scoped_instruction_loads_only_in_that_apps_conversations` —
  unscoped, app A, A+B, Slack+B and Web-app rules against in-app / A / B.
- `test_conversation_started_through_an_app_keeps_its_instructions` — a real
  OAuth app token creates the report (origin stamped by the real auth path);
  the agent's own `prime_static` (with the shared cache) runs BOW → app → BOW
  with the same prompt.
- `test_app_channels_are_validated_and_deleted_apps_dropped` — foreign and
  unknown ids rejected; deleted app dropped on save; an edit that doesn't
  send the scope leaves it alone.
- `test_application_targets_list_the_orgs_apps` /
  `test_instruction_editors_list_application_targets_without_settings_access`
  — disabled flagged, deleted hidden, other orgs hidden; a per-agent manager
  can list (but not manage) apps, a plain member gets 403.

Observed with the change stashed: **5 failed** (no `application_id` param;
scoped rule never loads; foreign id saved with 200; endpoint 404). With the
change minus the cache-key fix, the origin test fails:
`AssertionError: assert '<id>' in set()`. With everything: **5 passed**.
Related suites (instructions, roster scope, connected apps, origin app, skill
catalog, build authority, backend unit tests) pass.

## Loop B — live (real stack, real agent run)

```bash
tools/agent/boot_stack.sh --dev
cd backend && uv run python ../tools/agent/seed_org.py --sqlite-sources 1
ANTHROPIC_KEY=... uv run python ../docs/feedback-loops/2026-10-31-instruction-application-scope.live.py
```

Creates two OAuth apps, one rule scoped to "Northwind ERP" and an unscoped
control, then asks the same question in BOW, via Northwind ERP's token and
via Support Portal's token, streaming each turn and reading the agent's own
`instructions.context` event (what it put in the prompt, emitted before the
model call).

Before the cache-key fix:
```
BOW web app    | ERP rule loaded: False | control loaded: True
Northwind ERP  | ERP rule loaded: False | control loaded: True   <- wrong
Support Portal | ERP rule loaded: False | control loaded: True
```
After:
```
BOW web app    | app: None     | ERP rule loaded: False | control loaded: True
Northwind ERP  | app: 96707c6e | ERP rule loaded: True  | control loaded: True
Support Portal | app: 58cbd2d5 | ERP rule loaded: False | control loaded: True
```
The sandbox's Anthropic key had no credit, so the model call itself failed
("credit balance is too low"); the context assertion does not depend on it.
`reply_has_marker` was therefore not exercised.

## UI evidence

`docs/feedback-loops/2026-10-31-instruction-application-scope.ui.mjs`
(Playwright, `/agents/instructions/<id>` → Advanced → Channels).
Screenshots in `media/pr/instruction-application-scope/`:

| | |
|---|---|
| before | Channels lists the five fixed channels only |
| after-1 | + "Northwind ERP", "Support Portal"; disabled "Legacy Kiosk" not offered |
| after-2 | both apps checked |
| after-3 | after reload: "Northwind ERP +1" (saved; API returns both `connected_app:` ids) |
| after-4 | reopened: both still checked |

## Regression notes / found in passing

- `GET /api/console/agent_executions/{id}/context_snapshot/{id}` returns 500:
  the route calls `ContextSnapshotService.get_context_snapshot`, which
  doesn't exist (`get_snapshot` does, and it doesn't check that the
  snapshot belongs to the run). Not touched here.
- Locale catalogs other than `en` already have key drift at HEAD; this change
  adds the same three keys to all ten catalogs and adds no drift.
