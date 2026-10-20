<template>
  <div class="mt-3 pt-3 border-t border-gray-200 dark:border-gray-700 space-y-3" data-testid="member-identity">
    <div>
      <div class="text-xs font-medium text-gray-700 dark:text-gray-300">{{ $t('data.memberIdentity.title') }}</div>
      <p class="text-[11px] text-gray-500 dark:text-gray-400 mt-0.5">{{ $t('data.memberIdentity.hint') }}</p>
    </div>

    <div v-for="field in fields" :key="field.field_name" class="space-y-1" :data-testid="`member-binding-${field.field_name}`">
      <label class="block text-xs text-gray-700 dark:text-gray-300">{{ field.title || field.field_name }}</label>
      <div class="flex items-center gap-2">
        <select
          :value="kinds[field.field_name]"
          :disabled="readOnly"
          class="w-48 shrink-0 px-2 py-1.5 border border-gray-300 dark:border-gray-600 rounded-md text-xs bg-white dark:bg-gray-800 text-gray-900 dark:text-gray-100 focus:outline-none focus:border-blue-500"
          :aria-label="$t('data.memberIdentity.sourceFor', { field: field.title || field.field_name })"
          :data-testid="`member-binding-kind-${field.field_name}`"
          @change="setKind(field.field_name, ($event.target as HTMLSelectElement).value as Kind)"
        >
          <option value="fixed">{{ $t('data.memberIdentity.sameForEveryone') }}</option>
          <option value="attr">{{ $t('data.memberIdentity.fromAttribute') }}</option>
        </select>
        <input
          v-if="kinds[field.field_name] === 'attr'"
          :value="keys[field.field_name] || ''"
          :disabled="readOnly"
          :list="datalistId"
          dir="ltr"
          :placeholder="$t('data.memberIdentity.attributePlaceholder')"
          class="flex-1 min-w-0 px-2 py-1.5 border rounded-md text-xs font-mono bg-white dark:bg-gray-800 text-gray-900 dark:text-gray-100 focus:outline-none"
          :class="keyError(field.field_name) ? 'border-red-400 focus:border-red-500' : 'border-gray-300 dark:border-gray-600 focus:border-blue-500'"
          :aria-label="$t('data.memberIdentity.attributeFor', { field: field.title || field.field_name })"
          :data-testid="`member-binding-key-${field.field_name}`"
          @input="setKey(field.field_name, ($event.target as HTMLInputElement).value)"
        />
        <span v-else class="flex-1 min-w-0 text-[11px] text-gray-500 dark:text-gray-400 truncate">
          {{ fixedValue(field.field_name) ? $t('data.memberIdentity.usesValue', { value: fixedValue(field.field_name) }) : $t('data.memberIdentity.usesValueAbove') }}
        </span>
      </div>
      <p v-if="keyError(field.field_name)" class="text-[11px] text-red-500">{{ keyError(field.field_name) }}</p>
    </div>
    <datalist :id="datalistId">
      <option v-for="k in attributeKeys" :key="k" :value="k" />
    </datalist>

    <p v-if="!hasBinding" class="text-[11px] text-amber-600" data-testid="member-identity-need-one">{{ $t('data.memberIdentity.needOne') }}</p>

    <!-- Who acts as whom (full admins: it reveals member attributes). -->
    <div v-if="connectionId && canPreview" class="space-y-1.5" data-testid="member-identity-preview">
      <div class="flex items-center justify-between gap-2">
        <span class="text-xs text-gray-600 dark:text-gray-400">{{ $t('data.memberIdentity.previewTitle') }}</span>
        <span v-if="preview && hasBinding" class="text-[11px] text-gray-500" data-testid="member-identity-counts">
          {{ $t('data.memberIdentity.previewCounts', { ok: preview.ok_count, blocked: preview.blocked_count }) }}
        </span>
      </div>
      <p v-if="previewError" class="text-[11px] text-red-500">{{ previewError }}</p>
      <div v-else-if="preview && hasBinding" class="max-h-56 overflow-y-auto border border-gray-100 dark:border-gray-800 rounded-md divide-y divide-gray-100 dark:divide-gray-800">
        <div v-for="row in preview.members" :key="row.membership_id" class="px-2 py-1.5 text-xs" :data-testid="`member-identity-row-${row.email}`">
          <div class="flex items-center gap-2">
            <UIcon
              :name="row.status === 'ok' ? 'heroicons-check-circle' : 'heroicons-no-symbol'"
              class="w-3.5 h-3.5 shrink-0"
              :class="row.status === 'ok' ? 'text-green-600' : 'text-red-500'"
            />
            <span class="truncate min-w-0 flex-1 text-gray-700 dark:text-gray-300" :title="row.email || ''">{{ row.email }}</span>
            <span v-if="row.status === 'ok'" class="font-mono text-gray-600 dark:text-gray-400 truncate max-w-[45%]" dir="ltr" :title="identityLabel(row)">{{ identityLabel(row) }}</span>
            <span v-else class="text-red-500 truncate max-w-[45%]" :title="missingLabel(row)">{{ missingLabel(row) }}</span>
            <button
              v-if="row.status === 'ok'"
              type="button"
              class="text-[11px] text-blue-600 hover:underline disabled:opacity-50 shrink-0"
              :disabled="!!testing[row.user_id]"
              :data-testid="`member-identity-test-${row.email}`"
              @click="testMember(row)"
            >{{ testing[row.user_id] ? $t('data.testing') : $t('data.memberIdentity.test') }}</button>
          </div>
          <p
            v-if="testResults[row.user_id]"
            class="ps-5 pt-0.5 text-[11px] break-words"
            :class="testResults[row.user_id].success ? 'text-green-700 dark:text-green-500' : 'text-red-600'"
            :data-testid="`member-identity-test-result-${row.email}`"
          >
            {{ testResults[row.user_id].success ? $t('data.memberIdentity.testOk') : (testResults[row.user_id].message || $t('data.requestFailed')) }}
          </p>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
// "Require user authentication" for a source whose one shared secret can act
// as any account (Qlik's client certificate): instead of asking members for
// credentials, the admin binds identity fields of this form to a member
// attribute (Settings → Members). Each member then acts as their own account;
// a member without the attribute is blocked. Fields left "Same for everyone"
// keep the value entered above. See backend/app/services/member_identity.py.
import { useCan } from '~/composables/usePermissions'

type Kind = 'fixed' | 'attr'
type PreviewRow = { membership_id: string; user_id: string; email: string | null; identity: Record<string, string | null>; missing: string[]; status: 'ok' | 'blocked' }
type Preview = { attribute_keys: string[]; members: PreviewRow[]; ok_count: number; blocked_count: number }

const props = defineProps<{
  fields: Array<{ field_name: string; title?: string }>
  modelValue: Record<string, string>
  fixedValues?: Record<string, any>
  connectionId?: string
  readOnly?: boolean
}>()
const emit = defineEmits<{ (e: 'update:modelValue', value: Record<string, string>): void; (e: 'valid', ok: boolean): void }>()
const { t } = useI18n()

const KEY_RE = /^[A-Za-z][A-Za-z0-9_.-]{0,63}$/
const datalistId = `member-identity-keys-${Math.random().toString(36).slice(2, 8)}`
const kinds = reactive<Record<string, Kind>>({})
const keys = reactive<Record<string, string>>({})
const canPreview = computed(() => useCan('full_admin_access'))

function load(value: Record<string, string>) {
  for (const f of props.fields) {
    const key = value?.[f.field_name]
    kinds[f.field_name] = key ? 'attr' : (kinds[f.field_name] === 'attr' ? 'attr' : 'fixed')
    keys[f.field_name] = key || keys[f.field_name] || ''
  }
}
load(props.modelValue || {})
watch(() => props.fields.map(f => f.field_name).join(','), () => load(props.modelValue || {}))

const bindings = computed<Record<string, string>>(() => {
  const out: Record<string, string> = {}
  for (const f of props.fields) {
    const key = (keys[f.field_name] || '').trim()
    if (kinds[f.field_name] === 'attr' && key) out[f.field_name] = key
  }
  return out
})
const hasBinding = computed(() => Object.keys(bindings.value).length > 0)

function keyError(field: string): string | null {
  if (kinds[field] !== 'attr') return null
  const key = (keys[field] || '').trim()
  if (!key) return t('data.memberIdentity.attributeRequired')
  return KEY_RE.test(key) ? null : t('data.memberIdentity.attributeInvalid')
}
const valid = computed(() => hasBinding.value && props.fields.every(f => !keyError(f.field_name)))

function sync() {
  emit('update:modelValue', bindings.value)
  emit('valid', valid.value)
  for (const id of Object.keys(testResults)) delete testResults[id]
  schedulePreview()
}
function setKind(field: string, kind: Kind) { kinds[field] = kind; sync() }
function setKey(field: string, key: string) { keys[field] = key; sync() }
onMounted(() => emit('valid', valid.value))

function fixedValue(field: string): string {
  const v = props.fixedValues?.[field]
  return v == null ? '' : String(v).trim()
}

function identityLabel(row: PreviewRow): string {
  return props.fields.map(f => row.identity?.[f.field_name]).filter(Boolean).join(' · ')
}
function missingLabel(row: PreviewRow): string {
  return t('data.memberIdentity.blocked', { attributes: row.missing.join(', ') })
}

// ── Preview + per-member test (edit mode, full admins) ──
const preview = ref<Preview | null>(null)
const previewError = ref<string | null>(null)
const attributeKeys = computed(() => preview.value?.attribute_keys || [])
const testing = reactive<Record<string, boolean>>({})
const testResults = reactive<Record<string, { success: boolean; message: string }>>({})

let timer: ReturnType<typeof setTimeout> | null = null
function schedulePreview() {
  if (timer) clearTimeout(timer)
  timer = setTimeout(refreshPreview, 300)
}

async function refreshPreview() {
  if (!props.connectionId || !canPreview.value) return
  if (!valid.value) { preview.value = null; previewError.value = null; return }
  const { data, error } = await useMyFetch(`/connections/${props.connectionId}/member-identity/preview`, {
    method: 'POST',
    body: { member_bindings: bindings.value },
  })
  if (error.value) {
    previewError.value = (error.value as any)?.data?.detail || t('data.requestFailed')
    return
  }
  previewError.value = null
  preview.value = data.value as Preview
}

async function testMember(row: PreviewRow) {
  testing[row.user_id] = true
  try {
    const { data, error } = await useMyFetch(`/connections/${props.connectionId}/member-identity/test`, {
      method: 'POST',
      body: { user_id: row.user_id, member_bindings: bindings.value },
    })
    const res: any = error.value ? { success: false, message: (error.value as any)?.data?.detail } : data.value
    testResults[row.user_id] = { success: !!res?.success, message: res?.message || '' }
  } finally {
    testing[row.user_id] = false
  }
}

onMounted(() => { if (props.connectionId) refreshPreview() })
// The parent may hydrate saved bindings after mount, and permissions may load
// after it too — re-sync and retry the preview when either arrives.
watch(() => JSON.stringify(props.modelValue || {}), (next) => {
  if (next === JSON.stringify(bindings.value)) return
  load(props.modelValue || {})
  emit('valid', valid.value)
  refreshPreview()
})
watch(canPreview, (ok) => { if (ok) refreshPreview() })
</script>
