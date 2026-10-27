# Feedback Loop — "I have no read_file tool" (Auto) and "I have no OCR tool, I didn't see any image" (Bedrock / Gemini)

**Status: FIXED and VERIFIED (2026-10-27).** Loop A (unit + e2e) and Loop B (live
stack, real provider clients, real LibreOffice rendering) both flip from FAIL on
`main` to PASS on the branch.

Two customer reports from the same ServiceNow agent, both in Hebrew:

1. *"Read KB0013962 and use read_file to read its images."* — the agent answered
   that `read_file` is not in its tool list (it listed `read_query`,
   `read_artifact`, `read_resources`, `read_mcp_resource`), and repeated an org
   instruction claiming ServiceNow needs a separate File Source connection.
2. After `read_file` became available: *"read_file returned images, but it does
   not describe them. With as_images=true I got 8 pages of 14 as images and saw
   no content in any of them — I have no OCR tool."*

Claims validated here: (1) is a tool-scope bug of **Auto** chats (no agents
attached), not a ServiceNow limitation; (2) is the provider clients for
**Bedrock and Gemini** dropping the page images, not a model limitation.

## Root cause (validated)

### 1. Auto chats never got file tools, and could not resolve a file source

"Auto" = the report has no attached agents; the run executes against every
agent the user can access (`agent_focus_common.resolve_run_agents`). Several
places still read `report.data_sources` (empty in Auto) instead of the run's
working set:

| Where (main) | Effect in Auto |
|---|---|
| `backend/app/ai/agent_v2.py:913` — capability gating of the tool catalog | `list_files` / `read_file` / `search_files` / `grep_files` / `attach_file` / `write_file`, the mail tools and the note tools are filtered out of the catalog |
| `backend/app/ai/tools/implementations/_file_tool_common.py:204, 243` — file-source resolver / allow-list | a forced `read_file` fails: *"does not match any file source attached … Retry with one of: (none attached)"* |
| `backend/app/ai/tools/implementations/search_files.py:202` | same, for keyword search pinning |
| `backend/app/services/artifact_verification_policy.py:119` — browser connector policy | an Auto chat never finds its browser connection |

Also: the catalog was built once per turn, so an agent attached **mid-run**
(`create_agent`) never brought its file tools until the next turn.

`execute_mcp` already did this right (`runtime_ctx["run_agent_ids"]`), which
is why MCP tools worked in Auto and file tools did not.

### 2. Bedrock and Gemini dropped tool images (and Gemini dropped uploads)

On the native-transcript path (default), a tool's images are *not* sent on the
side channel — `AgentV2._planner_images` (`agent_v2.py:1974-1994`) leaves them
to the transcript, which hoists them as `{"type": "image"}` blocks into the
tool-result turn (`context/transcript.py:387`). The translators:

- `backend/app/ai/llm/clients/bedrock_client.py:431` (`_translate_messages`)
- `backend/app/ai/llm/clients/google_client.py:237` (`_translate_messages`)

only understood `text` / `tool_use` / `tool_result`. In a tool-result turn they
also dropped the turn's own text (clock, steering, nudges). Anthropic, OpenAI,
Azure and Responses translate `image` blocks — the parity test
(`tests/unit/test_message_translation_parity.py`) explicitly excluded
Google/Bedrock, so nothing caught it. Separately,
`google_client.py:369` (`inference_stream_v2`) ignored its `images`
argument entirely, so user-uploaded images never reached Gemini either.

The "tell": `read_file` reported *"1 of 1 page(s) as image(s) for vision"*
while the provider request carried **0** images (Loop B).

## Loop A — deterministic reproduction (no external services)

```bash
cd backend
export BOW_DATABASE_URL=sqlite:///db/app.db TESTING=true
uv run pytest tests/unit/test_transcript_images_reach_every_provider.py -q
uv run pytest tests/e2e/rbac/test_file_tools_run_scope.py \
              tests/e2e/rbac/test_browser_connector_run_scope.py -m e2e --db=sqlite -q
```

On `main` (worktree, same test files):

```
test_transcript_images_reach_every_provider.py   12 failed, 18 passed
  FAILED ...[bedrock] / [google]  page N never reached the request
  FAILED test_gemini_side_channel_images_reach_the_request[plain|after_tool_results]
test_file_tools_run_scope.py +
test_browser_connector_run_scope.py               13 failed, 4 passed
  AssertionError: ['[failed] {"summary": "Invalid file-source selection: ... Retry with one of: (none attached)."'
```

The tests that pass on `main` are the access-control invariants (a member never
reads an agent they cannot access; a pinned selection stays a hard scope) and
the Bedrock upload path that already worked — they must hold before and after.

On the branch: **all pass** (30 + 17). The mid-run test
(`test_file_agent_attached_mid_run_brings_its_file_tools_to_the_next_step`)
also fails on the branch with only the mid-run refresh disabled.

## Loop B — live stack, real provider clients

Everything real except the network edges: backend (`python main.py`), the real
`BedrockClient` / `Google` clients (boto3 / google-genai), the real
`ServiceNowClient`, real `read_file(as_images=true)` (LibreOffice renders the
.docx).

- `tools/agent/fake_servicenow.py` — KB0013962 with a real .docx attachment:
  procedure text + an embedded screenshot carrying a code (`ZEBRA-7731`) that
  appears **nowhere in the text**.
- `tools/agent/provider_bridge_llm.py` — speaks Bedrock ConverseStream (AWS
  event-stream frames) and Gemini `generateContent` / SSE; the backend is
  pointed at it with `AWS_ENDPOINT_URL_BEDROCK_RUNTIME` /
  `GOOGLE_GEMINI_BASE_URL`. Default `BRIDGE_MODE=scripted`: a deterministic
  agent that lists the KB files, calls `read_file(as_images=true)`, then OCRs
  (tesseract) **whatever images actually arrived in the provider request**.
  (`BRIDGE_MODE=anthropic` forwards to the real Anthropic API instead; the
  sandbox key had no credit, so the scripted mode was used.)
- `tools/agent/verify_auto_file_tools_vision.py --provider bedrock|gemini [--attach]`
  — seeds the provider (vision on) + ServiceNow agent, opens an Auto chat (or
  attaches the agent with `--attach`), asks the question, asserts.

```bash
apt-get install -y tesseract-ocr        # once
cd backend
uv run python ../tools/agent/fake_servicenow.py &
uv run python ../tools/agent/provider_bridge_llm.py &
# backend with the provider endpoints redirected
AWS_ENDPOINT_URL_BEDROCK_RUNTIME=http://127.0.0.1:9312 GOOGLE_GEMINI_BASE_URL=http://127.0.0.1:9312 \
  TESTING=true ENVIRONMENT=production TEST_DATABASE_URL=sqlite:///db/agent.db uv run python main.py &
uv run python ../tools/agent/seed_org.py --db-path db/agent.db
uv run python ../tools/agent/verify_auto_file_tools_vision.py --provider bedrock
uv run python ../tools/agent/verify_auto_file_tools_vision.py --provider gemini --attach
```

`main` — reproduces both reports exactly:

```
===== bedrock (auto) =====                       ===== gemini (auto) =====
tools=[]                                         tools=[]
NO_READ_FILE_TOOL: read_file is not in my tool list (add_parameter, ... create_data, ...)
FAIL 1. read_file offered to the model           (same for gemini)
FAIL 2. read_file resolved the ServiceNow file
FAIL 3. page images reached the provider

===== bedrock --attach =====                     ===== gemini --attach =====
tools=[('list_files', True), ('list_files', True), ('read_file', True)]
read_file: "Read KB0013962 — 1 of 1 page(s) as image(s) for vision"
images per provider request: [0, 0, 0, 0]
IMAGES_RECEIVED=0. OCR of the page images: (none)
PASS 1, PASS 2, FAIL 3. page images reached the provider
```

Branch — all four runs (bedrock/gemini × auto/attach):

```
tools=[('list_files', True), ('list_files', True), ('read_file', True)]
images per bedrock request: [0, 0, 0, 1]
answer: IMAGES_RECEIVED=1. OCR of the page images: Purpose: work instruction ...
        ROUTING CODE: ZEBRA-7731 APPD node availability: 3/4 nodes up End of procedure.
PASS 1. read_file offered to the model
PASS 2. read_file resolved the ServiceNow file
PASS 3. page images reached the provider
PASS 3b. model read the screenshot code ZEBRA-7731
```

## The fix

**Run scope (fix 1)**
- `agent_focus_common.run_scope_agents(runtime_ctx)` — the run's working set
  (`runtime_ctx["run_agents"]`, set by AgentV2 from `self.data_sources`) plus
  the report's attachments. Without a working set (standalone calls, tests) it
  is the attachments alone, exactly as before.
- `_file_tool_common` (resolver + allow-list) and `search_files` resolve
  against it. The existing per-user access guard in `resolve_file_client`
  still runs on every resolution.
- `AgentV2`: capabilities are computed over the run's agents
  (`capabilities_for_agents`); capability-gated tools no agent backs yet go
  through every catalog filter and are held as candidates;
  `_refresh_capability_tools` adds them before a planner step when the run's
  agent set changes (mid-run `create_agent` / `set_report_agents`), respecting a
  sub-agent's allowed tools.
- Browser policy: `resolve_run_agents` on the freshly loaded report (same
  per-user access check on each browser connection).

**Provider images (fix 2)**
- Bedrock `_translate_messages`: image blocks become Converse image blocks
  (through the existing `_image_block`, so size normalization applies);
  tool-result turns keep `toolResult` blocks first, then images, then the
  turn's text.
- Gemini `_translate_messages`: image blocks become `Part.from_bytes` /
  `from_uri`; a function-response turn stays function responses only, and the
  images + turn text follow as their own user turn. `inference_stream_v2` now
  attaches the side-channel `images` (uploads).

## What this proves / regression notes

- Auto and pinned selections now offer exactly the file tools of the agents the
  run executes against, for admins and members; members still cannot read an
  agent they cannot access, and a pinned selection is still a hard scope.
- Bedrock and Gemini receive tool images and the turn text on the transcript
  path, and Gemini receives uploads.
- **Not covered live:** real AWS Bedrock and real Gemini (no credentials in the
  sandbox). The Bedrock wire shape (toolResult first, images after, same turn)
  matches what the client already sent for uploads. The Gemini shape (images in
  a separate user turn after the function responses) is the conservative
  choice; the bridge accepts any shape, so a live Gemini call is the remaining
  check.
- Pre-existing, unrelated failures (identical on `main`): SharePoint 2010 /
  on-prem unit + e2e tests assert `session.verify is True`, but this sandbox's
  proxy sets `REQUESTS_CA_BUNDLE`, making it `/root/.ccr/ca-bundle.crt`;
  `test_list_files_persist_per_user` errors in fixture setup for the same reason.
- The customer's org instruction ("ServiceNow without a File Source connection
  does not work with read_file") is wrong since v0.0.554 and should be removed —
  it is org content, not in this repo.
