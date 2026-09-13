# Feedback loop — tree shows two instructions, Agent Card shows zero

The pbi2 tree showed two loaded instruction rows while its overview and Agent Card continued showing zero. This is a cached-count consistency bug, not a reason to add global instructions to the agent-specific total.

## Root cause and read-only confirmation

`KnowledgeExplorer.vue` switched the tree's Instructions count to `listForAgent(id).length` once its lazy group loaded. The overview used `agentCount`, which always read the earlier `/instructions/counts` aggregate. The Agent Card read that aggregate directly, with a truthy initial empty object turning a missing count into zero. Connection/access changes refreshed agent data without refreshing instruction counts.

A read-only local database check confirmed pbi2 has two directly linked instructions, one primary. Calling the real instruction service against the local database in SQLite read-only/query-only mode returned count=2, list_total=2, list_length=2 for its owner. No local records were changed. The earlier pbi investigation concerned a different agent and did not explain this inconsistency.

## Reproduce and verify

With the frontend development server on port 3100:

```sh
cd frontend
node tests/instructions/agent-instruction-count.mjs
```

The regression mounts the real KnowledgeExplorer with synthetic API responses. An earlier aggregate says zero and the subsequently loaded instruction list contains two. Before the fix (`BEFORE=1` on pre-fix code), the tree loaded two but the card displayed zero. After: the card and overview both display two even if the old aggregate is returned again. The test also checks a cold card refresh and failed count requests, which must not display zero.

Evidence: `media/pr/agent-instruction-count/before.png`, `after.png`, and `flow.gif`. Temporary preview pages are removed on completion.

## Fix and scope

All agent summary count surfaces now use the complete unfiltered loaded group when available, with the aggregate as the fallback for unopened groups. Opening the Agent Card refreshes the aggregate, as do connection/access updates. Loading/unavailable counts are distinct from a confirmed zero; no globals are added. Requests carry a generation guard so an older response cannot replace newer state. Tree filters continue to apply only to the filtered tree list.

No backend behavior, instruction content, memberships or access rules changed. Existing aggregate and list APIs remain the sources of truth; verification does not claim the synthetic boundary proves the cause of every historical stale response.
