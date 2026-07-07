# Feedback Loop — Triggers: user-owned webhooks that spawn agent sessions

Validates the trigger feature end-to-end (design: `docs/design/agent-triggers.md`):
a user-owned webhook (a `Webhook` row with `report_id NULL`) that, on each
verified + accepted delivery, spawns a NEW session (report) owned by the
trigger's creator, attached to the trigger's agents, and runs the agent with
the trigger's task template / mode / model. Includes the AI classifier gate
(decline = no orphan report), delivery dedup, owner-scoped CRUD, the
`/automations` page (Scheduled + Triggers tabs), and the ⚡ origin indicator
in the reports list.

## Loop A — deterministic pytest (no LLM, clean sandbox)

Model-dependent steps are stubbed (`CompletionService.create_completion`
recorded; `WebhookClassifier.classify` canned) — precedent:
`tests/e2e/test_scheduled_prompt.py`.

```bash
cd backend
source .venv/bin/activate   # python3.12 venv, `uv sync --frozen --extra dev`
TESTING=true BOW_DATABASE_URL="sqlite:///db/app.db" BOW_SMTP_PASSWORD="dummy" \
  python -m pytest tests/e2e/test_triggers.py -q --db=sqlite --disable-warnings
```

Observed: `9 passed` —

1. `test_trigger_crud_and_run_spec` — create (secret shown once, delivery URL,
   run spec persisted), list, update, rotate, delete.
2. `test_triggers_are_private_to_their_owner` — same-org member sees an empty
   list and gets **404** (not 403 — no existence leak) on read/update/rotate/delete.
3. `test_trigger_rejects_inaccessible_agents` — 403 on agents the creator
   can't access.
4. `test_trigger_delivery_spawns_session` — delivery → new report stamped with
   `webhook_id`, agents attached, event entry in chat, agent run received
   `<task>{template}</task><inbound_event …>` + the trigger's mode/model,
   `/api/triggers/{id}/runs` shows the run.
5. `test_trigger_delivery_dedup` — same `X-BOW-Delivery` twice → one session.
6. `test_classifier_decline_leaves_no_orphan_report` — classification happens
   BEFORE spawning; declined events create nothing; on act, the template wins
   over the classifier-authored task.
7. `test_classifier_enabled_without_model_skips_delivery` — no LLM configured
   → safe skip.
8. `test_receiver_auth_for_triggers` — 401 wrong secret, 404 unknown token,
   200 accepted, 404 after deactivation.
9. `test_report_bound_webhook_unchanged` — legacy report-bound webhooks are
   byte-for-byte unaffected (event into the bound report, no spawn, not
   listed under `/api/triggers`).

## Loop B — live end-to-end (real LLM + UI)

```bash
tools/agent/boot_stack.sh --dev
# seed org (registration auto-creates one) + install the chinook demo source
# configure an Anthropic provider via POST /api/llm/providers
#   (claude-sonnet-4-6 default, claude-haiku-4-5 small default for the classifier)
```

1. **UI**: `/automations` → Triggers tab → New trigger: name "Production
   alerts", agent = Music Store, task template, classifier ON with guidance
   "Act only on real production alerts (P1/P2); ignore heartbeats". Secret +
   delivery URL revealed once. (Playwright screenshots.)
2. **Dispatcher** (simulates the external alert system):
   ```bash
   python tools/trigger_dispatcher.py --url http://localhost:8000/webhooks/whk_… \
     --secret whsec_… --sample heartbeat --delivery-id e2e-heartbeat-1
   ```
   Observed: classifier DECLINED (`act=False conf=0.99 reason=Event is a
   heartbeat/uptime check…`), `run_count` stayed 0, no orphan report.
3. ```bash
   python tools/trigger_dispatcher.py --url … --secret … --sample alert \
     --delivery-id e2e-alert-1
   ```
   Observed: classifier acted → session "alert: High error rate on
   checkout-service" spawned with the Music Store agent attached → agent ran
   (queried invoice activity, honestly reported the demo data can't explain a
   checkout-service alert, recommended observability sources) → run status
   `success` in ~40s → run visible in the trigger's history drawer.
4. Re-sent the SAME delivery id → `total runs: 1` (live dedup) and a
   "duplicate delivery — skipping" backend log line.
5. **UI verification** (screenshots): run-history drawer with the green run;
   `/reports` shows the ⚡ amber lightning on the spawned session; the session
   itself shows the inbound event + the agent's analysis.

## Root-cause-of-design notes (for future readers)

- Classification runs BEFORE report creation in spawn mode specifically so a
  declined delivery leaves no orphan session (`webhook_service.py`,
  `_process_trigger_delivery` step 1).
- `reports.webhook_id` is a plain indexed string, NOT an FK — an FK would be
  circular with `webhooks.report_id`.
- Delivery URL host comes from `bow_config.base_url`; in sandboxes it may
  render as `0.0.0.0:3000` — POST to the backend (`:8000`) directly, the
  `/webhooks/{token}` route lives there (the `:3000` proxy only forwards `/api`).

## Round 2 — scheduled report-per-run routing + PromptBoxV2 trigger modal

Added in the same branch (plan §6.3 / §6.4):

- **ScheduledPrompt.spawn_new_report** (migration `trig0002`): per-task
  "Results" routing — `This report` (default, keeps cross-run memory) vs
  `New report per run` (fresh dated report per run, stamped with
  `reports.scheduled_prompt_id` → 🕐 origin icon in the reports list).
  Notifications link to the spawned run; inbox grouping stays per schedule.
- **TriggerModal composes via PromptBoxV2** in standalone (no report_id)
  mode — the standard DataSourceSelector/mode/model pills author the trigger's
  run spec; saved values verified round-tripping through the box's getters.

Loop A additions: `tests/e2e/test_scheduled_spawn_routing.py` (3 tests —
spawn routing with agents copied + provenance stamp + second run spawns a
second report; default routing regression; routing updatable). Combined run:
`test_triggers.py + test_scheduled_spawn_routing.py + test_scheduled_prompt.py`
→ 26 passed.

Loop B (live): created a trigger through the PromptBoxV2 modal (task, agents,
mode, model persisted from the box); created a scheduled task with
`New report per run`, fired it via the trigger endpoint → spawned
"Scheduled task — Jul 07, 2026" with a real agent answer (revenue by genre),
🕐 shown in the reports list next to the ⚡ trigger session.

Sandbox gotcha: `BOW_ENCRYPTION_KEY` is generated per-process when unset, so
LLM provider credentials stored before a backend restart fail to decrypt
after it ("Failed to decrypt credentials"). Pin the key in `backend/.env`
for multi-restart sandbox sessions.
