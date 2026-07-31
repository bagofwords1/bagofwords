<template>
  <!-- Agent hover flyout (teleported so it never gets clipped by popovers).
       Minimal by design: icon + title, connections/counts line, description. -->
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
          class="w-max min-w-[320px] max-w-[min(440px,calc(100vw-24px))] bg-white dark:bg-gray-900 rounded-xl shadow-2xl border border-gray-200 dark:border-gray-800 overflow-hidden"
          :style="panelStyle"
        >
          <div v-if="loadingDetails" class="flex items-center justify-center py-8">
            <Spinner class="w-5 h-5 text-gray-400 dark:text-gray-500 animate-spin" />
          </div>

          <div v-else class="px-4 py-3.5">
            <!-- Icon + title -->
            <div class="flex items-center justify-between gap-3">
              <div class="flex items-center gap-2 min-w-0 flex-1">
                <DataSourceIcon
                  :type="primaryConnection?.type"
                  :connector-key="primaryConnection?.connector_key"
                  :icon="agentDetails?.icon"
                  class="h-4 w-4 flex-shrink-0"
                />
                <div class="text-sm font-semibold text-gray-900 dark:text-white truncate">
                  {{ agentDetails?.name || $t('agentFlyout.loading') }}
                </div>
              </div>
              <NuxtLink
                v-if="agentId"
                :to="`/agents/${agentId}`"
                class="text-[11px] font-medium text-gray-400 hover:text-indigo-600 flex-shrink-0 whitespace-nowrap"
              >
                {{ $t('agentFlyout.openAgent') }}
              </NuxtLink>
            </div>

            <!-- Connections | counts -->
            <div v-if="connectionNames || countsLabel" class="mt-1.5 text-[11px] text-gray-400 dark:text-gray-500 truncate">
              <span v-if="connectionNames">{{ connectionNames }}</span>
              <span v-if="connectionNames && countsLabel" class="mx-1.5 text-gray-300 dark:text-gray-600">|</span>
              <span v-if="countsLabel">{{ countsLabel }}</span>
            </div>

            <!-- Description -->
            <div
              v-if="agentDetails?.description"
              class="mt-2 text-xs text-gray-600 dark:text-gray-400 leading-relaxed line-clamp-4"
            >
              {{ agentDetails.description }}
            </div>

            <!-- Locked: agent needs the user's own connection first -->
            <button
              v-if="locked"
              @click.stop="emit('connect', agentDetails)"
              class="mt-3 w-full inline-flex items-center justify-center gap-1.5 px-3 py-1.5 text-xs text-blue-600 bg-blue-50 border border-blue-200 rounded-lg hover:bg-blue-100 transition-colors"
            >
              <Icon name="heroicons-key" class="w-3.5 h-3.5" />
              {{ $t('data.connect') }}
            </button>
          </div>
        </div>
      </div>
    </Transition>
  </Teleport>
</template>

<script setup lang="ts">
import Spinner from '~/components/Spinner.vue'
import DataSourceIcon from '~/components/DataSourceIcon.vue'

const props = defineProps<{
  agentId: string | null
  visible: boolean
  position: { top?: number; bottom?: number; left: number; maxHeight?: number }
}>()

const positionStyle = computed(() => {
  const style: Record<string, string> = { left: `${props.position.left}px` }
  if (props.position.bottom !== undefined) style.bottom = `${props.position.bottom}px`
  else if (props.position.top !== undefined) style.top = `${props.position.top}px`
  return style
})

const panelStyle = computed(() => {
  const h = props.position.maxHeight
  return h ? { maxHeight: `${h}px` } : {}
})

const emit = defineEmits<{
  mouseenter: []
  mouseleave: []
  // Parent owns the credentials modal / OAuth flow.
  connect: [agent: any]
}>()

const { needsUserConnection } = useDataSourceConnect()
const locked = computed(() => !!agentDetails.value && needsUserConnection(agentDetails.value))

const agentDetails = ref<any>(null)
const loadingDetails = ref(false)
const detailsCache = ref<Record<string, any>>({})

const primaryConnection = computed<any>(() => (agentDetails.value?.connections || [])[0] || null)

// "PBI, Fabric" — connection names (fallback to type)
const connectionNames = computed<string>(() => {
  const conns = agentDetails.value?.connections || []
  return conns
    .map((c: any) => (c?.name || c?.type || '').trim())
    .filter(Boolean)
    .slice(0, 4)
    .join(', ')
})

// "33 tools, 23 tables" — catalog counts grouped by each connection's data_shape
const countsLabel = computed<string>(() => {
  const conns = agentDetails.value?.connections || []
  const sums: Record<string, number> = {}
  for (const c of conns) {
    const n = typeof c?.table_count === 'number' ? c.table_count : 0
    if (n > 0) {
      const shape = c?.data_shape || 'tables'
      sums[shape] = (sums[shape] || 0) + n
    }
  }
  const noun: Record<string, string> = { tables: 'tables', tools: 'tools', files: 'files', objects: 'objects' }
  return Object.entries(sums)
    .map(([shape, n]) => `${n} ${noun[shape] || shape}`)
    .join(', ')
})

// Allow the parent to clear cached details after a successful connect.
function refreshDetails() {
  const id = props.agentId
  if (!id) return
  delete detailsCache.value[id]
  fetchAgentDetails(id)
}
defineExpose({ refreshDetails })

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
      if (props.agentId === agentId) agentDetails.value = data.value
    }
  } catch (e) {
    console.error('Failed to load agent details:', e)
  } finally {
    loadingDetails.value = false
  }
}

watch(() => props.agentId, async (newId, oldId) => {
  if (newId && newId !== oldId) {
    agentDetails.value = null
    await fetchAgentDetails(newId)
  }
}, { immediate: true })
</script>
