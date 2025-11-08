<template>
  <div class="w-full">
    <div v-if="showHeader" class="mb-2 flex items-center justify-between">
      <div>
        <h1 class="text-lg font-semibold">{{ headerTitle }}</h1>
        <p class="text-gray-500 text-sm">{{ headerSubtitle }}</p>
      </div>
      <div>
        <button
          v-if="showRefresh"
          @click="onRefresh"
          :disabled="loading || refreshing"
          :class="refreshIconOnly ? 'p-1.5 rounded-lg border border-gray-300 text-gray-700 hover:bg-gray-50 disabled:opacity-50' : 'flex items-center gap-2 border border-gray-300 rounded-lg px-3 py-1.5 text-xs text-gray-700 hover:bg-gray-50 disabled:opacity-50'"
        >
          <Spinner v-if="loading || refreshing" class="w-4 h-4" />
          <span v-if="!refreshIconOnly">Reload tables</span>
        </button>
      </div>
    </div>
    <div v-else class="mb-2 flex items-center justify-end">
      <button
        v-if="showRefresh"
        @click="onRefresh"
        :disabled="loading || refreshing"
        :class="refreshIconOnly ? 'p-1.5 rounded-lg border border-gray-300 text-gray-700 hover:bg-gray-50 disabled:opacity-50' : 'flex items-center gap-2 border border-gray-300 rounded-lg px-3 py-1.5 text-xs text-gray-700 hover:bg-gray-50 disabled:opacity-50'"
      >
        <Spinner v-if="loading || refreshing" class="w-4 h-4" />
        <span v-if="!refreshIconOnly">Reload tables</span>
      </button>
    </div>

    <div>
      <input v-model="search" type="text" placeholder="Search tables..." class="border border-gray-300 rounded-lg px-3 py-2 w-full h-9 text-sm focus:outline-none focus:border-blue-500" />
      <div class="mt-1 text-xs text-gray-500 text-right">{{ selectedCount }} of {{ totalTables }} selected</div>
    </div>

    <div v-if="loading" class="text-sm text-gray-500 py-10 flex items-center justify-center">
      <Spinner class="w-4 h-4 mr-2" />
      Loading schema...
    </div>

    <div v-else class="flex-1 flex flex-col h-full">
      <div v-if="filteredTables.length === 0" class="text-sm text-gray-500">No tables found.</div>
      <div v-else class="flex-1 flex flex-col min-h-full">
        <div class="flex-1 overflow-y-auto min-h-0" :style="{ maxHeight }">
          <ul class="divide-y divide-gray-100">
            <li v-for="table in filteredTables" :key="table.name" class="py-2 px-2">
              <div class="flex items-center">
                <UCheckbox v-if="canUpdate" color="blue" v-model="table.is_active" class="mr-3" />
              <button type="button" class="flex items-center justify-between text-left flex-1" @click="toggleTable(table)">
                <div class="flex items-center min-w-0">
                  <UIcon :name="expandedTables[table.name] ? 'heroicons-chevron-down' : 'heroicons-chevron-right'" class="w-4 h-4 mr-1 text-gray-500" />
                  <span class="text-sm text-gray-800 truncate">{{ table.name }}</span>
                  <span v-if="!table.is_active && canUpdate" class="ml-2 text-[10px] px-1 py-0.5 rounded bg-gray-100 text-gray-500">inactive</span>
                </div>
                <span v-if="props.showStats && (table.usage_count !== undefined)" class="ml-2 text-[11px] text-gray-500 whitespace-nowrap flex items-center gap-2">
                  <span>usage {{ table.usage_count }}</span>

                    <UTooltip text="Successful executed queries">
                  <span class="inline-flex items-center gap-1">
                    <UIcon name="heroicons-check-circle" class="w-3 h-3 text-green-600" />
                    <span>{{ table.success_count ?? 0 }}</span>
                  </span>

                    </UTooltip>

                    <UTooltip text="Failed executed queries">
                  <span class="inline-flex items-center gap-1">
                    <UIcon name="heroicons-x-circle" class="w-3 h-3 text-red-600" />
                    <span>{{ table.failure_count ?? 0 }}</span>
                    </span>
                  </UTooltip>

                    <UTooltip text="Positive feedback">
                  <span class="inline-flex items-center gap-1">
                    <UIcon name="heroicons-hand-thumb-up" class="w-3 h-3 text-green-600" />
                    <span>{{ table.pos_feedback_count ?? 0 }}</span>
                  </span>
                  </UTooltip>

                    <UTooltip text="Negative feedback">
                  <span class="inline-flex items-center gap-1">
                    <UIcon name="heroicons-hand-thumb-down" class="w-3 h-3 text-red-600" />
                    <span>{{ table.neg_feedback_count ?? 0 }}</span>
                  </span>
                </UTooltip>
              </span>
              </button>
              </div>
              <div v-if="expandedTables[table.name] && table.columns" class="mt-2 ml-7">
                <div class="border border-gray-100 rounded">
                  <div class="grid grid-cols-2 text-xs font-medium text-gray-500 bg-gray-50 px-2 py-1 rounded-t">
                    <div>Name</div>
                    <div>Type</div>
                  </div>
                  <div class="divide-y divide-gray-100">
                    <div v-for="col in table.columns" :key="col.name" class="grid grid-cols-2 text-xs px-2 py-1">
                      <div class="text-gray-700">{{ col.name }}</div>
                      <div class="text-gray-500">{{ col.dtype || col.type }}</div>
                    </div>
                  </div>
                </div>
              </div>
            </li>
          </ul>
        </div>
      </div>
    </div>

    <div v-if="showSave && canUpdate" class="mt-3 flex justify-end">
      <button @click="onSave" :disabled="saving" class="bg-blue-500 hover:bg-blue-600 text-white text-xs font-medium py-1.5 px-3 rounded disabled:opacity-50">
        <span v-if="saving">Saving...</span>
        <span v-else>{{ saveLabel }}</span>
      </button>
    </div>
  </div>
</template>

<script setup lang="ts">
import Spinner from '@/components/Spinner.vue'

type Column = { name: string; dtype?: string; type?: string }
type Table = { name: string; is_active: boolean; columns?: Column[]; pks?: any[]; fks?: any[]; usage_count?: number; success_count?: number; failure_count?: number; pos_feedback_count?: number; neg_feedback_count?: number }

const props = withDefaults(defineProps<{ dsId: string; schema: 'full' | 'user'; canUpdate?: boolean; showRefresh?: boolean; refreshIconOnly?: boolean; showSave?: boolean; saveLabel?: string; maxHeight?: string; showHeader?: boolean; headerTitle?: string; headerSubtitle?: string; showStats?: boolean }>(), { canUpdate: true, showRefresh: true, refreshIconOnly: false, showSave: true, saveLabel: 'Save', maxHeight: '50vh', showHeader: false, headerTitle: 'Select tables', headerSubtitle: 'Choose which tables to enable', showStats: false })
const emit = defineEmits<{ (e: 'saved', tables: Table[]): void; (e: 'error', err: any): void }>()

const loading = ref(false)
const refreshing = ref(false)
const saving = ref(false)
const tables = ref<Table[]>([])
const search = ref('')
const expandedTables = ref<Record<string, boolean>>({})

const totalTables = computed(() => (tables.value || []).length)
const selectedCount = computed(() => (tables.value || []).filter(t => !!t.is_active).length)

const visibleTables = computed(() => {
  let list = tables.value || []
  if (!props.canUpdate) {
    list = list.filter(t => !!t.is_active)
  }
  return list
})

const filteredTables = computed(() => {
  const q = search.value.trim().toLowerCase()
  const list = visibleTables.value
  if (!q) return list
  return list.filter(t => String(t.name).toLowerCase().includes(q))
})

function endpointForSchema(): string {
  return props.schema === 'user' ? 'schema' : 'full_schema'
}

async function fetchTables() {
  const wasLoading = loading.value
  if (!wasLoading) refreshing.value = true
  loading.value = true
  try {
    const endpoint = endpointForSchema()
    const res = await useMyFetch(`/data_sources/${props.dsId}/${endpoint}${props.showStats ? '?with_stats=true' : ''}`, { method: 'GET' })
    if ((res as any)?.status?.value === 'success') {
      tables.value = (((res as any).data?.value) || []) as Table[]
    } else {
      tables.value = []
    }
  } catch (e) {
    emit('error', e)
  } finally {
    loading.value = false
    refreshing.value = false
  }
}

function toggleTable(table: Table) {
  const current = expandedTables.value[table.name]
  expandedTables.value[table.name] = !current
}

// (centrality hidden for now)

async function onSave() {
  if (saving.value) return
  saving.value = true
  try {
    const payload = (tables.value || []).map(t => ({ ...t, datasource_id: props.dsId, pks: t.pks || [], fks: t.fks || [] }))
    const res = await useMyFetch(`/data_sources/${props.dsId}/update_schema`, { method: 'PUT', body: payload })
    if ((res as any)?.status?.value === 'success') {
      const updated = (((res as any).data?.value) || tables.value) as Table[]
      tables.value = updated
      emit('saved', updated)
    }
  } catch (e) {
    emit('error', e)
  } finally {
    saving.value = false
  }
}

async function onRefresh() {
  if (loading.value || refreshing.value) return
  // Show spinner on the button immediately and disable it during the entire refresh flow
  refreshing.value = true
  try {
    // For admin/system views (full schema), trigger a live backend refresh first
    if (endpointForSchema() === 'full_schema') {
      await useMyFetch(`/data_sources/${props.dsId}/refresh_schema`, { method: 'GET' })
    }
    // Then reload the current schema view
    await fetchTables()
  } catch (e) {
    // Swallow refresh errors; we'll still attempt to re-fetch the current schema view above
  } finally {
    // In case fetchTables didn't reset it (or errored early), ensure the spinner stops
    refreshing.value = false
  }
}

watch(() => [props.dsId, props.schema], () => { if (props.dsId) fetchTables() }, { immediate: true })
</script>

<style scoped>
</style>


