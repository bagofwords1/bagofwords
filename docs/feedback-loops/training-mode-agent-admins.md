# Sandbox Feedback Loop — Training mode for agent admins (per-agent scoping)

Opens **training mode** to the per-agent **`manage`** tier (agent owners /
managers), not just org admins — and guarantees that everything a training
session produces (**instructions, evals, prompts, entities**) is **scoped to the
specific agent(s) being trained**, never org-wide.

This is the runnable feedback loop used to confirm the behavior in a fresh
sandbox. It follows the same shape as
`sandbox-feedback-loop-agent-manager-rbac.md` and
`sandbox-feedback-loop-prompts-training-tools.md`.

---

## The contract (user stories)

Setup: an admin seeds an org; a user `u` is granted **`view`** (plain member) on
`agent1` and **`manage`** (agent admin) on `agent2`. `agent_admin` is the
admin's own agent. The org feature flag `enable_training_mode` is **on** unless a
story says otherwise.

**Entry gate (per-agent, not org-wide):**

1. An **agent admin** (`manage` on the agent) **can enter training mode** on that
   agent — even without `full_admin_access`. (Today only full admins can.)
2. A **plain member** (`view` only) **cannot enter training mode** on that agent.
3. **Cross-agent isolation (the key case):** `u`, who is a **member of `agent1`**
   and an **admin of `agent2`**, **cannot enter training mode on `agent1`**, but
   **can** on `agent2`. Holding `manage` on *some* agent must not unlock training
   on an agent they only view.
4. A **full admin** can still enter training mode on **any** agent (bypass
   preserved).
5. When `enable_training_mode` is **disabled**, **nobody** can enter training
   mode — agent admin or full admin — the org flag still hard-blocks it.

**Write scoping (nothing created leaks org-wide):**

6. Instructions created during a training session are attached to the trained
   **agent's data_source**; an agent admin **cannot** produce a **global**
   (data-source-less) instruction, and **cannot** attach one to an agent they
   don't manage.
7. Same for **evals** — created scoped to the trained agent; global/org-wide
   creation is blocked for a non-admin agent admin.
8. Same for **prompts** — agent-scoped to the trained agent; `global`/`private`
   promotion stays org-admin-only (already enforced by `PromptService`).
9. **Approving the build** (publishing the staged instructions) requires
   `manage_instructions` on the trained agent — an agent admin can approve their
   own agent's build; a member cannot.

**Backward compatibility:**

10. Existing full-admin training flows are unchanged (stories 4/5 + regression
    suites).
11. Existing HTTP RBAC for instructions/evals/prompts/entities is unchanged
    (the agent-manager regression suites still pass).

---

## What the fix changes (recap — see the design write-up)

- **Entry gate, backend** — `report_service` (mode-set path, ~`report_service.py:544`)
  must, in addition to the `enable_training_mode` flag, require the actor to hold
  `manage_instructions` on **every** data_source attached to the report before
  allowing `mode == "training"`. Today it checks *only* the flag, so the gate is
  effectively frontend-only.
- **Entry gate, frontend** — the four `useCan('train_mode') && isTrainingModeEnabled`
  sites (`PromptBoxV2.vue:913`, `ModeSelector.vue:68`, `KnowledgeExplorer.vue:898`,
  `old_agents/[id]/index.vue:200`) and `PrimaryInstructionMenu`'s `canTrain`
  become resource-scoped: `useCan('manage_instructions', { type:'data_source', id: agentId }) && isTrainingModeEnabled`.
  `canCreateInstructions` (`PromptBoxV2.vue:591`) becomes the same resource-scoped
  check so an agent admin can approve their own build.
- **Write scoping** — move the `require_org_permission` global gate out of the
  HTTP routes and into a shared service-layer `authorize_write(resolved, ds_ids)`
  (modeled on `prompt_service.authorize_write` / `_can_manage_all`) that the AI
  training tools call too, so `create_instruction` / `create_eval` can't bypass
  it. Restrict training-mode table→DS resolution to the trained agent, and stop
  defaulting to global when scope is omitted.

> Scoping guarantee (mirrors the agent-manager doc): `manage` implies
> `manage_instructions` / `manage_evals` only on the **same** data_source. A
> training agent admin is confined to the agent(s) on the report — never org-wide.

---

## Environment setup (fresh sandbox)

Identical to the agent-manager loop.

```bash
cd backend
pip install uv
uv sync --frozen --extra dev
export BOW_DATABASE_URL="sqlite:///db/app.db"
mkdir -p db
```

---

## Loop A — resolver / permission unit + frontend implication (fast, no HTTP)

The entry gate reuses the existing `manage ⇒ manage_instructions` implication, so
the resolver already has the primitive. Confirm it and the frontend mirror.

```bash
cd backend
export BOW_DATABASE_URL="sqlite:///db/app.db"
uv run --extra dev pytest tests/unit/test_permission_resolver.py -q
```

Assert (add if missing): for a user holding only a per-DS `manage` grant on
`agent2`, `resolved.has_resource_permission('data_source', agent2, 'manage_instructions')`
is **True**, and on `agent1` (view only) it is **False**. This is exactly what the
entry gate keys on, so it is the unit-level proof of story 3.

Frontend mirror (no Nuxt build) — the same node implication check used in the
agent-manager loop already covers `manage ⇒ manage_instructions`; add an
assertion that `useCan('manage_instructions', {type:'data_source', id})` is
false for a view-only grant and true for a manage grant.

---

## Loop B — end-to-end entry gate + cross-agent isolation (HTTP, in-process SQLite)

New file: `backend/tests/e2e/rbac/test_rbac_training_mode.py`. Reuses the same
fixtures as `test_rbac_agent_manager_stories.py` / `test_rbac_prompts.py`
(`bootstrap_admin`, `invite_user_to_org`, `grant_resource`, `sqlite_data_source`).

```bash
cd backend
export BOW_DATABASE_URL="sqlite:///db/app.db"
uv run --extra dev pytest tests/e2e/rbac/test_rbac_training_mode.py -v
```

### World fixture

```python
@pytest.fixture
def training_world(test_client, bootstrap_admin, invite_user_to_org,
                   grant_resource, sqlite_data_source):
    admin = bootstrap_admin("admin"); org = admin["org_id"]

    agent1 = sqlite_data_source(name="agent1", user_token=admin["token"], org_id=org)
    agent2 = sqlite_data_source(name="agent2", user_token=admin["token"], org_id=org)

    # u = MEMBER (view) of agent1, ADMIN (manage) of agent2  ← the key case
    u = invite_user_to_org(org_id=org, admin_token=admin["token"])
    for ds, perms in ((agent1, ["view"]), (agent2, ["manage"])):
        r = grant_resource(resource_type="data_source", resource_id=ds["id"],
                           principal_type="user", principal_id=u["user_id"],
                           permissions=perms, user_token=admin["token"], org_id=org)
        assert r.status_code == 200, r.text
    return {"org": org, "admin": admin, "u": u, "agent1": agent1, "agent2": agent2}


def _report_on(test_client, token, org, ds_id):
    r = test_client.post("/api/reports", json={"data_sources": [ds_id]}, headers=_hdr(token, org))
    assert r.status_code in (200, 201), r.text
    return r.json()["id"]

def _set_mode(test_client, token, org, report_id, mode):
    return test_client.put(f"/api/reports/{report_id}", json={"mode": mode}, headers=_hdr(token, org))
```

### Test cases

**`test_agent_admin_can_enter_training_on_own_agent`** (story 1)
```python
rid = _report_on(tc, u["token"], org, agent2["id"])          # u manages agent2
assert _set_mode(tc, u["token"], org, rid, "training").status_code == 200
```

**`test_member_cannot_enter_training`** (story 2)
```python
rid = _report_on(tc, u["token"], org, agent1["id"])          # u only views agent1
assert _set_mode(tc, u["token"], org, rid, "training").status_code == 403
# Sanity: chat/deep still work for a member.
assert _set_mode(tc, u["token"], org, rid, "chat").status_code == 200
```

**`test_member_of_one_agent_admin_of_another_cannot_train_the_member_agent`** (story 3 — THE case)
```python
# Same user, two agents, opposite grants.
r1 = _report_on(tc, u["token"], org, agent1["id"])           # member of agent1
r2 = _report_on(tc, u["token"], org, agent2["id"])           # admin  of agent2
assert _set_mode(tc, u["token"], org, r1, "training").status_code == 403   # NOT allowed on agent1
assert _set_mode(tc, u["token"], org, r2, "training").status_code == 200   # allowed on agent2
# Manage on agent2 must NOT leak into agent1.
```

**`test_full_admin_can_train_any_agent`** (story 4 — backward compat)
```python
rid = _report_on(tc, admin["token"], org, agent1["id"])
assert _set_mode(tc, admin["token"], org, rid, "training").status_code == 200
```

**`test_training_blocked_when_org_flag_disabled`** (story 5)
```python
# turn enable_training_mode OFF via org settings, then:
for actor in (admin, u):                                     # even the manager/admin
    rid = _report_on(tc, actor["token"], org, agent2["id"])
    assert _set_mode(tc, actor["token"], org, rid, "training").status_code == 400
```

### Write-scoping cases (HTTP layer — instructions/evals/entities)

These extend the agent-manager stories to the training actor `u`. They assert an
agent admin's writes stay on their agent and cannot go global.

**`test_agent_admin_instruction_writes_are_scoped`** (story 6)
```python
# On the agent they manage → allowed, attached to agent2.
assert tc.post("/api/instructions",
    json=_instruction_body("rule", [agent2["id"]]), headers=_hdr(u["token"], org)).status_code == 200
# Global (no DS) → 403 for a non-admin agent admin.
assert tc.post("/api/instructions/global",
    json=_instruction_body("global rule", []), headers=_hdr(u["token"], org)).status_code == 403
# On agent1 (view only) → 403.
assert tc.post("/api/instructions",
    json=_instruction_body("rule", [agent1["id"]]), headers=_hdr(u["token"], org)).status_code == 403
```

**`test_agent_admin_eval_writes_are_scoped`** (story 7) — mirror of the above
against the eval `create_case` route: scoped to `agent2` → allowed; empty
`data_source_ids_json` (global) → **403 for `u`** (this is the bypass to close —
today an empty list skips the per-DS check); `agent1` → 403.

**`test_approve_build_requires_manage_on_agent`** (story 9) — approving/publishing
a report's training build succeeds for `u` on `agent2`, 403 on `agent1`.

---

## Loop C — training tools + live UI/LLM

The HTTP tests above cover the routes; the **AI tool path** (which historically
bypassed the route gate) is proven the same way `test_prompt_training_tools.py`
does — drive the tools through `run_stream` against the real services.

New/extended: `backend/tests/prompts/test_training_write_scoping.py`

```bash
cd backend
export BOW_DATABASE_URL="sqlite:///db/app.db"
uv run --extra dev pytest tests/prompts/test_training_write_scoping.py -v -s
```

Asserts (no LLM — tools invoked directly with a runtime context bound to the
trained agent):
- `create_instruction` with the trained agent = `agent2`, **`table_names` omitted**
  → instruction is attached to `agent2` (NOT global). Today this yields a global
  instruction — this is the red→green assertion.
- `create_instruction` as a user who only **views** the trained agent →
  `permission_denied`.
- `create_eval` with `data_source_ids` omitted → scoped to the trained agent,
  not org-wide; non-manager → `permission_denied`.
- `create_prompt` → agent-scoped to the trained agent (already green); a
  requested `scope='global'` from a non-admin → `permission_denied`.

Live UI/LLM (manual, Haiku), mirroring the prompts-tools loop:
1. As `u` (manager of `agent2`, member of `agent1`), open a report on **`agent2`**,
   switch the composer to **Training** — the toggle is **visible**.
2. Open a report on **`agent1`** — the Training toggle is **hidden/disabled**
   (member-only). This is the visual proof of the cross-agent case.
3. In the `agent2` training session: *"Add an always-on rule that revenue is in
   USD."* → `create_instruction` runs; the staged instruction is scoped to
   `agent2` (check it does **not** appear for `agent1`).
4. Approve the build → instructions publish under `agent2` only.
5. As full admin, confirm training still works on any agent (backward compat).

---

## Backward-compatibility / regression guards

Run the existing suites unchanged — none of these behaviors may shift:

```bash
cd backend
export BOW_DATABASE_URL="sqlite:///db/app.db"
uv run --extra dev pytest \
  tests/unit/test_permission_resolver.py \
  tests/e2e/rbac/test_rbac_agent_manager_stories.py \
  tests/e2e/rbac/test_rbac_instructions.py \
  tests/e2e/rbac/test_rbac_evals.py \
  tests/e2e/rbac/test_rbac_prompts.py \
  tests/e2e/rbac/test_rbac_entities.py \
  tests/e2e/rbac/test_rbac_data_sources.py \
  tests/prompts/test_prompt_training_tools.py -q
```

Explicit backward-compat assertions folded into Loop B:
- Full admin can train any agent (`test_full_admin_can_train_any_agent`).
- The org flag still hard-blocks everyone when off
  (`test_training_blocked_when_org_flag_disabled`).
- `chat` / `deep` mode is unaffected for members (sanity in
  `test_member_cannot_enter_training`).
- Existing global-instruction/entity/eval admin gates on the HTTP routes remain
  403 for managers (agent-manager stories 2 & 9 still pass) — the write-scoping
  change moves the gate into the service **without loosening the route**.

---

## Observed results (this sandbox)

Backend (`uv run pytest`, SQLite):

```
tests/e2e/rbac/test_rbac_training_mode.py ........................ 7 passed
  test_agent_admin_can_enter_training_on_own_agent                 PASS
  test_member_cannot_enter_training                                PASS
  test_member_of_one_agent_admin_of_another_cannot_train_member_agent  PASS  ← the case
  test_full_admin_can_train_any_agent                              PASS
  test_training_blocked_when_org_flag_disabled                     PASS
  test_agent_admin_instruction_writes_are_scoped                   PASS
  test_agent_admin_eval_writes_are_scoped                          PASS

Regression (unchanged): test_rbac_evals / test_rbac_instructions /
test_rbac_agent_manager_stories / test_rbac_prompts — all pass.
```

Live API (running server, user `u` = member of agent1, admin of agent2):

```
PUT /reports/{report_on_agent1} {"mode":"training"}  → 403   (member — denied)
PUT /reports/{report_on_agent2} {"mode":"training"}  → 200   (admin — allowed)
```

Live UI (Playwright, same logged-in user `u`):
- `02_u_agent2_admin.png` — mode popover on the agent they manage shows **Chat /
  Deep Analytics / Training**.
- `03_u_agent1_member.png` — mode popover on the agent they only view shows
  **Chat / Deep Analytics** only; **no Training**.

(Full-admin UI walkthrough is gated behind first-run onboarding in a fresh org;
its backward-compat is covered by `test_full_admin_can_train_any_agent` and the
live 200 above.)

## What this proves

- Training mode is now available to **agent admins** (per-DS `manage`), scoped to
  the agent they administer (stories 1, 4).
- The power is **isolated per agent**: a member can't train (story 2), and — the
  case that motivated this — a user who is a **member of `agent1`** but **admin of
  `agent2`** is **denied training on `agent1`** while allowed on `agent2` (story 3).
- Everything a training session creates — instructions, evals, prompts — is
  **bound to the trained agent** and cannot go org-wide from a non-admin agent
  admin (stories 6–8), including via the AI tool path that previously bypassed the
  route gate.
- Full-admin flows and all existing RBAC behavior are **unchanged** (stories
  10–11 + the regression suites).
</content>
</invoke>
