<!--
  Chat card for a submit_<list> call (executed as the submit_list gateway).

  Collapsed: "Saved to Contracts · 3 added · 1 updated". Expanded: the records
  the agent submitted as a compact table, with links to open the list and to
  download this run's rows as CSV. A rejected submission shows the validation
  errors the agent was asked to fix — it retries on its own.
-->
<template>
  <div class="mt-1" data-testid="submit-list-tool">
    <div class="flex items-center text-xs text-gray-500 dark:text-gray-400 cursor-pointer hover:text-gray-700 dark:hover:text-gray-300" @click="isExpanded = !isExpanded">
      <span v-if="status === 'running'" class="tool-shimmer flex items-center">
        <Icon name="heroicons-list-bullet" class="w-3 h-3 me-1.5 text-gray-400 dark:text-gray-500" />
        {{ $t('tools.submitList.saving') }}
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
        <span class="ms-1.5 shrink-0 text-gray-400 dark:text-gray-500">· {{ $t('tools.submitList.retry') }}</span>
        <Icon :name="isExpanded ? 'heroicons-chevron-down' : 'heroicons-chevron-right'" class="w-3 h-3 ms-1 text-gray-400 dark:text-gray-500 shrink-0 rtl-flip" />
      </span>
    </div>

    <Transition name="slide">
      <div v-if="isExpanded" class="mt-2 ms-[18px] space-y-2">
        <ul v-if="!ok && errors.length" class="rounded-md border border-amber-100 dark:border-amber-900/50 bg-amber-50/50 dark:bg-amber-950/20 px-3 py-2 text-[11px] font-mono text-amber-800 dark:text-amber-300 space-y-0.5">
          <li v-for="(e, i) in errors.slice(0, 8)" :key="i" class="break-all">{{ e }}</li>
        </ul>

        <div v-if="ok && records.length" class="rounded-md border border-gray-100 dark:border-gray-800 overflow-auto max-h-64">
          <table class="min-w-full text-[11px]">
            <thead class="bg-gray-50 dark:bg-gray-900 text-gray-500 dark:text-gray-400">
              <tr><th v-for="c in columns" :key="c" class="px-2 py-1 text-start font-mono font-medium whitespace-nowrap">{{ c }}</th></tr>
            </thead>
            <tbody>
              <tr v-for="(r, i) in records" :key="i" class="border-t border-gray-50 dark:border-gray-800/60">
                <td v-for="c in columns" :key="c" class="px-2 py-1 text-start whitespace-nowrap max-w-[220px] truncate" :class="r[c] === null ? 'text-gray-300 dark:text-gray-600' : 'text-gray-700 dark:text-gray-300'"><bdi>{{ fmt(r[c]) }}</bdi></td>
              </tr>
            </tbody>
          </table>
        </div>

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
import { computed, ref } from 'vue'
import { useMyFetch } from '~/composables/useMyFetch'

interface Props {
  toolExecution: {
    tool_name: string
    arguments_json?: { list_id?: string; records?: any[] }
    result_json?: {
      success?: boolean; list_id?: string; list_name?: string; data_source_id?: string
      inserted?: number; updated?: number; unchanged?: number; errors?: string[]
    }
    status: string
  }
  readonly?: boolean
}
const props = defineProps<Props>()
const { t } = useI18n()
const route = useRoute()

const isExpanded = ref(false)
const status = computed(() => props.toolExecution.status)
const res = computed(() => props.toolExecution.result_json || {})
const ok = computed(() => status.value === 'success' && res.value.success !== false)
const listName = computed(() => res.value.list_name || t('tools.submitList.list'))
const listId = computed(() => res.value.list_id || props.toolExecution.arguments_json?.list_id || '')
const dsId = computed(() => res.value.data_source_id || '')
const errors = computed(() => res.value.errors || [])
const reportId = computed(() => (route.params as any)?.id as string | undefined)
const counts = computed(() => {
  const out: string[] = []
  if (res.value.inserted) out.push(t('tools.submitList.added', { n: res.value.inserted }))
  if (res.value.updated) out.push(t('tools.submitList.updated', { n: res.value.updated }))
  if (res.value.unchanged) out.push(t('tools.submitList.unchanged', { n: res.value.unchanged }))
  return out
})

// The submitted records, flattened to value-only rows for a compact preview.
const records = computed(() => (props.toolExecution.arguments_json?.records || []).map((r: any) => {
  const row: Record<string, any> = {}
  for (const [k, env] of Object.entries(r?.fields || {})) row[k] = env === null ? null : ((env as any)?.value ?? null)
  return row
}))
const columns = computed(() => {
  const cols: string[] = []
  for (const r of records.value) for (const k of Object.keys(r)) if (!cols.includes(k)) cols.push(k)
  return cols
})

const fmt = (v: any) => {
  if (v === null || v === undefined) return '—'
  if (typeof v === 'number') return v.toLocaleString(undefined, { maximumFractionDigits: 4 })
  if (typeof v === 'boolean') return v ? t('lists.row.yes') : t('lists.row.no')
  return String(v)
}

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
