# Feedback Loop — "PII redaction on the agent level too, not just the LLM provider"

Request: an agent's PII policy should apply whenever that agent takes part in a
chat, whatever model the chat uses. Admins set it in Settings → PII (with a
default for agents), and agent admins set it on the agent's own settings.

Claim validated here: **the effective policy of every LLM call is the merge of
the provider's policy and the policies of every agent in the call's scope,
strictest option per rule.** One agent's policy covers the whole prompt,
including other agents' data in the same chat. Design:
`docs/design/pii-agent-policies.md`.

## Root cause (why it didn't work before)

The policy was chosen by provider only: `CompiledPiiConfig.for_provider` in
`backend/app/ai/llm/pii/policy.py`, called from the LLM chokepoint
(`_aget_pii_redactor` / `_get_pii_redactor_sync` in
`backend/app/ai/llm/llm.py`). The LLM call didn't know which agents a run
involved. An Exempt provider therefore sent every agent's data unredacted, and
there was no place to store a policy per agent.

## The test-case matrix

Written first, as an executable table in
`backend/tests/unit/test_pii_agent_policies.py` (`MATRIX`, 35 cases). Groups:

| Group | Examples (agents A, B; provider P) | Expected |
|---|---|---|
| One agent with a policy | A-pii + B-none, chat with both | pii (whole chat) |
| | B alone / no agents in the chat | raw |
| Both agents with policies | A-emails + B-phones | both hidden (union) |
| Same rule, different actions | A-block + B-replace | blocked |
| | A-mask + B-replace, A-pseudo + B-replace | replaced (strongest) |
| Provider policy in the merge | P-emails + A-phones | both hidden |
| | P-block + A-replace, P-replace + A-block | blocked |
| Exempt provider | Exempt + A-pii | pii (agent overrides Exempt) |
| | Exempt + B-none | raw |
| Default agent policy | A unassigned, default = emails | pii |
| | A explicit `none`, default = emails | raw |
| | A explicit phones, default = emails | phones only (own choice replaces default) |
| Unknown scope (call didn't say) | any agent has a policy | every agent policy applies (fail closed) |
| | nothing assigned | provider policy only (unchanged) |
| PII off for the org | anything | raw |

Also in that file:
- a randomized property test: for 40 seeds, the merge never hides less than any input and never uses a weaker action;
- the lowest detector threshold wins;
- an org with no agent configuration behaves exactly as before, for every scope (16 combinations);
- merged redactors are cached per policy set;
- config normalize, validate, deleting a policy, the schema round trip, and audit.

**109 passed.**

## Loop A — deterministic (no external services)

```bash
cd backend
export TESTING=true BOW_DATABASE_URL="sqlite:///db/app.db"
uv run pytest tests/unit/test_pii_agent_policies.py -q          # 109 passed
uv run pytest tests/e2e/test_pii_agent_policies.py -m e2e -q    # 4 passed
```

The e2e file goes through the real settings API, the agent endpoint and the
real `LLM` class, with only the provider client stubbed:
- **One LLM instance, reused across scopes:** `[HR, Sales]` → redacted, `[Sales]` → raw, `[]` → raw, `[HR, Ops]` → union, unknown scope → every policy, then back to `[Sales]` → raw. The redactor follows the scope; it isn't cached per instance.
- **Default agent policy:** an agent without its own choice inherits it; a blocking default blocks.
- **Exempt provider:** an agent policy overrides it.
- **Agent endpoint:**
  - an agent manager (a `manage` grant on the agent only) can read the policy, set it, and go back to inherit;
  - `none` returns 403 for them and 200 for an org admin;
  - Exempt and unknown policies are rejected with 400;
  - a plain member gets 403, and the agent manager gets 403 on another agent;
  - every change is audited (`pii_protection.agent_policies_updated`).
- **Org save:** unknown agent ids are rejected with 400.

## Loop B — live stack: mock LLM (16 permutations) and real Claude

```bash
export BOW_ENCRYPTION_KEY=<any fernet key, pinned across restarts>
BOW_LLM_TRACE_FILE=/tmp/bow-agent/llm_trace.jsonl BOW_LLM_TRACE_FULL=1 tools/agent/boot_stack.sh --dev
cd backend
uv run python ../tools/agent/seed_org.py --demo
PII_CAPTURE_FILE=/tmp/bow-agent/pii_captures.jsonl uv run python ../tools/agent/pii_capture_llm.py &   # :9199
ANTHROPIC_KEY_LOOP=... uv run python ../tools/agent/pii_agent_policies_setup.py   # agents HR + Sales, mock + Anthropic
uv run python ../tools/agent/pii_agent_policies_live.py        # mock, 16 permutations
uv run python ../tools/agent/pii_agent_policies_claude.py --trace /tmp/bow-agent/llm_trace.jsonl --model claude-haiku-5-5
cd ..
PW_CHROMIUM=/opt/pw-browsers/chromium HR_AGENT=<id> SALES_AGENT=<id> \
  node tools/agent/pii_agent_policies_ui.mjs flow media/pr/pii-agent-policies
```

### Mock: every LLM call of a real agent run

The message is `Contact jane.roe@contoso.com or call 415-555-0134 about the Q3 numbers.`
Each case uses a fresh report with the listed agents. The run goes through
AgentV2 to the mock, which records every call (4 calls per run). A case passes
only if **every** call matches.

```
PASS  HR-pii + Sales-none, chat with both: both hidden (4 LLM calls)
PASS  HR-pii + Sales-none, chat with Sales only: raw
PASS  HR-pii + Sales-none, chat with HR only: both hidden
PASS  HR-pii, chat with no agents (Auto: every accessible agent): both hidden
PASS  HR-pii + Sales-emails, chat with Sales: email hidden, phone raw
PASS  HR-pii + Sales-emails, chat with both (union): both hidden
PASS  default agent = Sales-emails, Sales inherits: email hidden, phone raw
PASS  default agent = Sales-emails, Sales explicit none: raw
PASS  default agent = Sales-emails, HR own policy wins for HR: both hidden
PASS  provider Exempt + HR-pii, chat with HR: both hidden
PASS  provider Exempt + HR-pii, chat with Sales: raw
PASS  provider Sales-emails + HR-none-assigned, chat with HR: email hidden, phone raw
PASS  HR blocks emails + Sales, chat with both: blocked (0 LLM calls)
PASS  HR blocks emails, chat with Sales only: raw
PASS  nothing configured, chat with both: raw
PASS  default agent = Sales-emails, Music Store inherits: email hidden, phone raw
16/16 live cases passed
```

In the blocked case, nothing reached the provider. The chat shows the typed
error `pii_blocked`: "PII protection blocked this request to mock-public (rule:
Email address)", with policy "HR (block emails)".

### Real model (Claude Haiku 5.5): PII in the data, not the prompt

The model queries the agents' tables. HR has employees with emails and phones;
Sales has accounts with contact emails. Policies: HR → emails + phones, Sales →
none. The trace records each request as sent to Anthropic, after redaction.

```
PASS HR chat: the run reached the model                  {"calls": 3}
PASS HR chat: no HR email or phone reached Anthropic      {"leaked": []}
PASS HR chat: the data reached the model redacted         {"email_tokens": 4, "phone_tokens": 4}
PASS HR chat: names (not covered by the policy) did reach the model
PASS Sales chat (control): Sales emails reached Anthropic as written   (all 3)
PASS HR + Sales chat: Sales emails did NOT reach Anthropic {"leaked": []}
PASS HR + Sales chat: the Sales data reached the model redacted
PASS no context markers in any request
8/8 checks passed
```

What the model answered in the HR + Sales chat, asked for the Sales contact
emails:

> | Globex | [REDACTED_EMAIL] | EMEA | … The contact emails came back redacted in
> the query output, so I can't show the actual addresses here.

In the Sales-only chat it listed `buyer@globex.example` and the other two
addresses.

### UI: both places edit the same data (11/11)

```
PASS  settings: agents section visible / one row per agent
PASS  settings: default agent policy and HR policy persisted after reload; Sales shows inherit
PASS  agent: control visible; inherits org default; inherit option names the default
PASS  agent: choice persisted after reload
PASS  settings: reflects the agent page choice
PASS  agent: back to inherit
```

Screenshots are in `media/pr/pii-agent-policies/`:
- `before-*` and `after-*`, plus `after-*-he.png` for RTL;
- `flow-*`.

The Hebrew shots caught a bug: a Latin policy name in parentheses was reordered
inside the Hebrew "org default (…)" label and clipped. Fixed with an
`Org default: {policy}` label, Unicode isolate marks around the name, and wider
dropdowns.

## The fix

- **`backend/app/ai/llm/pii/policy.py`:**
  - `agent_policies` and `default_agent_policy` in the config: normalize, validate, audit diff;
  - `none` is reserved as a policy id;
  - `merge_policies` (strictest per rule);
  - `CompiledPiiConfig.for_call(provider, agent_ids)`, which skips policies with no active rule and caches merged redactors per policy set.
- **`backend/app/ai/llm/pii/loader.py`:** `load_redactor_for_call`.
- **`backend/app/ai/llm/llm.py`:**
  - the chokepoint reads the run's `data_source_ids` from the usage attribution; absent means unknown scope;
  - the redactor is cached per scope;
  - the sync path reads the scope in the calling thread, before hopping to the main loop.
- **`backend/app/ai/agent_v2.py`:**
  - stamps `data_source_ids`: the run's working set, sign-in candidates and the report's agents;
  - it's a list that only grows, shared by every attribution snapshot;
  - agents attached mid-run are added (`_extend_pii_agent_scope`).
- **`backend/app/services/pii_agent_policy_service.py`** and **`backend/app/routes/data_source.py`:**
  - `GET`/`PUT /data_sources/{id}/pii_policy` for agent managers (`manage` on the agent);
  - `none` requires `manage_settings`.
- **`backend/app/services/organization_settings_service.py`:** agent ids are checked against the org's live agents on save.
- **Frontend:**
  - an "Agent policies" section in Settings → PII: the default for agents, and one row per agent;
  - `AgentPiiPolicyControl` in the agent's settings panel;
  - strings in all 10 catalogs.

## Regression notes

- The related suites pass: PII, LLM, org settings, data source, audit, public output parity, report message search. **626 passed, 1 skipped.**
- The agent-run e2e tests (`tests/e2e/test_agent_*.py`, `test_bow_runs_token_split.py`) pass. **153 passed.**
- Catalog sync: missing/extra key counts are unchanged from the base branch; the new keys exist in all 10 catalogs.
- Not covered (documented in the design doc):
  - **"Sticky" agents:** an agent detached from a report doesn't keep its policy there. Queries don't record their agent, so there's no cheap source.
  - **Unknown-scope paths:** calls outside an agent run (artifact-page resources, indexing) get every agent policy. Those paths over-redact rather than leak.
- Loop hazards hit along the way:
  - the setup's table activation failed silently at first, which made the real-model leg vacuous ("Described 0 tables"); the setup now fails loudly;
  - the backend ignored SIGTERM during a slow graceful shutdown, so restarts use `kill -9` on the process group.
