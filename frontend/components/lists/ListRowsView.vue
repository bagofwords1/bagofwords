<!--
  One list: its rows as a grid, plus the actions around it.

  Clicking a row opens a side panel with every field: the value (editable for
  agent managers), its status, the evidence quote the agent cited (with whether
  the quote was found in the source), who last changed it, and the history
  with revert. A field a person edited is locked — the agent will not
  overwrite it on later runs — and can be handed back from the same panel.

  Managers can tick rows to delete them, or clear every row from the menu;
  both keep the list itself (fields, save tool, table) so it can be refilled.
-->
<template>
  <div class="flex flex-col h-full min-h-0 relative" data-testid="list-rows-view">
    <div class="shrink-0 px-6 pt-3 pb-3 border-b border-gray-100 dark:border-gray-800">
      <div class="flex items-center gap-2">
        <button type="button" data-testid="list-back" class="flex items-center gap-1.5 min-w-0 rounded px-1 -mx-1 hover:bg-gray-100 dark:hover:bg-gray-800/70" @click="$emit('back')">
          <UIcon name="i-heroicons-arrow-left" class="w-3.5 h-3.5 text-gray-400 dark:text-gray-500 shrink-0 rtl:rotate-180" />
          <span class="text-[13px] text-gray-500 dark:text-gray-400">{{ $t('lists.back') }}</span>
        </button>
        <div v-if="checked.length" class="ms-auto flex items-center gap-2" data-testid="list-selection-bar">
          <span class="text-xs text-gray-600 dark:text-gray-300 tabular-nums" data-testid="list-selected-count">{{ $t('lists.selectedCount', { n: checked.length }) }}</span>
          <button type="button" class="h-8 px-2.5 rounded-md text-xs text-gray-500 dark:text-gray-400 hover:bg-gray-100 dark:hover:bg-gray-800" @click="checked = []">{{ $t('lists.clearSelection') }}</button>
          <button type="button" data-testid="list-delete-selected" class="h-8 px-2.5 rounded-md border border-red-200 dark:border-red-900/60 text-xs font-medium text-red-600 dark:text-red-400 hover:bg-red-50 dark:hover:bg-red-950/30 inline-flex items-center gap-1" @click="confirm = 'rows'">
            <UIcon name="i-heroicons-trash" class="w-3.5 h-3.5" />{{ $t('lists.delete') }}
          </button>
        </div>
        <div v-else class="ms-auto flex items-center gap-2">
          <span class="text-xs text-gray-400 dark:text-gray-500 tabular-nums" data-testid="list-row-total">{{ $t('lists.rowsCount', { n: total }, total) }}</span>
          <button
            type="button"
            data-testid="list-export-csv"
            :disabled="exporting"
            class="h-8 px-2.5 rounded-md border border-gray-200 dark:border-gray-700 text-xs font-medium text-gray-700 dark:text-gray-300 hover:bg-gray-50 dark:hover:bg-gray-800/50 inline-flex items-center gap-1 disabled:opacity-50"
            @click="exportCsv(false)"
          >
            <Spinner v-if="exporting" class="w-3 h-3" />
            <UIcon v-else name="i-heroicons-arrow-down-tray" class="w-3.5 h-3.5" />
            {{ $t('lists.exportCsv') }}
          </button>
          <UDropdown :items="menu" :popper="{ placement: 'bottom-end' }" :ui="{ width: 'w-52', item: { size: 'text-xs', padding: 'px-3 py-2' } }">
            <button type="button" data-testid="list-more" :aria-label="$t('lists.more')" class="h-8 w-8 rounded-md border border-gray-200 dark:border-gray-700 text-gray-500 dark:text-gray-400 hover:bg-gray-50 dark:hover:bg-gray-800/50 inline-flex items-center justify-center">
              <UIcon name="i-heroicons-ellipsis-horizontal" class="w-4 h-4" />
            </button>
          </UDropdown>
        </div>
      </div>
      <h2 dir="auto" class="mt-2 text-base font-semibold text-gray-900 dark:text-white" data-testid="list-title">{{ list.name }}</h2>
      <p v-if="list.description" dir="auto" class="mt-0.5 text-xs text-gray-500 dark:text-gray-400">{{ list.description }}</p>
      <p class="mt-1.5 text-[11px] text-gray-400 dark:text-gray-500 flex flex-wrap items-center gap-x-1.5 gap-y-0.5">
        <span>{{ $t('lists.saveHint') }}</span><code class="font-mono text-gray-500 dark:text-gray-400">{{ list.tool_name }}</code>
        <span>·</span>
        <span>{{ $t('lists.queryHint') }}</span><code class="font-mono text-gray-500 dark:text-gray-400">{{ list.table_name }}</code>
      </p>
    </div>

    <div class="flex-1 min-h-0 overflow-auto">
      <div v-if="loading && !rows.length" class="flex items-center gap-2 py-8 justify-center text-xs text-gray-400 dark:text-gray-500">
        <Spinner class="w-3.5 h-3.5" /><span>{{ $t('lists.loading') }}</span>
      </div>

      <div v-else-if="!rows.length" class="flex flex-col items-center justify-center text-center py-14 px-4" data-testid="list-rows-empty">
        <div class="w-12 h-12 flex items-center justify-center rounded-xl bg-white dark:bg-gray-900 ring-1 ring-gray-200/70 dark:ring-gray-700/70 shadow-sm">
          <UIcon name="i-heroicons-table-cells" class="w-5 h-5 text-gray-400 dark:text-gray-500" />
        </div>
        <h3 class="mt-3 text-sm font-medium text-gray-900 dark:text-white">{{ $t('lists.noRows') }}</h3>
        <p dir="auto" class="mt-1.5 max-w-sm text-xs leading-relaxed text-gray-500 dark:text-gray-400">{{ $t('lists.noRowsHint', { name: list.name }) }}</p>
        <div class="mt-4 flex flex-wrap justify-center gap-1.5 max-w-md">
          <span v-for="f in list.fields" :key="f.id" class="px-2 h-6 inline-flex items-center rounded-md bg-gray-50 dark:bg-gray-800 border border-gray-100 dark:border-gray-700 text-[11px] font-mono text-gray-500 dark:text-gray-400">{{ f.name }}</span>
        </div>
      </div>

      <table v-else class="min-w-full text-xs" data-testid="list-rows-table">
        <thead class="sticky top-0 z-10 bg-gray-50/95 dark:bg-gray-900/95 backdrop-blur">
          <tr class="text-start text-[11px] text-gray-500 dark:text-gray-400">
            <th v-if="list.can_manage" class="w-8 ps-3 pe-0 py-2 border-b border-gray-100 dark:border-gray-800">
              <input type="checkbox" data-testid="list-select-all" class="rounded border-gray-300 dark:border-gray-600 text-blue-500 focus:ring-blue-500/30" :aria-label="$t('lists.selectAll')" :checked="allChecked" :indeterminate="checked.length > 0 && !allChecked" @change="toggleAll" />
            </th>
            <th v-for="f in list.fields" :key="f.id" class="px-3 py-2 font-medium text-start whitespace-nowrap border-b border-gray-100 dark:border-gray-800">
              <span class="font-mono">{{ f.name }}</span>
              <UIcon v-if="f.id === list.key_field_id" name="i-heroicons-key" class="w-3 h-3 ms-1 align-[-2px] text-gray-400" />
            </th>
            <th class="px-3 py-2 font-medium text-end whitespace-nowrap border-b border-gray-100 dark:border-gray-800">{{ $t('lists.updated') }}</th>
          </tr>
        </thead>
        <tbody>
          <tr
            v-for="r in rows"
            :key="r.id"
            data-testid="list-row"
            class="cursor-pointer border-b border-gray-50 dark:border-gray-800/60 hover:bg-gray-50 dark:hover:bg-gray-800/40"
            :class="selected?.id === r.id || checked.includes(r.id) ? 'bg-blue-50/60 dark:bg-blue-500/10' : ''"
            @click="openRow(r)"
          >
            <td v-if="list.can_manage" class="w-8 ps-3 pe-0 py-2 align-top" @click.stop>
              <input v-model="checked" type="checkbox" :value="r.id" data-testid="list-row-check" class="rounded border-gray-300 dark:border-gray-600 text-blue-500 focus:ring-blue-500/30" :aria-label="$t('lists.selectRow')" />
            </td>
            <td v-for="f in list.fields" :key="f.id" class="px-3 py-2 align-top max-w-[260px]">
              <div class="flex items-center gap-1 min-w-0">
                <span v-if="cell(r, f).empty" class="text-gray-300 dark:text-gray-600">—</span>
                <span v-else class="truncate text-gray-800 dark:text-gray-200" :class="numeric(f) ? 'tabular-nums' : ''"><bdi>{{ cell(r, f).text }}</bdi></span>
                <UIcon v-if="r.locked_fields.includes(f.id!)" name="i-heroicons-lock-closed" class="w-3 h-3 shrink-0 text-gray-400" :title="$t('lists.row.edited')" />
              </div>
            </td>
            <td class="px-3 py-2 text-end whitespace-nowrap text-gray-400 dark:text-gray-500">
              <UIcon v-if="r.stale" name="i-heroicons-exclamation-triangle" class="w-3 h-3 me-1 align-[-2px] text-amber-500" :title="$t('lists.row.stale')" />
              {{ timeAgo(r.updated_at) }}
            </td>
          </tr>
        </tbody>
      </table>
      <div v-if="rows.length < total" class="py-3 text-center">
        <button type="button" class="h-7 px-3 rounded-md text-xs text-gray-600 dark:text-gray-400 hover:bg-gray-100 dark:hover:bg-gray-800" @click="loadMore">{{ $t('lists.loadMore') }}</button>
      </div>
    </div>

    <!-- Row panel -->
    <Transition name="slide-over">
      <ListRowPanel
        v-if="selected"
        :key="selected.id"
        :ds-id="dsId"
        :list="list"
        :row="selected"
        :can-manage="list.can_manage"
        @close="selected = null"
        @updated="onRowUpdated"
        @deleted="onRowDeleted"
      />
    </Transition>

    <UModal :model-value="confirm === 'rows' || confirm === 'clear'" :ui="{ width: 'sm:max-w-md' }" @update:model-value="v => { if (!v) confirm = null }">
      <div class="p-5" data-testid="list-rows-delete-confirm">
        <h3 dir="auto" class="text-sm font-semibold text-gray-900 dark:text-white">
          {{ confirm === 'clear' ? $t('lists.clearConfirmTitle', { name: list.name }) : $t('lists.deleteRowsTitle', { n: checked.length }, checked.length) }}
        </h3>
        <p class="mt-2 text-xs text-gray-500 dark:text-gray-400">{{ confirm === 'clear' ? $t('lists.clearConfirmBody', { n: total }) : $t('lists.deleteRowsBody') }}</p>
        <div class="mt-5 flex justify-end gap-2">
          <button type="button" class="h-8 px-3 rounded-md text-xs font-medium text-gray-600 dark:text-gray-400 hover:bg-gray-100 dark:hover:bg-gray-800" @click="confirm = null">{{ $t('lists.cancel') }}</button>
          <button type="button" data-testid="list-rows-delete-confirm-button" :disabled="deleting" class="h-8 px-3 rounded-md bg-red-600 text-white text-xs font-medium hover:bg-red-700 disabled:opacity-50 inline-flex items-center gap-1.5" @click="deleteRows(confirm === 'clear')">
            <Spinner v-if="deleting" class="w-3 h-3" />{{ confirm === 'clear' ? $t('lists.clear') : $t('lists.delete') }}
          </button>
        </div>
      </div>
    </UModal>

    <UModal :model-value="confirm === 'list'" :ui="{ width: 'sm:max-w-md' }" @update:model-value="v => { if (!v) confirm = null }">
      <div class="p-5" data-testid="list-delete-confirm">
        <h3 dir="auto" class="text-sm font-semibold text-gray-900 dark:text-white">{{ $t('lists.deleteConfirmTitle', { name: list.name }) }}</h3>
        <p class="mt-2 text-xs text-gray-500 dark:text-gray-400">{{ $t('lists.deleteConfirmBody') }}</p>
        <div class="mt-5 flex justify-end gap-2">
          <button type="button" class="h-8 px-3 rounded-md text-xs font-medium text-gray-600 dark:text-gray-400 hover:bg-gray-100 dark:hover:bg-gray-800" @click="confirm = null">{{ $t('lists.cancel') }}</button>
          <button type="button" data-testid="list-delete-confirm-button" :disabled="deleting" class="h-8 px-3 rounded-md bg-red-600 text-white text-xs font-medium hover:bg-red-700 disabled:opacity-50 inline-flex items-center gap-1.5" @click="deleteList">
            <Spinner v-if="deleting" class="w-3 h-3" />{{ $t('lists.delete') }}
          </button>
        </div>
      </div>
    </UModal>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import Spinner from '~/components/Spinner.vue'
import ListRowPanel from '~/components/lists/ListRowPanel.vue'
import { useMyFetch } from '~/composables/useMyFetch'
import { formatValue, type AgentList, type ListField, type ListRow } from '~/components/lists/types'

const props = defineProps<{ dsId: string; list: AgentList; rowId?: string | null }>()
const emit = defineEmits<{
  (e: 'row', rowId: string | null): void
  (e: 'back'): void
  (e: 'edit'): void
  (e: 'deleted'): void
  (e: 'changed'): void
}>()

const { t } = useI18n()
const toast = useToast()
const PAGE = 200
const rows = ref<ListRow[]>([])
const total = ref(0)
const loading = ref(true)
const selected = ref<ListRow | null>(null)
const exporting = ref(false)
const confirm = ref<null | 'list' | 'rows' | 'clear'>(null)
const deleting = ref(false)
// Rows ticked for bulk delete (managers only). Selection is over loaded rows.
const checked = ref<string[]>([])
const allChecked = computed(() => rows.value.length > 0 && rows.value.every(r => checked.value.includes(r.id)))
function toggleAll() { checked.value = allChecked.value ? [] : rows.value.map(r => r.id) }

const base = computed(() => `/api/data_sources/${props.dsId}/lists/${props.list.id}`)
const numeric = (f: ListField) => f.type === 'number' || f.type === 'integer'
const cell = (r: ListRow, f: ListField) => {
  const env = r.values[f.id!]
  const text = env ? formatValue(f, env.value, t) : ''
  return { text, empty: !text }
}

const menu = computed(() => {
  const groups: any[][] = [[
    { label: t('lists.exportWithEvidence'), icon: 'i-heroicons-document-arrow-down', click: () => exportCsv(true) },
  ]]
  if (props.list.can_manage) {
    groups.push([{ label: t('lists.editFields'), icon: 'i-heroicons-pencil-square', click: () => emit('edit') }])
    groups.push([
      { label: t('lists.clearRows'), icon: 'i-heroicons-archive-box-x-mark', class: 'text-red-600', disabled: total.value === 0, click: () => { confirm.value = 'clear' } },
      { label: t('lists.deleteList'), icon: 'i-heroicons-trash', class: 'text-red-600', click: () => { confirm.value = 'list' } },
    ])
  }
  return groups
})

async function load(offset = 0) {
  loading.value = true
  try {
    const { data } = await useMyFetch<any>(`${base.value}/rows?offset=${offset}&limit=${PAGE}`, { method: 'GET' })
    const page = data.value || { rows: [], total: 0 }
    rows.value = offset ? [...rows.value, ...page.rows] : page.rows
    total.value = page.total
  } finally {
    loading.value = false
  }
}
const loadMore = () => load(rows.value.length)

function openRow(r: ListRow) { selected.value = selected.value?.id === r.id ? null : r }
// Keep the URL in step with the open row (deep links from the chat card).
watch(() => selected.value?.id || null, (id) => emit('row', id))
// Open a deep-linked row: from the loaded page if present, else fetch it.
async function openRowById(id?: string | null) {
  if (!id || selected.value?.id === id) return
  const hit = rows.value.find(r => r.id === id)
  if (hit) { selected.value = hit; return }
  const { data } = await useMyFetch<ListRow>(`${base.value}/rows/${id}`, { method: 'GET' })
  if (data.value) selected.value = data.value as ListRow
}
function onRowUpdated(r: ListRow) {
  const i = rows.value.findIndex(x => x.id === r.id)
  if (i >= 0) rows.value[i] = r
  selected.value = r
}
function onRowDeleted(id: string) {
  rows.value = rows.value.filter(r => r.id !== id)
  checked.value = checked.value.filter(x => x !== id)
  total.value = Math.max(0, total.value - 1)
  selected.value = null
  emit('changed')
}

async function exportCsv(withEvidence: boolean) {
  if (exporting.value) return
  exporting.value = true
  try {
    const qs = withEvidence ? '?include=status,evidence,provenance' : ''
    const { data, error } = await useMyFetch<Blob>(`${base.value}/rows.csv${qs}`, { method: 'GET', responseType: 'blob' as any })
    if (error.value || !data.value) throw new Error(t('lists.toastError'))
    const url = URL.createObjectURL(data.value as Blob)
    const a = document.createElement('a')
    a.href = url
    a.download = `${props.list.slug}-${new Date().toISOString().slice(0, 10)}.csv`
    document.body.appendChild(a)
    a.click()
    document.body.removeChild(a)
    URL.revokeObjectURL(url)
  } catch (e: any) {
    toast.add({ title: t('lists.toastError'), description: e?.message, color: 'red' })
  } finally {
    exporting.value = false
  }
}

// Delete the ticked rows, or clear the list (`all`) — the list itself stays.
async function deleteRows(all: boolean) {
  deleting.value = true
  try {
    const body = all ? { all: true } : { row_ids: checked.value }
    const { data, error } = await useMyFetch<{ deleted: number }>(`${base.value}/rows/delete`, { method: 'POST', body })
    if (error.value) throw new Error(t('lists.toastError'))
    const n = data.value?.deleted ?? 0
    const gone = all ? null : new Set(checked.value)
    if (selected.value && (!gone || gone.has(selected.value.id))) selected.value = null
    checked.value = []
    confirm.value = null
    toast.add({ title: t('lists.toastRowsDeleted', { n }, n), color: 'green' })
    await load()
    emit('changed')
  } catch (e: any) {
    toast.add({ title: t('lists.toastError'), description: e?.message, color: 'red' })
  } finally {
    deleting.value = false
  }
}

async function deleteList() {
  deleting.value = true
  try {
    const { error } = await useMyFetch(base.value, { method: 'DELETE' })
    if (error.value) throw new Error(t('lists.toastError'))
    confirm.value = null
    toast.add({ title: t('lists.toastDeleted'), color: 'green' })
    emit('deleted')
  } catch (e: any) {
    toast.add({ title: t('lists.toastError'), description: e?.message, color: 'red' })
  } finally {
    deleting.value = false
  }
}

function timeAgo(iso?: string) {
  if (!iso) return ''
  const hasTZ = /Z|[+-]\d{2}:?\d{2}$/.test(iso)
  const d = new Date(hasTZ ? iso : `${iso}Z`)
  const mins = Math.floor(Math.max(0, Date.now() - d.getTime()) / 60000)
  if (mins < 1) return t('lists.justNow')
  if (mins < 60) return t('queries.timeMinutesAgo', { n: mins })
  const hrs = Math.floor(mins / 60)
  if (hrs < 24) return t('queries.timeHoursAgo', { n: hrs })
  return t('queries.timeDaysAgo', { n: Math.floor(hrs / 24) })
}

onMounted(async () => { await load(); await openRowById(props.rowId) })
watch(() => props.rowId, (id) => openRowById(id))
defineExpose({ reload: () => load() })
</script>

<style scoped>
.slide-over-enter-active, .slide-over-leave-active { transition: transform .18s ease, opacity .18s ease; }
.slide-over-enter-from, .slide-over-leave-to { transform: translateX(16px); opacity: 0; }
[dir="rtl"] .slide-over-enter-from, [dir="rtl"] .slide-over-leave-to { transform: translateX(-16px); }
</style>
