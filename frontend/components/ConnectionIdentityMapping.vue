<template>
  <div v-if="canManage && slots.length" data-testid="identity-mapping">
    <details class="py-3 border-t border-gray-100 dark:border-gray-800" :open="openByDefault">
      <summary class="flex items-center gap-2 text-[13px] text-gray-700 dark:text-gray-300 cursor-pointer list-none">
        <UIcon name="heroicons-chevron-right" class="identity-chevron w-3.5 h-3.5 shrink-0 text-gray-400" />
        <span>{{ $t('data.identityMapping.title') }}</span>
        <span class="ms-auto text-xs text-end" :class="savedEnabled ? 'text-green-600' : 'text-gray-500'" data-testid="identity-mapping-summary">
          {{ savedEnabled ? $t('data.identityMapping.statusOn') : $t('data.identityMapping.statusOff') }}
        </span>
      </summary>

      <div class="pt-3 space-y-3">
        <div class="flex items-center justify-between gap-3">
          <p class="text-[11px] text-gray-500 dark:text-gray-400">{{ $t('data.identityMapping.hint') }}</p>
          <UToggle :model-value="enabled" size="sm" :disabled="saving" data-testid="identity-mapping-toggle" @update:model-value="onToggle" />
        </div>

        <template v-if="enabled">
          <div v-for="slot in slots" :key="slot.param" class="space-y-1" :data-testid="`identity-slot-${slot.param}`">
            <div class="flex items-baseline justify-between gap-2">
              <span class="text-xs font-medium text-gray-700 dark:text-gray-300">{{ slotText(slot, 'title') }}</span>
            </div>
            <p v-if="slot.description" class="text-[11px] text-gray-400 dark:text-gray-500">{{ slotText(slot, 'description') }}</p>
            <div class="flex items-center gap-1.5">
              <select
                v-model="drafts[slot.param].kind"
                class="text-xs border border-gray-200 dark:border-gray-700 rounded-md px-1.5 py-1 bg-white dark:bg-gray-800 text-gray-700 dark:text-gray-100"
                :aria-label="$t('data.identityMapping.sourceKind')"
                :data-testid="`identity-kind-${slot.param}`"
                @change="onDraftChange"
              >
                <option value="attr">{{ $t('data.identityMapping.kindAttribute') }}</option>
                <option value="static">{{ $t('data.identityMapping.kindStatic') }}</option>
                <option value="email">{{ $t('data.identityMapping.kindEmail') }}</option>
                <option value="name">{{ $t('data.identityMapping.kindName') }}</option>
              </select>
              <input
                v-if="drafts[slot.param].kind === 'attr' || drafts[slot.param].kind === 'static'"
                v-model="drafts[slot.param].text"
                :list="drafts[slot.param].kind === 'attr' ? datalistId : undefined"
                :placeholder="drafts[slot.param].kind === 'attr' ? $t('data.identityMapping.attributePlaceholder') : $t('data.identityMapping.staticPlaceholder')"
                class="flex-1 min-w-0 text-xs font-mono border border-gray-200 dark:border-gray-700 rounded-md px-2 py-1 bg-white dark:bg-gray-800 text-gray-700 dark:text-gray-100 focus:outline-none focus:ring-1 focus:ring-blue-300"
                :data-testid="`identity-text-${slot.param}`"
                @input="onDraftChange"
              />
            </div>
          </div>
          <datalist :id="datalistId">
            <option v-for="k in attributeKeys" :key="k" :value="k" />
          </datalist>

          <!-- Who acts as whom, resolved from the draft rules. -->
          <div class="space-y-1.5" data-testid="identity-preview">
            <div class="flex items-center justify-between">
              <span class="text-xs text-gray-500 dark:text-gray-400">{{ $t('data.identityMapping.previewTitle') }}</span>
              <span v-if="preview" class="text-[11px] text-gray-500">
                {{ $t('data.identityMapping.previewCounts', { resolved: preview.resolved_count, blocked: preview.blocked_count }) }}
              </span>
            </div>
            <p v-if="previewError" class="text-[11px] text-red-500">{{ previewError }}</p>
            <div v-else-if="preview" class="max-h-56 overflow-y-auto border border-gray-100 dark:border-gray-800 rounded-md divide-y divide-gray-100 dark:divide-gray-800">
              <div v-for="row in preview.members" :key="row.membership_id" class="px-2 py-1.5 text-xs" :data-testid="`identity-row-${row.email}`">
                <div class="flex items-center gap-2">
                  <UIcon
                    :name="row.missing.length ? 'heroicons-no-symbol' : 'heroicons-check-circle'"
                    class="w-3.5 h-3.5 shrink-0"
                    :class="row.missing.length ? 'text-red-500' : 'text-green-600'"
                  />
                  <span class="truncate min-w-0 flex-1 text-gray-700 dark:text-gray-300" :title="row.email || ''">
                    {{ row.email }}<span v-if="row.pending" class="text-gray-400"> · {{ $t('settings.members.statusPending') }}</span>
                  </span>
                  <span v-if="!row.missing.length" class="font-mono text-gray-600 dark:text-gray-400 truncate max-w-[45%]" :title="formatIdentity(row.values)">{{ formatIdentity(row.values) }}</span>
                  <span v-else class="text-red-500 truncate max-w-[45%]" :title="missingLabel(row)">{{ $t('data.identityMapping.blocked') }}</span>
                  <button
                    v-if="!row.missing.length"
                    type="button"
                    class="text-[11px] text-blue-600 hover:underline disabled:opacity-50 shrink-0"
                    :disabled="!!testing[row.membership_id]"
                    :data-testid="`identity-test-${row.email}`"
                    @click="testMember(row)"
                  >{{ testing[row.membership_id] ? $t('data.testing') : $t('data.identityMapping.test') }}</button>
                </div>
                <p
                  v-if="testResults[row.membership_id]"
                  class="ps-5 pt-0.5 text-[11px] break-words"
                  :class="testResults[row.membership_id].success ? 'text-green-700 dark:text-green-500' : 'text-red-600'"
                  :data-testid="`identity-test-result-${row.email}`"
                >
                  {{ testResults[row.membership_id].message || (testResults[row.membership_id].success ? $t('data.identityMapping.testOk') : $t('data.requestFailed')) }}
                </p>
              </div>
            </div>
          </div>
        </template>

        <p v-if="saveError" class="text-[11px] text-red-500" data-testid="identity-mapping-error">{{ saveError }}</p>
        <div v-if="dirty" class="flex justify-end gap-2">
          <button type="button" class="text-xs px-2 py-1 text-gray-600 dark:text-gray-300" :disabled="saving" @click="reset">{{ $t('data.cancel') }}</button>
          <button
            type="button"
            class="bg-blue-500 hover:bg-blue-600 text-white text-xs font-medium py-1 px-3 rounded disabled:opacity-50"
            :disabled="saving"
            data-testid="identity-mapping-save"
            @click="save"
          >{{ saving ? $t('data.saving') : $t('common.save') }}</button>
        </div>
      </div>
    </details>
  </div>
</template>

<script setup lang="ts">
// Per-member identity for a connection (Connection.identity_mapping). The
// connector type declares which identity slots it takes (registry
// `identity_slots`, sent on the connection detail); the admin maps each slot
// to a source the member cannot edit — an admin-set member attribute, a fixed
// value, or the member's email/name. The backend resolves it per query and
// blocks a member whose value is missing. See
// backend/app/services/connection_identity_mapping.py.
import { useCan } from '~/composables/usePermissions'

type Slot = { param: string; title: string; description?: string; example?: string | null; required?: boolean }
type Draft = { kind: 'attr' | 'static' | 'email' | 'name'; text: string }
type PreviewRow = { membership_id: string; email: string | null; name: string | null; pending: boolean; values: Record<string, string>; missing: string[] }
type Preview = { mapping: Record<string, string>; attribute_keys: string[]; members: PreviewRow[]; resolved_count: number; blocked_count: number }

const props = defineProps<{ connection: any }>()
const { t, te } = useI18n()
const toast = useToast()
const canManage = computed(() => useCan('manage_connection', { type: 'connection', id: props.connection?.id }))

const slots = ref<Slot[]>([])
const saved = ref<Record<string, string>>({})
const enabled = ref(false)
const drafts = reactive<Record<string, Draft>>({})
const preview = ref<Preview | null>(null)
const previewError = ref<string | null>(null)
const saveError = ref<string | null>(null)
const saving = ref(false)
const testing = reactive<Record<string, boolean>>({})
const testResults = reactive<Record<string, { email: string | null; success: boolean; message: string }>>({})
const datalistId = `identity-attr-keys-${Math.random().toString(36).slice(2, 8)}`

const savedEnabled = computed(() => Object.keys(saved.value).length > 0)
const openByDefault = computed(() => savedEnabled.value)
const attributeKeys = computed(() => preview.value?.attribute_keys || [])

function parseSource(source: string | null | undefined): Draft {
  const s = (source || '').trim()
  if (s === 'user.email') return { kind: 'email', text: '' }
  if (s === 'user.name') return { kind: 'name', text: '' }
  if (s.startsWith('membership.attr:')) return { kind: 'attr', text: s.slice('membership.attr:'.length) }
  if (s.startsWith('static:')) return { kind: 'static', text: s.slice('static:'.length) }
  return { kind: 'attr', text: '' }
}

function toSource(d: Draft): string {
  if (d.kind === 'email') return 'user.email'
  if (d.kind === 'name') return 'user.name'
  const text = (d.text || '').trim()
  if (!text) return ''
  return d.kind === 'attr' ? `membership.attr:${text}` : `static:${text}`
}

const draftMapping = computed<Record<string, string>>(() => {
  if (!enabled.value) return {}
  const out: Record<string, string> = {}
  for (const slot of slots.value) {
    const d = drafts[slot.param]
    if (d) out[slot.param] = toSource(d)
  }
  return out
})

const dirty = computed(() => {
  const a = draftMapping.value
  const b = saved.value
  const keys = new Set([...Object.keys(a), ...Object.keys(b)])
  for (const k of keys) if ((a[k] || '') !== (b[k] || '')) return true
  return false
})

function loadDrafts(mapping: Record<string, string>) {
  for (const slot of slots.value) {
    drafts[slot.param] = parseSource(mapping[slot.param] ?? slot.example ?? '')
  }
}

// Slot labels come from the registry in English; a catalog entry
// `data.identitySlots.<type>.<param>.{title,description}` localizes them
// (same convention as the connect form's setup guides).
function slotText(slot: Slot, key: 'title' | 'description'): string {
  const k = `data.identitySlots.${props.connection?.type}.${slot.param}.${key}`
  return te(k) ? t(k) : String(slot[key] || '')
}

function formatIdentity(values: Record<string, string>): string {
  return slots.value.map(s => values[s.param]).filter(Boolean).join(' · ')
}

function missingLabel(row: PreviewRow): string {
  const titles = row.missing.map(p => { const slot = slots.value.find(s => s.param === p); return slot ? slotText(slot, 'title') : p })
  return t('data.identityMapping.blockedMissing', { fields: titles.join(', ') })
}

let previewTimer: ReturnType<typeof setTimeout> | null = null
function schedulePreview() {
  if (previewTimer) clearTimeout(previewTimer)
  previewTimer = setTimeout(refreshPreview, 350)
}

async function refreshPreview() {
  if (!props.connection?.id || !enabled.value) return
  const mapping = draftMapping.value
  if (Object.values(mapping).some(v => !v)) {
    previewError.value = t('data.identityMapping.incomplete')
    return
  }
  const { data, error } = await useMyFetch(`/connections/${props.connection.id}/identity-mapping/preview`, {
    method: 'POST',
    body: { identity_mapping: mapping },
  })
  if (error.value) {
    previewError.value = (error.value as any)?.data?.detail || t('data.requestFailed')
    return
  }
  previewError.value = null
  preview.value = data.value as Preview
}

function onDraftChange() {
  saveError.value = null
  for (const id of Object.keys(testResults)) delete testResults[id]
  schedulePreview()
}

function onToggle(value: boolean) {
  enabled.value = value
  saveError.value = null
  if (value) {
    loadDrafts(saved.value)
    schedulePreview()
  }
}

function reset() {
  enabled.value = savedEnabled.value
  loadDrafts(saved.value)
  saveError.value = null
  if (enabled.value) schedulePreview()
}

async function save() {
  if (!props.connection?.id || saving.value) return
  saving.value = true
  saveError.value = null
  try {
    const mapping = draftMapping.value
    const { error } = await useMyFetch(`/connections/${props.connection.id}`, {
      method: 'PUT',
      body: { identity_mapping: mapping },
    })
    if (error.value) {
      saveError.value = (error.value as any)?.data?.detail || t('data.identityMapping.saveFailed')
      return
    }
    saved.value = enabled.value ? { ...mapping } : {}
    toast.add({ title: enabled.value ? t('data.identityMapping.savedOn') : t('data.identityMapping.savedOff'), color: 'green' })
  } finally {
    saving.value = false
  }
}

async function testMember(row: PreviewRow) {
  testing[row.membership_id] = true
  try {
    const { data, error } = await useMyFetch(`/connections/${props.connection.id}/identity-mapping/test`, {
      method: 'POST',
      body: { membership_id: row.membership_id, identity_mapping: draftMapping.value },
    })
    const res: any = error.value ? { success: false, message: (error.value as any)?.data?.detail } : data.value
    testResults[row.membership_id] = { email: row.email, success: !!res?.success, message: res?.message || '' }
  } finally {
    testing[row.membership_id] = false
  }
}

watch(() => [props.connection?.id, canManage.value], async ([id]) => {
  if (!id || !canManage.value) return
  const { data } = await useMyFetch(`/connections/${id}`, { method: 'GET' })
  const d: any = data.value
  if (!d) return
  slots.value = d.identity_slots || []
  saved.value = { ...(d.identity_mapping || {}) }
  enabled.value = savedEnabled.value
  loadDrafts(saved.value)
  if (enabled.value) refreshPreview()
}, { immediate: true })
</script>

<style scoped>
details[open] > summary .identity-chevron { transform: rotate(90deg); }
.identity-chevron { transition: transform 0.15s ease; }
</style>
