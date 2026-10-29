# Shared-dashboard chat answered out of the creator's rows

Reported: on a dashboard shared with "Allow chat", the bubble ignores the
per-viewer filter and answers from the creator's stored results, so customer
A's chat can use customer B's data.

Confirmed, fixed, and verified against a live sandbox on 2026-10-26.

## Root cause

`ArtifactChatService.build_platform_context` collected the dashboard's
visualization data with no reader in scope:

```python
viz_payload = await collect_visualizations(db, artifact)   # viewer=UNSCOPED
```

`UNSCOPED` means "bake the stored snapshot" — `Step.data`, materialized under
the report owner's identity and credentials. Those rows went straight into the
planner prompt (up to 100 per visualization). Every other surface that serves
step rows — the public JSON view, CSV/XLSX export, PDF/thumbnail/PPTX renders,
the websocket broadcast, Slack notifications — resolves them through
`viewer_data_policy.resolve_step_data` first. Artifact chat was the one that
did not, so the surface that *shows* the data and the surface that *talks
about* it disagreed for exactly the readers the policy protects.

`read_query` — in the artifact-chat tool allowlist — had the same gap: it read
`step.data` directly, and resolves a query id against the whole organization
rather than the current report.

## Fix

- `build_platform_context` takes the viewer and passes it to
  `collect_visualizations`; the route hands it the authenticated sender.
- `collect_visualizations` now reports `withheld` per visualization for
  reader-scoped callers, so "you may not see this" is distinguishable from "the
  query returned nothing".
- The planner prompt renders a withheld chart as NOT AVAILABLE TO THIS VIEWER
  with an instruction never to estimate its values.
- `read_query` resolves every saved step through `resolve_step_data` for the
  user running the agent: owner snapshot for the owner, the reader's own run
  when they have one, and nothing (no rows, no code, no creator identity in
  `applied_params`) when the policy withholds it.

## Sandbox run

Backend booted per the skill (sqlite, pinned `BOW_ENCRYPTION_KEY`). Through the
real API: owner signs up, adds an Anthropic provider, gets a dashboard whose
query is identity-scoped with a creator snapshot of
`[{site: ACME-HQ, revenue: 918000}, {site: ACME-West, revenue: 442500}]`,
shares it internally with chat enabled in data-only scope, invites a second
member; that member opens `/r/{id}` chat and asks for every site and its
number. The planner prompt the backend built for them was read out of the
backend log.

Before the fix, the viewer's own prompt carried the creator's rows:

```
<visualization>
  title: "Revenue by site"
  columns: [{"field": "site"}, {"field": "revenue"}]
  rows:
  [{"site": "ACME-HQ", "revenue": 918000}, {"site": "ACME-West", ...
```

After the fix, same scenario, same viewer:

```
<visualization>
  title: "Revenue by site"
  rows: NOT AVAILABLE TO THIS VIEWER — this chart runs under each viewer's own
  identity and they have no result of their own yet. ...
```

And the dashboard that viewer actually sees agrees with the bubble:

| Reader | `GET /api/r/{id}/queries/{qid}/step` | chat prompt |
| --- | --- | --- |
| owner | both rows, `snapshot_withheld: false` | both rows |
| viewer | no rows, `snapshot_withheld: true` | no rows |

The model's own reply was not part of the proof: the sandbox key's Anthropic
credit was exhausted, so the run ended at the planner prompt. The leak lives in
the prompt, which is what was observed.

## Tests

`backend/tests/e2e/test_artifact_chat_viewer_data.py` (10 tests, sqlite):

- chat withholds the creator snapshot from another viewer, for `identity` and
  `input_identity_default` params;
- chat rows equal what the dashboard's own read path serves the *same* reader,
  for owner and viewer, on identity-scoped and plain dashboards;
- a viewer with a run of their own gets their slice, not the creator's;
- a plain shared dashboard still reaches its viewers unchanged;
- the route hands the sender to the context builder (agent loop stubbed, so no
  LLM is contacted);
- `read_query` withholds a saved step from another viewer and still serves the
  owner and plain dashboards.

With the fix stashed, all of them fail; the `read_query` withholding test fails
by returning the creator's rows.

## Not covered

Agents-scope chat (the viewer queries attached agents) was examined and left
alone: those runs already execute as the viewer — identity params are
re-derived from the running user, and accelerated relations filter in the
per-session catalog against the viewer's identity. A plain warehouse source on
a system credential with no RLS policy and no identity params is filtered only
by whatever the dashboard author wrote into the query; a freshly written query
has none of it. That is a configuration gap, not this bug.
