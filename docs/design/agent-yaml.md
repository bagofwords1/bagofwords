# Declarative Agent YAML

Single declarative apply endpoint per resource. By-name refs, structured
errors with did-you-mean, idempotent on `(organization_id, name)`.
Externally surfaced under `/agents` and `/evals` — internally the
underlying models are `DataSource` and `TestSuite` (the rename is
product-side only).

## Endpoints

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/api/agents/apply?dry_run=` | Create or update an agent from YAML. |
| `GET` | `/api/agents/{name}.yaml` | Export an agent's full manifest. Round-trip safe. |
| `GET` | `/api/agents` | List `{name, description, id}` for all visible agents. |
| `POST` | `/api/evals/apply?dry_run=&strategy=` | Create or update an eval (test suite) from YAML. |
| `GET` | `/api/evals/{name}.yaml` | Export an eval's YAML. |
| `GET` | `/api/evals` | List `{name, description, id}` for all evals. |
| `GET` | `/api/data_sources/{id}/tools` | Per-agent effective tools (overlay merged with defaults). |
| `PUT` | `/api/data_sources/{id}/tools/{tool_id}` | Upsert per-agent overlay. |
| `DELETE` | `/api/data_sources/{id}/tools/{tool_id}` | Remove overlay (revert to connection default). |

Apply takes a raw YAML body (`Content-Type: application/yaml`). Errors
come back as a structured `ApplyResult` envelope, not HTTP 4xx.

## Apply semantics

- **Identity**: `(organization_id, manifest.name)`. Rename = new resource.
- **Omitted optional fields**: revert to defaults. The YAML expresses
  *desired state*, not a patch. Always do `get → edit → apply` of the
  full document.
- **Idempotency**: re-applying identical YAML returns
  `status: unchanged`, no DB writes.
- **Response statuses**: `created | updated | unchanged | dry_run | error`.
- **Errors are collected, not short-circuited**: ref resolution returns
  every missing connection / group / user / instruction / tool in one
  response so callers (and MCP-driven LLMs) can fix the whole YAML in
  one round.

## Manifest

```yaml
name: revenue-analyst            # required, org-unique
description: Helps GTM analyze pipeline and ARR
context: Sales analytics agent   # surfaced to the LLM
is_public: false
use_llm_sync: false

connections:                     # by name; org-unique via uq_connections_org_name
  - postgres-prod
  - hubspot-mcp
  - slack-mcp

tables:                          # one-shot filter applied to DataSourceTable.is_active
  include: ["postgres-prod.public.*"]
  exclude: ["*.staging_*"]

tools:                           # per-agent overlay, only for mcp / custom_api conns
  hubspot-mcp:
    allow:   [search_contacts, get_deal]
    confirm: [post_message]
    deny:    [delete_contact]
  slack-mcp:
    allow: ["*"]

conversation_starters:
  - What's our Q3 pipeline coverage?
  - Top 10 churned accounts last 90 days

instructions:
  - id: inst_abc                 # attach an existing instruction
  - inline:                      # create a yaml-owned instruction
      title: ARR definition
      text: "ARR = sum(mrr) * 12"
      category: general          # InstructionCategory enum
      load_mode: always          # InstructionLoadMode enum

members:                         # polymorphic — user OR group, with optional perms
  - user: yochze@gmail.com
  - user: alice@example.com
    permissions: [view, view_schema]
  - group: data-team
  - group: gtm-leads
    permissions: [manage]
```

### Error envelope

```json
{
  "status": "error",
  "id": null,
  "name": "ref-test",
  "diff": null,
  "warnings": [],
  "errors": [
    {
      "loc": ["connections", 0],
      "code": "connection_not_found",
      "message": "Connection 'sqlite-chinok' not found in this organization.",
      "value": "sqlite-chinok",
      "suggestion": "SQLite Chinook"
    },
    {
      "loc": ["instructions", 0, "inline", "category"],
      "code": "enum_invalid",
      "message": "Input should be 'code_gen', 'data_modeling', 'general', 'dashboard' or 'visualization'"
    }
  ]
}
```

### Error codes

| Code | Meaning |
|---|---|
| `yaml_parse_error` | Malformed YAML. `loc` carries `line X / col Y`. |
| `schema_invalid` | Pydantic validation (missing field, wrong type, etc.). |
| `enum_invalid` | Value outside allowed enum (e.g. instruction `category`). |
| `connection_not_found` | Connection name not in org. `suggestion` shows closest match. |
| `connection_type_mismatch` | `tools:` on a non-MCP/custom_api connection. |
| `tool_not_found` | Tool name not on the listed connection. |
| `group_not_found` | Group name not in org. |
| `user_not_found` | Email not in org's members. |
| `instruction_not_found` | Instruction id not in org. |
| `duplicate_entry` | Duplicate item in a list (e.g. same connection twice). |
| `license_required` | Connection type gated behind enterprise license. |
| `permission_denied` | Caller lacks `create_data_source` (create) or `manage` (update). |

### Warning codes (non-blocking)

| Code | Meaning |
|---|---|
| `connection_indexing_pending` | Connection's `ConnectionTable` rows aren't populated yet — re-apply once indexing completes for tables/tools to take effect. |
| `tables_filter_empty` | `tables.include` is empty; no tables will be active. |
| `glob_overlap` | A table is matched by both `include` and `exclude`. |

## Permissions

| Op | Required |
|---|---|
| Create agent | org `create_data_source` or `full_admin_access` |
| Update agent | resource `manage` on the data source, or `full_admin_access` |
| Update tool overlay | resource `manage` on the data source |
| Read tools | resource `view` on the data source |
| Apply / read / export eval | org `manage_evals` or `full_admin_access` |
| Reference a connection in YAML | implicitly resolved via name; no extra check beyond org membership today |

## Tables filter

Stored declaratively in YAML only. On apply the service iterates each
linked connection's `ConnectionTable` rows and flips
`DataSourceTable.is_active`. If the connection is still indexing on
apply, a `connection_indexing_pending` warning is returned and the
filter applies on the next apply (or when re-export is triggered).

There is no `table_rules` JSON column. The YAML *is* the source of
truth — re-export from DB reconstructs the include list from current
`is_active` flags, so `get → apply` round-trips.

## Tools overlay

Per-agent overrides live in `data_source_connection_tool`:
- `is_enabled` (bool) and `policy` (`allow | confirm | deny`).
- One row per `(data_source_id, connection_tool_id)`.
- The runtime tool loader and the UI's `/data_sources/{id}/tools`
  endpoint read the overlay first; absent rows fall back to the
  `ConnectionTool` defaults.

## Schema changes

One migration: `e9f0a1b2c3d4_add_data_source_connection_tool_overlay.py`
adds the overlay table and merges two pre-existing heads
(`uq_connections_org_name` from PR 275 and `add_events_json`).

## Limitations (v1)

- No inline `Connection` definitions in YAML (creds + env-var refs).
  Reference existing connections by name.
- No group resolution by `external_id` (AD/Okta/SCIM).
- Globs are limited to `*` wildcards (case-insensitive `fnmatch`).
- Rename = new resource. No immutable `slug`.

## Validation: sandbox harness

`backend/scripts/random_agent_eval.py` generates N random agents,
applies them via the live API, generates matching evals, and writes a
CSV report. Use `--inject-errors` to drive the negative-path coverage
(typos → did-you-mean, schema-invalid → structured errors, etc.).

See `docs/design/sandbox-feedback-loop.md` for environment setup.
