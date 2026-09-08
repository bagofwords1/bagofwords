# Feedback loop — BOW queries time out and keep retrying

BOW history queries must produce ordinary saved tables in training mode, without waiting on the agent's own database transaction. Every read and saved-artifact access must enforce the caller's current agent management scope.

## Observed failures

The local report `1bb009be-36f1-4b59-a515-f373f4a15fd3` repeatedly ran `create_data` for the same low-confidence request. Tool records showed roughly 30 seconds per database attempt and 74–101 seconds per failed tool invocation. Both `BOW query timed out` and the generic 30-second query-timeout message were recorded. A later request to create a report also failed with SQLite `database is locked`.

The BOW client used an independent session to save source provenance while the agent session could already hold a write transaction. SQLite permits one writer. A generic executor timeout could abandon the worker while that operation still waited. The coder and planner then repeated the infrastructure failure. The old task-owned worker retained SQLite's WAL write lock even after graceful server shutdown; the OS lock query identified that worker before it was terminated.

A separate discovery failure appeared in report `3701ad8b-0e3f-4912-b29d-2cb5f561d75c`: the initial training request could not resolve `bow.runs`, but a later training turn could. Execution used the current request mode, while schema discovery used the persisted report mode. Later queries in that report took 50–206 ms. Its zero low-confidence result covered the default 30 days, not all history.

## Deterministic checks

From `backend`, with Python 3.12 dependencies installed:

```sh
MPLCONFIGDIR=/tmp/bow-mpl TESTING=true .venv/bin/python -m pytest \
  tests/e2e/rbac/test_bow_source.py \
  tests/unit/test_bow_source_contract.py -q
```

The API fixtures create an admin, an agent manager, an unrelated member, two agents, and runs on reports attached to each agent, both agents, and neither agent. They do not use local development data or LLM credentials.

The tests cover:

- Scope filtering before row selection and aggregation, including mixed-agent reports.
- Discovery from the current request mode, and refusal for ordinary chat or a member without manage access.
- BOW execution while the agent's own session already has an uncommitted write.
- Ordinary query persistence, reload, viewer refresh, saved-entity refresh, public-sharing refusal, and access revocation.
- Agent-column projection preserving unassigned runs.
- Infrastructure errors stopping code-generation retries, including generated code that catches an error and returns a fabricated result.

A new saved-entity refresh assertion first failed for the manager with `403: You do not have access to data source 'alpha-…'`. The BOW-only entity had incorrectly fallen back to constructing clients for every business agent. After restricting that fallback, the same lifecycle test passed for both admin and manager: **2 passed**. Discovery/projection checks passed: **5 passed**. The active-write regression passed: **1 passed**. The contract, generic timeout and heartbeat suite passed: **35 passed**. These are overlapping runs, not an additive total. The final combined source, contract, identity-taint, viewer-policy, and query-parameter regression run completed with **69 passed**; the subsequent numeric-contract checks completed with **2 passed**. Six changed Vue components compiled successfully, and the BOW label was checked in all ten locale catalogs.

## Implementation

- `backend/app/data_sources/clients/bow_client.py`: an authenticated read session, one asynchronous query deadline, and provenance persistence through the coordinated agent writer before returning rows.
- `backend/app/services/bow_source_access.py`: current management-scope checks and persistent access lineage for reports and reusable entities. Public sharing is refused.
- `backend/app/ai/context/context_hub.py` and `builders/schema_context_builder.py`: the current request mode controls schema discovery.
- `backend/app/ai/code_execution/code_execution.py`, `tools/implementations/create_data.py`, and `agent_v2.py`: terminal infrastructure failures stop coder/planner retries and produce an error completion.
- `backend/app/services/entity_service.py`: BOW-only entities refresh without constructing unrelated business-agent clients.
- The existing unreleased `diagrollup01` migration contains the nullable provenance fields. No additional migration revision is introduced.

## Live confirmation

The existing `localhost:3000` application, with its configured LLM, accepted this training request:

> Create a saved table named BOW verification showing counts of runs grouped by status over the last 7 days. Use the built-in BOW source. Only aggregate counts, no prompts or personal details.

The tool displayed **Created Data · BOW · bow.runs**, the small sidebar logo, **Execution succeeded**, and a three-row table. The tool took **8.3 seconds**, with database execution recorded at **48.4 ms** and **0 retries**. The Save Query flow persisted a draft reusable entity with BOW access lineage. Counts are time-dependent and are not test expectations.

The before screenshot is `media/pr/bow-training-source/before-timeout.png`. Successful UI screenshots were captured in the task. Exporting the after/flow evidence into the PR is still required before publishing the PR update.

## Limits of verification

The automated checks above ran on SQLite. PostgreSQL execution and cancellation under an external database lock have not been verified in this pass. Live checks used an admin; manager/revocation behavior was verified through API fixtures. The entire repository suite was not run.
