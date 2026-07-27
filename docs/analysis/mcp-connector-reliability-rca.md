# MCP connector reliability — root cause analysis

**Status:** investigation only. No code changed.
**Symptom:** agent calls to customer-configured MCP connectors fail often — wrong
argument types, wrong/missing values, arguments that don't match the tool's
schema. Reported as materially worse than other MCP clients (OpenCode, OpenClaw,
Hermes) on the same models, including Sonnet. Concrete example: monday.com, where
the agent "forgets to put the right type".

---

## TL;DR

BOW exposes MCP tools through a **generic gateway tool** (`execute_mcp`) whose
`arguments` field is an unconstrained `Dict[str, Any]`. Every other serious MCP
client registers **each MCP tool as a first-class provider tool carrying its own
JSON Schema**.

That one difference removes the provider's schema-constrained decoding from the
loop. The model is no longer *sampling into a grammar* — it is free-writing a
JSON object from memory of a schema it read, as text, several turns earlier, and
which our context compaction may already have deleted.

Everything else below compounds it. This is not a model-quality problem; it is a
**schema-plumbing problem**. Better models degrade later, not differently.

---

## The current architecture

```
<mcp_tools> context block      names + descriptions only, NO schemas
        │                       tables_schema_section.py:338-377
        ▼
   search_mcps                 returns full input_schemas — but only inside a
        │                       tool *observation* (text/JSON blob)
        ▼                       search_mcps.py:214-243
   execute_mcp                 {connection_id, tool_name, arguments: Dict[str,Any]}
        │                       schemas/execute_mcp.py:5-28
        ▼
   McpClient.acall_tool        raw passthrough, no validation
                                mcp_client.py:211-247
```

Three hops, and the type information present at hop 2 is gone by hop 3.

### How the reference implementations do it

OpenCode (`sst/opencode`, `packages/opencode/src/mcp/catalog.ts`):

```ts
export function convertTool(mcpTool: MCPToolDef, client: Client, timeout?: number): Tool {
  const inputSchema: JSONSchema7 = {
    ...(mcpTool.inputSchema as JSONSchema7),
    type: "object",
    properties: (mcpTool.inputSchema.properties ?? {}) as JSONSchema7["properties"],
    additionalProperties: false,
  }
  return dynamicTool({
    description: mcpTool.description ?? "",
    inputSchema: jsonSchema(inputSchema),   // ← the MCP schema IS the tool schema
    execute: async (args, options) => { /* client.callTool(...) */ },
  })
}

export const sanitize  = (v: string) => v.replace(/[^a-zA-Z0-9_-]/g, "_")
export const toolName  = (client: string, name: string) => sanitize(client) + "_" + sanitize(name)
```

One flat tool name per MCP tool. Real schema. Provider enforces it. Same core
pattern in Claude Code, Cline, Goose, and OpenClaw. Nobody in the ecosystem uses
a discover-then-invoke gateway as the *primary* path — gateways exist only as a
scaling escape hatch for very large catalogs, with a known accuracy cost.

---

## Root causes, ranked by impact

### RC-1 — `arguments` is an unconstrained object (PRIMARY)

`backend/app/ai/tools/schemas/execute_mcp.py:14`

```python
arguments: Dict[str, Any] = Field(
    default={},
    description="Arguments to pass to the tool, matching the tool's input schema."
)
```

Serialized to the provider this is approximately
`{"type": "object", "additionalProperties": true}`. The description literally
says "matching the tool's input schema" — but that schema is nowhere in the
request.

What is lost:

| Enforcement | Native registration | BOW today |
|---|---|---|
| Field types (`string` vs `object` vs `integer`) | provider-enforced | none |
| Required fields | provider-enforced | none |
| Enums / `const` | provider-enforced | none |
| Nested object shape | provider-enforced | none |
| Unknown-key rejection | `additionalProperties:false` | none |

**Why monday.com fails specifically.** monday's `change_item_column_values`
takes `columnValues` as a **JSON-encoded string**, not an object —
`'{"text5": "New text", "status3": {"label": "Done"}}'`. With a native schema the
provider sees `"type": "string"` and emits a string. With an open dict the model
does the natural thing and emits a nested object. The server rejects it. Same
class of bug for `boardId`/`itemId` (numeric IDs that several monday tools want
as strings, or vice versa depending on the tool).

This is *exactly* the reported symptom, and it is structural.

---

### RC-2 — The schema is delivered as prose, far from the point of use

The `<mcp_tools>` block deliberately ships names and descriptions only:

`backend/app/ai/context/sections/tables_schema_section.py:363-370`

```python
conn_parts.append(
    "<note>Only tool names and descriptions are shown above, not their argument schemas. "
    "Call search_mcps to get a tool's full input schema (exact argument names and types) "
    "before calling execute_mcp — do not guess arguments.</note>"
)
```

So the model must: read a note → call a discovery tool → parse a JSON blob out
of an observation → hold it → construct a matching object N turns later. Every
one of those steps is lossy, and none of them is enforced. A native tool schema
is present in the request *at the moment of generation*, every time, for free.

---

### RC-3 — The schema is silently deleted from context after 5 tool calls

`backend/app/ai/agents/planner/prompt_builder.py:17-27`

```python
_RECENT_OBS_FULL = 5
_OBS_KEEP_KEYS = {
    "summary", "step_id", "artifact_id", "visualization_id",
    "visualization_ids", "query_id", "mode", "title",
    "analysis_complete", "success", "data_preview",
}
```

`search_mcps` puts the schemas under `observation["tools"]`
(`search_mcps.py:240`). **`tools` is not in `_OBS_KEEP_KEYS`.** Once the
observation falls outside the 5-item full window, `_compact_past_observations`
(`prompt_builder.py:543-586`) reduces it to:

```json
{"tool_name": "search_mcps", "execution_number": 3, "summary": "Found 20 tool(s) across 1 connection(s).", "success": true}
```

Every argument name and every type — gone. The model has been *told* it
discovered 20 tools and given nothing about them. From here it must guess, and
the prompt contains no signal that it is guessing.

Two aggravating details:

- `input_schema` is also not in `_OBS_KEEP_KEYS`, so the good recovery hint that
  `_failure_payload` attaches (`execute_mcp.py:741-743`) evaporates on the same
  schedule.
- Minified observations drop `tool_input` entirely. The model cannot see the
  arguments it previously sent, so it has no way to diff a failed attempt against
  a new one — a direct driver of repeated identical failures.

Five observations is a handful of turns in any real MCP flow
(`search_mcps → execute_mcp → fail → execute_mcp → create_data → …`).

---

### RC-4 — No native `tool_use` / `tool_result` transcript

`backend/app/ai/agents/planner/prompt_builder_v3.py:67-75`

```python
msg = Message(role="user", content=user_content)
return PlannerInputV3(
    system=system,
    messages=[{"role": msg.role, "content": msg.content}],   # ← always exactly one message
    ...
)
```

Every planner iteration is a **fresh single-turn request**. Conversation history
is re-serialized into that one user message as a JSON blob:

`prompt_builder_v3.py:795-798`

```python
compacted = PromptBuilder._compact_past_observations(planner_input.past_observations)
parts.append(f"  <past_observations>{json.dumps(compacted)}</past_observations>")
last_obs = json.dumps(planner_input.last_observation) if planner_input.last_observation else "None"
parts.append(f"  <last_observation>{last_obs}</last_observation>")
```

The LLM client layer already supports the native blocks —
`anthropic_client.py:191-204` translates `tool_use` and `tool_result` — but
nothing ever constructs them.

Consequences:

1. **Tool-calling fidelity.** Frontier models are post-trained on the native
   tool loop. Re-presenting history as narrated JSON inside a user turn is
   off-distribution and measurably degrades argument accuracy. This hurts *all*
   tools; it hurts MCP most because MCP is where the schema isn't enforced.
2. **No `tool_use_id` correlation.** The model cannot bind "this error" to
   "that call I made". Errors arrive as
   `summary: "Tool 'x' failed: ..."` inside a blob rather than as an
   `is_error: true` tool_result attached to the offending call.
3. **Prompt cache is defeated for the bulk of the prompt.** Cache breakpoints
   sit on the system block and the last tool
   (`anthropic_client.py:294-311`) — but all the volatile *and* all the heavy
   content (schema context, instructions, observations) lives in the single user
   message, which changes every iteration and is never cacheable. It also grows
   monotonically, pushing whatever schema survives ever deeper into a long
   prompt.

---

### RC-5 — No argument validation before dispatch

`backend/app/ai/runner/tool_runner.py:52-53`

```python
if getattr(tool, "input_model", None) is not None:
    arguments = tool.input_model(**arguments).model_dump()
```

This validates against `ExecuteMCPInput` — the *wrapper*. `arguments` is
`Dict[str, Any]`, so it always passes. Then:

`backend/app/ai/tools/implementations/execute_mcp.py:272`

```python
result = await client.acall_tool(data.tool_name, data.arguments)
```

Straight to the network. Every type error costs a full remote round-trip
(hundreds of ms to seconds on an OAuth'd server) and comes back as a
vendor-specific message that may or may not name the offending field.

We already store `ConnectionTool.input_schema` locally
(`models/connection_tool.py:22`). Validating against it before the call is free
and would convert most of these failures into an instant, precise, local error.

**The recovery path is better than nothing but too shallow.**
`execute_mcp.py:719-743` builds a hint like
`Valid arguments for 'change_item_column_values': {boardId*:string, itemId*:string, columnValues*:string} (* = required)`.
That flattens `properties` **one level only** — it drops nested object shape,
enums, formats, and (critically) the fact that a `string` must *contain* JSON.
For monday/Atlassian-shaped schemas the hint is close to useless.

---

### RC-6 — `$ref` / `$defs` are never resolved for prompt rendering

The only `$ref` inlining in the codebase is Gemini-specific:

`backend/app/ai/llm/clients/google_client.py:158-166` —
*"Inline `$ref` references and strip / convert fields Google's SDK doesn't accept."*

MCP servers built on `zod-to-json-schema` (monday, Linear, Notion, Sentry — most
of our preset catalog) routinely emit `$ref`, `$defs`, `anyOf`, `allOf`. Dumped
raw into a text observation, `{"$ref": "#/$defs/ColumnValue"}` conveys nothing to
the model at the point where it must produce the value.

OpenCode does per-provider schema lowering as a first-class concern —
`packages/opencode/src/provider/transform.ts:1489 ProviderTransform.schema()`:
`sanitizeOpenAISchema` (mirrors Codex's Rust lowering), Moonshot `$ref`-sibling
stripping and tuple-`items` collapse, Gemini integer→string enum conversion.
BOW has no equivalent anywhere on the MCP path.

---

### RC-7 — Tool identity is indirect

`execute_mcp` requires `connection_id` **and** `tool_name` **and** `arguments`.
Three independent things to get right per call, versus one flat tool name. When
several MCP connections are attached to an agent, cross-wiring
`connection_id` to the wrong tool is a live failure mode.
`execute_mcp.py:138-148` softens this by accepting a name *or* an id, which is
good, but the coupling itself is the problem.

---

### RC-8 — A brand-new MCP session on every single call

`mcp_client.py:355-391` — `_connect()` opens a fresh SSE / streamable-HTTP
connection and runs `session.initialize()` for **every** `acall_tool`. And
`execute_mcp.py:261` re-runs `ConnectionService.construct_client` each time.

Costs:

- Full transport + handshake latency per tool call.
- No session state — servers that expect a stable session get none.
- `notifications/tools/list_changed` is never received, so schema drift is
  invisible at runtime.
- Capability negotiation repeated every call.

OpenCode holds long-lived clients (`s.clients`), caches tool defs (`s.defs`), and
refreshes on `ToolListChangedNotificationSchema` (`mcp/index.ts:461, 666-687`).

---

### RC-9 — No repair hook; the only backstop is a kill switch

OpenCode, `packages/opencode/src/session/llm.ts:296`:

```ts
async experimental_repairToolCall(failed) {
  const lower = failed.toolCall.toolName.toLowerCase()
  if (lower !== failed.toolCall.toolName && prepared.tools[lower]) {
    return { ...failed.toolCall, toolName: lower }        // fix casing
  }
  return {                                                // else hand the model
    ...failed.toolCall,                                   // a structured error
    input: JSON.stringify({ tool: failed.toolCall.toolName, error: failed.error.message }),
    toolName: "invalid",
  }
}
```

BOW's nearest equivalent is `tool_runner.py:54-83`: count validation failures,
and at 2 **terminate the entire run** with
`analysis_complete: True, final_answer: "Unable to complete task due to repeated
tool validation errors"`. That is a kill switch, not a repair — and because
`arguments` is an open dict it almost never fires for MCP anyway. Instead you get
remote errors, which count toward nothing, until the repeated-identical-call
breaker (`agent_v2.py:66-110`) eventually stops the turn.

---

### RC-10 — The active v3 system prompt contains no MCP flow guidance

The "discover before you call" instruction exists only in the **legacy v2**
builder:

`backend/app/ai/agents/planner/prompt_builder.py:142`

```
MCP/API TOOLS (if <mcp_tools> section is present in context)
- Use search_mcps to discover available external tools and get their full input schemas before calling execute_mcp.
- Use execute_mcp to invoke an external tool. ...
- Flow: search_mcps → execute_mcp → (optional: write_csv) → create_data for visualization.
```

`prompt_builder_v3.py` — **the default path** (`agent_v2.py:592-601` selects
`PlannerV3` unless `BOW_PLANNER` says otherwise) — has none of it. Grepping v3
for `execute_mcp` returns exactly one hit, and it is about the cosmetic `title`
argument (`prompt_builder_v3.py:400`).

The only surviving nudges are the `search_mcps` tool description and the
`<note>` inside `<mcp_tools>`. The system prompt gives the flow no priority at
all.

---

### RC-11 — Schema staleness

`ConnectionTool.input_schema` is a snapshot taken by
`ConnectionService.refresh_tools` (`connection_service.py:1786-1884`), triggered
only on connect, OAuth callback, explicit route, or the indexing service. There
is no TTL and no `tools/list_changed` subscription. When monday ships a schema
change, the agent faithfully follows a schema the server no longer accepts — and
the failure looks identical to a model error.

---

## Why "even Sonnet" fails

Because it isn't a reasoning failure. With a native tool schema the provider
constrains sampling — emitting an object where the schema says `string` is
largely *unreachable*. With `Dict[str, Any]` there is no grammar, and correctness
depends entirely on recall of a schema that (RC-2) arrived as prose, (RC-3) may
have been deleted from context, and (RC-6) may have been unresolvable `$ref`s
when it was there.

Stronger models push the failure rate down. They cannot remove a missing
constraint.

---

## Recommended direction

Not implemented — proposed, tiered by leverage against effort.

### Tier 1 — Native tool registration (highest leverage)

Expand each allowed `ConnectionTool` into a `ToolSpec` in the v3 catalog:

- name: `mcp__{connection_slug}__{tool_name}`, sanitized to `[a-zA-Z0-9_-]`,
  truncated to the provider's 64-char limit with a stable hash suffix on
  collision (OpenCode's `McpCatalog.sanitize`/`toolName` is the model).
- `input_schema`: `ConnectionTool.input_schema`, passed through
  `filter_locked_from_schema` (already exists,
  `mcp_context_injection.py:266-288`) then a normalization pass.
- Dispatch: route by name prefix straight into the existing
  `ConnectionToolGateway` — policy enforcement, identity forwarding, CSV/JSON
  materialization, and audit all stay exactly as they are.

Needs alongside it:
- A schema normalization pass: force `type: "object"`, default
  `properties: {}`, keep `$defs` intact for Anthropic, inline/lower per provider
  where required (the `google_client` ref-inliner is the seed).
- A catalog-size budget. Keep `execute_mcp` + `search_mcps` as the fallback path
  for agents with large tool counts, and register natively below the threshold.

This alone should remove the large majority of the reported failures.

### Tier 2 — Native `tool_use` / `tool_result` transcript

Make the agent loop maintain a real message array instead of rebuilding one user
message per iteration. Bigger refactor, but it fixes tool fidelity, error
attribution, and prompt caching for **every** tool, not just MCP. The client
layer already supports it (`anthropic_client.py:191-204`) — the gap is entirely
in `PromptBuilderV3` / `PlannerV3` / `agent_v2`.

### Tier 3 — Cheap fixes worth doing regardless of Tier 1

1. **Validate `arguments` against `ConnectionTool.input_schema` before the network
   call** (`jsonschema`), and return a path-based error
   (`columnValues: expected string, got object`) plus the resolved schema.
   Converts a slow, vague remote failure into an instant, precise local one.
2. **Add `tools` and `input_schema` to `_OBS_KEEP_KEYS`** — or better, pin the
   most recent `search_mcps` result so schemas never fall out of context.
3. **Keep `tool_input` on minified observations for failed calls**, so the model
   can diff its previous attempt.
4. **Resolve `$ref`/`$defs` before rendering any schema into a prompt.**
5. **Inline schemas into `<mcp_tools>` for small catalogs** (say < 15 tools) —
   the two-hop discovery step disappears entirely for most connectors.
6. **Restore the MCP flow guidance in the v3 system prompt**, with an explicit
   note that some servers expect JSON-encoded *strings* for structured fields.
7. **Pool/persist the MCP client per connection for the duration of a run**, and
   subscribe to `tools/list_changed`.
8. **Deepen `_failure_payload`** to render nested properties, enums, and formats
   rather than one flat level.

---

## How to verify any fix

Build a small eval harness against the preset catalog (monday, Notion, Linear,
Atlassian, Sentry) measuring, per connector:

- **first-call argument validity** — fraction of `execute_mcp` calls that pass
  local schema validation on the first attempt (the headline metric);
- **calls-to-success** — round trips needed to complete a task;
- **remote 4xx rate** — argument errors that reach the server at all;
- **schema-in-context rate** — how often the tool's schema was actually present
  in the prompt at generation time (this one will be damning today).

Track it per model so the "is it the model or is it us" question stops being a
matter of opinion.

---

## Files referenced

| Concern | Path |
|---|---|
| Gateway tool schema (RC-1) | `backend/app/ai/tools/schemas/execute_mcp.py` |
| Gateway execution (RC-5, RC-8) | `backend/app/ai/tools/implementations/execute_mcp.py` |
| Discovery tool (RC-2) | `backend/app/ai/tools/implementations/search_mcps.py` |
| `<mcp_tools>` rendering (RC-2) | `backend/app/ai/context/sections/tables_schema_section.py:338-377` |
| Observation compaction (RC-3) | `backend/app/ai/agents/planner/prompt_builder.py:17-27, 543-586` |
| Single-user-message prompt (RC-4, RC-10) | `backend/app/ai/agents/planner/prompt_builder_v3.py` |
| Native block support (unused) | `backend/app/ai/llm/clients/anthropic_client.py:174-231` |
| Wrapper-only validation (RC-5, RC-9) | `backend/app/ai/runner/tool_runner.py:32-83` |
| Per-call session (RC-8) | `backend/app/data_sources/clients/mcp_client.py:211-247, 355-391` |
| Schema snapshot (RC-11) | `backend/app/services/connection_service.py:1786-1884` |
| Presets | `backend/app/schemas/data_source_registry.py:1564-1669` |

### External references

- OpenCode MCP → tool conversion: `sst/opencode`, `packages/opencode/src/mcp/catalog.ts`
- OpenCode provider schema lowering: `packages/opencode/src/provider/transform.ts` (`ProviderTransform.schema`)
- OpenCode tool-call repair: `packages/opencode/src/session/llm.ts` (`experimental_repairToolCall`)
- monday.com `columnValues` is a JSON-encoded string: <https://developer.monday.com/api-reference/docs/change-item-column-values>
