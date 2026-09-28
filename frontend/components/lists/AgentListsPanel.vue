<!--
  An agent's Lists in the explorer's right pane.

  Three views, one pane (mirrors how Queries swaps a list for a detail):
  - index  : the agent's lists — the landing view of the Lists row
  - detail : one list's rows (ListRowsView)
  - editor : create / edit a list's fields (ListEditor)

  The explorer owns which list is open (it is what the URL reflects), so the
  selection comes in as `listId` and changes go out as `open`.
-->
<template>
  <div class="h-full min-h-0 flex flex-col text-sm" data-testid="agent-lists-panel">
    <ListEditor
      v-if="mode === 'create' || mode === 'edit'"
      :ds-id="dsId"
      :list="mode === 'edit' ? current : null"
      @cancel="mode = listId ? 'detail' : 'index'"
      @saved="onSaved"
    />

    <ListRowsView
      v-else-if="listId && current"
      :key="'rows-' + current.id"
      :ds-id="dsId"
      :list="current"
      :row-id="rowId"
      @row="$emit('row', $event)"
      @back="$emit('open', null)"
      @edit="mode = 'edit'"
      @deleted="onDeleted"
      @changed="load"
    />

    <div v-else-if="listId && loading" class="flex items-center gap-2 py-8 justify-center text-xs text-gray-400 dark:text-gray-500">
      <Spinner class="w-3.5 h-3.5" /><span>{{ $t('lists.loading') }}</span>
    </div>

    <div v-else class="px-6 py-4 flex-1 min-h-0 overflow-y-auto">
      <div class="flex items-center gap-2 mb-3">
        <span class="text-xs text-gray-400 dark:text-gray-500 tabular-nums ms-auto" data-testid="agent-lists-count">{{ lists.length }}</span>
        <button
          v-if="canManage"
          type="button"
          data-testid="agent-list-new"
          class="shrink-0 inline-flex items-center gap-1.5 h-8 px-3 rounded-md bg-blue-500 text-white text-xs font-medium hover:bg-blue-600"
          @click="mode = 'create'"
        >
          <UIcon name="i-heroicons-plus" class="w-3.5 h-3.5" />
          {{ $t('lists.newList') }}
        </button>
      </div>

      <div v-if="loading" class="flex items-center gap-2 py-8 justify-center text-xs text-gray-400 dark:text-gray-500">
        <Spinner class="w-3.5 h-3.5" /><span>{{ $t('lists.loading') }}</span>
      </div>

      <div v-else-if="lists.length === 0" class="flex flex-col items-center justify-center text-center py-14 px-4" data-testid="agent-lists-empty">
        <div class="w-12 h-12 flex items-center justify-center rounded-xl bg-white dark:bg-gray-900 ring-1 ring-gray-200/70 dark:ring-gray-700/70 shadow-sm">
          <UIcon name="i-heroicons-list-bullet" class="w-5 h-5 text-gray-400 dark:text-gray-500" />
        </div>
        <h3 class="mt-3 text-sm font-medium text-gray-900 dark:text-white">{{ $t('lists.empty') }}</h3>
        <p class="mt-1.5 max-w-xs text-xs leading-relaxed text-gray-500 dark:text-gray-400">
          {{ canManage ? $t('lists.emptyHint') : $t('lists.emptyViewer') }}
        </p>
        <button
          v-if="canManage"
          type="button"
          class="mt-4 inline-flex items-center gap-1 h-8 px-3 rounded-md bg-blue-500 text-white text-xs font-medium hover:bg-blue-600"
          @click="mode = 'create'"
        >
          <UIcon name="i-heroicons-plus" class="w-3.5 h-3.5" />
          {{ $t('lists.newList') }}
        </button>
      </div>

      <div v-else class="space-y-2">
        <button
          v-for="l in lists"
          :key="l.id"
          type="button"
          data-testid="agent-list-row"
          class="w-full text-start border border-gray-100 dark:border-gray-800 bg-white dark:bg-gray-900 rounded-lg px-3 py-2.5 hover:border-gray-200 dark:hover:border-gray-700 hover:shadow-sm transition-all"
          @click="$emit('open', l.id)"
        >
          <div class="flex items-center gap-2 min-w-0">
            <UIcon name="i-heroicons-list-bullet" class="w-4 h-4 shrink-0 text-gray-400 dark:text-gray-500" />
            <span dir="auto" class="min-w-0 truncate text-[13px] font-medium text-gray-900 dark:text-white">{{ l.name }}</span>
            <span class="ms-auto shrink-0 text-[11px] text-gray-400 dark:text-gray-500 tabular-nums">{{ $t('lists.rowsCount', { n: l.row_count }, l.row_count) }}</span>
          </div>
          <div class="mt-1 flex items-center gap-3 min-w-0 ps-6">
            <span dir="auto" class="min-w-0 truncate text-[12px] text-gray-500 dark:text-gray-400">{{ l.description || fieldNames(l) }}</span>
            <span class="ms-auto shrink-0 text-[11px] text-gray-400 dark:text-gray-500">{{ $t('lists.fieldsCount', { n: l.fields.length }, l.fields.length) }}</span>
          </div>
        </button>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import Spinner from '~/components/Spinner.vue'
import ListEditor from '~/components/lists/ListEditor.vue'
import ListRowsView from '~/components/lists/ListRowsView.vue'
import { useMyFetch } from '~/composables/useMyFetch'
import type { AgentList } from '~/components/lists/types'

const props = defineProps<{ dsId: string; listId?: string | null; rowId?: string | null; canManage?: boolean }>()
const emit = defineEmits<{
  (e: 'open', listId: string | null): void
  (e: 'row', rowId: string | null): void
  (e: 'changed'): void
}>()

const lists = ref<AgentList[]>([])
const loading = ref(true)
const mode = ref<'index' | 'detail' | 'create' | 'edit'>('index')
const current = computed(() => lists.value.find(l => l.id === props.listId) || null)
const fieldNames = (l: AgentList) => l.fields.map(f => f.name).join(', ')

async function load() {
  loading.value = true
  try {
    const { data } = await useMyFetch<AgentList[]>(`/api/data_sources/${props.dsId}/lists`, { method: 'GET' })
    lists.value = (data.value || []) as AgentList[]
  } finally {
    loading.value = false
  }
}

function onSaved(saved: AgentList) {
  const i = lists.value.findIndex(l => l.id === saved.id)
  if (i >= 0) lists.value[i] = { ...lists.value[i], ...saved }
  else lists.value.push(saved)
  mode.value = 'detail'
  emit('changed')
  emit('open', saved.id)
}

function onDeleted() {
  lists.value = lists.value.filter(l => l.id !== props.listId)
  mode.value = 'index'
  emit('changed')
  emit('open', null)
}

onMounted(load)
watch(() => props.dsId, () => { lists.value = []; mode.value = 'index'; load() })
watch(() => props.listId, (id) => { if (mode.value !== 'create') mode.value = id ? 'detail' : 'index' })
defineExpose({ reload: load, create: () => { mode.value = 'create' } })
</script>
