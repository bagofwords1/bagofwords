# Feedback Loop — scheduled agent emailed the same user twice

A daily Product/PostHog report emailed its owner twice during one scheduled
execution on 2026-10-04. The goal is one outbound agent email attempt per
recipient per execution, including after a worker resumes or the agent does
other work between send actions. A new execution may email the same recipient.

## Root cause (validated)

Production showed one scheduled completion and one agent execution for the
affected daily report. The run called `send_email` twice,
at 05:01:21 and 05:02:09 UTC. Both calls succeeded and used byte-identical
arguments. The other scheduler worker logged `already claimed — skipping` at
05:00:00 UTC, so this was not a second scheduled run.

The agent performed `create_data` actions between the sends. The repeat guard
in `backend/app/ai/agent_v2.py` examines only a trailing streak of identical
tool calls, and does so after dispatch. `EmailSendService.send` previously
contacted the outbound provider without an execution/recipient claim. Thus the
second send was accepted.

## Loop A — deterministic reproduction

From `backend/` with the repository's Python 3.12 environment:

```bash
BOW_DATABASE_URL=sqlite:///db/app.db uv run python -m pytest \
  tests/e2e/test_agent_email_delivery.py -q --disable-warnings
```

The test seeds two executions and stubs only the outbound email provider. It
sends twice to the same recipient in separate DB sessions, then tests a
different recipient, the next execution, and an uncertain provider timeout.

With the new pre-send guard temporarily disabled, the first test failed at:

```text
assert not duplicate.success
E AssertionError: assert not True
1 failed
```

With the guard restored:

```text
2 passed
```

## The fix

`EmailSendService.send` persists a unique `(agent_execution_id, normalized
recipient)` claim before contacting the provider. Both the agent `send_email`
tool and the email fallback of `notify` pass the execution id to this shared
service. A duplicate returns a failed tool result without contacting the
provider. The unique constraint survives process restarts and resolves
concurrent claims. A claim remains after provider errors because a timeout can
occur after the provider accepted the message.

## What this proves / regression notes

- The same run cannot send twice to the same recipient, even with changed
  content and separate DB sessions.
- Other recipients and later executions remain eligible.
- An uncertain provider outcome is not retried automatically in the same run.
- The guard applies to agent-generated emails with an execution id; ordinary
  MCP or user-triggered sends without an execution id retain their existing
  behavior.
- No live mail provider was contacted during the test.
