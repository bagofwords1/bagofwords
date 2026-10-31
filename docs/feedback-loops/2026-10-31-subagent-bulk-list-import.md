# Feedback Loop — delegated workbook import stops after trial rows

The shared conversation at https://eu.bagofwords.com/c/89265ebe2eed42c5b479665ea7cf4ffa
asked to load all nine workbook sheets into the agent's Lists. Inspection through
`ssh bow-eu` found a capability mismatch: validation children were instructed to
use `create_data` and `lists.submit`, but the mode-filtered catalog excluded
`create_data`. This document records the diagnosis and a local fix; production
data and configuration were not modified.

## Root cause (validated)

`backend/app/ai/tools/implementations/create_data.py:1556` previously declared
`allowed_modes=["chat", "training"]`. Children run in `subagent` mode.
`backend/app/ai/registry.py:211` rejects tools whose supported modes do not
include the current mode. AgentV2 builds this catalog at
`backend/app/ai/agent_v2.py:987`, before applying the child profile at
`backend/app/ai/agent_v2.py:8152`. That later intersection cannot restore an
already-removed tool.

The validate profile grants `create_data` and `submit_list` independently of
the optional investigate-profile query setting
(`backend/app/ai/sub_agent_profiles.py:84`). Production had that optional setting
off, but this was not why the validate children lost `create_data`.

Production version: `0.0.602`. The deployed metadata matched the local source.
The organization's sandbox list writes were enabled, with a 5,000-row code-run
limit. The bulk helper is supplied through `create_data`; `write_csv` does not
provide this write path. The planner therefore used native per-list submissions
with manually serialized records.

## Production evidence

Read-only inspection covered report `bb65e9d7-6d7c-48b0-80b4-980a36d80ceb`, its
33 completion records, 14 executions, 92 plan decisions, 162 tool records,
initial context snapshots, nine child records, server logs, organization sandbox
settings, and current List row counts. Encrypted payloads were decoded inside
the application container using its existing encryption helpers. Raw payloads
are deliberately excluded from this document.

One persisted planner decision states:

> there isn’t a create_data tool available in my namespace

The Plans child's finish record states:

> לא הייתה גישה ל-lists.submit מתוכנת ול-create_data בסביבת סוכן זה

Eight `submit_list` calls inserted 41 rows and updated four of those rows:

| List | Current rows | New rows from this conversation |
| --- | ---: | ---: |
| Plans | 505 | 0 |
| Quantities | 4 | 4 |
| Parcels | 5 | 5 |
| Addresses | 15 | 15 |
| Events | 0 | 0 |
| Decisions | 5 | 5 |
| Relations | 4 | 4 |
| Documents | 5 | 5 |
| Meetings | 3 | 3 |

These counts were confirmed directly against live `agent_list_rows`, excluding
soft-deleted rows. The final answer correctly disclosed incomplete loading.
Execution `success` describes a completed agent turn, not a complete import.

Secondary findings, separate from the catalog fix:

- Nine children were launched in two waves. Four extra delegate calls were
  refused at the configured five-child wave cap; the parent later launched
  the remaining four. This was recoverable, not the reason bulk loading failed.
- Every child consumed more than 500,000 reported tokens and finished after
  7–9 planning steps, despite a 40-step allowance. The configured token budget
  was 400,000. Restoring bulk operations avoids manually moving the rows
  through the planner; this change does not alter token accounting or budgets.
- The address batch contains one mistyped key. Recomputing SHA-256 over the
  submitted five source fields produces a different key from the submitted
  value (an extra `a` appears in the stored key). Evidence: tool execution
  `13a94574-320e-493c-8f15-ad6b033b0f14`.
- One quantity row's submitted remark differs from the source, and its stored
  hash does not match the submitted nine field values. The final parent
  verification identifies the remark difference. Evidence: submit tool
  `15b51e58-1ff4-4bc8-94fc-733cff6a1775` and verification tool
  `0b5b5caa-2bae-4c7e-bebf-fb3d77d8640e`. Separately, three otherwise matching
  quantity rows were flagged by verification because it compared a bare hash
  with a `sha256:`-prefixed key. Those flags do not prove three corrupt rows.
- Conflicting source IDs and duplicate `SOURCE` headers require explicit import
  rules. Removing the catalog bug does not resolve those source ambiguities.

## Loop A — deterministic reproduction (no external services)

Use the repository's Python 3.13 environment:

```bash
cd backend
BOW_DATABASE_URL=sqlite:///db/app.db .venv/bin/python -m pytest \
  tests/unit/test_sub_agent_data_catalog.py --disable-warnings -q
```

The test uses the real registry and both real profiles, with the optional query
setting on and off. Its invariant is that a profile-granted `create_data` tool
survives catalog mode filtering; denied tools remain unavailable. It needs no
LLM credentials, live workbook, or production database.

Before the fix:

```text
FAILED ...[False-validate]
FAILED ...[True-investigate]
FAILED ...[True-validate]
3 failed, 1 passed
```

## The fix

Add `subagent` to `create_data`'s supported modes. Existing profile filtering,
conversation tool opt-outs, sandbox settings, and List write permissions still
apply. The investigate profile with query access disabled still lacks the tool.

After the fix, the same reproduction plus the existing profile/prompt/browser
tests:

```bash
cd backend
BOW_DATABASE_URL=sqlite:///db/app.db .venv/bin/python -m pytest \
  tests/unit/test_sub_agent_data_catalog.py \
  tests/unit/test_sub_agent_prompts.py \
  tests/unit/test_sub_agent_browser.py --disable-warnings -q
```

```text
22 passed
```

Existing List permission and child-submission integration tests also passed:

```bash
cd backend
BOW_DATABASE_URL=sqlite:///db/app.db .venv/bin/python -m pytest \
  tests/e2e/rbac/test_sub_agent_list_submit.py \
  tests/e2e/rbac/test_sandbox_list_writes.py \
  -m e2e --db=sqlite --disable-warnings -q
```

```text
8 passed
```

## What this proves / regression notes

The deterministic loop proves tool eligibility and profile gating. It does not
prove that a live model will finish this workbook import, resolve duplicate
source facts correctly, or repair the two already-written inconsistent rows.
The fix is local and has not been deployed or replayed against customer Lists.
