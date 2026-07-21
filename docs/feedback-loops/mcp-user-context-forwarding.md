# Feedback Loop — "send user & membership data into each MCP invocation"

A customer's MCP server (Infor LN) expects per-user identity on every tool call:
outbound identity **HTTP headers** and a `custom_metadata` object inside the tool
arguments, e.g.

```jsonc
query_production_orders({
  "prompt": "…",
  "company": "111",
  "custom_metadata": {
    "_client_userId": "dp28376",
    "_client_full_userId": "elbit_nt\\dp28376"
  }
})
```

Some fields the LLM may decide; others (`user_email`, `_client_userId`, …) the
admin must be able to **hard-override** and hide from the model. Values map from
the signed-in user's identity / `Membership.profile_attributes`. This loop
validates that the mapping is configurable, resolves per user, and actually
reaches the wire.

## What was built (validated)

- `MCPConfig` gains `headers`, `header_injection`, `metadata_injection`
  (`backend/app/schemas/data_sources/configs.py`).
- A whitelist resolver, `app/services/mcp_context_injection.py`:
  - sources `user.email|name|id`, `membership.role`, `membership.attr:<key>`,
    `static:<text>` with `{token}` interpolation — nothing else resolves, so a
    bad mapping can't reach a secret;
  - metadata `mode`: `locked` (clobbers the model, hidden from its schema) vs
    `ai` (fills only where the model left a gap); `on_missing` = empty/omit/block.
- Injection wired into `execute_mcp.py` (metadata merged before the policy
  confirmation and the call; resolved headers forwarded via `construct_client`)
  and locked fields stripped from the model-facing schema in `search_mcps.py`
  and the failure payload.
- UI: `MCPConnectionForm.vue` Advanced section — header/metadata mapping rows
  with a source picker, lock toggle, and directory-attribute suggestions.
- `MCPTool.vue`: failed tool calls render inline in **amber** (not red); call
  time rounds to whole seconds.

## Loop A — deterministic reproduction (no external services)

Pure-logic invariants of the resolver — run in a clean sandbox:

```bash
cd backend
export TESTING=true BOW_DATABASE_URL="sqlite:///db/app.db" TEST_DATABASE_URL="sqlite:///db/agenttest.db"
mkdir -p db
uv run pytest tests/unit/test_mcp_context_injection.py -q
```

Observed: **27 passed**. Coverage includes locked-clobber, ai-fill-if-absent,
`static:` interpolation of `_client_full_userId` (`elbit_nt\dp28376`),
`on_missing` empty/omit/block, header omit-empty, whitelist safety (unknown
sources never resolve), and locked-field schema hiding.

To see it fail for the right reason, stash the resolver
(`git stash -- app/services/mcp_context_injection.py`) → import error /
assertion failures; restore to flip back to green.

## Loop B — live confirmation over the wire

A real streamable-HTTP MCP server echoes back what it received
(`tests/mocks/echo_mcp_http_server.py`):

```bash
cd backend
MOCK_MCP_CAPTURE_FILE=/tmp/bow-agent/mcp_capture.json \
  uv run python tests/mocks/echo_mcp_http_server.py --port 3333
```

Driving the real `McpClient` against it with resolved headers + metadata shows
both arrive:

```
WIRE OK success= True
header on wire: True | metadata on wire: True
```

and the capture file records the echoed `custom_metadata` and `x-user-email`
header verbatim.

## Loop C — end-to-end through the UI (Playwright, 0→1)

`frontend/tests/mcp_forwarding/context-forwarding.spec.ts` (config
`pw.mcp.config.ts`) signs an admin up from scratch, opens the MCP connection
modal, configures a header rule and two metadata fields (one locked, one
AI-fillable) in the Advanced section, runs the live **Test Connection** against
the echo server (green), saves, and asserts the persisted `config` round-trips
the forwarding spec:

```bash
# stack up: tools/agent/boot_stack.sh --dev
# echo server up on :3333
cd frontend
PLAYWRIGHT_BROWSERS_PATH=/opt/pw-browsers npx playwright test --config=pw.mcp.config.ts
```

Screenshots land in `frontend/test-results/mcp-forwarding/`.

## Loop D — live Haiku agent turn (real model → real MCP server)

`tests/e2e/test_mcp_context_forwarding_live.py` drives the whole stack: a real
Anthropic **Haiku** model plans a turn, decides to call `query_production_orders`,
and BOW injects the signed-in user's identity before the call reaches the echo
server.

```bash
# echo server up on :3333 with MOCK_MCP_CAPTURE_FILE set
cd backend
MOCK_MCP_CAPTURE_FILE=/tmp/bow-agent/mcp_capture.json \
ANTHROPIC_API_KEY_TEST=$ANTHROPIC_KEY \
  uv run pytest tests/e2e/test_mcp_context_forwarding_live.py -m e2e -s
```

Observed (`1 passed`) — the echo server received, over the wire:

```jsonc
{
  "received_arguments": {
    "prompt": "weekly production orders",   // Haiku authored
    "company": "111",                        // Haiku authored
    "custom_metadata": {                     // BOW server-injected (locked)
      "_client_userId": "admin",             // ← membership.role
      "user_email": "test@test.com",         // ← user.email
      "application_name": "BagOfWords"        // ← static
    }
  },
  "received_headers": { "x-user-email": "test@test.com", ... }
}
```

i.e. the model chose the tool and its natural arguments, while the locked
identity fields — which the model never saw in the tool schema — were injected
by the server and arrived intact alongside the `X-User-Email` header.

## What this proves / regression notes

The mapping is admin-configurable, resolves per user from identity/membership,
honors the locked-vs-AI override contract, hides locked fields from the model,
and the resolved headers + `custom_metadata` reach an MCP server over the wire.
Loop A survives as the regression suite for the resolver contract.
