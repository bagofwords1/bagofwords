# Sandbox Feedback Loop — API tokens: "MCP modal always shows 0 tokens" + expose keys in User Profile

Covers two reported items about **personal API keys** (the `bow_…` tokens used
for the MCP server / programmatic access):

1. **Bug:** the MCP server modal (`McpModal.vue`) **always shows 0 API tokens** —
   existing/old keys are never listed, so "Manage tokens (n)" never appears and
   the masked-token row never renders.
2. **Feature:** API keys are currently only reachable from the MCP modal. They
   should also be listed (with generate/delete) in the **User Profile** modal.

This doc is the runnable feedback loop used to confirm the root cause and verify
the fix in a fresh cloud sandbox.

---

## Background — how API keys work

- Keys are **per user** (not per org). `ApiKeyService.list_api_keys`
  (`backend/app/services/api_key_service.py:76`) filters by `user_id` only;
  `create` scopes `organization_id` but `list` does not. So the same key list is
  valid in both the MCP modal and the profile modal.
- Endpoints live at `/api/api_keys` (`backend/app/routes/api_key.py`,
  mounted in `backend/main.py:257`): `POST` create (returns the full key once),
  `GET` list (`ApiKeyResponse`, no full key), `DELETE /{id}` soft-delete.
- The full key is shown **once** on creation; the list only returns
  `key_prefix`, `created_at`, `last_used_at`, etc.

The backend is **not** the bug — see Loop A.

---

## Root cause (validated) — Bug #1

`McpModal.vue` loads its key list from a Vue watcher:

```js
watch(isOpen, async (open) => {
  if (open) { /* loadSettings() + loadApiKeys() */ }
})   // <-- NOT immediate
```

The modal is mounted **two different ways**:

- `frontend/pages/index.vue:181` — `<McpModal v-model="showMcpModal" />`
  (always mounted; `showMcpModal` toggles `false → true`). The non-immediate
  watch **does** see that transition, so keys load. Works.
- `frontend/layouts/default.vue:244` — `<McpModal v-if="showMcpModal" v-model="showMcpModal" />`
  (the global sidebar entry — the **common** way to open it). Here the component
  is only **mounted when `showMcpModal` is already `true`**, so `isOpen` is
  `true` on the very first render. A **non-immediate** watch never sees a
  `false → true` change in this component's lifetime, so **`loadApiKeys()` never
  runs** → `apiKeys` stays `[]` → "0 tokens", old keys hidden.

The sibling `UserProfileModal.vue` already hit and fixed this exact pattern — see
its watcher comment ("The component is v-if-mounted already-open, so the watcher
must be immediate"). `McpModal` was missing the same `{ immediate: true }`.

### The fix (Bug #1)

`frontend/components/McpModal.vue` — make the open-watcher immediate so an
already-open (`v-if`) mount also loads:

```diff
-})
+}, { immediate: true })
```

---

## The feature — API keys in User Profile (#2)

`frontend/components/UserProfileModal.vue` gains an **"API Keys"** nav tab
(`i-heroicons-key`, between Usage and Appearance) that reuses the same
`/api/api_keys` endpoints:

- Lists the user's keys (name, `key_prefix…`, created date, last-used / never).
- **Generate new key** — reveals the full `bow_…` key once with a copy button
  and a "won't be shown again" warning; prepends it to the list.
- **Delete** per key (confirm) — soft-deletes via `DELETE /api/api_keys/{id}`.
- Loads lazily when the tab is opened (and on already-open mount via the same
  `{ immediate: true }` profile watcher), and resets the one-time reveal on
  close.

i18n keys added under `profile.nav.apiKeys` and `profile.apiKeys.*` in all 10
locales (`locales/*.json`).

---

## Loop A — Backend list/create/delete is correct (no frontend needed)

Proves the API the modals call returns existing keys, so the "0 tokens" symptom
is a **frontend** issue, not the backend.

```bash
cd backend
pip install uv
uv sync --frozen --extra dev
export BOW_DATABASE_URL="sqlite:///db/app.db"
mkdir -p db
uv run python -m pytest tests/e2e/test_api_key.py -v
```

**Observed (PASS, 5/5):** `test_api_key_crud` (create → **list returns the
created key** → delete → list no longer returns it), `test_multiple_api_keys`
(create 3 → **list returns all 3**), plus auth/invalid/deleted-key checks. The
list endpoint returns the user's keys reliably — confirming the bug is the
client never calling it.

---

## Loop B — Frontend reproduction (the watcher-timing bug)

The bug is a Vue `watch`-immediate semantics issue at an **already-open mount**.
`repro.mjs` reproduces it with Vue's reactivity directly (no DOM/browser
needed): it mirrors the modal's `isOpen` computed + open-watcher under both
mount patterns, for both the broken (non-immediate) and fixed (immediate)
watcher.

```bash
mkdir -p /tmp/mcp-verify && cd /tmp/mcp-verify
cp <repo>/tools/sandbox/mcp-tokens-immediate-watch/repro.mjs .
npm i vue@3
node repro.mjs
```

**Observed (PASS):**

```
[v-if already-open mount]  broken(non-immediate): loadCount=0   <-- BUG: keys never load
[v-if already-open mount]  fixed(immediate):      loadCount=1   <-- FIX: keys load
[always-mounted toggle]    broken(non-immediate): loadCount=1   (index.vue path already works)
discriminates: true
```

The broken case asserting `0` (reproduces "always 0 tokens") and the fixed case
asserting `1`, with the always-mounted path showing `1` even when broken, prove
(a) the bug is real, (b) it is specific to the `v-if`-already-open mount used by
the global sidebar, and (c) `{ immediate: true }` fixes it.

---

## What this proves

- **Backend** returns the user's existing keys (Loop A) — so "0 tokens" is not a
  data/API problem.
- **Frontend** never loaded them on the common (`v-if`, sidebar) open path
  because the open-watcher was not `immediate` (Loop B). Adding
  `{ immediate: true }` — the same fix already present in `UserProfileModal` —
  restores listing of old keys.
- API keys are now also generatable/listable/deletable from the **User Profile**
  modal via the same per-user endpoints.
