# Feedback loop — “I would get a response that it sent?”

Parent/child messaging adds a visible send receipt, durable delivery history,
and blocking child questions without starting a second parent alongside the
current turn. Implemented on `origin/main` at `a827680b807e5c48c4ec0b558554bfd14ea83abb`
(fetched and checked 2026-10-15). Verification used an isolated worktree, a fresh
SQLite sandbox on ports 8100/3100, and a separate disposable PostgreSQL database.

## Scope

Parent → active/pending child directions and answers; child → parent questions
and findings. A question suspends the child between steps, releasing its child
execution slot. The existing deadline and stop controls still apply. Findings
do not wake the parent; final results still use `finish_subtask` and wave fan-in.

Sibling/cross-report messaging, nested teams, automatic propagation of every
user steer, and restarting/resuming terminal children are deferred. A fresh
child can use the previous child's result and notes. Durable messages are not
execution checkpoints.

## Root cause and defects caught

The baseline had delegation, final records and inspection, but no mailbox or
message tool. The initial registry probe failed because `message_sub_agent`
was absent. The new service persists messages separately from final records.

Verification caught these integration defects before completion:

- The delegation feature gate in `backend/app/ai/agent_v2.py:976` also hid the
  new tool from children. A scripted provider could emit an unavailable tool,
  so a passing execution alone was insufficient. The regression now asserts
  the child's *offered native tool catalog* contains `message_sub_agent`.
- A question dispatcher and a direct/streaming user submit could each create
  a parent turn. `backend/app/services/completion_service.py` now shares the
  report lock across queue dispatch and parent creation; head/system rows are
  committed atomically. A racing user prompt is saved in the existing queue.
  The report page handles the JSON queued response from a streaming request.

- A question near the 32-message cap could consume the last answer slot. The
  service now reserves that slot while a question is outstanding; a regression
  verifies the answer can still arrive.
- Expanding a terminal child after reloading could skip fetching its message
  history. The card now fetches details when expanded, including ended children.

The tool also uses `ToolErrorEvent.payload.message`, matching the runner's
error contract, so rejected sends retain an actionable reason.

## Loop A — deterministic tests, no external LLM

From the repository root:

```bash
cd backend
uv sync --frozen --extra dev
export TESTING=true BOW_DATABASE_URL=sqlite:///db/app.db
.venv/bin/pytest -m e2e tests/e2e/test_sub_agent_messages.py -q --db=sqlite
.venv/bin/pytest tests/e2e/test_sub_agents.py tests/e2e/test_completion_queue_steer.py tests/unit/test_sub_agent_prompts.py -q
```

Observed: **6 messaging tests passed**; **18 existing delegation, queue/steer,
and prompt tests passed**. Tests use real services, migrations, tools, queue,
and agent loops; only the external LLM boundary is scripted. The scenarios
cover scoped delivery/replay, idempotent retries, cross-report/org and sibling
rejection, terminal rejection, finding/no wake, question coalescing, duplicate
answer rejection, question state, answer/resume/fan-in, and direct plus streaming
user submissions while a question is queued, and the reserved answer slot.
Both database suites were rerun after updating to the latest main above.
The combined SQLite suite passed all **24 tests** on that final base. It emitted
dependency/deprecation warnings and aiosqlite event-loop-closed
thread teardown warnings in the queue tests; no test failed.

### Repeat the meaningful red → green check

The following temporarily restores the defective child tool gate and restores
the source even when pytest fails. Run while no reload-enabled server uses the
checkout:

```bash
.venv/bin/python - <<'PY'
from pathlib import Path
import subprocess
p = Path('app/ai/agent_v2.py')
original = p.read_text()
old = "not in ('delegate', 'read_sub_agent', 'stop_sub_agent')"
assert old in original
try:
    p.write_text(original.replace(old, "not in ('delegate', 'read_sub_agent', 'stop_sub_agent', 'message_sub_agent')", 1))
    result = subprocess.run(['.venv/bin/pytest', '-m', 'e2e',
        'tests/e2e/test_sub_agent_messages.py', '-k', 'real_child_question', '-q'])
    assert result.returncode != 0, 'The deliberately broken gate must fail'
finally:
    p.write_text(original)
PY
.venv/bin/pytest -m e2e tests/e2e/test_sub_agent_messages.py -k real_child_question -q
```

Observed red: the child tool-catalog assertion failed because the native tool
list lacked `message_sub_agent`. Restoring the gate made the same real-flow
test pass. This red/green check also ran on PostgreSQL.

### PostgreSQL

`--db=postgres` could not start its Testcontainers database on this host. We
used a uniquely named disposable local PostgreSQL database instead. Never use
an existing app database: `--db=external` resets its public schema.

```bash
createdb bow_submsg_verify_disposable
TEST_DATABASE_URL=postgresql:///bow_submsg_verify_disposable \
  .venv/bin/pytest -m e2e tests/e2e/test_sub_agent_messages.py -q --db=external
dropdb bow_submsg_verify_disposable
```

Observed: **6 passed** on PostgreSQL; the verification database was dropped.
Luna independently authored/ran these tests and reviewed stop/deadline,
message rejection, queue serialization, and tool exposure.

## Loop B — live browser and real Luna

Use `tools/agent/boot_stack.sh --dev` and `tools/agent/seed_org.py` in a fresh
sandbox, or launch on separate free ports when another stack is running.
Enable sub-agents, configure an authorized LLM provider, and sign in as a
seeded user. Credentials stay outside source and evidence. This run used a
local memory-only credential proxy forwarding requests to `gpt-6-luna`;
HTTP 200 provider responses were verified. The initial seeded child model
label was `Stub GPT`, but its requests were forwarded to real Luna as well.

Submit through the browser composer:

> Draft a weekly revenue reporting policy with one sub-agent named Revenue
> reviewer. First delegate the child to identify whether refunded orders belong
> in revenue. Tell the child that its first action must be to ask you using
> message_sub_agent(recipient="parent", kind="question") whether refunds should
> be excluded, and to wait for your answer before writing a note. Do not give
> the child the answer in its initial instructions. My decision, for you to
> answer when asked: exclude refunded orders, and use UTC weeks starting Monday.
> Send your answer through message_sub_agent with its actual question ID,
> confirm that you sent it, and let the child finish. Use only this prompt,
> no data queries.

Observed: child question → waiting → parent's sent receipt → delivered answer
→ same child finished. The parent confirmed sending and the final policy used
the answer. Expanding the child's card showed both messages as delivered to
agent context. The sent receipt explicitly does not claim compliance.

Reload the report, expand the receipt, and inspect child history. Switch the
sandbox organization language through Settings → General to Hebrew; verify
`html[lang=he][dir=rtl]` and the translated receipt, then restore English.

## UI evidence and build

Evidence lives under `media/pr/sub-agent-messaging/`:

- `before.jpg`: actual baseline delegation UI, before messaging.
- `before-receipt.jpg`: main's renderer on the same seeded report as the after
  screenshot (new backend data held constant); it shows a generic tool row.
- `after-receipt.jpg`: expandable sent receipt with body and delivery meaning.
- `after-history.jpg`: durable child/parent message history.
- `after-he.jpg`: Hebrew RTL receipt.
- `receipt-flow.gif`: browser interaction opening the receipt and child history.

`npm run build` completed successfully. Existing duplicate `message_type` key
warnings and dependency warnings remain; they are not build failures. New
backend files pass Ruff; `git diff --check` passes. All ten locale catalogs
contain the 12 new keys, without increasing their existing unrelated key drift.

## What this proves / limits

The deterministic loop verifies runtime delivery, isolation, question handling,
and serial parent scheduling on both databases. The live loop verifies a real
model can discover and use the protocol, and the UI displays the actual stored
messages. Sending never guarantees a model will comply; delivery means planner
context inclusion. Stop/deadline remain authoritative, and ended children are
not resumed. Browser evidence uses seeded data only.


## Loop C — natural user language, no tool names

The earlier live prompt prescribed the protocol. This follow-up checks whether
Luna chooses messaging from an ordinary request. No production prompt or tool
metadata was changed between these runs.

Initial request (report `d0b3fb58-c20e-4191-96b1-b9e8bc95e30c`):

> Prepare a launch-readiness pack for our customer-support pilot: 120 customers,
> a Friday launch, two support reps during business hours, no weekend coverage,
> and manual rollback if ticket volume doubles. Have a sub-agent independently
> review the plan and save three separate notes we can share with the owners:
> staffing and escalation, rollback and incident response, and customer
> communications and success measures. Each note should distinguish known facts
> from assumptions and include open decisions, concrete risks, and a proposed
> checklist. You handle the overall go/no-go brief. Use only the facts here.

While the reviewer was running, submit this update and click **Send now**:

> Update: we are moving the launch to Monday and adding a third support rep for
> the first two days. Please tell the reviewer now so all three notes reflect that.

This is a normal scope correction, with no tool name, recipient ID, message kind,
question ID, or instructions about receipt wording.

The first, shorter attempt (report `075842fb-5300-44d4-b18d-c7e2d4b9356c`)
requested a review without the three separate notes. Its child finished in five
seconds before the queued update ran. Luna independently selected messaging,
but the terminal-child guard rejected the send; it then delegated a fresh review.
That attempt demonstrates tool discovery and terminal rejection, not successful
live delivery. It also showed redundant parent polling; it is not evidence of
perfect orchestration. The second attempt uses realistic separate deliverables
and the existing Send now control to exercise a direction during active work.


Observed second-run result: Luna selected the messaging tool twice without
user guidance about tools. Both directions were accepted while the child was
running, but arrived during its final step. The durable history marked both
**undelivered** when the child finished (four steps, 26 seconds). The parent
subsequently edited the three notes itself and produced a Monday/third-rep
brief. This proves natural tool discovery and truthful delivery bookkeeping;
it does **not** prove that a live child consumed and acted on this correction.
The parent also sent a redundant second direction and scheduled a redundant
one-minute wait; the wait was cancelled through the UI after fan-in was already
processing. These are observed orchestration rough edges, not a clean full pass.

Evidence: `media/pr/sub-agent-messaging/natural-receipt.jpg` and
`media/pr/sub-agent-messaging/natural-history.jpg`. The latter explicitly shows
both messages as Not delivered. The live session is saved in the sandbox;
no production implementation changes were made during this follow-up.


## Finish-race fix and transcript refresh regression

`record_finish` now serializes with sends using report → wave → child locks.
A pending inbound message refuses a normal finish and requires another planner
step; that step delivers the direction before the child can finish again.
A send losing the lock race to a completed finish is rejected. Stop, deadline,
and budget exhaustion remain authoritative and may leave mail undelivered.
A finish arriving after stop cannot overwrite the stopped record. The finish
tool uses a separate short session so refusing a finish cannot roll back the
runner's pending tool/block writes.

Luna's provider-boundary regression held a child finish response in flight,
sent a direction through the real service, then released the stale finish. It
failed before the guard and passed after: the direction appeared in a subsequent
planner request and was delivered before the child finished. All **8 messaging
tests pass on SQLite and PostgreSQL**, including late finish after stop.

The natural run also exposed duplicate transcript turns when a canonical steer
row overlapped optimistic user/system placeholders. Refresh now matches the
placeholder's server completion ID and replaces the paired user message too,
while preserving genuinely older pagination history and stable rendering keys.
Two regressions (steering and fan-in overlaps) failed before and passed after;
`node --test frontend/tests/unit/kickoff-stream.test.mjs`: **8 passed**.

Parent guidance and send/read observations now explicitly say to confirm one
send and end the turn, relying on automatic wave results rather than repeating
messages, polling, or scheduling a redundant wait. `read_sub_agent` includes
stored delivery history so the parent can distinguish sent/delivered/undelivered.


Prompt guidance alone did not reliably prevent Luna from resending or polling
in the next natural repeat. The runtime now makes a successful parent direction
a handoff boundary: its next planner call has no tools and writes a short normal
confirmation; remaining work continues when the automatic wave result arrives.
A new user steering message restores tool use immediately. Answer messages do
not impose this boundary, so a parent can answer several waiting children.
This intended behavior prevents polling after a correction without prematurely
terminating the child's run or synthesizing an English-only receipt as the answer.


### Final natural repeat — passed

Report `419a2ef9-9bef-4b36-933f-e401d6e37f46`, same initial request and same
Monday/third-rep update above, submitted through the browser and Send now.
Observed **one direction, delivered**, followed by the parent's confirmation
and end of turn. No post-send status polling or scheduled wait in that turn.
The child finished after six steps and 48 seconds. Its three saved notes were
independently read from the sandbox database: all three contain Monday and the
additional rep. The automatic fan-in produced the final brief using those facts.
Evidence: `natural-fixed-receipt.jpg`, `natural-fixed-history.jpg` under the same
media directory. The test uses no tool names, IDs, protocol fields, or prescribed
receipt wording in the user's prompts. This final pass does not erase the prior
failed attempts documented above.


## Final verification totals

- Combined SQLite messaging/delegation/queue/prompt suite: **27 passed** (41.35s).
- Messaging suite on a fresh disposable PostgreSQL database: **9 passed** (32.98s); database dropped.
- Frontend kickoff/refresh regression suite: **8 passed**.
- Production frontend build: **passed**; deprecation/dependency and duplicate-key warnings logged.
- New backend files and finish/read tools plus messaging tests: **Ruff passed**.
- `git diff --check`: **passed**.
- Parent handoff regression: disabling the guard offered 43 tools and failed the
  empty-catalog assertion; restoring it passed. A fresh user steer restores tools.
- No credentials were committed; live verification proxy credential was cleared.

The backend suite logs dependency deprecations and aiosqlite teardown warnings;
none caused a test failure. These warnings were not exhaustively triaged here.
