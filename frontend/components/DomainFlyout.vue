<template>
  <Teleport to="body">
    <Transition
      enter-active-class="transition-all duration-150 ease-out"
      enter-from-class="opacity-0 translate-y-1"
      enter-to-class="opacity-100 translate-y-0"
      leave-active-class="transition-all duration-100 ease-in"
      leave-from-class="opacity-100 translate-y-0"
      leave-to-class="opacity-0 translate-y-1"
    >
      <div
        v-if="visible && domainId"
        class="fixed z-[2000]"
        :style="{ top: `${position.top}px`, left: `${position.left}px` }"
        @mouseenter="onFlyoutEnter"
        @mouseleave="onFlyoutLeave"
      >
        <div class="w-[560px] bg-white rounded-xl shadow-2xl border border-gray-200 overflow-hidden">
          <div class="px-5 py-4 border-b border-gray-100">
            <div class="text-sm font-semibold text-gray-900 truncate">
              {{ domainDetails?.name || 'Loading…' }}
            </div>
            <div class="text-xs text-gray-400">Domain</div>
          </div>

          <!-- Tabs (underline / border-bottom style like Settings) -->
          <div class="border-b border-gray-200 px-5">
            <nav class="-mb-px flex space-x-6">
              <button
                @click="activeTab = 'overview'"
                :class="[
                  activeTab === 'overview'
                    ? 'border-indigo-500 text-indigo-600'
                    : 'border-transparent text-gray-500 hover:border-gray-300 hover:text-gray-700',
                  'whitespace-nowrap border-b-2 py-2 text-xs font-medium'
                ]"
              >
                Overview
              </button>
              <button
                @click="activeTab = 'tables'"
                :class="[
                  activeTab === 'tables'
                    ? 'border-indigo-500 text-indigo-600'
                    : 'border-transparent text-gray-500 hover:border-gray-300 hover:text-gray-700',
                  'whitespace-nowrap border-b-2 py-2 text-xs font-medium'
                ]"
              >
                Tables
              </button>
            </nav>
          </div>

          <div class="p-5">
            <div v-if="loadingDetails" class="flex items-center justify-center py-12">
              <Spinner class="w-5 h-5 text-gray-400 animate-spin" />
            </div>

            <template v-else>
              <!-- Overview tab -->
              <div v-if="activeTab === 'overview'" class="space-y-5">
                <!-- Description rendered with MDC -->
                <div v-if="domainDetails?.description" class="markdown-wrapper text-xs text-gray-600 leading-relaxed max-h-[320px] overflow-auto pr-1">
                  <MDC :value="domainDetails.description" class="markdown-content" />
                </div>

                <!-- Sample Questions -->
                <div v-if="domainDetails?.conversation_starters?.length">
                  <div class="text-[10px] uppercase tracking-wider text-gray-400 font-semibold mb-3">Sample Questions</div>
                  <div class="space-y-2">
                    <div
                      v-for="(starter, idx) in domainDetails.conversation_starters.slice(0, 6)"
                      :key="idx"
                      class="bg-gray-50 border border-gray-100 text-gray-700 text-xs px-4 py-2.5 rounded-lg"
                    >
                      {{ starter.split('\n')[0] }}
                    </div>
                    <div
                      v-if="domainDetails.conversation_starters.length > 6"
                      class="text-[11px] text-gray-400"
                    >
                      +{{ domainDetails.conversation_starters.length - 6 }} more
                    </div>
                  </div>
                </div>

                <div
                  v-if="!domainDetails?.description && !domainDetails?.conversation_starters?.length"
                  class="text-xs text-gray-400 italic py-8 text-center"
                >
                  No details available
                </div>
              </div>

              <!-- Tables tab -->
              <div v-else-if="activeTab === 'tables'">
                <div v-if="tablesLoading" class="flex items-center justify-center py-12">
                  <Spinner class="w-5 h-5 text-gray-400 animate-spin" />
                </div>

                <div v-else-if="tablesError" class="text-xs text-gray-500">
                  {{ tablesError }}
                </div>

                <div v-else>
                  <div class="flex items-center justify-between mb-3">
                    <div class="text-[10px] uppercase tracking-wider text-gray-400 font-semibold">Tables</div>
                    <div class="text-[11px] text-gray-400">{{ tablesCount }}</div>
                  </div>

                  <div v-if="tablesCount === 0" class="text-xs text-gray-400 italic py-8 text-center">
                    No tables found
                  </div>

                  <div v-else>
                    <!-- List view (like MentionInput) -->
                    <div v-if="!selectedTable" class="border border-gray-200 rounded-lg overflow-hidden">
                      <div class="max-h-[400px] overflow-auto">
                        <button
                          v-for="t in tablesResources"
                          :key="t.id || t.name"
                          @click="selectTable(t)"
                          class="w-full px-3 py-2 text-left text-xs flex items-center gap-2 hover:bg-gray-50 border-b border-gray-100 last:border-b-0"
                        >
                          <span class="truncate flex-1 text-gray-800 font-medium">{{ t.name }}</span>
                          <span v-if="t.columns?.length" class="text-[11px] text-gray-400 flex-shrink-0">{{ t.columns.length }} cols</span>
                        </button>
                      </div>
                      <div v-if="tablesResources.length === 0" class="px-3 py-3 text-xs text-gray-400">No tables.</div>
                    </div>

                    <!-- Detail view (columns) -->
                    <div v-else class="space-y-3">
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

                      <div class="flex flex-wrap gap-1 max-h-[320px] overflow-auto border border-gray-200 rounded-lg p-3">
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

              <div class="mt-5 flex justify-end">
                <a
                  v-if="domainId"
                  :href="`/data/${domainId}`"
                  class="text-xs font-medium text-indigo-600 hover:text-indigo-700 hover:underline"
                >
                  Open data source →
                </a>
              </div>
            </template>
          </div>
        </div>
      </div>
    </Transition>
  </Teleport>
</template>

<script setup lang="ts">
import { ref, computed, watch } from 'vue'
import Spinner from '~/components/Spinner.vue'

const props = defineProps<{
  domainId: string | null
  visible: boolean
  position: { top: number; left: number }
}>()

const emit = defineEmits<{
  (e: 'enter'): void
  (e: 'leave'): void
}>()

// State
const activeTab = ref<'overview' | 'tables'>('overview')
const domainDetails = ref<any>(null)
const loadingDetails = ref(false)
const detailsCache = ref<Record<string, any>>({})

// Tables tab state
const tablesCache = ref<Record<string, any[]>>({})
const tablesLoading = ref(false)
const tablesError = ref<string | null>(null)
const selectedTable = ref<any | null>(null)

const tablesResources = computed<any[]>(() => {
  const id = props.domainId
  if (!id) return []
  return tablesCache.value[id] || []
})

const tablesCount = computed(() => tablesResources.value.length)

// Fetch domain details when domainId changes
watch(() => props.domainId, async (id) => {
  if (!id) {
    domainDetails.value = null
    return
  }

  activeTab.value = 'overview'
  tablesError.value = null
  selectedTable.value = null

  // Check cache first
  if (detailsCache.value[id]) {
    domainDetails.value = detailsCache.value[id]
    return
  }

  domainDetails.value = null
  loadingDetails.value = true

  try {
    const { data, error } = await useMyFetch(`/data_sources/${id}`, { method: 'GET' })
    if (!error?.value && data?.value) {
      detailsCache.value[id] = data.value
      if (props.domainId === id) {
        domainDetails.value = data.value
      }
    }
  } catch (e) {
    console.error('Failed to load domain details:', e)
  } finally {
    loadingDetails.value = false
  }
}, { immediate: true })

// Fetch tables when switching to tables tab
watch(activeTab, async (tab) => {
  if (tab !== 'tables') return
  const id = props.domainId
  if (!id) return
  await fetchTablesForDomain(id)
})

async function fetchTablesForDomain(domainId: string) {
  if (!domainId) return
  if (tablesCache.value[domainId]) return
  tablesLoading.value = true
  tablesError.value = null
  try {
    const { data, error } = await useMyFetch(`/data_sources/${domainId}/schema`, { method: 'GET' })
    if (error?.value) {
      tablesError.value = 'Failed to load tables'
      return
    }
    const payload: any = (data as any)?.value
    const tables = Array.isArray(payload) ? payload : []
    const filtered = tables.filter((t: any) => t?.is_active !== false)
    tablesCache.value[domainId] = filtered
  } catch (e) {
    tablesError.value = 'Failed to load tables'
  } finally {
    tablesLoading.value = false
  }
}

function selectTable(t: any) {
  selectedTable.value = t
}

function onFlyoutEnter() {
  emit('enter')
}

function onFlyoutLeave() {
  emit('leave')
}
</script>

<style scoped>
.markdown-wrapper :deep(.markdown-content) {
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

