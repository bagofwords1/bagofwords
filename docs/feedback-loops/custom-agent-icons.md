# Feedback Loop — "for each agent (fka data source), I want to be able to set custom icon … then dsicon or any other place where we show the agent icon will show that overridden icon; by default, still use the logic we have today"

Agents (data sources) had no way to carry a custom icon — every place that renders
an agent derived its glyph purely from the connection type (`DataSourceIcon`
maps `type`/`connector_key` → a static asset). This loop validates the added
per-agent icon override: a manager sets an emoji on `/agents/<id>/settings`, and
it then renders everywhere the agent is shown, while agents without an override
keep today's type-based icon.

## Design (validated)

- Stored as a nullable, namespaced token on `data_sources.icon`
  (`app/models/data_source.py`): `"emoji:<grapheme>"` today, `"preset:<key>"`
  reserved for later, `NULL` = default. A namespaced token means presets slot in
  without a schema change, and anything the client doesn't recognise degrades to
  the default icon (an old frontend never breaks on a newer token).
- Exposed on every agent-shaped payload: `DataSourceSchema`,
  `DataSourceListItemSchema`, `DataSourceReportSchema`,
  `DataSourceMinimal/SummarySchema`, plus `data_source_icon` on
  `InstructionReferenceSchema` and the mention / loaded-instruction context so
  reference chips and instructions can show it too.
- Settable via `PUT /data_sources/{id}` (`DataSourceUpdate.icon`), guarded by a
  validator (only `emoji:`/`preset:` tokens, ≤64 chars). An **explicit null**
  clears it — handled specially in `update_data_source` because the generic
  update loop skips `None`.
- Frontend: `DataSourceIcon.vue` gained an `:icon` prop; a shared parser
  (`utils/agentIcon.ts`) turns the token into `{kind, value}` and the component
  renders the emoji when `kind === 'emoji'`, else falls through to the existing
  type/connector logic. `AgentIconPicker.vue` (emoji grid + free paste + reset)
  lives in the agent settings General section. Every agent-identity call site
  now passes `:icon`.

## Loop A — deterministic reproduction / regression (no external services)

`backend/tests/e2e/test_data_source_icon.py::test_data_source_custom_icon_roundtrip`
is the runnable guard. On code **before** this change the `icon` field does not
exist, so the round-trip / list / validator / explicit-null-clear assertions all
fail; **after**, they pass.

```bash
cd backend
export BOW_DATABASE_URL="sqlite:///db/app.db"
uv run pytest tests/e2e/test_data_source_icon.py -q
# 1 passed
```

What it asserts (the general invariants, not one magic value):
- a new agent has `icon == None`;
- an `emoji:` token round-trips through PUT, GET, and the list endpoint;
- a `preset:` token is accepted (forward-compat);
- an un-namespaced/garbage token is rejected `422` and does **not** overwrite the
  stored value;
- an explicit `null` clears the override back to `None`;
- omitting `icon` in a later update leaves it unchanged.

## Loop B — live UI confirmation

Full stack (`tools/agent/boot_stack.sh --dev` + `seed_org.py --demo`), driven
with Playwright against the seeded "Music Store" demo agent:

1. `/agents/<id>/settings` shows a new **Icon** row: default (sqlite) preview +
   "Set custom icon".
2. Clicking it opens the emoji picker popover (grid + paste-any-emoji + reset).
3. Picking 📊 persists (`PUT /data_sources/{id}` → "Saved" toast); the preview
   updates and the button flips to "Change icon".
4. Back on `/agents`, the **Music Store** row in the tree now renders 📊 instead
   of the sqlite glyph — proving the override flows through the shared component.
5. Resetting the icon (`icon: null`) returns the row to the default sqlite icon.

Evidence: `media/pr/custom-agent-icons/` (settings before, picker open, settings
after, agents sidebar after, and before/after crops of the tree row).

## What this proves / regression notes

- The override is stored, validated, round-tripped, and cleared correctly, and it
  renders through the single shared `DataSourceIcon` component — so every
  agent-identity surface (agents tree, agent selector, report agent panel,
  prompts, instructions, review feed, command palette, …) picks it up by passing
  `:icon`.
- Agents without an override are untouched: `icon = NULL` → `parseAgentIcon`
  returns `kind: 'none'` → the existing type/connector icon renders. Connection-
  type pickers and table/connection glyphs deliberately keep the type-based icon.
- Pre-existing unrelated deprecation warnings (`datetime.utcnow`, Pydantic
  `.dict()`) surface in the test log; they reproduce without this change.
