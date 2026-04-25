<template>
  <UModal v-model="isOpen" :ui="{ width: 'sm:max-w-xl' }">
    <div class="p-5">
      <!-- Step 1: Select data source type -->
      <div v-if="step === 'select'">
        <div class="flex items-center justify-between mb-4">
          <h3 class="text-lg font-semibold">{{ $t('data.addConnection') }}</h3>
          <button @click="isOpen = false" class="text-gray-400 hover:text-gray-600">
            <UIcon name="heroicons-x-mark" class="w-5 h-5" />
          </button>
        </div>
        <p class="text-sm text-gray-500 mb-4">{{ $t('data.selectTypeHint') }}</p>

        <!-- Demo data sources at the top -->
        <div v-if="uninstalledDemos.length > 0" class="mb-4">
          <div class="text-xs text-gray-400 mb-2">{{ $t('data.trySample') }}</div>
          <div class="flex flex-wrap gap-2">
            <button
              v-for="demo in uninstalledDemos"
              :key="`demo-${demo.id}`"
              @click="handleInstallDemo(demo.id)"
              :disabled="installingDemo === demo.id"
              class="inline-flex items-center gap-2 px-3 py-1.5 text-xs text-gray-600 rounded-full border border-gray-200 bg-white hover:bg-gray-50 hover:border-gray-300 transition-colors disabled:opacity-50 disabled:cursor-not-allowed"
            >
              <Spinner v-if="installingDemo === demo.id" class="h-3 w-3" />
              <DataSourceIcon v-else class="h-4" :type="demo.type" />
              {{ demo.name }}
              <span class="text-[9px] font-medium uppercase tracking-wide text-purple-600 bg-purple-100 px-1.5 py-0.5 rounded">{{ $t('data.sampleTag') }}</span>
            </button>
          </div>
        </div>

        <!-- Search input -->
        <div class="mb-4">
          <UInput
            v-model="searchQuery"
            :placeholder="$t('data.searchSources')"
            icon="i-heroicons-magnifying-glass"
            size="sm"
          />
        </div>

        <!-- Loading state -->
        <div v-if="loadingDataSources" class="flex items-center justify-center py-12">
          <Spinner class="h-4 w-4 text-gray-400" />
        </div>

        <!-- Data source grid -->
        <div v-else class="grid grid-cols-3 sm:grid-cols-4 gap-3 max-h-[300px] overflow-y-auto">
          <button
            v-for="ds in filteredDataSources"
            :key="ds.type"
            type="button"
            :disabled="isLocked(ds)"
            @click="!isLocked(ds) && selectType(ds)"
            :class="[
              'group rounded-lg p-3 bg-white border transition-all w-full',
              isLocked(ds)
                ? 'opacity-60 cursor-not-allowed border-gray-200'
                : 'hover:bg-gray-50 border-gray-100 hover:border-blue-200'
            ]"
          >
            <div class="flex flex-col items-center text-center">
              <div class="p-1 relative">
                <DataSourceIcon class="h-6" :type="ds.type" />
                <div v-if="isLocked(ds)" class="absolute -top-1 -end-1">
                  <svg class="h-3 w-3 text-gray-400" fill="currentColor" viewBox="0 0 20 20">
                    <path fill-rule="evenodd" d="M5 9V7a5 5 0 0110 0v2a2 2 0 012 2v5a2 2 0 01-2 2H5a2 2 0 01-2-2v-5a2 2 0 012-2zm8-2v2H7V7a3 3 0 016 0z" clip-rule="evenodd" />
                  </svg>
                </div>
              </div>
              <div class="text-xs text-gray-500 mt-1">{{ ds.title }}</div>
              <div v-if="isLocked(ds)" class="mt-1">
                <span class="text-[9px] font-medium uppercase tracking-wide text-purple-600 bg-purple-100 px-1.5 py-0.5 rounded">
                  {{ $t('data.enterprise') }}
                </span>
              </div>
            </div>
          </button>
        </div>

        <!-- No results -->
        <div v-if="!loadingDataSources && filteredDataSources.length === 0" class="text-center py-8 text-gray-500 text-sm">
          {{ $t('data.noSourcesFound', { query: searchQuery }) }}
        </div>
      </div>

      <!-- Step 2: Connection form -->
      <div v-else-if="step === 'form'">
        <div class="flex items-center gap-2 mb-4">
          <button type="button" @click="backToSelect" class="text-gray-500 hover:text-gray-700">
            <UIcon name="heroicons-chevron-left" class="w-5 h-5" />
          </button>
          <DataSourceIcon :type="selectedDataSource?.type" class="h-5" />
          <h3 class="text-lg font-semibold">{{ selectedDataSource?.title }}</h3>
          <button @click="isOpen = false" class="ms-auto text-gray-400 hover:text-gray-600">
            <UIcon name="heroicons-x-mark" class="w-5 h-5" />
          </button>
        </div>

        <ConnectForm
          @success="handleConnectionSuccess"
          :initialType="selectedDataSource?.type"
          :initialName="selectedDataSource?.title"
          :allowNameEdit="true"
          :forceShowSystemCredentials="true"
          :showRequireUserAuthToggle="true"
          :initialRequireUserAuth="false"
          :showTestButton="true"
          :showLLMToggle="false"
          :hideHeader="true"
          mode="create_connection_only"
        />
      </div>

      <!-- Step 3: Indexing progress -->
      <div v-else-if="step === 'indexing'">
        <div class="flex items-center gap-2 mb-4">
          <DataSourceIcon :type="selectedDataSource?.type" class="h-5" />
          <h3 class="text-lg font-semibold">{{ createdConnection?.name || selectedDataSource?.title }}</h3>
          <span
            v-if="indexingState?.status === 'completed'"
            class="inline-flex items-center gap-1 text-xs px-2 py-0.5 rounded border bg-green-50 text-green-700 border-green-200"
          >
            <UIcon name="heroicons-check-circle" class="w-3.5 h-3.5" />
            Connected
          </span>
          <span
            v-else-if="indexingState?.status === 'failed'"
            class="inline-flex items-center gap-1 text-xs px-2 py-0.5 rounded border bg-red-50 text-red-700 border-red-200"
          >
            <UIcon name="heroicons-exclamation-triangle" class="w-3.5 h-3.5" />
            Failed
          </span>
          <span
            v-else
            class="inline-flex items-center gap-1 text-xs px-2 py-0.5 rounded border bg-blue-50 text-blue-700 border-blue-200"
          >
            <Spinner class="w-3 h-3" />
            Indexing
          </span>
        </div>

        <div class="border border-gray-100 rounded-lg p-4 bg-gray-50">
          <div class="text-xs uppercase tracking-wide text-gray-400 mb-2">Schema discovery</div>
          <ConnectionIndexingProgress :indexing="indexingState" :show-logs="true" />
        </div>

        <div class="flex items-center justify-end gap-2 mt-4">
          <UButton
            v-if="indexingState?.status === 'failed'"
            color="amber"
            variant="soft"
            size="sm"
            :loading="retrying"
            @click="retryIndexing"
          >
            <UIcon name="heroicons-arrow-path" class="w-4 h-4 me-1" />
            Retry
          </UButton>
          <UButton
            color="blue"
            size="sm"
            :disabled="!isIndexingTerminal"
            @click="finishConnect"
          >
            Connect
          </UButton>
        </div>
      </div>

    </div>
  </UModal>
</template>

<script setup lang="ts">
import Spinner from '~/components/Spinner.vue'
import ConnectForm from '~/components/datasources/ConnectForm.vue'
import ConnectionIndexingProgress from '~/components/ConnectionIndexingProgress.vue'
import { useEnterprise } from '~/ee/composables/useEnterprise'
import { isIndexingActive, type ConnectionIndexing } from '~/composables/useConnectionStatus'

const props = defineProps<{
  modelValue: boolean
  initialSelectedType?: string
}>()

const emit = defineEmits<{
  (e: 'update:modelValue', value: boolean): void
  (e: 'created', connection: any): void
}>()

const isOpen = computed({
  get: () => props.modelValue,
  set: (value) => emit('update:modelValue', value)
})

const { isLicensed } = useEnterprise()
const { t } = useI18n()
const toast = useToast()

// State
const step = ref<'select' | 'form' | 'indexing'>('select')
const searchQuery = ref('')
const dataSources = ref<any[]>([])
const demos = ref<any[]>([])
const loadingDataSources = ref(true)
const selectedDataSource = ref<any>(null)
const installingDemo = ref<string | null>(null)
const createdConnection = ref<any | null>(null)
const indexingState = ref<ConnectionIndexing | null>(null)
const retrying = ref(false)
let pollTimer: ReturnType<typeof setInterval> | null = null
const POLL_INTERVAL_MS = 2000

const isIndexingTerminal = computed(() =>
    !!indexingState.value && !isIndexingActive(indexingState.value)
)

// Computed
const uninstalledDemos = computed(() => (demos.value || []).filter((demo: any) => !demo.installed))

// Check if data source requires enterprise license
const isLocked = (ds: any) => ds.requires_license === 'enterprise' && !isLicensed.value

// Filter data sources by search query
const filteredDataSources = computed(() => {
  if (!searchQuery.value.trim()) return dataSources.value
  const query = searchQuery.value.toLowerCase()
  return dataSources.value.filter((ds: any) =>
    ds.title?.toLowerCase().includes(query) ||
    ds.type?.toLowerCase().includes(query)
  )
})

// Fetch available data sources and demos
async function fetchDataSources() {
  loadingDataSources.value = true
  try {
    const [availableRes, demosRes] = await Promise.all([
      useMyFetch('/available_data_sources', { method: 'GET' }),
      useMyFetch('/data_sources/demos', { method: 'GET' })
    ])
    if (availableRes.data.value) {
      dataSources.value = availableRes.data.value as any[]
    }
    if (demosRes.data.value) {
      demos.value = demosRes.data.value as any[]
    }
  } finally {
    loadingDataSources.value = false
  }
}

// Install a demo data source
async function handleInstallDemo(demoId: string) {
  installingDemo.value = demoId
  try {
    const response = await useMyFetch(`/data_sources/demos/${demoId}`, { method: 'POST' })
    const result = response.data.value as any
    if (result?.success) {
      const demoName = demos.value.find(d => d.id === demoId)?.name || t('data.sampleDataFallback')
      toast.add({
        title: t('data.sampleAdded'),
        description: t('data.sampleAddedNamed', { name: demoName }),
        icon: 'i-heroicons-check-circle',
        color: 'green'
      })
      emit('created', { id: result.data_source_id, isDemo: true })
      isOpen.value = false
    }
  } finally {
    installingDemo.value = null
  }
}

function selectType(ds: any) {
  selectedDataSource.value = ds
  step.value = 'form'
}

function backToSelect() {
  selectedDataSource.value = null
  step.value = 'select'
}

function handleConnectionSuccess(connection: any) {
  // Stash the created connection and switch to the indexing step. We do NOT
  // close the modal — the user watches indexing run, then clicks Connect.
  createdConnection.value = connection
  // Some create endpoints inline a starter `indexing` payload; otherwise
  // we fetch on first poll.
  indexingState.value = (connection?.indexing as ConnectionIndexing) || null
  step.value = 'indexing'
  startPolling()
}

async function fetchIndexing() {
  const id = createdConnection.value?.id
  if (!id) return
  try {
    const { data } = await useMyFetch(`/connections/${id}/indexing`, { method: 'GET' })
    if ((data as any).value) {
      indexingState.value = (data as any).value as ConnectionIndexing
    }
  } catch {
    // Transient — keep polling
  }
}

function startPolling() {
  stopPolling()
  // Initial fetch — if the create response didn't include indexing.
  fetchIndexing().then(() => {
    if (isIndexingActive(indexingState.value)) {
      pollTimer = setInterval(() => {
        if (!isIndexingActive(indexingState.value)) {
          stopPolling()
          return
        }
        fetchIndexing()
      }, POLL_INTERVAL_MS)
    }
  })
}

function stopPolling() {
  if (pollTimer) {
    clearInterval(pollTimer)
    pollTimer = null
  }
}

async function retryIndexing() {
  const id = createdConnection.value?.id
  if (!id || retrying.value) return
  retrying.value = true
  try {
    const { data } = await useMyFetch(`/connections/${id}/reindex`, { method: 'POST' })
    const result = (data as any).value
    if (result?.indexing) {
      indexingState.value = result.indexing as ConnectionIndexing
    }
    startPolling()
  } finally {
    retrying.value = false
  }
}

function finishConnect() {
  // The Connect button is enabled only at terminal state. Emit `created`
  // here (not on initial success) so the parent only refreshes once
  // schema is in place.
  if (createdConnection.value) {
    emit('created', createdConnection.value)
    if (indexingState.value?.status === 'completed') {
      toast.add({
        title: t('data.connectionCreated'),
        description: t('data.connectionCreatedDesc', { name: createdConnection.value?.name || t('data.connectionFallback') }),
        icon: 'i-heroicons-check-circle',
        color: 'green',
      })
    }
  }
  isOpen.value = false
}

function reset() {
  step.value = 'select'
  searchQuery.value = ''
  selectedDataSource.value = null
  createdConnection.value = null
  indexingState.value = null
  retrying.value = false
  stopPolling()
}

onBeforeUnmount(() => stopPolling())
watch(isOpen, (val) => { if (!val) stopPolling() })

// Reset state when modal opens
watch(isOpen, async (val) => {
  if (val) {
    reset()
    await fetchDataSources()

    // If initial type provided, auto-select it
    if (props.initialSelectedType) {
      const ds = dataSources.value.find((d: any) => d.type === props.initialSelectedType)
      if (ds) {
        selectType(ds)
      }
    }
  }
})
</script>
