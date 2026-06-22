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

## Loop D — Live LLM run + delivery to mock Teams (real Anthropic key)  ✅

Configures a real Anthropic model for the org, creates a catalog prompt,
subscribes with `channel=teams`, and runs the real scheduled path. A genuine
agent response is generated and delivered into the mock Teams outbox.

```bash
cd backend
export BOW_DATABASE_URL="sqlite:///db/app.db" BOW_CHANNELS_MOCK=1
export BOW_CHANNELS_MOCK_FILE=/tmp/loop_d_outbox.json
export ANTHROPIC_API_KEY=sk-ant-...                 # real key, env only
export BOW_ANTHROPIC_MODEL=claude-haiku-4-5-20251001 # a model the account serves
.venv/bin/python scripts/loop_d_live_anthropic.py
```

**Observed (PASS):**

```
[setup] org=... model=claude-haiku-4-5-20251001 subscription=... channel=teams
[run] invoking scheduled_run_prompt (live LLM)...
[result] teams deliveries: 1
[verdict] PASS — live LLM response delivered to mock Teams
mock Teams body: "*Sanity Check*\n\nHello! I am operational and ready to assist
                  with your data analysis needs."
```

Notes:
- The Anthropic key + network are confirmed working (a wrong model id returns a
  404 `not_found_error`, not a 401). Use a model the account actually serves —
  list them with `GET https://api.anthropic.com/v1/models`. For this account the
  4.x family is available (e.g. `claude-haiku-4-5-20251001`).

---

## Loop E — Playwright UI walkthrough + screenshots  ✅

Browser-drives the catalog and captures screenshots (in `docs/design/screenshots/`):
`catalog.png`, `subscribe-modal.png`, `edit-modal.png`, `assign-modal.png`.

```bash
# backend (note the sandbox config — see auth note below)
cd backend
BOW_CONFIG_PATH=$PWD/../configs/bow-config.sandbox.yaml \
  BOW_DATABASE_URL=sqlite:///db/app.db BOW_CHANNELS_MOCK=1 \
  .venv/bin/python -m uvicorn main:app --host 0.0.0.0 --port 8000
# frontend
cd frontend && npm install --legacy-peer-deps && npm run dev      # :3000
npx playwright test --project=prompts
```

**Observed (PASS):** 3/3 — catalog header/tabs/sort/filters render; a prompt
card with Try now / Subscribe / Assign / edit actions renders; the Subscribe
modal opens showing the **four-channel select (Microsoft Teams, Slack, AI
Mailbox, Email/SMTP)** and the Append / New-report run-mode toggle.

> **Auth in this sandbox.** The "`/api/auth/register` 404" is not a bug — the
> default `configs/bow-config.dev.yaml` runs `auth.mode: sso_only`, which does
> not mount the register/JWT-login routers. Point the backend at
> `configs/bow-config.sandbox.yaml` (`auth.mode: local_only`,
> `allow_uninvited_signups: true`) via `BOW_CONFIG_PATH` to enable local signup
> + login for browser-based testing. (Note `allow_multiple_organizations:false`
> means auto-org only fires for the very first org; seed a membership + admin
> role assignment to attach a user to an existing org.)
>
> Frontend `npm install` needs `--legacy-peer-deps` (a tiptap v1 peer-dep wants
> vue 2).

---

## Status

| Loop | Scope | State |
|---|---|---|
| A | Catalog logic + per-agent RBAC | ✅ passing |
| B | 4-channel delivery (mock) | ✅ passing |
| C | Scheduled run + run-mode + delivery | ✅ passing |
| C2 | HTTP API | ✅ passing |
| D | Live LLM run → mock Teams | ✅ passing (real Claude response delivered) |
| E | Playwright UI + screenshots | ✅ passing (3/3; catalog/subscribe/edit/assign captured) |
