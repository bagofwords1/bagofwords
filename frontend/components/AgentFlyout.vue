<template>
  <!-- Agent hover flyout (teleported so it never gets clipped by popovers) -->
  <!-- NOTE: teleport into Nuxt root to preserve Nuxt context (needed for MDC rendering) -->
  <Teleport to="#__nuxt">
    <Transition
      enter-active-class="transition-all duration-150 ease-out"
      enter-from-class="opacity-0 translate-y-1"
      enter-to-class="opacity-100 translate-y-0"
      leave-active-class="transition-all duration-100 ease-in"
      leave-from-class="opacity-100 translate-y-0"
      leave-to-class="opacity-0 translate-y-1"
    >
      <div
        v-if="visible && agentId"
        class="fixed z-[2000]"
        :style="positionStyle"
        @mouseenter="$emit('mouseenter')"
        @mouseleave="$emit('mouseleave')"
      >
        <div class="w-[400px] bg-white rounded-xl shadow-2xl border border-gray-200 overflow-hidden">
          <!-- Header with connection info -->
          <div class="px-4 py-3 border-b border-gray-100">
            <div class="flex items-start justify-between gap-2">
              <div class="min-w-0 flex-1">
                <div class="text-sm font-semibold text-gray-900 truncate">
                  {{ agentDetails?.name || 'Loading...' }}
                </div>
                <!-- Connection info: icons for all connections -->
                <div class="flex items-center gap-2 mt-1">
                  <div v-if="agentDetails?.connections?.length" class="flex items-center gap-1">
                    <!-- Show icons for up to 3 connections -->
                    <div class="flex -space-x-1">
                      <DataSourceIcon
                        v-for="conn in (agentDetails.connections || []).slice(0, 3)"
                        :key="conn.id"
                        :type="conn.type"
                        class="h-4 flex-shrink-0 ring-1 ring-white rounded"
                      />
                    </div>
                    <span class="text-xs text-gray-500 truncate">
                      {{ agentDetails.connections.length }} connection{{ agentDetails.connections.length > 1 ? 's' : '' }}
                    </span>
                  </div>
                  <span v-else class="text-xs text-gray-500 truncate">
                    No connections
                  </span>
                  <!-- Green circle if any connection active -->
                  <span
                    v-if="hasActiveConnection"
                    class="w-2 h-2 rounded-full bg-green-500 flex-shrink-0"
                    title="Connected"
                  ></span>
                  <span
                    v-else-if="agentDetails?.connections?.length"
                    class="w-2 h-2 rounded-full bg-gray-300 flex-shrink-0"
                    title="Not connected"
                  ></span>
                </div>
              </div>
              <!-- Open agent link - top right -->
              <a
                v-if="agentId"
                :href="`/data/${agentId}`"
                class="text-xs font-medium text-indigo-600 hover:text-indigo-700 hover:underline flex-shrink-0 whitespace-nowrap"
              >
                Open agent →
              </a>
            </div>
          </div>

          <!-- Tabs (underline / border-bottom style like Settings) -->
          <div class="border-b border-gray-200 px-4">
            <nav class="-mb-px flex space-x-4">
              <button
                @click="flyoutTab = 'overview'"
                :class="[
                  flyoutTab === 'overview'
                    ? 'border-indigo-500 text-indigo-600'
                    : 'border-transparent text-gray-500 hover:border-gray-300 hover:text-gray-700',
                  'whitespace-nowrap border-b-2 py-2 text-xs font-medium'
                ]"
              >
                Overview
              </button>
              <button
                @click="flyoutTab = 'tables'"
                :class="[
                  flyoutTab === 'tables'
                    ? 'border-indigo-500 text-indigo-600'
                    : 'border-transparent text-gray-500 hover:border-gray-300 hover:text-gray-700',
                  'whitespace-nowrap border-b-2 py-2 text-xs font-medium'
                ]"
              >
                Tables
                <span v-if="tablesCount > 0" class="ml-1 text-[10px] text-gray-400">({{ tablesCount }})</span>
              </button>
              <button
                @click="flyoutTab = 'instructions'"
                :class="[
                  flyoutTab === 'instructions'
                    ? 'border-indigo-500 text-indigo-600'
                    : 'border-transparent text-gray-500 hover:border-gray-300 hover:text-gray-700',
                  'whitespace-nowrap border-b-2 py-2 text-xs font-medium'
                ]"
              >
                Instructions
                <span v-if="instructionsCount > 0" class="ml-1 text-[10px] text-gray-400">({{ instructionsCount }})</span>
              </button>
              <button
                @click="flyoutTab = 'queries'"
                :class="[
                  flyoutTab === 'queries'
                    ? 'border-indigo-500 text-indigo-600'
                    : 'border-transparent text-gray-500 hover:border-gray-300 hover:text-gray-700',
                  'whitespace-nowrap border-b-2 py-2 text-xs font-medium'
                ]"
              >
                Queries
                <span v-if="queriesCount > 0" class="ml-1 text-[10px] text-gray-400">({{ queriesCount }})</span>
              </button>
            </nav>
          </div>

          <div class="p-4">
            <div v-if="loadingDetails" class="flex items-center justify-center py-8">
              <Spinner class="w-5 h-5 text-gray-400 animate-spin" />
            </div>

            <template v-else>
              <!-- Overview tab -->
              <div v-if="flyoutTab === 'overview'" class="space-y-4">
                <!-- Description rendered as Markdown -->
                <div
                  v-if="agentDetails?.description"
                  class="agent-flyout-markdown text-xs text-gray-600 leading-relaxed max-h-[320px] overflow-auto pr-1"
                >
                  <MDC :value="agentDetails.description" class="markdown-content" />
                </div>

                <!-- Sample Questions -->
                <div v-if="agentDetails?.conversation_starters?.length">
                  <div class="text-[10px] uppercase tracking-wider text-gray-400 font-semibold mb-2">Sample Questions</div>
                  <div class="space-y-1.5">
                    <button
                      v-for="(starter, idx) in agentDetails.conversation_starters.slice(0, 6)"
                      :key="idx"
                      @click.stop.prevent="startReportWithQuestion(starter, Number(idx))"
                      :disabled="creatingReport"
                      :class="[
                        'w-full text-left text-xs px-3 py-2 rounded-lg transition-colors flex items-center gap-2',
                        creatingReport && creatingQuestionIdx === idx
                          ? 'bg-indigo-100 border border-indigo-300 text-indigo-700'
                          : 'bg-gray-50 border border-gray-100 text-gray-700 hover:bg-indigo-50 hover:border-indigo-200 hover:text-indigo-700 cursor-pointer',
                        creatingReport && creatingQuestionIdx !== idx ? 'opacity-50 cursor-not-allowed' : ''
                      ]"
                    >
                      <Spinner v-if="creatingReport && creatingQuestionIdx === idx" class="w-3 h-3 flex-shrink-0 animate-spin" />
                      <span class="flex-1">{{ starter.split('\n')[0] }}</span>
                    </button>
                    <div
                      v-if="agentDetails.conversation_starters.length > 6"
                      class="text-[11px] text-gray-400"
                    >
                      +{{ agentDetails.conversation_starters.length - 6 }} more
                    </div>
                  </div>
                </div>

                <div
                  v-if="!agentDetails?.description && !agentDetails?.conversation_starters?.length"
                  class="text-xs text-gray-400 italic py-6 text-center"
                >
                  No details available
                </div>
              </div>

              <!-- Tables tab -->
              <div v-else-if="flyoutTab === 'tables'">
                <div v-if="tablesLoading" class="flex items-center justify-center py-10">
                  <Spinner class="w-5 h-5 text-gray-400 animate-spin" />
                </div>

                <div v-else-if="tablesError" class="text-xs text-gray-500">
                  {{ tablesError }}
                </div>

                <div v-else>
                  <div v-if="tablesCount === 0" class="text-xs text-gray-400 italic py-6 text-center">
                    No tables found
                  </div>

                  <div v-else>
                    <!-- List view (like MentionInput) -->
                    <div v-if="!selectedTable" class="border border-gray-200 rounded-lg overflow-hidden">
                      <div class="max-h-[320px] overflow-auto">
                        <button
                          v-for="t in tablesResources"
                          :key="t.id || t.name"
                          @click="selectTable(t)"
                          class="w-full px-3 py-2 text-left text-xs flex items-center gap-2 hover:bg-gray-50 border-b border-gray-100 last:border-b-0"
                        >
                          <DataSourceIcon v-if="hasMultipleConnections" :type="t.connection_type" class="h-3.5 flex-shrink-0" />
                          <span class="truncate flex-1 text-gray-800 font-medium">{{ t.name }}</span>
                          <span v-if="t.columns?.length" class="text-[11px] text-gray-400 flex-shrink-0">{{ t.columns.length }} cols</span>
                        </button>
                      </div>
                      <div v-if="tablesResources.length === 0" class="px-3 py-3 text-xs text-gray-400">No tables.</div>
                    </div>

                    <!-- Detail view (columns) -->
                    <div v-else class="space-y-2">
                      <div class="flex items-center justify-between">
                        <button
                          @click="selectedTable = null"
                          class="text-[11px] text-gray-500 hover:text-gray-700"
                        >
                          ← Back
                        </button>
                        <div class="text-[11px] text-gray-400">Columns</div>
                      </div>

                      <div class="text-sm font-semibold text-gray-900 truncate">{{ selectedTable.name }}</div>

                      <div class="flex flex-wrap gap-1 max-h-[240px] overflow-auto border border-gray-200 rounded-lg p-2">
                        <span
                          v-for="(col, idx) in (selectedTable.columns || [])"
                          :key="idx"
                          class="px-1.5 py-0.5 bg-white rounded border text-[11px] text-gray-700"
                        >
                          {{ typeof col === 'string' ? col : (col as any).name }}
                          <span v-if="typeof col === 'object' && (col as any).dtype" class="text-gray-400 ml-1">({{ (col as any).dtype }})</span>
                        </span>
                        <span v-if="!(selectedTable.columns || []).length" class="text-[12px] text-gray-400">No columns.</span>
                      </div>
                    </div>
                  </div>
                </div>
              </div>

              <!-- Instructions tab -->
              <div v-else-if="flyoutTab === 'instructions'">
                <div v-if="instructionsLoading" class="flex items-center justify-center py-10">
                  <Spinner class="w-5 h-5 text-gray-400 animate-spin" />
                </div>

                <div v-else-if="instructionsError" class="text-xs text-gray-500">
                  {{ instructionsError }}
                </div>

                <div v-else>
                  <div v-if="instructionsCount === 0" class="text-xs text-gray-400 italic py-6 text-center">
                    No instructions found
                  </div>

                  <div v-else class="border border-gray-200 rounded-lg overflow-hidden">
                    <div class="max-h-[320px] overflow-auto">
                      <a
                        v-for="inst in instructionsResources"
                        :key="inst.id"
                        :href="`/instructions?search=${encodeURIComponent(inst.title || '')}`"
                        class="w-full px-3 py-2 text-left text-xs flex items-start gap-2 hover:bg-gray-50 border-b border-gray-100 last:border-b-0 block"
                      >
                        <div class="flex-1 min-w-0">
                          <div class="flex items-center gap-1.5">
                            <span class="truncate text-gray-800 font-medium">{{ inst.title || 'Untitled' }}</span>
                            <span
                              v-if="!inst.data_sources?.length"
                              class="px-1 py-0.5 text-[9px] rounded bg-purple-50 text-purple-600 flex-shrink-0"
                            >
                              Global
                            </span>
                          </div>
                          <div class="text-[11px] text-gray-400 truncate mt-0.5">
                            {{ inst.category || 'general' }} · {{ inst.source_type || 'user' }}
                          </div>
                        </div>
                      </a>
                    </div>
                  </div>
                </div>
              </div>

              <!-- Queries tab -->
              <div v-else-if="flyoutTab === 'queries'">
                <div v-if="queriesLoading" class="flex items-center justify-center py-10">
                  <Spinner class="w-5 h-5 text-gray-400 animate-spin" />
                </div>

                <div v-else-if="queriesError" class="text-xs text-gray-500">
                  {{ queriesError }}
                </div>

                <div v-else>
                  <div v-if="queriesCount === 0" class="text-xs text-gray-400 italic py-6 text-center">
                    No saved queries found
                  </div>

                  <div v-else class="border border-gray-200 rounded-lg overflow-hidden">
                    <div class="max-h-[320px] overflow-auto">
                      <a
                        v-for="entity in queriesResources"
                        :key="entity.id"
                        :href="`/queries/${entity.id}`"
                        class="w-full px-3 py-2 text-left text-xs flex items-start gap-2 hover:bg-gray-50 border-b border-gray-100 last:border-b-0 block"
                      >
                        <div class="flex-1 min-w-0">
                          <div class="flex items-center gap-1.5">
                            <span
                              class="px-1 py-0.5 text-[9px] rounded border flex-shrink-0"
                              :class="entity.type === 'metric' ? 'text-emerald-700 border-emerald-200 bg-emerald-50' : 'text-blue-700 border-blue-200 bg-blue-50'"
                            >{{ (entity.type || 'entity').toUpperCase() }}</span>
                            <span class="truncate text-gray-800 font-medium">{{ entity.title || entity.slug }}</span>
                          </div>
                          <div v-if="entity.description" class="text-[11px] text-gray-400 truncate mt-0.5">
                            {{ entity.description }}
                          </div>
                        </div>
                      </a>
                    </div>
                  </div>
                </div>
              </div>

            </template>
          </div>
        </div>
      </div>
    </Transition>
  </Teleport>
</template>

<script setup lang="ts">
import Spinner from '~/components/Spinner.vue'
import DataSourceIcon from '~/components/DataSourceIcon.vue'

const router = useRouter()

const props = defineProps<{
  agentId: string | null
  visible: boolean
  position: { top?: number; bottom?: number; left: number }
}>()

// Compute position style - prefer bottom if provided (grows upward)
const positionStyle = computed(() => {
  const style: Record<string, string> = {
    left: `${props.position.left}px`
  }
  if (props.position.bottom !== undefined) {
    style.bottom = `${props.position.bottom}px`
  } else if (props.position.top !== undefined) {
    style.top = `${props.position.top}px`
  }
  return style
})

const emit = defineEmits<{
  mouseenter: []
  mouseleave: []
}>()

// Internal state
const agentDetails = ref<any>(null)
const loadingDetails = ref(false)
const detailsCache = ref<Record<string, any>>({})
const flyoutTab = ref<'overview' | 'tables' | 'instructions' | 'queries'>('overview')

// Tables tab state
const tablesCache = ref<Record<string, any[]>>({})
const tablesLoading = ref(false)
const tablesError = ref<string | null>(null)
const selectedTable = ref<any | null>(null)

// Instructions tab state
const instructionsCache = ref<Record<string, any[]>>({})
const instructionsLoading = ref(false)
const instructionsError = ref<string | null>(null)

// Queries tab state
const queriesCache = ref<Record<string, any[]>>({})
const queriesLoading = ref(false)
const queriesError = ref<string | null>(null)

// Report creation state
const creatingReport = ref(false)
const creatingQuestionIdx = ref<number | null>(null)

// Computed
const tablesResources = computed<any[]>(() => {
  if (!props.agentId) return []
  return tablesCache.value[props.agentId] || []
})
const tablesCount = computed(() => tablesResources.value.length)

const instructionsResources = computed<any[]>(() => {
  if (!props.agentId) return []
  return instructionsCache.value[props.agentId] || []
})
const instructionsCount = computed(() => instructionsResources.value.length)

const queriesResources = computed<any[]>(() => {
  if (!props.agentId) return []
  return queriesCache.value[props.agentId] || []
})
const queriesCount = computed(() => queriesResources.value.length)

const hasActiveConnection = computed(() => {
  const connections = agentDetails.value?.connections || []
  return connections.some((conn: any) => conn?.user_status?.connection === 'success')
})

const hasMultipleConnections = computed(() => {
  const connections = agentDetails.value?.connections || []
  return connections.length > 1
})

// Fetch functions
const fetchAgentDetails = async (agentId: string) => {
  if (detailsCache.value[agentId]) {
    agentDetails.value = detailsCache.value[agentId]
    return
  }

  loadingDetails.value = true
  try {
    const { data, error } = await useMyFetch(`/data_sources/${agentId}`, { method: 'GET' })
    if (!error?.value && data?.value) {
      detailsCache.value[agentId] = data.value
      if (props.agentId === agentId) {
        agentDetails.value = data.value
      }
    }
  } catch (e) {
    console.error('Failed to load agent details:', e)
  } finally {
    loadingDetails.value = false
  }
}

const fetchTablesForAgent = async (agentId: string) => {
  if (!agentId || tablesCache.value[agentId]) return
  tablesLoading.value = true
  tablesError.value = null
  try {
    const { data, error } = await useMyFetch(`/data_sources/${agentId}/schema`, { method: 'GET' })
    if (error?.value) {
      tablesError.value = 'Failed to load tables'
      return
    }
    const payload: any = (data as any)?.value
    const tables = Array.isArray(payload) ? payload : []
    const filtered = tables.filter((t: any) => t?.is_active !== false)
    tablesCache.value[agentId] = filtered
  } catch (e) {
    tablesError.value = 'Failed to load tables'
  } finally {
    tablesLoading.value = false
  }
}

const fetchInstructionsForAgent = async (agentId: string) => {
  if (!agentId || instructionsCache.value[agentId]) return
  instructionsLoading.value = true
  instructionsError.value = null
  try {
    const { data, error } = await useMyFetch('/api/instructions', {
      method: 'GET',
      query: {
        data_source_ids: agentId,
        include_global: true,
        limit: 50,
        include_own: true,
        include_drafts: false
      }
    })
    if (error?.value) {
      instructionsError.value = 'Failed to load instructions'
      return
    }
    const payload: any = (data as any)?.value
    const items = payload?.items || payload || []
    instructionsCache.value[agentId] = items
  } catch (e) {
    instructionsError.value = 'Failed to load instructions'
  } finally {
    instructionsLoading.value = false
  }
}

const fetchQueriesForAgent = async (agentId: string) => {
  if (!agentId || queriesCache.value[agentId]) return
  queriesLoading.value = true
  queriesError.value = null
  try {
    const { data, error } = await useMyFetch('/api/entities', {
      method: 'GET',
      query: {
        data_source_ids: agentId
      }
    })
    if (error?.value) {
      queriesError.value = 'Failed to load queries'
      return
    }
    const payload: any = (data as any)?.value
    const entities = Array.isArray(payload) ? payload : []
    const filtered = entities.filter((e: any) =>
      e.status === 'published' && e.global_status === 'approved'
    )
    queriesCache.value[agentId] = filtered
  } catch (e) {
    queriesError.value = 'Failed to load queries'
  } finally {
    queriesLoading.value = false
  }
}

const selectTable = (t: any) => {
  selectedTable.value = t
}

const startReportWithQuestion = async (question: string, idx: number) => {
  if (creatingReport.value) return
  creatingReport.value = true
  creatingQuestionIdx.value = idx

  try {
    const dataSourceIds = props.agentId ? [props.agentId] : []

    const response = await useMyFetch('/reports', {
      method: 'POST',
      body: JSON.stringify({
        title: 'untitled report',
        files: [],
        new_message: question,
        data_sources: dataSourceIds
      })
    })

    if ((response as any)?.error?.value) {
      throw new Error('Report creation failed')
    }

    const data = (response as any)?.data?.value as any
    if (data?.id) {
      await router.push({
        path: `/reports/${data.id}`,
        query: {
          new_message: question
        }
      })
    }
  } catch (error) {
    console.error('Failed to create report:', error)
  } finally {
    creatingReport.value = false
    creatingQuestionIdx.value = null
  }
}

// Watch for agentId changes to fetch data
watch(() => props.agentId, async (newId, oldId) => {
  if (newId && newId !== oldId) {
    // Reset state
    flyoutTab.value = 'overview'
    tablesError.value = null
    selectedTable.value = null
    instructionsError.value = null
    queriesError.value = null
    agentDetails.value = null

    // Fetch all data in parallel
    await Promise.all([
      fetchAgentDetails(newId),
      fetchTablesForAgent(newId),
      fetchInstructionsForAgent(newId),
      fetchQueriesForAgent(newId)
    ])
  }
}, { immediate: true })

// Watch tab changes to ensure data is loaded
watch(flyoutTab, async (tab) => {
  const id = props.agentId
  if (!id) return

  if (tab === 'tables') {
    await fetchTablesForAgent(id)
  } else if (tab === 'instructions') {
    await fetchInstructionsForAgent(id)
  } else if (tab === 'queries') {
    await fetchQueriesForAgent(id)
  }
})
</script>

<style lang="postcss">
/* Not scoped: flyout is teleported */
.agent-flyout-markdown .markdown-content {
  @apply leading-relaxed;
  font-size: 12px;

  :where(h1, h2, h3, h4, h5, h6) {
    @apply font-bold mb-2 mt-3;
  }

  h1 { @apply text-base; }
  h2 { @apply text-sm; }
  h3 { @apply text-xs; }

  ul, ol { @apply pl-4 mb-2; }
  ul { @apply list-disc; }
  ol { @apply list-decimal; }
  li { @apply mb-1; }

  pre { @apply bg-gray-50 p-2 rounded-lg mb-2 overflow-x-auto text-[11px]; }
  code { @apply bg-gray-50 px-1 py-0.5 rounded text-[11px] font-mono; }
  a { @apply text-blue-600 hover:text-blue-800 underline; }
  blockquote { @apply border-l-4 border-gray-200 pl-3 italic my-2; }
  table { @apply w-full border-collapse mb-2; }
  table th, table td { @apply border border-gray-200 p-1 text-[11px] bg-white; }
  p { @apply mb-2; }
}
</style>
