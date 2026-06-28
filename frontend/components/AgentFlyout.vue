<template>
  <!-- Agent hover flyout (teleported so it never gets clipped by popovers) -->
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
        v-if="visible && agentId"
        class="fixed z-[2000]"
        :style="positionStyle"
        @mouseenter="$emit('mouseenter')"
        @mouseleave="$emit('mouseleave')"
      >
        <div
          class="w-max min-w-[400px] max-w-[min(520px,calc(100vw-24px))] bg-white dark:bg-gray-900 rounded-xl shadow-2xl border border-gray-200 dark:border-gray-800 overflow-hidden flex flex-col"
          :style="panelStyle"
        >
          <!-- Header with connection info -->
          <div class="px-4 py-3 border-b border-gray-100 dark:border-gray-800 flex-shrink-0">
            <!-- Title row -->
            <div class="flex items-center justify-between gap-2">
              <div class="flex items-center gap-2 min-w-0 flex-1">
                <!-- Status dot — far left -->
                <span
                  v-if="hasActiveConnection"
                  class="w-2 h-2 rounded-full bg-green-500 flex-shrink-0"
                  :title="$t('agentFlyout.connected')"
                ></span>
                <span
                  v-else-if="agentDetails?.connections?.length"
                  class="w-2 h-2 rounded-full bg-gray-300 dark:bg-gray-600 flex-shrink-0"
                  :title="$t('agentFlyout.notConnected')"
                ></span>
                <span v-else class="w-2 h-2 rounded-full bg-gray-200 dark:bg-gray-700 flex-shrink-0"></span>

                <!-- Connection icons -->
                <div v-if="agentDetails?.connections?.length" class="flex -space-x-1 flex-shrink-0">
                  <DataSourceIcon
                    v-for="conn in (agentDetails.connections || []).slice(0, 3)"
                    :key="conn.id"
                    :type="conn.type"
                    class="h-4 flex-shrink-0 ring-1 ring-white dark:ring-gray-900 rounded"
                  />
                </div>

                <!-- Title -->
                <div class="text-sm font-semibold text-gray-900 dark:text-white truncate">
                  {{ agentDetails?.name || $t('agentFlyout.loading') }}
                </div>
              </div>

              <!-- Open agent link - top right -->
              <NuxtLink
                v-if="agentId"
                :to="`/agents/${agentId}`"
                class="text-xs font-medium text-indigo-600 hover:text-indigo-700 hover:underline flex-shrink-0 whitespace-nowrap"
              >
                {{ $t('agentFlyout.openAgent') }}
              </NuxtLink>
            </div>

            <!-- Description — full width below -->
            <div v-if="agentDetails?.description" class="text-xs text-gray-500 dark:text-gray-400 mt-1.5 leading-snug line-clamp-2">
              {{ agentDetails.description }}
            </div>
          </div>

          <!-- Locked state: agent requires per-user auth and this user hasn't
               connected (no creds, no system fallback). Show a slim Connect card
               instead of the tabs/content — notably hiding Sample Questions so a
               report can't be started against a source that has no usable creds.
               agentDetails comes from /data_sources/{id} (populates per-user
               status), so this correctly stays hidden for the service-account
               fallback. -->
          <div v-if="locked" class="px-4 py-4 flex-shrink-0">
            <p class="text-xs text-gray-500 dark:text-gray-400 mb-3">{{ $t('agentFlyout.connectToPreview') }}</p>
            <button
              @click.stop="emit('connect', agentDetails)"
              class="w-full inline-flex items-center justify-center gap-1.5 px-3 py-1.5 text-xs text-blue-600 bg-blue-50 border border-blue-200 rounded-lg hover:bg-blue-100 transition-colors"
            >
              <Icon name="heroicons-key" class="w-3.5 h-3.5" />
              {{ $t('data.connect') }}
            </button>
          </div>

          <!-- Tabs (underline / border-bottom style like Settings) -->
          <div v-else class="border-b border-gray-200 dark:border-gray-800 px-4 flex-shrink-0">
            <nav class="-mb-px flex space-x-4">
              <button
                @click="flyoutTab = 'overview'"
                :class="[
                  flyoutTab === 'overview'
                    ? 'border-indigo-500 text-indigo-600'
                    : 'border-transparent text-gray-500 dark:text-gray-400 hover:border-gray-300 dark:hover:border-gray-600 hover:text-gray-700 dark:hover:text-gray-300',
                  'whitespace-nowrap border-b-2 py-2 text-xs font-medium'
                ]"
              >
                {{ $t('agentFlyout.overview') }}
              </button>
              <button
                @click="flyoutTab = 'tables'"
                :class="[
                  flyoutTab === 'tables'
                    ? 'border-indigo-500 text-indigo-600'
                    : 'border-transparent text-gray-500 dark:text-gray-400 hover:border-gray-300 dark:hover:border-gray-600 hover:text-gray-700 dark:hover:text-gray-300',
                  'whitespace-nowrap border-b-2 py-2 text-xs font-medium'
                ]"
              >
                {{ $t('agentFlyout.tables') }}
                <span v-if="tablesCount > 0" class="ms-1 text-[10px] text-gray-400 dark:text-gray-500">({{ tablesCount }})</span>
              </button>
              <button
                @click="flyoutTab = 'instructions'"
                :class="[
                  flyoutTab === 'instructions'
                    ? 'border-indigo-500 text-indigo-600'
                    : 'border-transparent text-gray-500 dark:text-gray-400 hover:border-gray-300 dark:hover:border-gray-600 hover:text-gray-700 dark:hover:text-gray-300',
                  'whitespace-nowrap border-b-2 py-2 text-xs font-medium'
                ]"
              >
                {{ $t('agentFlyout.instructions') }}
                <span v-if="instructionsCount > 0" class="ms-1 text-[10px] text-gray-400 dark:text-gray-500">({{ instructionsCount }})</span>
              </button>
              <button
                @click="flyoutTab = 'queries'"
                :class="[
                  flyoutTab === 'queries'
                    ? 'border-indigo-500 text-indigo-600'
                    : 'border-transparent text-gray-500 dark:text-gray-400 hover:border-gray-300 dark:hover:border-gray-600 hover:text-gray-700 dark:hover:text-gray-300',
                  'whitespace-nowrap border-b-2 py-2 text-xs font-medium'
                ]"
              >
                {{ $t('agentFlyout.queries') }}
                <span v-if="queriesCount > 0" class="ms-1 text-[10px] text-gray-400 dark:text-gray-500">({{ queriesCount }})</span>
              </button>
            </nav>
          </div>

          <div v-if="!locked" class="p-4 flex-1 min-h-0 overflow-y-auto">
            <div v-if="loadingDetails" class="flex items-center justify-center py-8">
              <Spinner class="w-5 h-5 text-gray-400 dark:text-gray-500 animate-spin" />
            </div>

            <template v-else>
              <!-- Overview tab -->
              <div v-if="flyoutTab === 'overview'" class="space-y-4">
                <!-- Primary Instruction -->
                <div v-if="agentDetails?.primary_instruction" class="max-h-[40vh] overflow-y-auto pe-1">
                  <InstructionText
                    :text="agentDetails.primary_instruction.text"
                    :references="agentDetails.primary_instruction.references || []"
                    :prose="true"
                  />
                </div>

                <!-- Description rendered as Markdown -->
                <div
                  v-if="agentDetails?.description"
                  class="agent-flyout-markdown hidden text-xs text-gray-600 dark:text-gray-400 leading-relaxed max-h-[320px] overflow-auto pe-1"
                >
                  <MDC :value="agentDetails.description" class="markdown-content" />
                </div>

                <!-- Sample Questions (sourced from agent-scoped starter Prompts) -->
                <div v-if="starterTexts.length">
                  <div class="text-[10px] uppercase tracking-wider text-gray-400 dark:text-gray-500 font-semibold mb-2">{{ $t('agentFlyout.sampleQuestions') }}</div>
                  <div class="space-y-1.5">
                    <button
                      v-for="(starter, idx) in starterTexts.slice(0, 6)"
                      :key="idx"
                      @click.stop.prevent="startReportWithQuestion(starter, Number(idx))"
                      :disabled="creatingReport"
                      :class="[
                        'w-full text-start text-xs px-3 py-2 rounded-lg transition-colors flex items-center gap-2',
                        creatingReport && creatingQuestionIdx === idx
                          ? 'bg-indigo-100 border border-indigo-300 text-indigo-700'
                          : 'bg-gray-50 dark:bg-gray-800 border border-gray-100 dark:border-gray-700 text-gray-700 dark:text-gray-300 hover:bg-indigo-50 hover:border-indigo-200 hover:text-indigo-700 cursor-pointer',
                        creatingReport && creatingQuestionIdx !== idx ? 'opacity-50 cursor-not-allowed' : ''
                      ]"
                    >
                      <Spinner v-if="creatingReport && creatingQuestionIdx === idx" class="w-3 h-3 flex-shrink-0 animate-spin" />
                      <span class="flex-1">{{ starter.split('\n')[0] }}</span>
                    </button>
                    <div
                      v-if="starterTexts.length > 6"
                      class="text-[11px] text-gray-400 dark:text-gray-500"
                    >
                      {{ $t('agentFlyout.moreCount', { n: starterTexts.length - 6 }) }}
                    </div>
                  </div>
                </div>

                <div
                  v-if="!agentDetails?.primary_instruction && !agentDetails?.description && !starterTexts.length"
                  class="text-xs text-gray-400 dark:text-gray-500 italic py-6 text-center"
                >
                  {{ $t('agentFlyout.noDetails') }}
                </div>
              </div>

              <!-- Tables tab -->
              <div v-else-if="flyoutTab === 'tables'">
                <div v-if="tablesLoading" class="flex items-center justify-center py-10">
                  <Spinner class="w-5 h-5 text-gray-400 dark:text-gray-500 animate-spin" />
                </div>

                <div v-else-if="tablesError" class="text-xs text-gray-500 dark:text-gray-400">
                  {{ tablesError }}
                </div>

                <div v-else>
                  <div v-if="tablesCount === 0" class="text-xs text-gray-400 dark:text-gray-500 italic py-6 text-center">
                    {{ $t('agentFlyout.noTables') }}
                  </div>

                  <div v-else>
                    <!-- List view (like MentionInput) -->
                    <div v-if="!selectedTable" class="border border-gray-200 dark:border-gray-800 rounded-lg overflow-hidden">
                      <div class="max-h-[320px] overflow-auto">
                        <button
                          v-for="t in tablesResources"
                          :key="t.id || t.name"
                          @click="selectTable(t)"
                          class="w-full px-3 py-2 text-start text-xs flex items-center gap-2 hover:bg-gray-50 dark:hover:bg-gray-800/50 border-b border-gray-100 dark:border-gray-800 last:border-b-0"
                        >
                          <DataSourceIcon v-if="hasMultipleConnections" :type="t.connection_type" class="h-3.5 flex-shrink-0" />
                          <span class="truncate flex-1 text-gray-800 dark:text-gray-200 font-medium">{{ t.name }}</span>
                          <span v-if="t.columns?.length" class="text-[11px] text-gray-400 dark:text-gray-500 flex-shrink-0">{{ $t('agentFlyout.colsAbbr', { n: t.columns.length }) }}</span>
                        </button>
                      </div>
                      <div v-if="tablesResources.length === 0" class="px-3 py-3 text-xs text-gray-400 dark:text-gray-500">{{ $t('agentFlyout.noTablesShort') }}</div>
                    </div>

                    <!-- Detail view (columns) -->
                    <div v-else class="space-y-2">
                      <div class="flex items-center justify-between">
                        <button
                          @click="selectedTable = null"
                          class="text-[11px] text-gray-500 dark:text-gray-400 hover:text-gray-700 dark:hover:text-gray-300"
                        >
                          {{ $t('agentFlyout.back') }}
                        </button>
                        <div class="text-[11px] text-gray-400 dark:text-gray-500">{{ $t('agentFlyout.columns') }}</div>
                      </div>

                      <div class="text-sm font-semibold text-gray-900 dark:text-white truncate">{{ selectedTable.name }}</div>

                      <div class="flex flex-wrap gap-1 max-h-[240px] overflow-auto border border-gray-200 dark:border-gray-800 rounded-lg p-2">
                        <span
                          v-for="(col, idx) in (selectedTable.columns || [])"
                          :key="idx"
                          class="px-1.5 py-0.5 bg-white dark:bg-gray-800 rounded border dark:border-gray-700 text-[11px] text-gray-700 dark:text-gray-300"
                        >
                          {{ typeof col === 'string' ? col : (col as any).name }}
                          <span v-if="typeof col === 'object' && (col as any).dtype" class="text-gray-400 dark:text-gray-500 ms-1">({{ (col as any).dtype }})</span>
                        </span>
                        <span v-if="!(selectedTable.columns || []).length" class="text-[12px] text-gray-400 dark:text-gray-500">{{ $t('agentFlyout.noColumns') }}</span>
                      </div>
                    </div>
                  </div>
                </div>
              </div>

              <!-- Instructions tab -->
              <div v-else-if="flyoutTab === 'instructions'">
                <div v-if="instructionsLoading" class="flex items-center justify-center py-10">
                  <Spinner class="w-5 h-5 text-gray-400 dark:text-gray-500 animate-spin" />
                </div>

                <div v-else-if="instructionsError" class="text-xs text-gray-500 dark:text-gray-400">
                  {{ instructionsError }}
                </div>

                <div v-else>
                  <div v-if="instructionsCount === 0" class="text-xs text-gray-400 dark:text-gray-500 italic py-6 text-center">
                    {{ $t('agentFlyout.noInstructions') }}
                  </div>

                  <div v-else class="border border-gray-200 dark:border-gray-800 rounded-lg overflow-hidden">
                    <div class="max-h-[320px] overflow-auto">
                      <NuxtLink
                        v-for="inst in instructionsResources"
                        :key="inst.id"
                        :to="`/agents?search=${encodeURIComponent(inst.title || '')}`"
                        class="w-full px-3 py-2 text-start text-xs flex items-start gap-2 hover:bg-gray-50 dark:hover:bg-gray-800/50 border-b border-gray-100 dark:border-gray-800 last:border-b-0 block"
                      >
                        <div class="flex-1 min-w-0">
                          <div class="flex items-center gap-1.5">
                            <span class="truncate text-gray-800 dark:text-gray-200 font-medium">{{ inst.title || $t('agentFlyout.untitled') }}</span>
                            <span
                              v-if="!inst.data_sources?.length"
                              class="px-1 py-0.5 text-[9px] rounded bg-purple-50 text-purple-600 flex-shrink-0"
                            >
                              {{ $t('agentFlyout.global') }}
                            </span>
                          </div>
                          <div class="text-[11px] text-gray-400 dark:text-gray-500 truncate mt-0.5">
                            {{ inst.category || 'general' }} · {{ inst.source_type || 'user' }}
                          </div>
                        </div>
                      </NuxtLink>
                    </div>
                  </div>
                </div>
              </div>

              <!-- Queries tab -->
              <div v-else-if="flyoutTab === 'queries'">
                <div v-if="queriesLoading" class="flex items-center justify-center py-10">
                  <Spinner class="w-5 h-5 text-gray-400 dark:text-gray-500 animate-spin" />
                </div>

                <div v-else-if="queriesError" class="text-xs text-gray-500 dark:text-gray-400">
                  {{ queriesError }}
                </div>

                <div v-else>
                  <div v-if="queriesCount === 0" class="text-xs text-gray-400 dark:text-gray-500 italic py-6 text-center">
                    {{ $t('agentFlyout.noQueries') }}
                  </div>

                  <div v-else class="border border-gray-200 dark:border-gray-800 rounded-lg overflow-hidden">
                    <div class="max-h-[320px] overflow-auto">
                      <NuxtLink
                        v-for="entity in queriesResources"
                        :key="entity.id"
                        :to="`/queries/${entity.id}`"
                        class="w-full px-3 py-2 text-start text-xs flex items-start gap-2 hover:bg-gray-50 dark:hover:bg-gray-800/50 border-b border-gray-100 dark:border-gray-800 last:border-b-0 block"
                      >
                        <div class="flex-1 min-w-0">
                          <div class="flex items-center gap-1.5">
                            <span
                              class="px-1 py-0.5 text-[9px] rounded border flex-shrink-0"
                              :class="entity.type === 'metric' ? 'text-emerald-700 border-emerald-200 bg-emerald-50' : 'text-blue-700 border-blue-200 bg-blue-50'"
                            >{{ (entity.type || 'entity').toUpperCase() }}</span>
                            <span class="truncate text-gray-800 dark:text-gray-200 font-medium">{{ entity.title || entity.slug }}</span>
                          </div>
                          <div v-if="entity.description" class="text-[11px] text-gray-400 dark:text-gray-500 truncate mt-0.5">
                            {{ entity.description }}
                          </div>
                        </div>
                      </NuxtLink>
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
import InstructionText from '~/components/instructions/InstructionText.vue'

const router = useRouter()
const { t } = useI18n()

const props = defineProps<{
  agentId: string | null
  visible: boolean
  position: { top?: number; bottom?: number; left: number; maxHeight?: number }
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

// Cap the panel to the space available above its anchor so it never grows off
// the top of the viewport — the content area scrolls internally instead.
const panelStyle = computed(() => {
  const h = props.position.maxHeight
  return h ? { maxHeight: `${h}px` } : {}
})

const emit = defineEmits<{
  mouseenter: []
  mouseleave: []
  // Emitted with the fetched agent details when the user clicks Connect. The
  // parent owns the credentials modal / OAuth flow — the flyout itself is a
  // hover preview that unmounts on mouseleave, so it can't host a modal.
  connect: [agent: any]
}>()

// Detect whether the agent still needs a personal connection. agentDetails is
// fetched from /data_sources/{id} (which populates per-user status), so this is
// correct for admins on the service-account fallback.
const { needsUserConnection } = useDataSourceConnect()

// Locked = the agent requires per-user auth this user hasn't completed. In this
// state the flyout shows a slim Connect card instead of tabs/content (and no
// Sample Questions, which would start a report against an unusable source).
const locked = computed(() => !!agentDetails.value && needsUserConnection(agentDetails.value))

// Allow the parent to clear the cached details after a successful connect so
// the flyout reflects the new state on next hover.
function refreshDetails() {
  const id = props.agentId
  if (!id) return
  delete detailsCache.value[id]
  delete startersCache.value[id]
  fetchAgentDetails(id)
  fetchStartersForAgent(id)
}
defineExpose({ refreshDetails })

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

// Starter Prompts (agent-scoped) — source of the Sample Questions list.
const startersCache = ref<Record<string, string[]>>({})
const starterTexts = computed<string[]>(() => {
  if (!props.agentId) return []
  return startersCache.value[props.agentId] || []
})

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

const fetchStartersForAgent = async (agentId: string) => {
  if (!agentId || startersCache.value[agentId]) return
  try {
    const { data, error } = await useMyFetch(`/prompts?data_source_id=${agentId}`, { method: 'GET' })
    if (error?.value) return
    const prompts = (data?.value as any)?.prompts || []
    startersCache.value[agentId] = prompts.map((p: any) => String(p?.text ?? '')).filter((t: string) => t.trim().length > 0)
  } catch (e) {
    console.error('Failed to load agent starters:', e)
  }
}

const fetchTablesForAgent = async (agentId: string) => {
  if (!agentId || tablesCache.value[agentId]) return
  tablesLoading.value = true
  tablesError.value = null
  try {
    const { data, error } = await useMyFetch(`/data_sources/${agentId}/schema`, { method: 'GET' })
    if (error?.value) {
      tablesError.value = t('agentFlyout.tablesLoadFailed')
      return
    }
    const payload: any = (data as any)?.value
    const tables = Array.isArray(payload) ? payload : []
    const filtered = tables.filter((t: any) => t?.is_active !== false)
    tablesCache.value[agentId] = filtered
  } catch (e) {
    tablesError.value = t('agentFlyout.tablesLoadFailed')
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
      instructionsError.value = t('agentFlyout.instructionsLoadFailed')
      return
    }
    const payload: any = (data as any)?.value
    const items = payload?.items || payload || []
    instructionsCache.value[agentId] = items
  } catch (e) {
    instructionsError.value = t('agentFlyout.instructionsLoadFailed')
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
      queriesError.value = t('agentFlyout.queriesLoadFailed')
      return
    }
    const payload: any = (data as any)?.value
    const entities = Array.isArray(payload) ? payload : []
    const filtered = entities.filter((e: any) =>
      e.status === 'published' && e.global_status === 'approved'
    )
    queriesCache.value[agentId] = filtered
  } catch (e) {
    queriesError.value = t('agentFlyout.queriesLoadFailed')
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

    // Only fetch overview on hover; other tabs load on demand
    await fetchAgentDetails(newId)
    fetchStartersForAgent(newId)
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

  ul, ol { @apply ps-4 mb-2; }
  ul { @apply list-disc; }
  ol { @apply list-decimal; }
  li { @apply mb-1; }

  pre { @apply bg-gray-50 dark:bg-gray-800 p-2 rounded-lg mb-2 overflow-x-auto text-[11px]; }
  code { @apply bg-gray-50 dark:bg-gray-800 px-1 py-0.5 rounded text-[11px] font-mono; }
  a { @apply text-blue-600 hover:text-blue-800 underline; }
  blockquote { @apply border-l-4 border-gray-200 dark:border-gray-800 pl-3 italic my-2; }
  table { @apply w-full border-collapse mb-2; }
  table th, table td { @apply border border-gray-200 dark:border-gray-800 p-1 text-[11px] bg-white dark:bg-gray-900; }
  p { @apply mb-2; }
}
</style>
