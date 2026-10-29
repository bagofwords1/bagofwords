# Feedback Loop — revoked columns and unchecked @table mentions reach the agent prompt

Two related leaks in what the agent is told about tables. Both were validated
live against a real PostgreSQL connection running `auth_policy=user_required`
with column-level grants.

1. **Revoked columns.** When a user's column grant is revoked, the next
   per-user sync marks that column `is_accessible=False`. The user's schema API
   then hides it, but the agent's schema section still lists it.
2. **@table mentions.** A table mention renders the raw catalog row for
   whatever table id the client sends. It doesn't check the run's agents,
   table activation, or the caller's per-user overlay. So a normal UI mention
   also re-exposes revoked columns, and a crafted mention id exposes a table
   that is inactive and that the caller can't reach at all.

## Root cause (validated)

- `SchemaContextBuilder.build`, overlay branch
  (`backend/app/ai/context/builders/schema_context_builder.py`, the
  `UserDataSourceColumn` query), selected every overlay column row with no
  `is_accessible` filter. `_upsert_user_overlay`
  (`backend/app/services/data_source_service.py`, "Revoke columns no longer
  returned") keeps revoked column rows with `is_accessible=False`. The other
  reader, `read_user_data_source_schema`, already filters them out.
- `MentionContextBuilder.build`, TABLE branch
  (`backend/app/ai/context/builders/mention_context_builder.py`), called
  `db.get(DataSourceTable, mention.object_id)` and rendered its `columns` JSON
  directly. `object_id` is taken unvalidated from the completion's `mentions`
  payload (`MentionService.create_completion_mentions`), which is
  client-supplied. FILE mentions in the same method are already filtered to
  viewable files for exactly this reason.
  Side bugs in the same branch: it read `tbl.data_source_id` (the field is
  `datasource_id`, so `data_source_name` was always null), and it formatted
  dict columns with `getattr(c, "name")`, which produced
  `"{'name': 'id', 'dtype': 'integer'}:None"`.

## Loop A — deterministic reproduction (no external services)

```bash
cd backend
export TESTING=true BOW_DATABASE_URL='sqlite:///db/app.db'
uv run pytest -q tests/unit/test_mention_table_visibility.py \
  tests/unit/test_overlay_inactive_canonical_context.py -k "mention or revoked"
```

On the unfixed code (the fix stashed, with a one-line shim so the old
constructor accepts `data_sources=`):

```
FAILED test_overlay_inactive_canonical_context.py::test_columns_revoked_on_sync_never_reach_the_agent[revoked0]
FAILED ...::test_columns_revoked_on_sync_never_reach_the_agent[revoked1]
FAILED ...::test_columns_revoked_on_sync_never_reach_the_agent[revoked2]
FAILED test_mention_table_visibility.py::test_active_table_on_run_agent_is_rendered_with_real_columns
FAILED ...::test_only_visible_tables_survive_among_mixed_mentions
FAILED ...::test_unknown_table_id_is_dropped
FAILED ...::test_delegated_mention_lists_only_the_users_accessible_columns
FAILED ...::test_delegated_table_outside_users_overlay_is_dropped
```

## Loop B — live confirmation (real Postgres, real UI)

Local PostgreSQL 16 with a column-level grant:

```sql
CREATE ROLE svc LOGIN SUPERUSER;  CREATE ROLE analyst LOGIN;
CREATE DATABASE shop OWNER svc;   -- then in shop:
CREATE TABLE orders (id int primary key, region text, amount numeric, customer_ssn text);
CREATE TABLE payroll (id int primary key, employee text, salary numeric);
CREATE TABLE regions (code text primary key, name text);
GRANT USAGE ON SCHEMA public TO analyst;
GRANT SELECT (id, region, amount, customer_ssn) ON orders TO analyst;
GRANT SELECT ON regions TO analyst;
```

BOW, through the real API and UI:

1. Boot the stack as described in `.claude/skills/sandbox-feedback-loop` and
   seed an admin (`tools/agent/seed_org.py`).
2. `POST /api/data_sources`: postgresql, system creds `svc`,
   `auth_policy=user_required`, `allowed_user_auth_modes=["userpass"]`.
3. `PUT /update_tables_status`: activate `orders` and `regions`. Leave
   `payroll` inactive.
4. `POST /data_sources/{id}/my-credentials` as the admin, using `analyst`.
5. `REVOKE SELECT (customer_ssn) ON orders FROM analyst;`, then
   `POST /connections/{id}/my-schema/refresh`. The DB now has
   `user_data_source_columns(customer_ssn).is_accessible = 0`, and
   `GET /schema` returns `orders: [id, region, amount]`.
6. In the UI, open the home composer, type `@` and go to Agents → Shop →
   `public.orders`. The picker lists only `orders` and `regions`. Send the
   message.
7. Send a crafted completion whose `mentions` payload names the `payroll`
   table id.
8. Read `context_snapshots.context_view_json` (kind `initial`) for each report.
   Also render `SchemaContextBuilder` directly against the sandbox DB for the
   admin.

| Observation | Before | After |
|---|---|---|
| Rendered schema, `public.orders` | `cols="4"`, includes `customer_ssn` | `cols="3"`: id, region, amount |
| `schemas_usage` columns_count for orders | 4 | 3 |
| UI @orders mention, `columns_preview` | 4 garbled dict strings incl. `customer_ssn`; `data_source_name: null` | `id:integer, region:text, amount:numeric`; `data_source_name: Shop` |
| Crafted @payroll mention | rendered: `employee`, `salary` | dropped (`tables: []`) |

The sandbox Anthropic key had no credit, so the LLM call itself returned a
billing error. That doesn't affect this loop: the context snapshot is built and
saved before the LLM call, and the snapshot is exactly what the model would
see.

## The fix

- Schema builder: the overlay column query adds
  `UserDataSourceColumn.is_accessible.is_(True)`. `PromptTable.id` is now filled
  from the canonical table id, so callers can resolve a table by id. A new
  `split_file_scopes` flag (default `True`, which keeps the old behavior) lets
  a caller keep file-connection rows as tables.
- Mention builder: a table mention now resolves through `SchemaContextBuilder`
  for the run's own agents (`ContextHub` passes `data_sources`), with
  `active_only=True` and the caller as user. A mention whose id isn't in that
  result is dropped. Columns come from the resolved table. The builder caches
  one schema build per agent for the run.

Loop A after the fix: `8 passed`. The targeted context, mention, overlay and
schema unit and e2e files: `153 passed`.

## What this proves / regression notes

- The agent's prompt now matches what the per-user schema API already shows:
  revoked columns are gone in both the schema and the mention sections.
- A table mention can no longer widen the prompt beyond the run's schema
  context, whatever id the client sends.
- The picker (`/api/data_sources/{id}/schema`) was already correct. The leak
  was only in what the agent was told.
- Not covered: `DATA_SOURCE` mentions still resolve by id without an access
  check, but they only render the agent's name.
