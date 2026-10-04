<!--
  Chat card for a submit_<list> call (executed as the submit_list gateway).

  - Running: "Saving to <list>…", with a live count of records as the model
    streams them (planner_v3 emits it into arguments._progress).
  - Saved: counts in the header; expanded, one row per record — key first,
    each value with its status chip, an evidence mark (✓ quote found in the
    source / ? not matched), a lock where a person's edit was kept, and what
    happened to the row (added / updated / unchanged). Clicking a record
    shows its quotes and links to the row in the list.
  - Rejected: the submitted records with the invalid cells marked, and the
    validation errors the agent was asked to fix (it retries on its own).
-->
<template>
  <div class="mt-1" data-testid="submit-list-tool">
    <div class="flex items-center text-xs text-gray-500 dark:text-gray-400 cursor-pointer hover:text-gray-700 dark:hover:text-gray-300" data-testid="submit-list-header" @click="isExpanded = !isExpanded">
      <span v-if="running" class="tool-shimmer flex items-center min-w-0">
        <Icon name="heroicons-list-bullet" class="w-3 h-3 me-1.5 text-gray-400 dark:text-gray-500 shrink-0" />
        <span dir="auto" class="truncate">{{ listName ? $t('tools.submitList.savingTo', { name: listName }) : $t('tools.submitList.saving') }}</span>
        <span v-if="progress" class="ms-1.5 shrink-0 text-gray-400 dark:text-gray-500" data-testid="submit-list-progress">· {{ $t('tools.submitList.writing', { n: progress }, progress) }}</span>
      </span>
      <span v-else-if="ok" class="text-gray-600 dark:text-gray-400 flex items-center min-w-0">
        <Icon name="heroicons-list-bullet" class="w-3 h-3 me-1.5 text-emerald-500 shrink-0" />
        <span dir="auto" class="truncate">{{ $t('tools.submitList.saved', { name: listName }) }}</span>
        <span v-for="c in counts" :key="c" class="ms-1.5 shrink-0 text-gray-400 dark:text-gray-500">· {{ c }}</span>
        <Icon :name="isExpanded ? 'heroicons-chevron-down' : 'heroicons-chevron-right'" class="w-3 h-3 ms-1 text-gray-400 dark:text-gray-500 shrink-0 rtl-flip" />
      </span>
      <span v-else class="text-gray-600 dark:text-gray-400 flex items-center min-w-0">
        <Icon name="heroicons-arrow-path" class="w-3 h-3 me-1.5 text-amber-500 shrink-0" />
        <span dir="auto" class="truncate">{{ $t('tools.submitList.failed', { name: listName }) }}</span>
        <span class="ms-1.5 shrink-0 text-gray-400 dark:text-gray-500">· {{ $t('tools.submitList.errorsCount', { n: errors.length }, errors.length) }} · {{ $t('tools.submitList.retry') }}</span>
        <Icon :name="isExpanded ? 'heroicons-chevron-down' : 'heroicons-chevron-right'" class="w-3 h-3 ms-1 text-gray-400 dark:text-gray-500 shrink-0 rtl-flip" />
      </span>
    </div>

    <Transition name="slide">
      <div v-if="isExpanded && !running" class="mt-2 ms-[18px] space-y-2" data-testid="submit-list-body">
        <div v-if="rows.length" class="rounded-md border border-gray-100 dark:border-gray-800 overflow-auto max-h-80">
          <table class="min-w-full text-[11px]">
            <thead class="sticky top-0 bg-gray-50 dark:bg-gray-900 text-gray-500 dark:text-gray-400">
              <tr>
                <th v-if="ok" class="px-2 py-1 w-px"></th>
                <th v-for="c in columns" :key="c" class="px-2 py-1 text-start font-mono font-medium whitespace-nowrap">{{ c }}</th>
              </tr>
            </thead>
            <tbody>
              <template v-for="r in rows" :key="r.index">
                <tr
                  class="border-t border-gray-50 dark:border-gray-800/60 cursor-pointer hover:bg-gray-50 dark:hover:bg-gray-800/40"
                  :class="r.rowError ? 'bg-red-50/60 dark:bg-red-950/20' : ''"
                  data-testid="submit-list-record"
                  @click="openIdx = openIdx === r.index ? null : r.index"
                >
                  <td v-if="ok" class="px-2 py-1 whitespace-nowrap" data-testid="submit-list-action">
                    <span class="px-1.5 rounded text-[10px]" :class="actionCls(r.action)">{{ $t('tools.submitList.action.' + (r.action || 'inserted')) }}</span>
                  </td>
                  <td v-for="c in columns" :key="c" class="px-2 py-1 whitespace-nowrap max-w-[220px]" :class="cellError(r, c) ? 'ring-1 ring-inset ring-red-300 dark:ring-red-800' : ''" :title="cellError(r, c) || undefined">
                    <span class="inline-flex items-center gap-1 max-w-full">
                      <span v-if="isEmpty(r.fields[c])" class="text-gray-300 dark:text-gray-600">—</span>
                      <bdi v-else class="truncate text-gray-700 dark:text-gray-300">{{ fmt(r.fields[c]?.value) }}</bdi>
                      <span v-if="chip(r.fields[c]?.status)" class="shrink-0 px-1 rounded text-[9px]" :class="chipCls(r.fields[c]?.status)">{{ chip(r.fields[c]?.status) }}</span>
                      <Icon v-if="r.fields[c]?.quote && typeof r.fields[c]?.verified === 'boolean'" :name="r.fields[c]?.verified ? 'heroicons-check-circle' : 'heroicons-question-mark-circle'" class="w-3 h-3 shrink-0" :class="r.fields[c]?.verified ? 'text-emerald-500' : 'text-amber-500'" :title="r.fields[c]?.verified ? $t('lists.row.verified') : $t('lists.row.unverified')" />
                      <Icon v-else-if="r.fields[c]?.quote" name="heroicons-chat-bubble-bottom-center-text" class="w-3 h-3 shrink-0 text-gray-300 dark:text-gray-600" />
                      <Icon v-if="r.fields[c]?.locked" name="heroicons-lock-closed" class="w-3 h-3 shrink-0 text-gray-400" :title="$t('tools.submitList.keptEdit')" />
                    </span>
                  </td>
                </tr>
                <tr v-if="openIdx === r.index" class="bg-gray-50/60 dark:bg-gray-900/40" data-testid="submit-list-record-detail">
                  <td :colspan="columns.length + (ok ? 1 : 0)" class="px-3 py-2">
                    <div class="space-y-1.5">
                      <div v-for="c in columns.filter(c => r.fields[c]?.quote || r.fields[c]?.note || cellError(r, c))" :key="c" class="flex flex-wrap items-baseline gap-x-2 text-[11px]">
                        <span class="shrink-0 font-mono text-gray-500 dark:text-gray-400">{{ c }}</span>
                        <span v-if="cellError(r, c)" class="text-red-600 dark:text-red-400">{{ cellError(r, c) }}</span>
                        <bdi v-if="r.fields[c]?.quote" class="italic text-gray-700 dark:text-gray-300">“{{ r.fields[c]?.quote }}”</bdi>
                        <span v-if="r.fields[c]?.quote && (r.fields[c]?.ref || r.fields[c]?.page)" class="text-gray-400">({{ [r.fields[c]?.ref, r.fields[c]?.page ? $t('lists.row.page', { n: r.fields[c]?.page }) : ''].filter(Boolean).join(' · ') }})</span>
                        <bdi v-if="r.fields[c]?.note" class="text-gray-500 dark:text-gray-400">— {{ r.fields[c]?.note }}</bdi>
                      </div>
                      <div v-if="r.rowError" class="text-[11px] text-red-600 dark:text-red-400">{{ r.rowError }}</div>
                      <a v-if="ok && r.rowId && dsId" :href="`/agents/${dsId}/lists/${listId}/${r.rowId}`" class="inline-flex items-center gap-1 text-[11px] text-gray-500 dark:text-gray-400 hover:text-gray-900 dark:hover:text-white" data-testid="submit-list-open-row" @click.stop>
                        <Icon name="heroicons-arrow-top-right-on-square" class="w-3 h-3" />{{ $t('tools.submitList.openRow') }}
                      </a>
                    </div>
                  </td>
                </tr>
              </template>
            </tbody>
          </table>
        </div>

        <ul v-if="!ok && unplacedErrors.length" class="rounded-md border border-amber-100 dark:border-amber-900/50 bg-amber-50/50 dark:bg-amber-950/20 px-3 py-2 text-[11px] font-mono text-amber-800 dark:text-amber-300 space-y-0.5" data-testid="submit-list-errors">
          <li v-for="(e, i) in unplacedErrors.slice(0, 8)" :key="i" class="break-all">{{ e }}</li>
        </ul>

        <div v-if="ok && dsId" class="flex items-center gap-3 text-[11px]">
          <a :href="`/agents/${dsId}/lists/${listId}`" class="text-gray-500 dark:text-gray-400 hover:text-gray-900 dark:hover:text-white inline-flex items-center gap-1" data-testid="submit-list-open">
            <Icon name="heroicons-arrow-top-right-on-square" class="w-3 h-3" />{{ $t('tools.submitList.open') }}
          </a>
          <button v-if="reportId" type="button" class="text-gray-500 dark:text-gray-400 hover:text-gray-900 dark:hover:text-white inline-flex items-center gap-1" data-testid="submit-list-csv" @click.stop="downloadCsv">
            <Icon name="heroicons-arrow-down-tray" class="w-3 h-3" />{{ $t('tools.submitList.downloadCsv') }}
          </button>
        </div>
      </div>
    </Transition>
  </div>
</template>

<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { useMyFetch } from '~/composables/useMyFetch'

type FieldOut = { value?: any; status?: string; quote?: string | null; page?: number | null; ref?: string | null; verified?: boolean | null; locked?: boolean; note?: string | null }
type RecordOut = { record: number; row_id?: string; action?: string; key?: any; fields: Record<string, FieldOut> }

interface Props {
  toolExecution: {
    tool_name: string
    arguments_json?: { list_id?: string; records?: any[]; _progress?: { records?: number; list_name?: string } }
    result_json?: {
      success?: boolean; list_id?: string; list_name?: string; data_source_id?: string
      inserted?: number; updated?: number; unchanged?: number; errors?: string[]
      records?: RecordOut[]; rows?: any[]
    }
    status: string
  }
  readonly?: boolean
}
const props = defineProps<Props>()
const { t } = useI18n()
const route = useRoute()

const isExpanded = ref(false)
const openIdx = ref<number | null>(null)
const status = computed(() => props.toolExecution.status)
const res = computed(() => props.toolExecution.result_json || {})
const args = computed(() => props.toolExecution.arguments_json || {})
const running = computed(() => status.value === 'running' || status.value === 'in_progress')
const ok = computed(() => !running.value && status.value === 'success' && res.value.success !== false)
// The streamed name arrives on the kickoff args; tool.started later swaps in
// the real arguments, so remember it for the rest of the running state.
const streamedName = ref('')
watch(() => args.value._progress?.list_name, (n) => { if (n) streamedName.value = n }, { immediate: true })
const listName = computed(() => res.value.list_name || args.value._progress?.list_name || streamedName.value || '')
const listId = computed(() => res.value.list_id || args.value.list_id || '')
const dsId = computed(() => res.value.data_source_id || '')
const errors = computed(() => res.value.errors || [])
const reportId = computed(() => (route.params as any)?.id as string | undefined)
const progress = computed(() => {
  const n = args.value._progress?.records ?? (Array.isArray(args.value.records) ? args.value.records.length : 0)
  return n > 0 ? n : 0
})

const lockedCount = computed(() => (res.value.records || []).reduce((n, r) => n + Object.values(r.fields || {}).filter(f => f.locked).length, 0))
const counts = computed(() => {
  const out: string[] = []
  if (res.value.inserted) out.push(t('tools.submitList.added', { n: res.value.inserted }))
  if (res.value.updated) out.push(t('tools.submitList.updated', { n: res.value.updated }))
  if (res.value.unchanged) out.push(t('tools.submitList.unchanged', { n: res.value.unchanged }))
  if (lockedCount.value) out.push(t('tools.submitList.kept', { n: lockedCount.value }, lockedCount.value))
  return out
})

// Rows: the executor's per-record detail when saved; otherwise (rejected, or
// an older run without it) the submitted arguments.
const rows = computed(() => {
  const detail = res.value.records
  if (ok.value && Array.isArray(detail) && detail.length) {
    return detail.map(r => ({ index: r.record, rowId: r.row_id, action: r.action, fields: r.fields || {}, rowError: '' }))
  }
  return (args.value.records || []).map((r: any, i: number) => {
    const fields: Record<string, FieldOut> = {}
    for (const [k, env] of Object.entries(r?.fields || {})) {
      if (env === null) continue
      const e: any = env
      const ev = (e?.evidence || []).find((x: any) => x?.quote)
      fields[k] = { value: e?.value ?? null, status: e?.status, quote: ev?.quote, page: ev?.page, ref: ev?.ref, verified: null, note: e?.note }
    }
    return { index: i, rowId: undefined, action: undefined, fields, rowError: rowErrors.value[i] || '' }
  })
})
const columns = computed(() => {
  const cols: string[] = []
  for (const r of rows.value) for (const k of Object.keys(r.fields)) if (!cols.includes(k)) cols.push(k)
  return cols
})

// Validation errors are path-qualified: records.<i>.fields.<name>[.value|.status|.evidence]: msg
const ERR_RE = /^records\.(\d+)\.(?:fields\.([A-Za-z0-9_]+)|row_id)[^:]*:\s*(.*)$/
const cellErrors = computed(() => {
  const out: Record<string, string> = {}
  for (const e of errors.value) {
    const m = ERR_RE.exec(e)
    if (m && m[2]) out[`${m[1]}:${m[2]}`] = m[3]
  }
  return out
})
const rowErrors = computed(() => {
  const out: Record<number, string> = {}
  for (const e of errors.value) {
    const m = ERR_RE.exec(e)
    if (m && !m[2]) out[Number(m[1])] = m[3]
  }
  return out
})
const unplacedErrors = computed(() => errors.value.filter(e => !ERR_RE.test(e)))
const cellError = (r: any, c: string) => (!ok.value && cellErrors.value[`${r.index}:${c}`]) || ''

const isEmpty = (f?: FieldOut) => !f || f.value === null || f.value === undefined || f.value === ''
const fmt = (v: any) => {
  if (typeof v === 'number') return v.toLocaleString(undefined, { maximumFractionDigits: 4 })
  if (typeof v === 'boolean') return v ? t('lists.row.yes') : t('lists.row.no')
  return String(v)
}
const chip = (s?: string) => (s && s !== 'found') ? t('lists.row.status.' + s) : ''
const chipCls = (s?: string) => s === 'not_found' ? 'bg-gray-100 text-gray-500 dark:bg-gray-800 dark:text-gray-400'
  : s === 'ambiguous' ? 'bg-amber-100 text-amber-800 dark:bg-amber-500/10 dark:text-amber-400'
  : 'bg-blue-50 text-blue-700 dark:bg-blue-500/10 dark:text-blue-300'
const actionCls = (a?: string) => a === 'updated' ? 'bg-blue-50 text-blue-700 dark:bg-blue-500/10 dark:text-blue-300'
  : a === 'unchanged' ? 'bg-gray-100 text-gray-500 dark:bg-gray-800 dark:text-gray-400'
  : 'bg-emerald-50 text-emerald-700 dark:bg-emerald-500/10 dark:text-emerald-400'

async function downloadCsv() {
  if (!dsId.value || !listId.value || !reportId.value) return
  const { data } = await useMyFetch<Blob>(
    `/api/data_sources/${dsId.value}/lists/${listId.value}/rows.csv?report_id=${encodeURIComponent(reportId.value)}`,
    { method: 'GET', responseType: 'blob' as any })
  if (!data.value) return
  const url = URL.createObjectURL(data.value as Blob)
  const a = document.createElement('a')
  a.href = url
  a.download = `${(res.value.list_name || 'list').replace(/[^\w\d֐-׿ .-]+/g, '').trim() || 'list'}.csv`
  document.body.appendChild(a); a.click(); document.body.removeChild(a)
  URL.revokeObjectURL(url)
}
</script>
