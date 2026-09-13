# Role-Scoped Code Visibility (`view_code` / `run_custom_code`)

Status: Implemented
Scope: Core (role editor UI for custom roles remains EE-gated, as today)
Related: `app/core/permissions_registry.py`, `app/core/permission_resolver.py`,
`app/core/code_visibility.py`, `app/serializers/completion_v2.py`

---

## 1. Problem

An admin could not define a role that withholds the agent's generated code.
Every org member saw the SQL/Python behind every answer, because the generated
code is a field on the object (`Step.code`), not something behind a route.

The only related control was `enable_code_editing`, an org-wide
`FeatureConfig`. It was referenced in exactly two Vue components and **nowhere
in the backend** — `POST /queries/{id}/run` accepted arbitrary caller-supplied
code behind only `view_reports`. Turning the setting off hid buttons; it did
not stop anything.

## 2. Decisions

| # | Decision | Value |
|---|----------|-------|
| D1 | Granularity | Two org-level permissions, not one: `view_code` (see generated code) and `run_custom_code` (edit + execute arbitrary code). Seeing and executing are different risks. |
| D2 | Implication | `run_custom_code` implies `view_code`. Editing code you cannot read is not a coherent state. |
| D3 | Default | **Both ON** for `member` and (via the `full_admin_access` wildcard) for `admin`. Backward compatible: nothing changes until an admin withholds. |
| D4 | New custom roles | Both pre-checked in the role editor, so the editor's default matches the product's default. Restricting is the deliberate act. |
| D5 | `enable_code_editing` | **Removed.** It was UI-only and is fully superseded by `run_custom_code`, which is enforced server-side. |
| D6 | Enforcement | Server-side redaction at every surface that carries code. UI gating is cosmetic and never the boundary. |

## 3. Why redaction, not route gating

`StepBase.code` is a required field on the step schema, and steps ride along in
completions, queries, widgets, reports and the public/published path. Hiding
the editor modal leaves the code sitting in JSON the browser already received.

Three distinct layers carry generated code, and all three needed redaction —
the third is the one a UI-only approach would have missed entirely:

| Layer | Where | Hook |
|---|---|---|
| REST step payloads | `query_service`, `widget_service`, `report_service`, `completion_v2` | a `code` field serializer on `StepSchema` / `PublicStepSchema` |
| Tool-execution results | `result_json.code`, `.errors[][0]`, `.executed_queries`, `.error.failed_sql` | `_tool_execution_schema_data` (already ran `redact_deep_display`) |
| Tool-call arguments | `arguments_json.code` (the UI reads it as a fallback) | same serializer |
| Per-query timing telemetry | `result_json.query_timings[].sql`, `sub_timings_json.queries[].sql` | same serializer |
| **Live agent stream** | `create_data` / `inspect_data` progress events (`stage=generated_code`) | the SSE emitter, per run |

The timing surface was **not** found by reading code. It was found by diffing an
admin's live API payload against a restricted viewer's: `code` was redacted
while the executed statement sat one key over, in a record whose purpose is
measurement. It appears in two containers of identical shape, and the first fix
covered only one of them. See `docs/feedback-loops/role-scoped-code-visibility.md`.

Redaction there is structured rather than wholesale: `query_ms`, `rows` and
`result_bytes` survive. How long a query took is not code, and a viewer who may
not read it is still entitled to know that it ran.

### Scoping by tool, not by key name

Redaction is keyed to an allowlist of *tools*, not to fields named `code`. Two
things break otherwise, both found while implementing:

- Tool **error** payloads use `{"code": "FORBIDDEN"}` where `code` is an error
  identifier. Nulling it corrupts every error the UI renders.
- The **artifact** tools also key their payload on `code`, but there the value
  IS the component source the iframe renders. Redacting it would break
  artifacts for exactly the users this feature is meant to keep working.

An unrecognised tool is redacted conservatively, so a newly added
code-producing tool leaks nothing before anyone remembers to update the list.

`code` became `Optional[str]`. Redaction sets it to `None`, never `""` — an
empty string is indistinguishable from a legitimately empty step and would
have produced a silent, untestable failure mode.

## 4. Composition

With `enable_code_editing` gone, the role permission is the **single source of
truth** for code. There is no org-setting ceiling left to AND against. Other
feature flags keep their current org-wide semantics; this document does not
change them.

## 5. Rollout

A new non-baseline permission defaults to *not granted*, which on upgrade would
silently strip code visibility from every existing role. The migration
therefore appends both strings to **every existing role row** — system and
custom alike.

Seeding only the `member` role is insufficient: a user whose sole assignment is
a custom role never picks up member's permissions (the resolver adds only
`BASELINE_PERMISSIONS`, and these two are deliberately not baseline — a
baseline permission cannot be withheld, which is the entire feature).

Consequences accepted deliberately:

- **Orgs that had `enable_code_editing` off regain the affordance.** Since the
  flag was never enforced server-side, nothing that was actually protected
  becomes unprotected — but it is a visible change and is called out in the
  changelog. The remedy is now real: unchecking `run_custom_code` is enforced.
- **It cannot be migrated per-org.** System roles are global singletons
  (`organization_id IS NULL`, shared by every organization), so one org cannot
  be given a different `member` default without cloning a per-org role.
- **Service accounts inherit both.** They get no baseline and default to the
  `member` role, so the backfill grants existing API keys both permissions.
  This matches today's behavior exactly (nothing was enforced), and is a
  decision rather than an accident.

`DEFAULT_MEMBER_PERMISSIONS` now deliberately diverges from
`BASELINE_PERMISSIONS`; the unit test asserting their equality was updated to
assert the intended relationship (baseline is a strict subset) instead.

## 6. Removing `enable_code_editing`

No migration required. `_sync_new_features` already drops stored feature
entries whose schema field no longer exists, so every org's config self-prunes
on the next settings read, and the generically-rendered settings page stops
showing the toggle on its own.

## 7. Hardening done alongside

Role permission strings were never validated — `create_role` wrote the caller's
list straight to the JSON column, so a typo produced a silently dead role. Role
create/update now reject unknown permission strings. This is what makes the
registry a real contract rather than a convention.

### The deny-by-default contextvar

The decision rides on a request-scoped `ContextVar`, mirroring the PII display
redactor in `app/ai/llm/pii/display.py`. The sync serializers cannot take an
extra argument, and threading one through every call site is precisely how a
surface gets forgotten.

The default is **deny**. If a surface never sets the flag, an *authorized* user
sees a missing code block — loud, and caught by the e2e suite — instead of an
*unauthorized* user silently seeing code. The published-report path is the one
place that sets it explicitly, because anonymous readers hold no role and would
otherwise inherit the deny default, silently changing what published reports
show.

## 8. Verification

Unit + e2e coverage asserts redaction **per surface**, not per permission — the
failure mode is a forgotten serializer, so coverage that iterates permissions
would prove nothing. Verified end to end against a live sandbox (real LLM, real
browser); see `docs/feedback-loops/role-scoped-code-visibility.md`.
