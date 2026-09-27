<!--
  Side panel for one list row: every field with its value, status, evidence
  and lock state; editing for agent managers; history with revert.
-->
<template>
  <aside class="absolute inset-y-0 end-0 w-full sm:w-[420px] bg-white dark:bg-gray-900 border-s border-gray-200 dark:border-gray-800 shadow-xl z-20 flex flex-col" data-testid="list-row-panel">
    <div class="shrink-0 h-11 px-4 flex items-center gap-2 border-b border-gray-100 dark:border-gray-800">
      <span dir="auto" class="text-[13px] font-medium text-gray-900 dark:text-white truncate">{{ title }}</span>
      <a v-if="row.report_id" :href="`/reports/${row.report_id}`" target="_blank" class="ms-auto shrink-0 text-[11px] text-gray-500 dark:text-gray-400 hover:text-gray-800 dark:hover:text-gray-200 inline-flex items-center gap-1" data-testid="list-row-open-report">
        {{ $t('lists.row.openReport') }}<UIcon name="i-heroicons-arrow-top-right-on-square" class="w-3 h-3" />
      </a>
      <button type="button" class="h-7 w-7 rounded-md flex items-center justify-center text-gray-400 hover:bg-gray-100 dark:hover:bg-gray-800 shrink-0" :class="row.report_id ? '' : 'ms-auto'" @click="$emit('close')">
        <UIcon name="i-heroicons-x-mark" class="w-4 h-4" />
      </button>
    </div>

    <div class="flex-1 min-h-0 overflow-y-auto px-4 py-3 space-y-4">
      <div v-if="row.stale" class="rounded-md bg-amber-50 dark:bg-amber-500/10 text-amber-800 dark:text-amber-300 text-[11px] px-2.5 py-1.5 flex items-center gap-1.5">
        <UIcon name="i-heroicons-exclamation-triangle" class="w-3.5 h-3.5" />{{ $t('lists.row.stale') }}
      </div>

      <div v-for="f in list.fields" :key="f.id" class="space-y-1.5" :data-testid="`list-row-field-${f.name}`">
        <div class="flex items-center gap-1.5">
          <span class="text-[11px] font-mono text-gray-500 dark:text-gray-400">{{ f.name }}</span>
          <span v-if="env(f)?.status && env(f)!.status !== 'found'" class="text-[10px] px-1.5 h-4 inline-flex items-center rounded" :class="statusCls(env(f)!.status!)">{{ $t('lists.row.status.' + env(f)!.status) }}</span>
          <span v-if="isLocked(f)" class="ms-auto inline-flex items-center gap-1 text-[10px] text-gray-500 dark:text-gray-400" :title="$t('lists.row.edited')">
            <UIcon name="i-heroicons-lock-closed" class="w-3 h-3" />
            <button v-if="canManage" type="button" data-testid="list-row-unlock" class="underline decoration-dotted hover:text-gray-800 dark:hover:text-gray-200" @click="unlock(f)">{{ $t('lists.row.unlock') }}</button>
          </span>
        </div>

        <template v-if="canManage">
          <select v-if="f.type === 'enum' || f.type === 'boolean'" v-model="draft[f.id!]" :data-testid="`list-row-input-${f.name}`" :class="inputCls">
            <option value="">—</option>
            <template v-if="f.type === 'enum'"><option v-for="v in f.enum || []" :key="v" :value="v">{{ v }}</option></template>
            <template v-else><option value="true">{{ $t('lists.row.yes') }}</option><option value="false">{{ $t('lists.row.no') }}</option></template>
          </select>
          <input v-else v-model="draft[f.id!]" :type="f.type === 'date' ? 'date' : 'text'" :inputmode="f.type === 'number' || f.type === 'integer' ? 'decimal' : undefined" dir="auto" :data-testid="`list-row-input-${f.name}`" :class="inputCls" />
        </template>
        <div v-else dir="auto" class="text-[13px] text-gray-900 dark:text-gray-100 min-h-[20px]">
          <span v-if="display(f)">{{ display(f) }}</span><span v-else class="text-gray-300 dark:text-gray-600">—</span>
        </div>

        <div v-for="(ev, i) in quotes(f)" :key="i" class="rounded-md bg-gray-50 dark:bg-gray-800/60 px-2.5 py-1.5" :class="ev.superseded ? 'opacity-60' : ''" data-testid="list-row-evidence">
          <div class="flex items-start gap-1.5">
            <UIcon :name="ev.verified ? 'i-heroicons-check-circle' : 'i-heroicons-question-mark-circle'" class="w-3.5 h-3.5 mt-0.5 shrink-0" :class="ev.verified ? 'text-emerald-500' : 'text-amber-500'" :title="ev.verified ? $t('lists.row.verified') : $t('lists.row.unverified')" />
            <q dir="auto" class="text-[12px] leading-relaxed text-gray-700 dark:text-gray-300 italic">{{ ev.quote }}</q>
          </div>
          <div v-if="ev.ref || ev.page" class="mt-0.5 ps-5 text-[10px] text-gray-400 dark:text-gray-500 truncate">
            {{ [ev.ref, ev.page ? $t('lists.row.page', { n: ev.page }) : ''].filter(Boolean).join(' · ') }}
          </div>
        </div>
        <p v-if="env(f)?.note" dir="auto" class="text-[11px] text-gray-500 dark:text-gray-400">{{ env(f)!.note }}</p>
      </div>

      <div class="pt-2 border-t border-gray-100 dark:border-gray-800">
        <button type="button" data-testid="list-row-history-toggle" class="flex items-center gap-1 text-[11px] font-medium text-gray-500 dark:text-gray-400 hover:text-gray-800 dark:hover:text-gray-200" @click="toggleHistory">
          <UIcon :name="showHistory ? 'i-heroicons-chevron-down' : 'i-heroicons-chevron-right'" class="w-3 h-3 rtl:rotate-180" />{{ $t('lists.row.history') }}
        </button>
        <div v-if="showHistory" class="mt-2 space-y-1.5" data-testid="list-row-history">
          <div v-if="historyLoading" class="flex items-center gap-2 text-[11px] text-gray-400"><Spinner class="w-3 h-3" />{{ $t('lists.loading') }}</div>
          <div v-else-if="!history.length" class="text-[11px] text-gray-400">{{ $t('lists.row.noHistory') }}</div>
          <div v-for="h in history" :key="h.id" class="flex items-center gap-2 text-[11px] text-gray-600 dark:text-gray-400">
            <UIcon :name="h.actor_type === 'agent' ? 'i-heroicons-sparkles' : 'i-heroicons-user'" class="w-3 h-3 shrink-0 text-gray-400" />
            <span class="truncate">
              <b class="font-medium text-gray-800 dark:text-gray-200">{{ h.actor_type === 'agent' ? $t('lists.row.agent') : (h.actor_name || '') }}</b>
              {{ $t('lists.row.action.' + h.action) }}
              <span class="font-mono text-gray-500">{{ changedNames(h) }}</span>
            </span>
            <span class="ms-auto shrink-0 text-gray-400">{{ shortTime(h.created_at) }}</span>
            <button v-if="canManage && (h.action === 'update' || h.action === 'revert')" type="button" data-testid="list-row-revert" class="shrink-0 underline decoration-dotted hover:text-gray-900 dark:hover:text-white" @click="revert(h)">{{ $t('lists.row.revert') }}</button>
          </div>
        </div>
      </div>
    </div>

    <div v-if="canManage" class="shrink-0 px-4 py-3 border-t border-gray-100 dark:border-gray-800 flex items-center gap-2">
      <button type="button" data-testid="list-row-delete" class="h-8 px-2.5 rounded-md text-xs font-medium text-red-600 hover:bg-red-50 dark:hover:bg-red-500/10" @click="removeRow">{{ $t('lists.row.deleteRow') }}</button>
      <div v-if="error" class="text-[11px] text-red-600 truncate" data-testid="list-row-error">{{ error }}</div>
      <button type="button" data-testid="list-row-save" :disabled="!dirty || saving" class="ms-auto h-8 px-3 rounded-md bg-gray-900 dark:bg-white text-white dark:text-gray-900 text-xs font-medium hover:bg-gray-800 dark:hover:bg-gray-100 disabled:opacity-40 inline-flex items-center gap-1.5" @click="save">
        <Spinner v-if="saving" class="w-3 h-3" />{{ $t('lists.row.saveChanges') }}
      </button>
    </div>
  </aside>
</template>

<script setup lang="ts">
import { computed, reactive, ref, watch } from 'vue'
import Spinner from '~/components/Spinner.vue'
import { useMyFetch } from '~/composables/useMyFetch'
import { formatValue, type AgentList, type Envelope, type ListField, type ListRow, type Revision } from '~/components/lists/types'

const props = defineProps<{ dsId: string; list: AgentList; row: ListRow; canManage: boolean }>()
const emit = defineEmits<{ (e: 'close'): void; (e: 'updated', row: ListRow): void; (e: 'deleted', id: string): void }>()

const { t } = useI18n()
const toast = useToast()
const inputCls = 'w-full h-8 px-2.5 text-[13px] bg-white dark:bg-gray-800 border border-gray-200 dark:border-gray-700 dark:text-gray-100 rounded-md outline-none focus:border-gray-400'
const rowUrl = computed(() => `/api/data_sources/${props.dsId}/lists/${props.list.id}/rows/${props.row.id}`)

const env = (f: ListField): Envelope | undefined => props.row.values[f.id!]
const display = (f: ListField) => formatValue(f, env(f)?.value, t)
const isLocked = (f: ListField) => props.row.locked_fields.includes(f.id!)
const quotes = (f: ListField) => (env(f)?.evidence || []).filter(e => e.quote)
const statusCls = (s: string) => s === 'not_found' ? 'bg-gray-100 text-gray-500 dark:bg-gray-800 dark:text-gray-400'
  : s === 'ambiguous' ? 'bg-amber-100 text-amber-800 dark:bg-amber-500/10 dark:text-amber-400'
  : 'bg-blue-50 text-blue-700 dark:bg-blue-500/10 dark:text-blue-300'

const keyField = computed(() => props.list.fields.find(f => f.id === props.list.key_field_id) || props.list.fields[0])
const title = computed(() => display(keyField.value) || props.list.name)

const toDraft = (f: ListField): string => {
  const v = env(f)?.value
  if (v === null || v === undefined) return ''
  return String(v)
}
const draft = reactive<Record<string, string>>(Object.fromEntries(props.list.fields.map(f => [f.id!, toDraft(f)])))
// A save / revert / unlock hands back a new row: reset the draft to it.
watch(() => props.row, () => {
  for (const f of props.list.fields) draft[f.id!] = toDraft(f)
})
const changedFields = computed(() => props.list.fields.filter(f => draft[f.id!] !== toDraft(f)))
const dirty = computed(() => changedFields.value.length > 0)
const saving = ref(false)
const error = ref('')

async function patch(body: Record<string, any>) {
  saving.value = true
  error.value = ''
  try {
    const { data, error: err } = await useMyFetch<ListRow>(rowUrl.value, { method: 'PATCH', body: { row_version: props.row.row_version, ...body } })
    if (err.value) {
      const status = (err.value as any)?.statusCode || (err.value as any)?.status
      const detail = (err.value as any)?.data?.detail
      if (status === 409) {
        toast.add({ title: t('lists.row.conflict'), color: 'amber' })
        await reloadRow()
        return
      }
      error.value = detail?.errors?.join('; ') || (typeof detail === 'string' ? detail : t('lists.toastError'))
      return
    }
    // No success toast: it would sit over this panel's footer, and the value +
    // lock icon updating in place is the confirmation.
    emit('updated', data.value as ListRow)
    if (showHistory.value) loadHistory()
  } finally {
    saving.value = false
  }
}

async function reloadRow() {
  const { data } = await useMyFetch<any>(`/api/data_sources/${props.dsId}/lists/${props.list.id}/rows?limit=500`, { method: 'GET' })
  const fresh = (data.value?.rows || []).find((r: ListRow) => r.id === props.row.id)
  if (fresh) emit('updated', fresh)
}

function save() {
  const fields: Record<string, any> = {}
  for (const f of changedFields.value) fields[f.id!] = draft[f.id!] === '' ? null : draft[f.id!]
  patch({ fields })
}
function unlock(f: ListField) { patch({ fields: {}, unlock: [f.id] }) }

async function removeRow() {
  const { error: err } = await useMyFetch(rowUrl.value, { method: 'DELETE' })
  if (err.value) { toast.add({ title: t('lists.toastError'), color: 'red' }); return }
  toast.add({ title: t('lists.row.deleted'), color: 'green' })
  emit('deleted', props.row.id)
}

const showHistory = ref(false)
const history = ref<Revision[]>([])
const historyLoading = ref(false)
async function loadHistory() {
  historyLoading.value = true
  try {
    const { data } = await useMyFetch<Revision[]>(`${rowUrl.value}/revisions`, { method: 'GET' })
    history.value = (data.value || []) as Revision[]
  } finally {
    historyLoading.value = false
  }
}
function toggleHistory() {
  showHistory.value = !showHistory.value
  if (showHistory.value) loadHistory()
}
const changedNames = (h: Revision) => Object.keys(h.changed || {})
  .map(id => props.list.fields.find(f => f.id === id)?.name).filter(Boolean).join(', ')

async function revert(h: Revision) {
  const { data, error: err } = await useMyFetch<ListRow>(`${rowUrl.value}/revisions/${h.id}/revert`, { method: 'POST' })
  if (err.value) { toast.add({ title: t('lists.toastError'), color: 'red' }); return }
  emit('updated', data.value as ListRow)
  loadHistory()
}

const _df = useFormatDate()
const shortTime = (s?: string) => { if (!s) return ''; try { return _df.format(s, { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' }) } catch { return s } }
</script>
