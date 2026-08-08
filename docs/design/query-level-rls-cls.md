# Row- and column-level security on report queries

A report creator writes a query once. Everyone they share the report — or its
artifact — with runs that same saved code. This feature is what makes one saved
query return a different slice to each of them: rows filtered by who is asking
(RLS), and sensitive columns masked from anyone without a grant (CLS).

Enterprise, gated on the existing `rls` license feature.

## Not the same thing as custom-query RLS

`docs/design/custom-query-rls.md` describes row policies on **accelerated
relations** — admin-authored SQL materialized once to an encrypted DuckDB
artifact, filtered at read time in the per-session catalog.

This is a different feature with the same vocabulary:

|                | custom-query RLS            | query-level RLS/CLS (this)      |
|----------------|-----------------------------|---------------------------------|
| Author         | connection admin            | report creator                  |
| Attaches to    | `connection_tables` (bow)   | `queries`                       |
| Data           | materialized artifact       | live query result               |
| Enforced by    | the DuckDB catalog          | every `execute_query` return    |
| Applies when   | any agent reads the relation| someone else views the report   |

They share `app/data_sources/fast/rls.py` — the policy shapes, the compiler,
the identity sources and `principal_matches` — because two implementations of
"does this grant name this identity" would be two places for a matching bug to
hide.

## Enforcement is per `execute_query` return value

Not per step, and not per report. One step's generated code routinely calls
`execute_query` several times — once per table, per join leg, per data source —
and a report holds many queries, each with its own policy. There is no single
"the query" to intercept, so the filter runs on every individual call's result
frame, evaluated on its own terms.

It rides on `QueryCapturingClientWrapper`
(`app/ai/code_execution/code_execution.py`), which already wraps every client on
every execution path and already routes the `.query()` alias back through its
own `execute_query`. That last detail matters more than it looks: model-written
code reaches for `.query(...)` constantly, and a wrapper that delegated the
alias straight to the raw client would leave the policy a no-op for most real
code. There is a test pinning exactly that.

### Why the boundary, and not later

Filtering before generated code touches the frame is load-bearing, and more so
for columns than for rows: code doing `df.rename(columns={'salary': 'sal'})`
would walk a restricted column straight past any mask applied downstream.
Masking at the boundary closes that by construction rather than by hoping the
rename never happens.

### Why the wrapper is built per execution, not per client

`ReportService.viewer_rerun_report_steps` builds `ds_clients` **once** and
reuses the same dict for every step in the report. A policy baked into the
clients at construction time would apply one query's rules to every query in the
report. `wrap_clients_for_capture` takes the policy per call instead — these are
thin proxies over the same client objects, so wrapping per execution costs
nothing.

## The viewer is not the credential

`Report.shared_run_identity` decides whose **database credentials** run the
query. The policy compiles against **whoever is looking**. Those are different
questions, and keeping them separate is what makes the common enterprise setup
work: a shared service-account connection, `shared_run_identity='creator'`, and
per-user row rules on top. `run_step_to_user_result` therefore passes
`run_user` (the viewer) to the policy and `credential_user` to the executor.

`ANONYMOUS` stays default-deny, so a background path that forgets to thread an
identity yields no rows rather than every row.

## Owner vs. viewer

Enforcement engages only on the viewer path (`run_step_to_user_result` →
`step_user_results`), never on the creator's own `rerun_step` → shared
`Step.data`. A creator must see real rows and real columns to author the query
at all — the same reason `preview_as_user` exists on the accelerated side.

Because the shared snapshot is then the creator's *unfiltered* result,
`viewer_data_policy.has_query_level_policies` withholds it from every non-owner:
they run as themselves and read their own `step_user_results` row. Unlike a
`user_required` source, `'creator'` mode is **not** an opt-in to share the
owner's view here — it only picks the login, after which the viewer's own policy
still applies to what comes back.

Scoped by report rather than by step: a viewer seeing filtered rows in one
widget beside the creator's unfiltered snapshot in the next would be worse than
either.

## Artifacts need no artifact code

Artifacts don't execute queries. The rendered page calls
`POST /api/r/{id}/run?artifact_id=…` → `viewer_rerun_report_steps` →
`run_step_to_user_result`, and reads back through `resolve_step_data`. Wiring
the policy into that one path is what makes "share an artifact with a user →
their view is filtered" work; `artifact_service.py` is untouched.

## Semantics, both fail-safe

| Situation | Result |
|---|---|
| Row policy denies | Empty frame, **same columns** |
| Policy column absent from the result | Empty frame (cannot filter on what is gone) |
| Restricted column, no grant | Column kept, values set to `None` |
| Non-tabular result under a policy | Raises — we could not filter it, so we do not serve it |
| Unknown mode / operator / source | Deny |
| Identity has no value for the attribute | Deny (unless `rls_default_deny` is off) |

Masked cells are `None`, never a `"***"` sentinel: a sentinel is a value — it
changes the column's dtype, breaks numeric aggregation, and renders as literal
text on a chart axis.

Values compare as strings, because the identity side is always strings (an IdP
claim, a group name); a numeric column would never match otherwise. `NaN`
stringifies outside the allowed set, so null cells fall out — the closed
direction. Column lookup falls back to case-insensitive, because Oracle and
Snowflake upper-case unquoted identifiers and failing to match would silently
un-restrict the column.

## Where it lives

| Concern | File |
|---|---|
| Policy columns | `app/models/query.py`, `alembic/versions/qrls0001_*` |
| Column compiler, mask, frame application | `app/services/query_access_policy.py` |
| Row compiler (shared) | `app/data_sources/fast/rls.py` |
| Identity resolution (shared) | `app/services/rls_identity_service.py` |
| Enforcement point | `app/ai/code_execution/code_execution.py` |
| Viewer wiring | `app/services/step_service.py` |
| Snapshot withholding | `app/services/viewer_data_policy.py` |
| Authoring, preview, options | `app/services/query_service.py`, `app/routes/query.py` |
| Editor | `frontend/components/tools/QueryAccessPolicyEditor.vue` |

The editor is the **Access** tab of the query editor
(`QueryCodeEditorModal.vue`), with a lock badge on the tab whenever a policy is
live — a creator returning to a dashboard needs to know that without opening the
tab to find out. "Check it" previews a saved *or unsaved* policy as a specific
member, re-executing through the same compiled policy a real viewer's run uses;
a simulation that applied the filter differently could disagree with reality,
which is the one thing a preview must not do. It also prints the identity the
rules resolved against, so an empty preview can be told apart from a member
whose directory attributes never synced.

## Licensing

Authoring, editing and previewing a policy require the `rls` feature. Enforcing
a saved policy does **not** — skipping the filter on a lapsed license would turn
a billing event into a data leak. Disabling a policy stays open so a lapsed org
is not trapped behind a filter it can no longer administer.

## Verified end to end

A sandbox run (real Anthropic model, real SQLite source, four members) against
one shared dashboard in `shared_run_identity='creator'` mode — so every query
below executed under the **owner's** credentials:

| Member | Directory identity | Rows | `commission` |
|---|---|---|---|
| Elena | `officeLocation=EMEA` (Entra-style profile attr) | 3, EMEA only | masked |
| Aki | `officeLocation=APAC` | 2, APAC only | masked |
| Marco | in group `Global Revenue` (wildcard grant) | all 8 | visible |
| Nadia | never signed in through the IdP | 0 | — |
| Owner | creator | all 8, unfiltered | visible |

## Phase 2

- **SQL pushdown.** Filtering happens after the fetch, so a large table still
  crosses the wire in full. Pushdown would rewrite each `sql` at the same
  per-call site (`SELECT * FROM (<original>) t WHERE t.col IN (…)`, bound
  params) — same granularity, just earlier. Per-dialect work; deferred exactly
  as `fast/rls.py` defers its own SQL mode.
- **SQL-mode row policies.** `rls_mode='sql'` is accepted by the model and
  denies at compile time, as it does on the accelerated side.
- **Aggregation side-channel.** Filtering raw rows does not stop a widget's own
  `groupby().sum()` from hinting at excluded rows. Inherent, and already
  accepted by the accelerated-relation RLS.
- **Multi-table scoping.** The proxy sees a frame, not a table identity, so it
  can only ask "does this result contain the policy's column". A creator naming
  the target table explicitly would tighten that.
