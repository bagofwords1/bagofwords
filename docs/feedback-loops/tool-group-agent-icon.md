# Agent icons on transcript tool groups

Open a report with a completed `create_data` call, expand the run, and compare the icon beside “Created…” with the source agent in the prompt composer. The report and the shared conversation both use the same header.

## Reproduce and verify

Before the change, the header rendered the tool's chart glyph even when the execution identified an Elasticsearch agent. The localhost browser fixture used the title “South-region hourly p95 request duration” and a source with `icon_token: "type:elasticsearch"`. The updated header rendered `/data_sources_icons/elasticsearch.svg`. The captures use the same component, execution, and viewport. [Before](../../media/pr/tool-group-agent-icon/before.png) · [After](../../media/pr/tool-group-agent-icon/after.png)

Run the attribution and disclosure checks from the repository root with Node 22.6 or later and the frontend dependencies installed.

```sh
node --experimental-strip-types frontend/tests/unit/toolAgentIcon.mjs
node --experimental-strip-types frontend/tests/unit/useRunFold.mjs
```

The first check covers saved data sources, live table arguments, MCP/API connections, ambiguous ownership, and a change in available agents. The second exercises the existing expand/collapse behavior.

## Display rules

`ToolAgentScope.vue` supplies the owning agent to the outer `ToolCallHeader.vue`. It uses the composer's `DataSourceIcon` renderer. A group with ambiguous or missing attribution uses the composer's generic agent icon. Expanding a multi-call group shows each member's operation glyph. Failed headers keep their existing warning indicator.
