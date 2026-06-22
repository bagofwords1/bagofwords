# Sandbox Feedback Loop — Org Prompt Catalog + Scheduled Prompt Subscriptions

Runnable feedback loop validating the new feature in a fresh cloud sandbox:
an org **prompt catalog** users can browse / "try now" / subscribe to, that
admins can **assign** (per-agent RBAC) to users/groups/all, executing **as the
target user** and delivering to a **channel** (Teams / Slack / AI mailbox /
plain SMTP), with **run modes** (append vs new report each run).

Mirrors `docs/design/sandbox-feedback-loop.md`: each loop is self-contained and
prints PASS markers; iterate on a candidate change and re-run until green.

---

## Environment setup (fresh sandbox)

App targets **Python 3.12**.

```bash
cd backend
pip install uv
uv sync --frozen --extra dev
export BOW_DATABASE_URL="sqlite:///db/app.db"
mkdir -p db
.venv/bin/alembic upgrade head        # applies migration promptsub01
```

Secrets (never commit) — used by the live loops:

```bash
export ANTHROPIC_API_KEY=...           # for the live LLM run (Loop D)
export BOW_LICENSE_KEY=...             # enterprise tier → per-agent RBAC
export BOW_CHANNELS_MOCK=1             # record channel sends to an outbox
export BOW_CHANNELS_MOCK_FILE=/tmp/bow_channel_outbox.json
```

---

## Loop A — Catalog logic & per-agent RBAC (no LLM, no live channel)

Seeds an org, an agent (data source), and three users (admin with
`manage`+`assign_prompts`; a member with agent access; a member without), then
exercises the catalog service: agent-scoped visibility, self-subscribe, admin
assign fan-out with per-user access filtering, and assign denial without
`assign_prompts`.

```bash
cd backend
export BOW_DATABASE_URL="sqlite:///db/app.db"
.venv/bin/python -m pytest tests/e2e/test_prompt_catalog_subscriptions.py -v -s
```

**Observed (PASS):**

```
[create] prompt <id> on agent <ds>
[visibility] admin=1 member_ok=1 member_no=0
[subscribe] member_ok sp=<id> runs_as=<member_ok>
[rbac] member_ok assign -> 403 (correct)
[assign-org] created=2 skipped=1
[done] total subscriptions for prompt: 3
1 passed
```

Proves: visibility is gated on access to **all** of a prompt's agents; assigning
to others requires `assign_prompts` on those agents; org/group fan-out skips
users without agent access.

---

## Loop B — Channel delivery, all four channels (mock)

Delivers a result on each channel in mock mode and asserts the outbox, including
the **plain-SMTP plain-text + human body + continue link** and chat external ids.

```bash
cd backend
export BOW_DATABASE_URL="sqlite:///db/app.db" BOW_CHANNELS_MOCK=1
.venv/bin/python -m pytest tests/e2e/test_channel_delivery.py -v -s
```

**Observed (PASS):**

```
[deliver] teams -> status=sent used=teams mock=True
[deliver] slack -> status=sent used=slack mock=True
[deliver] ai_mailbox -> status=sent used=ai_mailbox mock=True
[deliver] smtp -> status=sent used=smtp mock=True
[outbox] channels=['teams', 'slack', 'ai_mailbox', 'smtp']
[smtp] plain human body + continue link OK
[teams] external_user_id=mock-teams-<uuid>
1 passed
```

---

## Loop C — Scheduled run: run-mode + delivery (LLM stubbed)

Drives `scheduled_run_prompt` with the LLM stubbed: `new_report` clones a fresh
report grouped under the task and delivers to the channel; `append` reuses the
anchor report.

```bash
cd backend
export BOW_DATABASE_URL="sqlite:///db/app.db" BOW_CHANNELS_MOCK=1
.venv/bin/python -m pytest tests/e2e/test_scheduled_run_delivery.py -v -s
```

**Observed (PASS):**

```
[new_report] run reports created: 1
[delivery] teams deliveries: 1
[delivery] body preview: '*Daily Ops — ...*\n\nOps are healthy. 3 incidents...'
[append] reused anchor report; smtp delivery recorded
2 passed
```

---

## Loop C2 — HTTP API (real ASGI app)

Exercises the routers/schemas over HTTP (auth injected via dependency overrides,
since the signup route is unavailable under the sandbox config — pre-existing).

```bash
cd backend
export BOW_DATABASE_URL="sqlite:///db/app.db"
.venv/bin/python -m pytest tests/prompts/test_prompt_catalog_api.py -v -s
```

**Observed (PASS):** create → list(top) → get → update → subscribe →
subscriber_count=1; cross-user private visibility blocked. `2 passed`.

---

## Loop D — Live LLM run + delivery to mock Teams (real Anthropic key)  [planned]

With `ANTHROPIC_API_KEY` + an Anthropic LLM model configured for the org, run a
real catalog prompt (`POST /api/prompts/{id}/run` or a triggered subscription)
and confirm a real agent response is generated and delivered into the mock Teams
outbox. Verifies the full path with a real model.

---

## Loop E — Playwright UI walkthrough + screenshots  [in progress]

Browser-drives the catalog: browse / top prompts, "try now", subscribe modal
(schedule + channel + run-mode), admin assign modal (user/group/all), and
my-subscriptions; captures screenshots at each step. Config:
`frontend/playwright.config.ts`.

---

## Status

| Loop | Scope | State |
|---|---|---|
| A | Catalog logic + per-agent RBAC | ✅ passing |
| B | 4-channel delivery (mock) | ✅ passing |
| C | Scheduled run + run-mode + delivery | ✅ passing |
| C2 | HTTP API | ✅ passing |
| D | Live LLM run → mock Teams | ⏳ planned |
| E | Playwright UI + screenshots | ⏳ in progress |
