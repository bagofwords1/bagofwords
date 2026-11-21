<template>
  <div class="space-y-5" data-testid="connection-tools-panel">
    <p class="text-xs text-ink-muted">{{ t('connectionTools.intro') }}</p>

    <Spinner v-if="loading" class="w-5 h-5 mx-auto" />
    <p v-else-if="loadError" role="alert" class="text-xs text-red-600">{{ loadError }}</p>
    <p v-else-if="!tools.length" class="text-xs text-ink-muted">{{ t('connectionTools.empty') }}</p>

    <!-- Missing permissions for switched-on actions: what to grant, then sign in again. -->
    <div
      v-if="missingForEnabled.length"
      class="rounded border border-amber-200 dark:border-amber-800 bg-amber-50 dark:bg-amber-950/40 px-3 py-2 text-xs text-amber-800 dark:text-amber-300 space-y-1.5"
      data-testid="connection-tools-missing-scopes"
    >
      <p>{{ t('connectionTools.missingScopes', { scopes: missingForEnabled.join(', ') }) }}</p>
      <button
        v-if="canSignIn"
        type="button"
        :disabled="signingIn"
        class="inline-flex items-center gap-1.5 rounded bg-amber-600 px-2.5 py-1 font-medium text-white disabled:opacity-50"
        data-testid="connection-tools-grant"
        @click="signIn"
      ><Spinner v-if="signingIn" class="h-3 w-3" />{{ t('connectionTools.grantAccess') }}</button>
    </div>

    <!-- Built-in read tools are always available; listed so the full picture is in one place. -->
    <section v-if="builtinReads.length" class="space-y-1" data-testid="connection-tools-group-builtin">
      <div class="border-b border-gray-100 dark:border-gray-800 pb-1.5">
        <h5 class="text-sm font-medium text-ink">{{ t('connectionTools.effect.read.title') }}</h5>
        <p class="text-xs text-ink-muted">{{ t('connectionTools.builtinReadHint') }}</p>
      </div>
      <div class="flex flex-wrap gap-1.5 pt-1">
        <code v-for="name in builtinReads" :key="name" dir="ltr" class="text-2xs px-1.5 py-0.5 rounded bg-gray-100 dark:bg-gray-800 text-ink-muted">{{ name }}</code>
      </div>
    </section>

    <section v-for="group in groups" :key="group.effect" class="space-y-1" :data-testid="`connection-tools-group-${group.effect}`">
      <div class="flex items-start gap-3 border-b border-gray-100 dark:border-gray-800 pb-1.5">
        <div class="min-w-0">
          <h5 class="text-sm font-medium text-ink">{{ t(`connectionTools.effect.${group.effect}.title`) }}</h5>
          <p class="text-xs text-ink-muted">{{ t(`connectionTools.effect.${group.effect}.hint`) }}</p>
        </div>
        <UToggle
          v-if="canManage && group.effect !== 'read'"
          class="ms-auto mt-1 shrink-0"
          color="blue"
          size="xs"
          :model-value="group.tools.every(x => x.is_enabled)"
          :aria-label="t('connectionTools.enableGroup', { group: t(`connectionTools.effect.${group.effect}.title`) })"
          :data-testid="`connection-tools-group-toggle-${group.effect}`"
          @update:model-value="(v: boolean) => setGroup(group, v)"
        />
      </div>
      <div
        v-for="tool in group.tools"
        :key="tool.id"
        class="flex flex-wrap items-center gap-x-3 gap-y-1 py-1.5"
        :data-testid="`connection-tool-row-${tool.name}`"
      >
        <div class="min-w-0 flex-1 basis-64">
          <div class="flex items-center gap-1.5">
            <code class="text-xs font-medium text-ink" dir="ltr">{{ tool.name }}</code>
            <span
              v-for="scope in missingFor(tool)"
              :key="scope"
              class="text-2xs px-1 py-0.5 rounded bg-amber-50 dark:bg-amber-950 text-amber-700 dark:text-amber-300"
              :title="t('connectionTools.needsScope', { scope })"
            >{{ t('connectionTools.needsScope', { scope }) }}</span>
          </div>
          <p class="text-2xs text-ink-muted line-clamp-2" dir="auto">{{ tool.description }}</p>
        </div>
        <div class="flex items-center gap-2 shrink-0">
          <UToggle
            v-if="canManage"
            color="blue"
            size="2xs"
            :model-value="tool.is_enabled"
            :disabled="busy[tool.id]"
            :aria-label="t('connectionTools.enabled', { name: tool.name })"
            data-testid="connection-tool-enabled"
            @update:model-value="(v: boolean) => update(tool, { is_enabled: v })"
          />
          <span v-else class="text-2xs text-ink-muted">{{ tool.is_enabled ? t('connectionTools.on') : t('connectionTools.off') }}</span>
          <select
            v-if="canManage"
            :value="tool.policy"
            :disabled="busy[tool.id] || !tool.is_enabled"
            :aria-label="t('connectionTools.policyFor', { name: tool.name })"
            class="text-2xs border border-gray-200 dark:border-gray-700 rounded px-1 py-0.5 bg-white dark:bg-gray-900 text-gray-600 dark:text-gray-400 disabled:opacity-50"
            data-testid="connection-tool-policy"
            @change="(e: Event) => update(tool, { policy: (e.target as HTMLSelectElement).value })"
          >
            <option v-for="p in allowedPolicies(tool.effect)" :key="p" :value="p">{{ policyLabel(p) }}</option>
          </select>
          <span v-else class="text-2xs px-1.5 py-0.5 rounded bg-gray-100 dark:bg-gray-800 text-ink-muted">{{ policyLabel(tool.policy) }}</span>
          <button
            v-if="canManage"
            type="button"
            :disabled="busy[tool.id] || !tool.is_enabled"
            class="p-0.5 rounded disabled:opacity-40"
            :class="tool.policy_locked ? 'text-amber-600' : 'text-ink-muted hover:text-ink'"
            :aria-pressed="tool.policy_locked"
            :aria-label="t(tool.policy_locked ? 'connectionTools.unlock' : 'connectionTools.lock', { name: tool.name })"
            :title="t('connectionTools.lockHint')"
            data-testid="connection-tool-lock"
            @click="update(tool, { policy_locked: !tool.policy_locked })"
          ><UIcon :name="tool.policy_locked ? 'heroicons-lock-closed' : 'heroicons-lock-open'" class="w-3.5 h-3.5" /></button>
          <UIcon v-else-if="tool.policy_locked" name="heroicons-lock-closed" class="w-3.5 h-3.5 text-amber-600" :title="t('connectionTools.lockHint')" />
        </div>
      </div>
    </section>

    <p v-if="error" role="alert" class="text-xs text-red-600">{{ error }}</p>
  </div>
</template>

<script setup lang="ts">
import { isSafeHttpUrl } from '~/utils/safeUrl'

type Tool = {
  id: string; name: string; description?: string; is_enabled: boolean; policy: string
  policy_locked: boolean; effect?: string | null; source?: string | null
}

const props = defineProps<{ connectionId: string; connectionType?: string; active: boolean; canManage: boolean; canSignIn?: boolean }>()
const { t } = useI18n()
const route = useRoute()

const EFFECT_ORDER = ['read', 'modify', 'external', 'destructive'] as const
// Mirrors connector_actions.ALLOWED_POLICIES: external actions are not left to
// a model judge; destructive ones never run silently.
const ALLOWED: Record<string, string[]> = {
  read: ['allow', 'ask', 'auto', 'deny'],
  modify: ['allow', 'ask', 'auto', 'deny'],
  external: ['allow', 'ask', 'deny'],
  destructive: ['ask', 'deny'],
}
// The read tools a built-in connector always offers (capability tools, not rows).
const BUILTIN_READS: Record<string, string[]> = {
  outlook_mail: ['list_emails', 'search_email', 'read_email'],
  teams_messages: ['list_chat_messages', 'search_chat_messages', 'read_chat_thread'],
}

const tools = ref<Tool[]>([])
const loading = ref(false)
const loadError = ref('')
const error = ref('')
const busy = reactive<Record<string, boolean>>({})
const permissions = ref<{ known: boolean; missing: Record<string, string[]> }>({ known: false, missing: {} })
const signingIn = ref(false)

const builtinReads = computed(() => BUILTIN_READS[props.connectionType || ''] || [])

function allowedPolicies(effect?: string | null) {
  return ALLOWED[effect || 'read'] || ALLOWED.read
}
function policyLabel(p: string) {
  return ({ allow: t('toolsSelector.policyAllow'), ask: t('toolsSelector.policyAsk'), auto: t('toolsSelector.policyAuto'), deny: t('toolsSelector.policyDeny') } as Record<string, string>)[p] || p
}
function missingFor(tool: Tool) {
  return permissions.value.known ? (permissions.value.missing[tool.name] || []) : []
}
const missingForEnabled = computed(() => {
  const out = new Set<string>()
  for (const tool of tools.value) if (tool.is_enabled) for (const s of missingFor(tool)) out.add(s)
  return [...out]
})

const groups = computed(() => {
  const by: Record<string, Tool[]> = {}
  for (const tool of tools.value) {
    const effect = EFFECT_ORDER.includes((tool.effect || '') as any) ? (tool.effect as string) : 'read'
    ;(by[effect] ||= []).push(tool)
  }
  return EFFECT_ORDER.filter(e => by[e]?.length).map(effect => ({ effect, tools: by[effect].sort((a, b) => a.name.localeCompare(b.name)) }))
})

async function load() {
  if (!props.connectionId) return
  loading.value = true
  loadError.value = ''
  try {
    const { data, error: err } = await useMyFetch(`/connections/${props.connectionId}/tools`, { method: 'GET' })
    if (err.value) { loadError.value = t('connectionTools.loadFailed'); return }
    tools.value = (data.value as Tool[]) || []
  } finally { loading.value = false }
  loadPermissions()
}

async function loadPermissions() {
  if (!BUILTIN_READS[props.connectionType || '']) return
  const { data } = await useMyFetch(`/connections/${props.connectionId}/tools/permissions`, { method: 'GET' })
  if (data.value) permissions.value = data.value as any
}

function replace(updated: Tool) {
  tools.value = tools.value.map(x => (x.id === updated.id ? { ...x, ...updated } : x))
}

async function update(tool: Tool, body: Partial<Tool>) {
  busy[tool.id] = true
  error.value = ''
  try {
    const { data, error: err } = await useMyFetch(`/connections/${props.connectionId}/tools/${tool.id}`, { method: 'PUT', body })
    if (err.value) { error.value = (err.value as any)?.data?.detail || t('connectionTools.saveFailed'); return }
    if (data.value) replace(data.value as Tool)
  } finally { busy[tool.id] = false }
  if ('is_enabled' in body) loadPermissions()
}

async function setGroup(group: { tools: Tool[] }, value: boolean) {
  error.value = ''
  const ids = group.tools.map(x => x.id)
  ids.forEach(id => { busy[id] = true })
  try {
    const { data, error: err } = await useMyFetch(`/connections/${props.connectionId}/tools/batch`, { method: 'PUT', body: { tool_ids: ids, is_enabled: value } })
    if (err.value) { error.value = (err.value as any)?.data?.detail || t('connectionTools.saveFailed'); return }
    for (const row of (data.value as Tool[]) || []) replace(row)
  } finally { ids.forEach(id => { busy[id] = false }) }
  loadPermissions()
}

async function signIn() {
  signingIn.value = true
  try {
    const { data, error: err } = await useMyFetch(`/connections/${props.connectionId}/oauth/authorize?return_to=${encodeURIComponent(route.fullPath)}`, { method: 'GET' })
    const url = (data.value as any)?.authorization_url
    if (err.value || !isSafeHttpUrl(url)) { error.value = t('data.requestFailed'); signingIn.value = false; return }
    window.location.href = url
  } catch { error.value = t('data.requestFailed'); signingIn.value = false }
}

watch(() => [props.active, props.connectionId], ([active]) => { if (active) load() }, { immediate: true })
</script>
