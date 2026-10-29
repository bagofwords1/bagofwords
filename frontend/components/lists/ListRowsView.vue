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
  <div ref="rootEl" class="flex flex-col h-full min-h-0 relative" :class="embedded ? 'bg-white dark:bg-gray-900' : ''" data-testid="list-rows-view">
    <!-- Side-pane header: title, a quiet meta line, icon actions. -->
    <div v-if="embedded" class="shrink-0 px-5 pt-5 pb-4" data-testid="list-pane-header">
      <div class="flex items-start gap-3">
        <div class="min-w-0 flex-1">
          <h2 dir="auto" class="text-base font-semibold tracking-tight text-gray-900 dark:text-gray-100 truncate" data-testid="list-title">{{ list.name }}</h2>
          <div class="mt-1 flex items-center gap-2 text-xs text-gray-500 dark:text-gray-400">
            <span class="tabular-nums" data-testid="list-row-total">{{ $t('lists.rowsCount', { n: total }, total) }}</span>
            <Transition name="fade">
              <span v-if="writing" class="inline-flex items-center gap-1.5 text-gray-700 dark:text-gray-300" data-testid="list-writing">
                <span class="relative flex w-1.5 h-1.5"><span class="absolute inset-0 rounded-full bg-gray-900 dark:bg-gray-100 opacity-60 animate-ping" /><span class="relative w-1.5 h-1.5 rounded-full bg-gray-900 dark:bg-gray-100" /></span>
                <span class="tool-shimmer">{{ $t('tools.submitList.writing', { n: writing }, writing) }}</span>
              </span>
            </Transition>
          </div>
          <p v-if="list.description" dir="auto" class="mt-2 text-sm leading-relaxed text-gray-500 dark:text-gray-400 line-clamp-2">{{ list.description }}</p>
        </div>
        <div class="shrink-0 flex items-center gap-0.5 -me-1.5">
          <button type="button" data-testid="list-export-csv" :disabled="exporting" :title="$t('lists.exportCsv')" :aria-label="$t('lists.exportCsv')" class="h-8 w-8 rounded-full inline-flex items-center justify-center text-gray-500 dark:text-gray-400 hover:bg-gray-100 dark:hover:bg-gray-800 hover:text-gray-900 dark:hover:text-white transition-colors disabled:opacity-50" @click="exportCsv(false)">
            <Spinner v-if="exporting" class="w-3.5 h-3.5" />
            <UIcon v-else name="i-heroicons-arrow-down-tray" class="w-4 h-4" />
          </button>
          <a v-if="fullHref" :href="fullHref" data-testid="list-panel-open-full" :title="$t('tools.submitList.openFull')" :aria-label="$t('tools.submitList.openFull')" class="h-8 w-8 rounded-full inline-flex items-center justify-center text-gray-500 dark:text-gray-400 hover:bg-gray-100 dark:hover:bg-gray-800 hover:text-gray-900 dark:hover:text-white transition-colors">
            <UIcon name="i-heroicons-arrow-top-right-on-square" class="w-4 h-4" />
          </a>
          <UDropdown :items="menu" :popper="{ placement: 'bottom-end' }" :ui="{ width: 'w-52', item: { size: 'text-xs', padding: 'px-3 py-2' } }">
            <button type="button" data-testid="list-more" :aria-label="$t('lists.more')" class="h-8 w-8 rounded-full inline-flex items-center justify-center text-gray-500 dark:text-gray-400 hover:bg-gray-100 dark:hover:bg-gray-800 hover:text-gray-900 dark:hover:text-white transition-colors">
              <UIcon name="i-heroicons-ellipsis-horizontal" class="w-4 h-4" />
            </button>
          </UDropdown>
        </div>
      </div>
    </div>
    <div v-else class="shrink-0 px-6 pt-3 pb-3 border-b border-gray-100 dark:border-gray-800">
      <div class="flex items-center gap-2">
        <button type="button" data-testid="list-back" class="flex items-center gap-1.5 min-w-0 rounded px-1 -mx-1 hover:bg-gray-100 dark:hover:bg-gray-800/70" @click="$emit('back')">
          <UIcon name="i-heroicons-arrow-left" class="w-3.5 h-3.5 text-ink-muted shrink-0 rtl:rotate-180" />
          <span class="text-sm text-gray-500 dark:text-gray-400">{{ $t('lists.back') }}</span>
        </button>
        <div v-if="checked.length" class="ms-auto flex items-center gap-2" data-testid="list-selection-bar">
          <span class="text-xs text-gray-600 dark:text-gray-300 tabular-nums" data-testid="list-selected-count">{{ $t('lists.selectedCount', { n: checked.length }) }}</span>
          <button type="button" class="h-8 px-2.5 rounded-md text-xs text-gray-500 dark:text-gray-400 hover:bg-gray-100 dark:hover:bg-gray-800" @click="checked = []">{{ $t('lists.clearSelection') }}</button>
          <button type="button" data-testid="list-delete-selected" class="h-8 px-2.5 rounded-md border border-red-200 dark:border-red-900/60 text-xs font-medium text-red-600 dark:text-red-400 hover:bg-red-50 dark:hover:bg-red-950/30 inline-flex items-center gap-1" @click="confirm = 'rows'">
            <UIcon name="i-heroicons-trash" class="w-3.5 h-3.5" />{{ $t('lists.delete') }}
          </button>
        </div>
        <div v-else class="ms-auto flex items-center gap-2">
          <span class="text-xs text-ink-muted tabular-nums" data-testid="list-row-total">{{ $t('lists.rowsCount', { n: total }, total) }}</span>
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
      <p v-if="list.shared" class="mt-1 flex items-center gap-1 text-2xs text-ink-muted" data-testid="list-shared">
        <UIcon name="i-heroicons-link" class="w-3 h-3 shrink-0" />
        <span dir="auto">{{ sharedWithLabel(list, t) }}</span>
      </p>
      <p class="mt-1.5 text-2xs text-ink-muted flex flex-wrap items-center gap-x-1.5 gap-y-0.5">

        <span>{{ $t('lists.saveHint') }}</span><code class="font-mono text-gray-500 dark:text-gray-400">{{ list.tool_name }}</code>
        <span>·</span>
        <span>{{ $t('lists.queryHint') }}</span><code class="font-mono text-gray-500 dark:text-gray-400">{{ list.table_name }}</code>
      </p>
    </div>

    <div class="flex-1 min-h-0 overflow-auto">
      <div v-if="loading && !rows.length" class="flex items-center gap-2 py-8 justify-center text-xs text-ink-muted">
        <Spinner class="w-3.5 h-3.5" /><span>{{ $t('lists.loading') }}</span>
      </div>

      <div v-else-if="!rows.length" class="flex flex-col items-center justify-center text-center py-14 px-4" data-testid="list-rows-empty">
        <div class="w-12 h-12 flex items-center justify-center rounded-xl bg-white dark:bg-gray-900 ring-1 ring-gray-200/70 dark:ring-gray-700/70 shadow-sm">
          <UIcon name="i-heroicons-table-cells" class="w-5 h-5 text-ink-muted" />
        </div>
        <h3 class="mt-3 text-sm font-medium text-gray-900 dark:text-white">{{ $t('lists.noRows') }}</h3>
        <p dir="auto" class="mt-1.5 max-w-sm text-xs leading-relaxed text-gray-500 dark:text-gray-400">{{ $t('lists.noRowsHint', { name: list.name }) }}</p>
        <div class="mt-4 flex flex-wrap justify-center gap-1.5 max-w-md">
          <span v-for="f in list.fields" :key="f.id" class="px-2 h-6 inline-flex items-center rounded-md bg-gray-50 dark:bg-gray-800 border border-gray-100 dark:border-gray-700 text-2xs font-mono text-gray-500 dark:text-gray-400">{{ f.name }}</span>
        </div>
      </div>

      <!-- Side-pane grid: readable column labels, no selection or timestamp
           columns, hairline separators. Row actions stay in the row panel. -->
      <div v-else-if="embedded" class="px-3 pb-4">
        <table class="w-full text-sm" data-testid="list-rows-table">
          <thead class="sticky top-0 z-10 bg-white dark:bg-gray-900">
            <tr>
              <th v-for="f in list.fields" :key="f.id" class="px-2 pt-1 pb-2 text-xs font-medium text-gray-500 dark:text-gray-400 whitespace-nowrap border-b border-gray-200/70 dark:border-gray-800" :class="numeric(f) ? 'text-end' : 'text-start'">{{ label(f) }}</th>
            </tr>
          </thead>
          <TransitionGroup tag="tbody" name="list-row">
            <tr
              v-for="r in rows"
              :key="r.id"
              data-testid="list-row"
              :data-row-id="r.id"
              :data-flash="flash[r.id] || undefined"
              class="group cursor-pointer"
              :class="flash[r.id] ? 'row-flash-' + flash[r.id] : ''"
              @click="openRow(r)"
            >
              <td
                v-for="(f, i) in list.fields"
                :key="f.id"
                class="px-2 py-2.5 max-w-[240px] border-b border-gray-100 dark:border-gray-800/70 transition-colors group-hover:bg-gray-50 dark:group-hover:bg-gray-800/50"
                :class="[selected?.id === r.id ? 'bg-gray-50 dark:bg-gray-800/50' : '', numeric(f) ? 'text-end tabular-nums' : 'text-start', i === 0 ? 'rounded-s-lg' : '', i === list.fields.length - 1 ? 'rounded-e-lg' : '']"
              >
                <span v-if="cell(r, f).empty" class="text-gray-300 dark:text-gray-600">—</span>
                <span v-else class="inline-flex items-center gap-1 max-w-full">
                  <bdi class="truncate" :class="i === 0 ? 'font-medium text-gray-900 dark:text-gray-100' : 'text-gray-600 dark:text-gray-300'">{{ cell(r, f).text }}</bdi>
                  <UIcon v-if="r.locked_fields.includes(f.id!)" name="i-heroicons-lock-closed" class="w-3 h-3 shrink-0 text-ink-muted" :title="$t('lists.row.edited')" />
                </span>
              </td>
            </tr>
          </TransitionGroup>
        </table>
      </div>

      <table v-else class="min-w-full text-xs" data-testid="list-rows-table">
        <thead class="sticky top-0 z-10 bg-gray-50/95 dark:bg-gray-900/95 backdrop-blur">
          <tr class="text-start text-2xs text-gray-500 dark:text-gray-400">
            <th v-if="list.can_manage" class="w-8 ps-3 pe-0 py-2 border-b border-gray-100 dark:border-gray-800">
              <input v-if="canBulkDelete" type="checkbox" data-testid="list-select-all" class="rounded border-gray-300 dark:border-gray-600 text-blue-500 focus:ring-blue-500/30" :aria-label="$t('lists.selectAll')" :checked="allChecked" :indeterminate="checked.length > 0 && !allChecked" @change="toggleAll" />
            </th>
            <th v-for="f in list.fields" :key="f.id" class="px-3 py-2 font-medium text-start whitespace-nowrap border-b border-gray-100 dark:border-gray-800">
              <span class="font-mono">{{ f.name }}</span>
              <UIcon v-if="f.id === list.key_field_id" name="i-heroicons-key" class="w-3 h-3 ms-1 align-[-2px] text-ink-muted" />
            </th>
            <th class="px-3 py-2 font-medium text-end whitespace-nowrap border-b border-gray-100 dark:border-gray-800">{{ $t('lists.updated') }}</th>
          </tr>
        </thead>
        <TransitionGroup tag="tbody" name="list-row">
          <tr
            v-for="r in rows"
            :key="r.id"
            data-testid="list-row"
            :data-row-id="r.id"
            :data-flash="flash[r.id] || undefined"
            class="cursor-pointer border-b border-gray-50 dark:border-gray-800/60 hover:bg-gray-50 dark:hover:bg-gray-800/40"
            :class="[selected?.id === r.id || checked.includes(r.id) ? 'bg-blue-50/60 dark:bg-blue-500/10' : '', flash[r.id] ? 'row-flash-' + flash[r.id] : '']"
            @click="openRow(r)"
          >
            <td v-if="list.can_manage" class="w-8 ps-3 pe-0 py-2 align-top" @click.stop>
              <input v-model="checked" type="checkbox" :value="r.id" data-testid="list-row-check" class="rounded border-gray-300 dark:border-gray-600 text-blue-500 focus:ring-blue-500/30" :aria-label="$t('lists.selectRow')" />
            </td>
            <td v-for="f in list.fields" :key="f.id" class="px-3 py-2 align-top max-w-[260px]">
              <div class="flex items-center gap-1 min-w-0">
                <span v-if="cell(r, f).empty" class="text-gray-300 dark:text-gray-600">—</span>
                <span v-else class="truncate text-gray-800 dark:text-gray-200" :class="numeric(f) ? 'tabular-nums' : ''"><bdi>{{ cell(r, f).text }}</bdi></span>
                <UIcon v-if="r.locked_fields.includes(f.id!)" name="i-heroicons-lock-closed" class="w-3 h-3 shrink-0 text-ink-muted" :title="$t('lists.row.edited')" />
              </div>
            </td>
            <td class="px-3 py-2 text-end whitespace-nowrap text-ink-muted">
              <UIcon v-if="r.stale" name="i-heroicons-exclamation-triangle" class="w-3 h-3 me-1 align-[-2px] text-amber-500" :title="$t('lists.row.stale')" />
              {{ timeAgo(r.updated_at) }}
            </td>
          </tr>
        </TransitionGroup>
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
        <p class="mt-2 text-xs text-gray-500 dark:text-gray-400">{{ confirm === 'clear' ? (list.shared ? $t('lists.clearConfirmBodyShared', { n: total }) : $t('lists.clearConfirmBody', { n: total })) : $t('lists.deleteRowsBody') }}</p>
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
        <p class="mt-2 text-xs text-gray-500 dark:text-gray-400">{{ list.shared ? $t('lists.deleteEverywhereBody') : $t('lists.deleteConfirmBody') }}</p>
        <div class="mt-5 flex justify-end gap-2">
          <button type="button" class="h-8 px-3 rounded-md text-xs font-medium text-gray-600 dark:text-gray-400 hover:bg-gray-100 dark:hover:bg-gray-800" @click="confirm = null">{{ $t('lists.cancel') }}</button>
          <button type="button" data-testid="list-delete-confirm-button" :disabled="deleting" class="h-8 px-3 rounded-md bg-red-600 text-white text-xs font-medium hover:bg-red-700 disabled:opacity-50 inline-flex items-center gap-1.5" @click="deleteList('all')">
            <Spinner v-if="deleting" class="w-3 h-3" />{{ $t('lists.delete') }}
          </button>
        </div>
      </div>
    </UModal>

    <UModal :model-value="confirm === 'unlink'" :ui="{ width: 'sm:max-w-md' }" @update:model-value="v => { if (!v) confirm = null }">
      <div class="p-5" data-testid="list-unlink-confirm">
        <h3 dir="auto" class="text-sm font-semibold text-gray-900 dark:text-white">{{ $t('lists.unlinkConfirmTitle', { name: list.name }) }}</h3>
        <p class="mt-2 text-xs text-gray-500 dark:text-gray-400">{{ $t('lists.unlinkConfirmBody') }}</p>
        <div class="mt-5 flex justify-end gap-2">
          <button type="button" class="h-8 px-3 rounded-md text-xs font-medium text-gray-600 dark:text-gray-400 hover:bg-gray-100 dark:hover:bg-gray-800" @click="confirm = null">{{ $t('lists.cancel') }}</button>
          <button type="button" data-testid="list-unlink-confirm-button" :disabled="deleting" class="h-8 px-3 rounded-md bg-red-600 text-white text-xs font-medium hover:bg-red-700 disabled:opacity-50 inline-flex items-center gap-1.5" @click="deleteList('agent')">
            <Spinner v-if="deleting" class="w-3 h-3" />{{ $t('lists.remove') }}
          </button>
        </div>
      </div>
    </UModal>
  </div>
</template>

<script setup lang="ts">
import { computed, nextTick, onMounted, ref, watch } from 'vue'
import Spinner from '~/components/Spinner.vue'
import ListRowPanel from '~/components/lists/ListRowPanel.vue'
import { useMyFetch } from '~/composables/useMyFetch'
import { formatValue, type AgentList, type ListField, type ListRow } from '~/components/lists/types'
import { sharedWithLabel } from '~/components/lists/shared'

const props = defineProps<{
  dsId: string
  list: AgentList
  rowId?: string | null
  // Rendered inside the report's side pane: compact header, no back link.
  embedded?: boolean
  // Records the agent is writing into this list right now (streamed count).
  writing?: number
  // Link to the full list page, shown as an icon in the side-pane header.
  fullHref?: string
}>()
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
const confirm = ref<null | 'list' | 'unlink' | 'rows' | 'clear'>(null)
const deleting = ref(false)
// Rows ticked for bulk delete (managers only). Selection is over loaded rows.
const checked = ref<string[]>([])
const allChecked = computed(() => rows.value.length > 0 && rows.value.every(r => checked.value.includes(r.id)))
function toggleAll() { checked.value = allChecked.value ? [] : rows.value.map(r => r.id) }
// Clearing or deleting several rows at once of a shared list wipes what every
// agent saved: same bar as its fields. A contributor who manages one agent may
// still remove a single row, so keep selection to one row for them.
const canBulkDelete = computed(() => !props.list.shared || (props.list.can_manage_schema ?? props.list.can_manage))
watch(checked, (v) => { if (!canBulkDelete.value && v.length > 1) checked.value = [v[v.length - 1]] })

const base = computed(() => `/api/data_sources/${props.dsId}/lists/${props.list.id}`)
// snake_case field name as a sentence-case column label (side pane only).
const label = (f: ListField) => { const s = (f.name || '').replace(/_/g, ' '); return s.charAt(0).toUpperCase() + s.slice(1) }
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
    // A shared list's fields belong to every agent it is shared with, so only
    // someone who manages all of them may change them or delete it everywhere.
    const canSchema = props.list.can_manage_schema ?? props.list.can_manage
    if (canSchema) {
      groups.push([{ label: t('lists.editFields'), icon: 'i-heroicons-pencil-square', click: () => emit('edit') }])
    }
    const danger: any[] = []
    // Clearing a shared list wipes what every agent saved: same bar as its fields.
    if (!props.list.shared || canSchema) {
      danger.push({ label: t('lists.clearRows'), icon: 'i-heroicons-archive-box-x-mark', class: 'text-red-600', disabled: total.value === 0, click: () => { confirm.value = 'clear' } })
    }
    if (props.list.shared) {
      danger.push({ label: t('lists.removeFromAgent'), icon: 'i-heroicons-link-slash', class: 'text-red-600', click: () => { confirm.value = 'unlink' } })
      if (canSchema) {
        danger.push({ label: t('lists.deleteEverywhere'), icon: 'i-heroicons-trash', class: 'text-red-600', click: () => { confirm.value = 'list' } })
      }
    } else {
      danger.push({ label: t('lists.deleteList'), icon: 'i-heroicons-trash', class: 'text-red-600', click: () => { confirm.value = 'list' } })
    }
    groups.push(danger)
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

// `agent`: take the list off this agent only (it stays on the others);
// `all`: delete it from every agent.
async function deleteList(scope: 'agent' | 'all' = 'all') {
  deleting.value = true
  try {
    const { error } = await useMyFetch(`${base.value}?scope=${scope}`, { method: 'DELETE' })
    if (error.value) throw new Error(t('lists.toastError'))
    confirm.value = null
    toast.add({ title: scope === 'agent' ? t('lists.toastRemoved') : t('lists.toastDeleted'), color: 'green' })
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
// A submit_<list> call finished while this list is open: pull the fresh
// rows and flash the ones it touched (added / updated). Unchanged rows are
// left alone so the grid only lights up where something actually moved.
const rootEl = ref<HTMLElement | null>(null)
const flash = ref<Record<string, 'inserted' | 'updated'>>({})
const FLASH_MS = 1800
async function applySubmit(records: Array<{ row_id?: string; action?: string }> = []) {
  await load()
  const touched: Record<string, 'inserted' | 'updated'> = {}
  for (const r of records) {
    if (r?.row_id && (r.action === 'inserted' || r.action === 'updated')) touched[r.row_id] = r.action
  }
  if (selected.value && touched[selected.value.id]) {
    const fresh = rows.value.find(x => x.id === selected.value!.id)
    if (fresh) selected.value = fresh
  }
  flash.value = { ...flash.value, ...touched }
  // Rows are oldest-first, so new ones land at the bottom: bring the first
  // touched row into view so the user sees the change happen.
  await nextTick()
  const first = Object.keys(touched).find(id => touched[id] === 'inserted') || Object.keys(touched)[0]
  if (first) rootEl.value?.querySelector(`[data-row-id="${first}"]`)?.scrollIntoView({ block: 'nearest', behavior: 'smooth' })
  setTimeout(() => {
    const next = { ...flash.value }
    for (const id of Object.keys(touched)) if (next[id] === touched[id]) delete next[id]
    flash.value = next
  }, FLASH_MS)
  emit('changed')
}
defineExpose({ reload: () => load(), applySubmit })
</script>

<style scoped>
.list-row-enter-active { transition: opacity .35s ease, transform .35s ease; }
.list-row-enter-from { opacity: 0; transform: translateY(-6px); }
.list-row-move { transition: transform .3s ease; }
.row-flash-inserted > td { animation: row-flash-inserted 1.8s ease-out; }
.row-flash-updated > td { animation: row-flash-updated 1.8s ease-out; }
@keyframes row-flash-inserted { 0%, 30% { background-color: rgb(16 185 129 / .08); } 100% { background-color: transparent; } }
@keyframes row-flash-updated { 0%, 30% { background-color: rgb(59 130 246 / .08); } 100% { background-color: transparent; } }
.fade-enter-active, .fade-leave-active { transition: opacity .2s ease; }
.fade-enter-from, .fade-leave-to { opacity: 0; }
@media (prefers-reduced-motion: reduce) {
  .list-row-enter-active, .list-row-move { transition: none; }
  .row-flash-inserted > td, .row-flash-updated > td { animation: none; }
}
.slide-over-enter-active, .slide-over-leave-active { transition: transform .18s ease, opacity .18s ease; }
.slide-over-enter-from, .slide-over-leave-to { transform: translateX(16px); opacity: 0; }
[dir="rtl"] .slide-over-enter-from, [dir="rtl"] .slide-over-leave-to { transform: translateX(-16px); }
</style>
