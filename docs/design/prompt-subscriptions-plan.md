# Org Prompt Catalog + Scheduled Prompt Subscriptions — Implementation Plan

Status: **proposed / awaiting approval**. Branch: `claude/scheduled-prompt-subscriptions-6gwu3y`.

---

## 1. Goal & scope

Let admins curate a catalog of **prompts** (tied to one or more agents/data sources),
let users **browse** them (incl. "top prompts") and **run one now** or **subscribe**
(recurring), and let privileged users **assign** a prompt to a user / group / all users
as a recurring **scheduled task** whose result is **delivered to a channel** (Teams,
Slack, AI mailbox, plain SMTP). Each run executes **as the target user** (their data
access, their context), and replies on conversational channels continue the same report.

This **reuses the existing scheduled-tasks engine** (`ScheduledPrompt` + APScheduler) —
no parallel scheduler — and **reuses the existing `Prompt` model** as the catalog.

### In scope
- `Prompt` becomes an org **catalog item + execution spec** (content, mode, model, mentions, agents).
- Prompt ↔ data_source is **many-to-many** (a prompt can span multiple agents).
- Browse catalog + **top prompts**; **run now** (one-off); **subscribe** (self).
- **Assign** to user / group / all → per-user `ScheduledPrompt` rows (RBAC-gated, per agent).
- Per-task **run mode**: `append` (same report) or `new_report` (fresh report each run).
- **Channel delivery**: Teams, Slack, AI mailbox, plain SMTP — with reply-continuation.
- **Conversation starters** are absorbed into the catalog (`Prompt.is_starter`).
- Enterprise **RBAC** scoped per agent; verified under the provided enterprise license.
- Wide automated + **live-sandbox** verification (Playwright + screenshots + mock Teams).

### Out of scope (documented future extensions)
- Mid-report **"also send this answer to Teams"** checkbox (explicit user action; same delivery service).
- WhatsApp in the channel picker (adapter exists; not offered for now).
- Per-subscription model/mode override UI beyond the prompt defaults (data model will allow it; UI later).

---

## 2. Architecture summary

Two reused models + one new association + one delivery service:

```
Prompt  (catalog item + execution spec, org-scoped)
  ├─ M2M → data_source        (the agents; drives visibility + access)
  ├─ content, mode, model_id, mentions   (the execution spec — PromptSchema-compatible)
  └─ scope, is_starter, status, default_cron, default_channel, category/tags

ScheduledPrompt  (the subscription / run instance — EXISTING, extended)
  ├─ prompt_id → Prompt        (live reference; falls back to frozen `prompt` JSON if null)
  ├─ user_id                   (runs AS this user)
  ├─ channel, run_mode, created_by
  └─ cron_schedule, is_active, last_run_at   (existing)

ChannelDeliveryService  (NEW — the only genuinely new subsystem)
  resolve channel → send via platform adapter / email → persist conversation ref for replies
```

Execution flow on fire (reuses today's path):
`APScheduler → scheduled_run_prompt(sp_id) → claim_scheduled_run (dedup) →
resolve user+org+prompt spec → run_mode branch (append | clone new report) →
create_completion(current_user=user) → ChannelDeliveryService.deliver(result, user, channel)`

---

## 3. Data model & migrations

### 3.1 `Prompt` (extend `backend/app/models/prompt.py`)
| Column | Type | Notes |
|---|---|---|
| `content` | Text | rename/added from `text`; the instruction |
| `title` | String | existing |
| `organization_id` | FK | existing |
| `created_by` | FK users | rename from `user_id` (author) |
| `mode` | String | `chat` \| `deep` \| `training` (default `chat`) |
| `model_id` | FK llm_model, nullable | LLM override; null = org default |
| `mentions` | JSON, nullable | pinned context (files/instructions/entities) |
| `scope` | String | `private` (creator only) \| `agent` (all with data_source access) |
| `is_starter` | Boolean | surface as a home-screen conversation starter |
| `status` | String | `draft` \| `published` |
| `default_cron` | String, nullable | suggested schedule |
| `default_channel` | String, nullable | suggested channel |
| `category` / `tags` | String / JSON, nullable | browsing |

### 3.2 `prompt_data_source_association` (NEW M2M)
Mirror `report_data_source_association`. `Prompt.data_sources` ⇄ `DataSource.prompts`.

### 3.3 `ScheduledPrompt` (extend `backend/app/models/scheduled_prompt.py`)
| Column | Type | Notes |
|---|---|---|
| `prompt_id` | FK prompts, nullable | catalog link; null = legacy ad-hoc task |
| `channel` | String | `teams` \| `slack` \| `ai_mailbox` \| `smtp` |
| `run_mode` | String | `append` (default; preserves current behavior) \| `new_report` |
| `created_by` | FK users, nullable | distinguishes self-subscribe vs admin-assign |

### 3.4 `Report` (extend, optional but recommended)
`source_scheduled_prompt_id` (nullable FK) — groups `new_report` runs under their task for listing/retention.

### 3.5 Migrations (`backend/alembic/versions/`)
- One revision adding the columns + association table, with **sqlite + postgres** dialect branches (batch_alter for sqlite), following existing migration style.
- **Backfill**: materialize each `data_source.conversation_starters` string into a `Prompt` row (`is_starter=true`, `scope=agent`, linked via M2M). Idempotent.
- Tighten the existing thin `prompt_service`/route: add **org scoping**, fix `int` vs `UUID` id mismatches, add visibility filtering.

---

## 4. RBAC (per-agent, enterprise)

Per-agent resource permission, **not** org-level (consistent with `manage_instructions`/`manage_evals`).

- Add to `RESOURCE_PERMISSIONS["data_source"]` and `RESOURCE_SCOPED_GROUPS["data_source"]`:
  - **`assign_prompts`** — push an agent's prompts to *other* users/groups (the privileged action).
  - Authoring/editing prompts folds into the existing **`manage`** on data_source (no extra checkbox) — unless we decide to split out `manage_prompts`.
- **No** `ORG_PERM_IMPLIES_RESOURCE` entry (keeps it per-agent, not "general"). `FULL_ADMIN` still bypasses.

| Action | Check |
|---|---|
| See a prompt | `has_resource_membership('data_source', ds)` for **all** the prompt's agents |
| Subscribe myself | same as see (no special perm) |
| Author/edit prompt | `manage` on all the prompt's agents |
| Assign to others | `assign_prompts` on all the prompt's agents |
| Assign fan-out | skip target users who lack access to all the prompt's agents |

The role editor already renders resource-scoped permissions, so `assign_prompts` shows up
automatically. Enterprise gating verified against the provided license (`BOW_LICENSE_KEY`).

---

## 5. Backend services

- **`PromptCatalogService`** — list (visibility + `sort=top`), get, create/update/delete, publish/draft,
  starter materialization. "Top" = aggregate of active subscriptions (`COUNT ScheduledPrompt WHERE prompt_id`)
  + recent runs; org-global ranking to start.
- **Subscription logic** (extend `ScheduledPromptService`) —
  - `subscribe(prompt, user, cron, channel, run_mode)` → one `ScheduledPrompt`.
  - `assign(prompt, principal, …)` → **expand** principal (user | group | org) to users, filter by agent access, create one row each.
  - `trigger_now(prompt, user)` → fresh report + `create_completion` (no scheduled row).
- **`ChannelDeliveryService`** (NEW) —
  - `resolve(user, channel)` → org-enabled ∩ user-enabled; fallback **email → skip**.
  - `deliver(completion_result, user, channel)` → send via platform adapter / email; **persist conversation reference** (thread/channel/user-mapping) on the report/completion so the existing inbound manager routes replies.
  - **Plain-SMTP path**: plain-text, human-sounding body (new renderer, no HTML template) + **"continue this discussion" deep link**.
  - **AI-mailbox path**: conversational email (reply via IMAP poller continues report).

---

## 6. API endpoints

```
GET    /prompts                       # filter scope/agent/category; sort=top  (visibility-filtered)
GET    /prompts/{id}
POST   /prompts                       # author            (manage on agents)
PUT    /prompts/{id} / DELETE         #                   (manage on agents)
POST   /prompts/{id}/publish
POST   /prompts/{id}/run              # "try it now" one-off (see access)
POST   /prompts/{id}/subscribe        # self  { cron, channel, run_mode }   (see access)
POST   /prompts/{id}/assign           # { principal: user|group|org, cron, channel, run_mode }  (assign_prompts)
GET    /me/scheduled-prompts          # my subscriptions
PUT/DELETE existing scheduled-prompt endpoints (extended with channel/run_mode)
```
`whoami` exposes the new resource permissions to the frontend.

---

## 7. Execution & run mode

`scheduled_run_prompt` changes:
1. dedup via `claim_scheduled_run` (unchanged).
2. resolve `user`, `org`, and the **prompt spec** — live from `prompt_id` (admin edits propagate) or frozen `prompt` JSON (legacy).
3. **run_mode**: `append` → reuse report; `new_report` → clone a fresh `Report` (inherit mode/visibility/owner, attach the prompt's `data_sources`, titled e.g. *"Weekly Marketing Forecast — 2026-06-22"*, set `source_scheduled_prompt_id`).
4. `create_completion(current_user=user, prompt=spec)` (unchanged path).
5. `ChannelDeliveryService.deliver(...)`.

Retention: note for `new_report` sprawl — list under the task; optional auto-archive later.

---

## 8. Channel delivery & reply matrix

| Channel | Send | Reply / continue |
|---|---|---|
| Teams | proactive DM/thread (needs stored conversation reference) | reply in-thread → continues report as user |
| Slack | DM/thread | reply in-thread → continues report as user |
| AI mailbox | conversational email | reply by email → IMAP poller → continues report |
| Plain SMTP | **plain-text, human-like, no template** | **no inbound** → body includes **continue-discussion deep link** |

Riskiest dependency to confirm early: **Teams proactive *initiate*** (not just reply). If the
adapter can't initiate, the daily-to-everyone Teams push needs a stored `conversationReference`
captured at link time; we verify against the **mock Teams** harness and document the requirement.

---

## 9. Conversation starters migration
- Backfill manifest/`data_source.conversation_starters` → `Prompt` rows (`is_starter`, `scope=agent`).
- Repoint `frontend/components/DataSourceQuestionsHome.vue` to query starter prompts.
- Keep `conversation_starters` as the **manifest import format**; `agent_yaml_service` import materializes/updates `Prompt` rows.

---

## 10. Frontend (Nuxt/Vue)
- **Catalog view**: browse prompts for accessible agents, "top prompts" section, category filter.
- **Prompt detail**: *Try it now* (streams a one-off run), *Subscribe* modal (schedule + channel picker, gated to enabled channels).
- **Admin author**: prompt editor (content, mode, model, agents multi-select, mentions, starter/publish).
- **Assign modal** (gated by `assign_prompts`): pick user / group / all + schedule + channel.
- **My subscriptions**: list, pause, edit channel/schedule, unsubscribe.
- Role editor: `assign_prompts` appears automatically under the agent's resource permissions.

---

## 11. Mock Teams harness
- A test double capturing outbound channel messages (mock Teams endpoint/adapter), toggled by env,
  so tests + Playwright can **assert delivery and screenshot** the "sent to Teams channel" result
  without a real tenant. Optionally drive content generation with the provided **Anthropic key**.

---

## 12. Testing strategy (the core deliverable — verify everything live)

Mirrors `docs/design/sandbox-feedback-loop.md`: runnable loops, observed PASS outputs, iterate to green.

- **Unit** (`backend/tests/unit/`): channel resolver + fallback; plain-SMTP renderer; principal expansion; RBAC visibility/assign across multiple agents; starter materialization; run_mode branch.
- **Integration / e2e-pytest** (`backend/tests/e2e/`, like the Fabric repro): subscribe → scheduled run → deliver to **mock Teams**; assign-to-group fan-out (incl. access filtering); RBAC enforcement under the **enterprise license**; reply-continuation routing.
- **Playwright + screenshots** (`frontend/tests/`): catalog browse, top prompts, try-now stream, subscribe modal, admin author + assign, my-subscriptions, role-editor `assign_prompts`, delivery viewer. Screenshots captured at each step.
- **Live sandbox feedback loop** (new doc, §13): boot backend (uv + sqlite) + frontend, apply license, run real flows, deliver to mock Teams using the Anthropic key, capture screenshots, iterate until 100%.

### 12.1 Wide test-case matrix (sample — full list in the loop doc)
| # | Case | Expect |
|---|---|---|
| 1 | User browses catalog, only sees prompts for agents they can access | hidden otherwise |
| 2 | Multi-agent prompt; user lacks one agent | not visible / not runnable |
| 3 | Try-now one-off run as user | report created, streamed, no schedule row |
| 4 | Subscribe self, weekly, channel=Teams | row created, job registered, fires |
| 5 | Admin assign to group "Marketing" | N rows, one per accessible member |
| 6 | Assign to all; some users lack Teams mapping | those fall back to email or skip |
| 7 | run_mode=append | same report grows |
| 8 | run_mode=new_report | fresh report per run, grouped under task |
| 9 | Plain SMTP delivery | plain text, human tone, continue link present |
| 10 | AI-mailbox reply | continues same report as user |
| 11 | Teams reply in thread | continues same report as user |
| 12 | Non-privileged user tries `/assign` | 403 |
| 13 | `assign_prompts` on Agent A only, assigns Agent B prompt | 403 |
| 14 | Enterprise license absent | RBAC/feature gated correctly |
| 15 | Conversation starters appear from catalog on home | parity with old strings |
| 16 | Top prompts ranking reflects subscriptions | order correct |
| 17 | Scheduler dedup across workers | exactly one run |
| 18 | Channel offered only if org-enabled | picker filtered |

---

## 13. Sandbox feedback-loop doc (to be authored)
`docs/design/prompt-subscriptions-feedback-loop.md` — runnable loops with observed PASS blocks:
- **Loop A** — app-logic (seeded data, no live channel): subscribe/assign/run-mode/RBAC asserts.
- **Loop B** — live delivery to **mock Teams** + plain-SMTP capture, content via Anthropic key.
- **Loop C** — Playwright UI walkthrough with screenshots.

---

## 14. Secrets, license & env
- `ANTHROPIC_API_KEY`, `BOW_LICENSE_KEY` provided **via gitignored env only** — never tracked/committed.
- Recommend **rotating both** after testing (shared in plaintext chat).
- `BOW_DATABASE_URL=sqlite:///db/app.db` for the sandbox (per the existing loop doc).

---

## 15. Phasing / sequencing
1. **Models + migrations + RBAC + catalog API + subscribe/trigger** (channels limited to email/skip) + unit + first Playwright.
2. **ChannelDeliveryService** (Teams/Slack/AI mailbox/plain SMTP) + reply threading + mock Teams + Loop B.
3. **Admin assign** (group/all fan-out + access filtering) + **starters migration** + top-prompts ranking.
4. **Full live verification**: run the sandbox loops, capture screenshots, iterate to 100%.

Each phase: implement → unit/integration green → Playwright + screenshots → live-loop verify → iterate.

---

## 16. Definition of done ("100% perfect")
- All matrix cases pass in unit + integration.
- Live sandbox: every flow verified in-product with screenshots (browse, try-now, subscribe, assign, delivery to mock Teams, plain-SMTP body + link, reply-continuation).
- Enterprise RBAC verified under the provided license (per-agent `assign_prompts`, visibility, fan-out filtering).
- Conversation starters at parity via the catalog.
- No regressions in existing scheduled-prompt / completion / data-source tests.
- Branch pushed; (PR only if you ask).
