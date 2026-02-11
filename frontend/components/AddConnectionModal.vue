<template>
  <UModal v-model="isOpen" :ui="{ width: 'sm:max-w-xl' }">
    <div class="p-5">
      <!-- Step 1: Select data source type -->
      <div v-if="step === 'select'">
        <div class="flex items-center justify-between mb-4">
          <h3 class="text-lg font-semibold">Add Connection</h3>
          <button @click="isOpen = false" class="text-gray-400 hover:text-gray-600">
            <UIcon name="heroicons-x-mark" class="w-5 h-5" />
          </button>
        </div>
        <p class="text-sm text-gray-500 mb-4">Select a data source type to connect.</p>

        <!-- Demo data sources at the top -->
        <div v-if="uninstalledDemos.length > 0" class="mb-4">
          <div class="text-xs text-gray-400 mb-2">Try a sample database:</div>
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
              <span class="text-[9px] font-medium uppercase tracking-wide text-purple-600 bg-purple-100 px-1.5 py-0.5 rounded">sample</span>
            </button>
          </div>
        </div>

        <!-- Search input -->
        <div class="mb-4">
          <UInput
            v-model="searchQuery"
            placeholder="Search data sources..."
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
                <div v-if="isLocked(ds)" class="absolute -top-1 -right-1">
                  <svg class="h-3 w-3 text-gray-400" fill="currentColor" viewBox="0 0 20 20">
                    <path fill-rule="evenodd" d="M5 9V7a5 5 0 0110 0v2a2 2 0 012 2v5a2 2 0 01-2 2H5a2 2 0 01-2-2v-5a2 2 0 012-2zm8-2v2H7V7a3 3 0 016 0z" clip-rule="evenodd" />
                  </svg>
                </div>
              </div>
              <div class="text-xs text-gray-500 mt-1">{{ ds.title }}</div>
              <div v-if="isLocked(ds)" class="mt-1">
                <span class="text-[9px] font-medium uppercase tracking-wide text-purple-600 bg-purple-100 px-1.5 py-0.5 rounded">
                  Enterprise
                </span>
              </div>
            </div>
          </button>
        </div>

        <!-- No results -->
        <div v-if="!loadingDataSources && filteredDataSources.length === 0" class="text-center py-8 text-gray-500 text-sm">
          No data sources found matching "{{ searchQuery }}"
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
          <button @click="isOpen = false" class="ml-auto text-gray-400 hover:text-gray-600">
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

    </div>
  </UModal>
</template>

<script setup lang="ts">
import Spinner from '~/components/Spinner.vue'
import ConnectForm from '~/components/datasources/ConnectForm.vue'
import { useEnterprise } from '~/ee/composables/useEnterprise'

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
const toast = useToast()

// State
const step = ref<'select' | 'form'>('select')
const searchQuery = ref('')
const dataSources = ref<any[]>([])
const demos = ref<any[]>([])
const loadingDataSources = ref(true)
const selectedDataSource = ref<any>(null)
const installingDemo = ref<string | null>(null)

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
      const demoName = demos.value.find(d => d.id === demoId)?.name || 'Sample data'
      toast.add({
        title: 'Sample data added',
        description: `${demoName} has been added successfully.`,
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
  emit('created', connection)

  toast.add({
    title: 'Connection created',
    description: `${connection?.name || 'Connection'} has been connected successfully.`,
    icon: 'i-heroicons-check-circle',
    color: 'green'
  })

  isOpen.value = false
}

function reset() {
  step.value = 'select'
  searchQuery.value = ''
  selectedDataSource.value = null
}

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
