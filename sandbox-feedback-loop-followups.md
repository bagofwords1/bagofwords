# Sandbox Feedback Loop — Follow-up Suggestions

Validates the new **follow-up suggestions** feature end-to-end against a fresh
cloud sandbox with a **real Anthropic key**:

> After each answer in the **web** app, when the org setting **Follow-up
> suggestions** is enabled, the agent proposes a few follow-up questions —
> generated on the **small/default model** — rendered as minimalist chips
> **below** the thumbs-up/down bar (without blocking it).

Three gates, all first-class primitives already in the codebase:

| Gate | Mechanism | Where |
|---|---|---|
| Web session only | `platform is None` (Slack/Teams/Email/Excel/scheduled carry a non-null platform) | `agent_v2._follow_ups_enabled()` |
| Org setting on | `enable_follow_ups` FeatureConfig | `organization_settings_schema.py` |
| Small model | `get_default_model(is_small=True)` → `self.small_model` → `Reporter` | `agent_v2`, `reporter.py` |

---

## What changed

**Backend**
- `models/completion.py` — new nullable JSON column `follow_ups` (durable store).
- `alembic/versions/followups01_add_follow_ups.py` — adds the column **and**
  merges the two pre-existing heads (`judge0001`, `usravatar01`) into one.
- `schemas/organization_settings_schema.py` — `enable_follow_ups` FeatureConfig
  (auto-surfaces in the admin AI-settings UI; `_sync_new_features` backfills
  existing orgs, no data migration needed).
- `ai/agents/reporter/reporter.py` — `generate_follow_ups()` runs on the small
  model; tolerant JSON parsing; never raises.
- `ai/agent_v2.py` — `_follow_ups_enabled()` + `_generate_and_emit_follow_ups()`.
  Generated **inline at the tail of `main_execution`** (not a fire-and-forget
  task like the title), so it's reliable: the DB session is alive, the result is
  persisted, and the `completion.follow_ups` SSE event is enqueued **before**
  `[DONE]`.
- `schemas/completion_v2_schema.py` + `services/completion_service.py` —
  `follow_ups` surfaced on the completion payload so a reload rehydrates the chips.

**Frontend**
- `components/report/FollowUpSuggestions.vue` — minimalist OpenWebUI-style chips
  (header + hairline-divided rows, `+` icon on hover).
- `composables/useOrgSettings.ts` — `isFollowUpsEnabled` flag.
- `pages/reports/[id]/index.vue` — handles the `completion.follow_ups` SSE event,
  maps the persisted field on reload, and renders the component **below** the
  feedback bar on the latest message (gated on `isFollowUpsEnabled`).

### Why inline, not fire-and-forget (the title-generation bug)

Report-title generation is unreliable because it is spawned as
`asyncio.create_task(...)` **after** `completion.finished`, is never registered
in `_drain_bg_writes`, and is swallowed by a broad `except` — so the request can
tear down before it runs. Follow-ups deliberately avoid that: they're `await`ed
within the live run, persisted, and the SSE event rides the still-open stream.

---

## Environment setup (fresh sandbox)

App targets **Python 3.12**. See `docs/design/sandbox-feedback-loop.md` for the
canonical runbook. Short version:

```bash
cd backend
uv sync --extra dev
cat > .env <<'EOF'
BOW_DATABASE_URL=sqlite:///db/app.db
BOW_ENCRYPTION_KEY=<fernet key: python -c "from cryptography.fernet import Fernet;print(Fernet.generate_key().decode())">
BOW_SMTP_PASSWORD=dummy
EOF
mkdir -p db uploads/files uploads/branding
.venv/bin/alembic upgrade head        # applies followups01 (single head)
.venv/bin/python main.py &            # backend on :8000

cd ../frontend && yarn install && yarn dev &   # frontend on :3000
```

Bootstrap a sandbox org with the **real Anthropic key** (default = Sonnet,
**small_default = Haiku**):

```bash
# register + login (see docs/design/sandbox-feedback-loop.md), then:
curl -X POST :8000/api/llm/providers -H "$AUTH" -d '{
  "name":"Anthropic","provider_type":"anthropic",
  "credentials":{"api_key":"sk-ant-..."},
  "models":[
    {"name":"Claude 4.6 Sonnet","model_id":"claude-sonnet-4-6","is_default":true},
    {"name":"Claude 4.5 Haiku","model_id":"claude-haiku-4-5-20251001","is_small_default":true}
  ]}'
# the create path applies its own flag defaults; pin Haiku as small via SQL:
sqlite3 db/app.db "UPDATE llm_models SET is_small_default=(model_id='claude-haiku-4-5-20251001')"
# install chinook demo + create a report (see runbook)
```

---

## Loop A — Backend, live LLM (no browser needed)

Stream a real completion and assert the event + persistence:

```bash
curl -sN -X POST ":8000/api/reports/$RID/completions" -H "$AUTH" \
  -d '{"prompt":{"content":"What are the top 5 artists by total sales revenue?","mode":"chat","platform":null},"stream":true}' > /tmp/sse.log

grep -nE "completion.follow_ups|\[DONE\]" /tmp/sse.log
sqlite3 db/app.db "SELECT role, follow_ups FROM completions WHERE follow_ups IS NOT NULL ORDER BY created_at DESC LIMIT 1"
```

**Observed (PASS, real Haiku output):**

```
event: completion.follow_ups   # line 154
data: {"questions":[
  "How many tracks and albums does Iron Maiden have in the catalog?",
  "What's the average revenue per track for these top 5 artists?",
  "Which genres do these top 5 artists belong to?",
  "How does Iron Maiden's revenue compare to the bottom 5 artists?",
  "What's the total number of purchases for each of these artists?"]}
data: [DONE]                    # line 163  → event lands BEFORE [DONE] (instant)

# persisted on the completion row (durable across reload):
system -> ["How many tracks and albums does Iron Maiden have ...", ...]
```

Gate checks:
- `platform:null` → web → generated. A non-null platform (`"excel"`/`"slack"`)
  skips generation.
- `enable_follow_ups=false` in org settings → no event, no persistence.

## Loop B — Frontend, real screenshots

```bash
.venv/bin/python -m playwright install chromium
.venv/bin/python scripts/ui_followups_shots.py
# -> /tmp/followup_shots/{01_followups_on,02_followups_closeup,03_followups_off}.png
```

`scripts/ui_followups_shots.py` logs in through the real UI, opens the report
(persisted follow_ups rehydrate on load), screenshots the chips **below** the
thumbs, then flips `enable_follow_ups` OFF via the settings API, reloads, and
screenshots that the chips are gone — proving the enable/disable gate end-to-end.

---

## What this proves

- Real small-model (Haiku) generation produces relevant, context-aware
  follow-ups from the actual conversation.
- The SSE event is delivered **in-stream before `[DONE]`** (instant) **and**
  the questions are persisted on the completion (survive reload).
- The web-only and org-setting gates both work.
- The chips render below the feedback bar without blocking the thumbs.
