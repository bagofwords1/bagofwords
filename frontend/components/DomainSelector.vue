<template>
  <div class="domain-selector">
    <UPopover 
      :popper="{ placement: 'bottom-start', offsetDistance: 4, strategy: 'fixed' }"
      :ui="{ 
        width: 'max-w-none',
        container: 'overflow-visible',
        inner: 'overflow-visible'
      }"
    >
      <button 
        :class="[
          'flex items-center w-full rounded-lg transition-all duration-200',
          'bg-white hover:bg-gray-50',
          'border border-gray-200 shadow-sm hover:shadow hover:border-gray-300',
          collapsed ? 'justify-center p-2' : 'gap-1.5 px-2.5 py-2'
        ]"
      >
        <UTooltip v-if="collapsed" :text="currentDomainName" :popper="{ placement: 'right' }">
          <span class="flex items-center justify-center w-5 h-5">
            <Spinner v-if="loading" class="w-4 h-4 text-gray-400 animate-spin" />
            <UIcon v-else name="heroicons-chevron-down" class="w-4 h-4 text-gray-500" />
          </span>
        </UTooltip>
        <template v-else>
          <span v-if="showText" class="flex-1 text-left min-w-0">
            <span class="block text-[8px] uppercase tracking-wide text-gray-400 font-semibold leading-none">CONTEXT</span>
            <span class="flex items-center gap-1.5 mt-0.5">
              <Spinner v-if="loading" class="w-3 h-3 text-gray-400 animate-spin flex-shrink-0" />
              <span class="text-xs font-medium text-gray-700 truncate">
                {{ currentDomainName }}
              </span>
            </span>
          </span>
          <UIcon v-if="showText" name="heroicons-chevron-down" class="w-3.5 h-3.5 text-gray-400 flex-shrink-0" />
        </template>
      </button>

      <template #panel>
        <div class="overflow-visible">
          <!-- Domain list - compact -->
          <div class="w-44 bg-white rounded-lg shadow-lg border border-gray-200 overflow-hidden flex-shrink-0">
            <div class="p-1">
              <!-- Loading state inside panel -->
              <div v-if="loading" class="flex items-center justify-center py-4">
                <Spinner class="w-5 h-5 text-gray-400 animate-spin" />
              </div>

              <template v-else>
                <!-- All Domains option -->
                <button 
                  @click="toggleDomain(null)"
                  @mouseenter="hoveredDomainId = null"
                  @mouseleave="onDomainHoverLeave()"
                  :class="[
                    'w-full flex items-center gap-2 px-2.5 py-1.5 rounded-md text-left transition-colors',
                    isAllDomains ? 'bg-indigo-50 text-indigo-700' : 'text-gray-600 hover:bg-gray-50'
                  ]"
                >
                  <span class="text-xs font-medium">All Domains</span>
                  <UIcon v-if="isAllDomains" name="heroicons-check" class="w-3 h-3 ml-auto text-indigo-600" />
                </button>

                <!-- Divider -->
                <div class="my-1 border-t border-gray-100" />

                <!-- Domain list -->
                <div class="max-h-48 overflow-y-auto">
                  <button 
                    v-for="d in domains" 
                    :key="d.id"
                    @click="toggleDomain(d.id)"
                    @mouseenter="onDomainHover(d.id, $event)"
                    @mouseleave="onDomainHoverLeave()"
                    :class="[
                      'w-full flex items-center gap-2 px-2.5 py-1.5 rounded-md text-left transition-colors',
                      isDomainSelected(d.id) ? 'bg-indigo-50 text-indigo-700' : 'text-gray-600 hover:bg-gray-50'
                    ]"
                  >
                    <span class="text-xs font-medium truncate flex-1">{{ d.name }}</span>
                    <UIcon v-if="isDomainSelected(d.id)" name="heroicons-check" class="w-3 h-3 text-indigo-600 flex-shrink-0" />
                  </button>
                </div>

                <!-- Divider -->
                <div class="my-1 border-t border-gray-100" />

                <!-- Manage link -->
                <a 
                  href="/data"
                  class="w-full flex items-center gap-2 px-2.5 py-1.5 rounded-md text-left text-gray-400 hover:bg-gray-50 hover:text-gray-600 transition-colors"
                >
                  <UIcon name="heroicons-cog-6-tooth" class="w-3 h-3 flex-shrink-0" />
                  <span class="text-[11px]">Manage</span>
                </a>
              </template>
            </div>
          </div>
        </div>
      </template>
    </UPopover>

    <!-- Domain hover flyout (teleported so it never gets clipped by popovers) -->
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
          v-if="flyout.visible && hoveredDomainId"
          class="fixed z-[2000]"
          :style="{ top: `${flyout.top}px`, left: `${flyout.left}px` }"
          @mouseenter="onFlyoutEnter"
          @mouseleave="onFlyoutLeave"
        >
          <div class="w-[400px] bg-white rounded-xl shadow-2xl border border-gray-200 overflow-hidden">
            <!-- Header with connection info -->
            <div class="px-4 py-3 border-b border-gray-100">
              <div class="flex items-start justify-between gap-2">
                <div class="min-w-0 flex-1">
                  <div class="text-sm font-semibold text-gray-900 truncate">
                    {{ hoveredDomainDetails?.name || 'Loading…' }}
                  </div>
                  <!-- Connection info: icon + name + status indicator -->
                  <div class="flex items-center gap-2 mt-1">
                    <DataSourceIcon 
                      v-if="hoveredDomainDetails?.connection?.type" 
                      :type="hoveredDomainDetails.connection.type" 
                      class="w-4 h-4 flex-shrink-0" 
                    />
                    <span class="text-xs text-gray-500 truncate">
                      {{ hoveredDomainDetails?.connection?.name || 'No connection' }}
                    </span>
                    <!-- Green circle if connected -->
                    <span 
                      v-if="isConnectionActive" 
                      class="w-2 h-2 rounded-full bg-green-500 flex-shrink-0"
                      title="Connected"
                    ></span>
                    <span 
                      v-else-if="hoveredDomainDetails?.connection" 
                      class="w-2 h-2 rounded-full bg-gray-300 flex-shrink-0"
                      title="Not connected"
                    ></span>
                  </div>
                </div>
                <!-- Open domain link - top right -->
                <a
                  v-if="hoveredDomainId"
                  :href="`/data/${hoveredDomainId}`"
                  class="text-xs font-medium text-indigo-600 hover:text-indigo-700 hover:underline flex-shrink-0 whitespace-nowrap"
                >
                  Open domain →
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
                  @click="flyoutTab = 'catalog'"
                  :class="[
                    flyoutTab === 'catalog'
                      ? 'border-indigo-500 text-indigo-600'
                      : 'border-transparent text-gray-500 hover:border-gray-300 hover:text-gray-700',
                    'whitespace-nowrap border-b-2 py-2 text-xs font-medium'
                  ]"
                >
                  Catalog
                  <span v-if="catalogCount > 0" class="ml-1 text-[10px] text-gray-400">({{ catalogCount }})</span>
                </button>
              </nav>
            </div>

            <div class="p-4">
              <div v-if="loadingDomainDetails" class="flex items-center justify-center py-8">
                <Spinner class="w-5 h-5 text-gray-400 animate-spin" />
              </div>

              <template v-else>
                <!-- Overview tab -->
                <div v-if="flyoutTab === 'overview'" class="space-y-4">
                  <!-- Description rendered as Markdown -->
                  <div
                    v-if="hoveredDomainDetails?.description"
                    class="domain-selector-flyout-markdown text-xs text-gray-600 leading-relaxed max-h-[320px] overflow-auto pr-1"
                  >
                    <MDC :value="hoveredDomainDetails.description" class="markdown-content" />
                  </div>

                  <!-- Sample Questions -->
                  <div v-if="hoveredDomainDetails?.conversation_starters?.length">
                    <div class="text-[10px] uppercase tracking-wider text-gray-400 font-semibold mb-2">Sample Questions</div>
                    <div class="space-y-1.5">
                      <div
                        v-for="(starter, idx) in hoveredDomainDetails.conversation_starters.slice(0, 6)"
                        :key="idx"
                        class="bg-gray-50 border border-gray-100 text-gray-700 text-xs px-3 py-2 rounded-lg"
                      >
                        {{ starter.split('\n')[0] }}
                      </div>
                      <div
                        v-if="hoveredDomainDetails.conversation_starters.length > 6"
                        class="text-[11px] text-gray-400"
                      >
                        +{{ hoveredDomainDetails.conversation_starters.length - 6 }} more
                      </div>
                    </div>
                  </div>

                  <div
                    v-if="!hoveredDomainDetails?.description && !hoveredDomainDetails?.conversation_starters?.length"
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

                <!-- Catalog tab -->
                <div v-else-if="flyoutTab === 'catalog'">
                  <div v-if="catalogLoading" class="flex items-center justify-center py-10">
                    <Spinner class="w-5 h-5 text-gray-400 animate-spin" />
                  </div>

                  <div v-else-if="catalogError" class="text-xs text-gray-500">
                    {{ catalogError }}
                  </div>

                  <div v-else>
                    <div v-if="catalogCount === 0" class="text-xs text-gray-400 italic py-6 text-center">
                      No catalog entities found
                    </div>

                    <div v-else class="border border-gray-200 rounded-lg overflow-hidden">
                      <div class="max-h-[320px] overflow-auto">
                        <a
                          v-for="entity in catalogResources"
                          :key="entity.id"
                          :href="`/catalog/${entity.id}`"
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
  </div>
</template>

<script setup lang="ts">
import Spinner from '~/components/Spinner.vue'
import DataSourceIcon from '~/components/DataSourceIcon.vue'

const props = withDefaults(defineProps<{
  collapsed?: boolean
  showText?: boolean
}>(), {
  collapsed: false,
  showText: true
})

// Domain management
const { 
  selectedDomains,
  domains, 
  loading,
  hasDomains, 
  selectedCount,
  isAllDomains,
  currentDomainName,
  selectedDomainObjects,
  toggleDomain,
  isDomainSelected,
  initDomain 
} = useDomain()

// Domain hover preview
const hoveredDomainId = ref<string | null>(null)
const hoveredDomainDetails = ref<any>(null)
const loadingDomainDetails = ref(false)
const domainDetailsCache = ref<Record<string, any>>({})
const flyout = reactive({ visible: false, top: 0, left: 0 })
let flyoutHideTimer: ReturnType<typeof setTimeout> | null = null
const flyoutTab = ref<'overview' | 'tables' | 'instructions' | 'catalog'>('overview')

// Tables tab state (data source schema)
const tablesCache = ref<Record<string, any[]>>({})
const tablesLoading = ref(false)
const tablesError = ref<string | null>(null)
const selectedTable = ref<any | null>(null)

const tablesResources = computed<any[]>(() => {
  const id = hoveredDomainId.value
  if (!id) return []
  return tablesCache.value[id] || []
})
const tablesCount = computed(() => tablesResources.value.length)

// Instructions tab state
const instructionsCache = ref<Record<string, any[]>>({})
const instructionsLoading = ref(false)
const instructionsError = ref<string | null>(null)

const instructionsResources = computed<any[]>(() => {
  const id = hoveredDomainId.value
  if (!id) return []
  return instructionsCache.value[id] || []
})
const instructionsCount = computed(() => instructionsResources.value.length)

// Catalog tab state
const catalogCache = ref<Record<string, any[]>>({})
const catalogLoading = ref(false)
const catalogError = ref<string | null>(null)

const catalogResources = computed<any[]>(() => {
  const id = hoveredDomainId.value
  if (!id) return []
  return catalogCache.value[id] || []
})
const catalogCount = computed(() => catalogResources.value.length)

// Computed: check if connection is active (status === 'success')
const isConnectionActive = computed(() => {
  const userStatus = hoveredDomainDetails.value?.connection?.user_status
  const connectionStatus = userStatus?.connection
  return connectionStatus === 'success'
})

const showFlyoutAtEvent = (evt: MouseEvent) => {
  const el = evt.currentTarget as HTMLElement | null
  if (!el) return
  const rect = el.getBoundingClientRect()

  // Position to the right of the hovered row, with a small gap.
  // Clamp to viewport height to avoid going off-screen.
  const desiredLeft = rect.right + 12
  const desiredTop = rect.top - 8
  const maxTop = window.innerHeight - 720 // flyout approx height
  flyout.left = Math.max(12, desiredLeft)
  flyout.top = Math.max(12, Math.min(desiredTop, maxTop))
  flyout.visible = true
}

const onDomainHover = async (domainId: string, evt: MouseEvent) => {
  if (flyoutHideTimer) {
    clearTimeout(flyoutHideTimer)
    flyoutHideTimer = null
  }
  if (typeof window !== 'undefined') showFlyoutAtEvent(evt)
  hoveredDomainId.value = domainId
  flyoutTab.value = 'overview'
  // Reset all tab errors
  tablesError.value = null
  selectedTable.value = null
  instructionsError.value = null
  catalogError.value = null
  
  // Check cache first for domain details
  const needsDomainDetails = !domainDetailsCache.value[domainId]
  if (!needsDomainDetails) {
    hoveredDomainDetails.value = domainDetailsCache.value[domainId]
  } else {
    hoveredDomainDetails.value = null
    loadingDomainDetails.value = true
  }

  // Fetch domain details and all tab data in parallel for faster loading
  const fetchDomainDetails = async () => {
    if (!needsDomainDetails) return
    try {
      const { data, error } = await useMyFetch(`/data_sources/${domainId}`, { method: 'GET' })
      if (!error?.value && data?.value) {
        domainDetailsCache.value[domainId] = data.value
        // Only set if still hovering this domain
        if (hoveredDomainId.value === domainId) {
          hoveredDomainDetails.value = data.value
        }
      }
    } catch (e) {
      console.error('Failed to load domain details:', e)
    } finally {
      loadingDomainDetails.value = false
    }
  }

  // Pre-fetch all data in parallel to show counts faster
  await Promise.all([
    fetchDomainDetails(),
    fetchTablesForDomain(domainId),
    fetchInstructionsForDomain(domainId),
    fetchCatalogForDomain(domainId)
  ])
}

const onDomainHoverLeave = () => {
  // Give the user time to move cursor from list → flyout
  if (flyoutHideTimer) clearTimeout(flyoutHideTimer)
  flyoutHideTimer = setTimeout(() => {
    flyout.visible = false
    hoveredDomainId.value = null
    hoveredDomainDetails.value = null
  }, 120)
}

const onFlyoutEnter = () => {
  if (flyoutHideTimer) {
    clearTimeout(flyoutHideTimer)
    flyoutHideTimer = null
  }
  flyout.visible = true
}

const onFlyoutLeave = () => {
  onDomainHoverLeave()
}

const fetchTablesForDomain = async (domainId: string) => {
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
    // Respect is_active if present; otherwise keep all
    const filtered = tables.filter((t: any) => t?.is_active !== false)
    tablesCache.value[domainId] = filtered
  } catch (e) {
    tablesError.value = 'Failed to load tables'
  } finally {
    tablesLoading.value = false
  }
}

const fetchInstructionsForDomain = async (domainId: string) => {
  if (!domainId) return
  if (instructionsCache.value[domainId]) return
  instructionsLoading.value = true
  instructionsError.value = null
  try {
    // Fetch instructions for this domain + global instructions (those without data_source)
    // The API will include global instructions when filtering by domain
    const { data, error } = await useMyFetch('/api/instructions', { 
      method: 'GET',
      query: {
        data_source_ids: domainId,
        include_global: true, // Include global instructions
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
    // Handle paginated response
    const items = payload?.items || payload || []
    instructionsCache.value[domainId] = items
  } catch (e) {
    instructionsError.value = 'Failed to load instructions'
  } finally {
    instructionsLoading.value = false
  }
}

const fetchCatalogForDomain = async (domainId: string) => {
  if (!domainId) return
  if (catalogCache.value[domainId]) return
  catalogLoading.value = true
  catalogError.value = null
  try {
    const { data, error } = await useMyFetch('/api/entities', { 
      method: 'GET',
      query: {
        data_source_ids: domainId
      }
    })
    if (error?.value) {
      catalogError.value = 'Failed to load catalog'
      return
    }
    const payload: any = (data as any)?.value
    const entities = Array.isArray(payload) ? payload : []
    // Filter to only published entities
    const filtered = entities.filter((e: any) => 
      e.status === 'published' && e.global_status === 'approved'
    )
    catalogCache.value[domainId] = filtered
  } catch (e) {
    catalogError.value = 'Failed to load catalog'
  } finally {
    catalogLoading.value = false
  }
}

watch(flyoutTab, async (tab) => {
  const id = hoveredDomainId.value
  if (!id) return
  
  if (tab === 'tables') {
    await fetchTablesForDomain(id)
  } else if (tab === 'instructions') {
    await fetchInstructionsForDomain(id)
  } else if (tab === 'catalog') {
    await fetchCatalogForDomain(id)
  }
})

const selectTable = (t: any) => {
  selectedTable.value = t
}
</script>

<style lang="postcss">
/* Not scoped: flyout is teleported */
.domain-selector-flyout-markdown .markdown-content {
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

