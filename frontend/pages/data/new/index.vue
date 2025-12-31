<template>
  <div class="flex pl-2 md:pl-4 text-sm mx-auto md:w-1/2 md:pt-10">
    <div class="w-full px-4 pl-0 py-4">
      <div>
        <h1 class="text-lg font-semibold text-center">Create Domain</h1>
        <p class="mt-4 text-gray-500 text-center">Reuse an existing connection or create a new one</p>
      </div>

      <WizardSteps class="mt-4" current="connect" />

      <!-- Loading connections -->
      <div v-if="loadingConnections" class="flex flex-col items-center justify-center py-16">
        <Spinner class="h-4 w-4 text-gray-400" />
        <p class="text-sm text-gray-500 mt-2">Loading connections...</p>
      </div>

      <div v-else class="mt-6 bg-white rounded-lg border border-gray-200 p-4">
        <!-- Domain name -->
        <div class="mb-4">
          <label class="block text-sm font-medium text-gray-700 mb-1">
            Domain Name <span class="text-red-500">*</span>
          </label>
          <UInput
            v-model="domainName"
            placeholder="e.g., Sales, Marketing, Finance"
            size="lg"
            :disabled="creatingFromConnection"
          />
        </div>

        <!-- Connection selector (existing connections + New option) -->
        <div class="mb-4">
          <label class="block text-sm font-medium text-gray-700 mb-1">
            Connection <span class="text-red-500">*</span>
          </label>
          <USelectMenu
            v-model="selectedOption"
            :options="connectionOptions"
            placeholder="Select a connection"
            size="lg"
            :disabled="creatingFromConnection"
            by="id"
            searchable
            searchable-placeholder="Search connections..."
          >
            <template #label>
              <div v-if="selectedOption" class="flex items-center gap-2">
                <template v-if="selectedOption.id === '__new__'">
                  <UIcon name="heroicons-plus-circle" class="h-4 w-4 text-blue-500 flex-shrink-0" />
                  <span class="text-blue-600 font-medium">Create new connection</span>
                </template>
                <template v-else>
                  <DataSourceIcon :type="selectedOption.type" class="h-4 w-4 flex-shrink-0" />
                  <span class="truncate">{{ selectedOption.name }}</span>
                  <span class="text-xs text-gray-400 ml-1">· {{ selectedOption.table_count || 0 }} tables</span>
                </template>
              </div>
              <span v-else class="text-gray-400">Select a connection</span>
            </template>
            <template #option="{ option }">
              <div v-if="option.id === '__new__'" class="flex items-center gap-2 w-full text-blue-600">
                <UIcon name="heroicons-plus-circle" class="h-4 w-4 flex-shrink-0" />
                <span class="font-medium">Create new connection</span>
              </div>
              <div v-else class="flex items-center gap-2 w-full">
                <DataSourceIcon :type="option.type" class="h-4 w-4 flex-shrink-0" />
                <div class="flex-1 min-w-0">
                  <div class="font-medium truncate">{{ option.name }}</div>
                  <div class="text-[10px] text-gray-400">
                    {{ option.table_count || 0 }} tables · {{ option.domain_count || 0 }} domains
                  </div>
                </div>
              </div>
            </template>
          </USelectMenu>
        </div>

        <!-- New connection flow (DataSourceGrid + ConnectForm) -->
        <div v-if="isNewConnectionSelected">
          <div v-if="!selectedDataSource" class="mt-2">
            <DataSourceGrid @select="selectDataSource" :navigate-on-demo="true" />
          </div>

          <div v-else class="mt-4">
            <div class="flex items-center gap-2 mb-4">
              <button type="button" @click="backToGrid" class="text-gray-500 hover:text-gray-700">
                <UIcon name="heroicons-chevron-left" class="w-5 h-5" />
              </button>
              <DataSourceIcon :type="selectedDataSource.type" class="h-5" />
              <span class="text-sm font-medium text-gray-800">{{ selectedDataSource.title }}</span>
            </div>

            <ConnectForm
              @success="handleSuccess"
              :initialType="selectedDataSource.type"
              :initialName="domainName"
              :forceShowSystemCredentials="true"
              :showRequireUserAuthToggle="true"
              :initialRequireUserAuth="false"
              :showTestButton="true"
              :showLLMToggle="true"
              :allowNameEdit="false"
              :hideHeader="true"
              mode="create"
            />
          </div>

          <div class="mt-6 text-center">
            <NuxtLink to="/data" class="text-sm text-gray-500 hover:text-gray-700">
              ← Back to Data
            </NuxtLink>
          </div>
        </div>

        <!-- Existing connection flow -->
        <div v-else-if="selectedOption && selectedOption.id !== '__new__'">
          <div class="flex items-center gap-2 mb-4">
            <UToggle v-model="useLlmSync" :disabled="creatingFromConnection" size="xs" color="blue" />
            <span class="text-xs text-gray-700">Use LLM to learn domain</span>
          </div>

          <div v-if="errorMessage" class="p-3 bg-red-50 text-red-700 rounded-lg text-sm mb-4">
            {{ errorMessage }}
          </div>

          <div class="flex justify-between items-center pt-4 border-t border-gray-100">
            <NuxtLink to="/data" class="text-sm text-gray-500 hover:text-gray-700">
              ← Cancel
            </NuxtLink>
            <UButton
              color="blue"
              size="xs"
              :loading="creatingFromConnection"
              :disabled="!canSubmitExisting"
              @click="createDomainFromExistingConnection"
            >
              Save & Continue
            </UButton>
          </div>
        </div>

        <!-- No selection yet (just show cancel) -->
        <div v-else class="flex justify-start pt-4 border-t border-gray-100">
          <NuxtLink to="/data" class="text-sm text-gray-500 hover:text-gray-700">
            ← Cancel
          </NuxtLink>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
definePageMeta({ auth: true })
import Spinner from '~/components/Spinner.vue'
import ConnectForm from '@/components/datasources/ConnectForm.vue'
import WizardSteps from '@/components/datasources/WizardSteps.vue'
import DataSourceGrid from '@/components/datasources/DataSourceGrid.vue'

const route = useRoute()

interface Connection {
  id: string
  name: string
  type: string
  table_count?: number
  domain_count?: number
}

interface ConnectionOption extends Connection {}

const NEW_CONNECTION_OPTION: ConnectionOption = {
  id: '__new__',
  name: 'Create new connection',
  type: '',
}

const connections = ref<Connection[]>([])
const loadingConnections = ref(true)
const selectedOption = ref<ConnectionOption | undefined>(undefined)
const domainName = ref('')
const useLlmSync = ref(true)
const creatingFromConnection = ref(false)
const errorMessage = ref('')

const selectedDataSource = ref<any | null>(null)

const connectionOptions = computed<ConnectionOption[]>(() => {
  return [...connections.value, NEW_CONNECTION_OPTION]
})

const isNewConnectionSelected = computed(() => {
  return selectedOption.value?.id === '__new__'
})

function selectDataSource(ds: any) {
  selectedDataSource.value = ds
}

function backToGrid() {
  selectedDataSource.value = null
}

function handleSuccess(ds: any) {
  const id = ds?.id
  if (id) {
    navigateTo(`/data/new/${id}/schema`)
  } else {
    navigateTo('/data')
  }
}

const canSubmitExisting = computed(() => {
  return (
    selectedOption.value &&
    selectedOption.value.id !== '__new__' &&
    domainName.value.trim().length > 0 &&
    !creatingFromConnection.value
  )
})

async function loadConnections() {
  loadingConnections.value = true
  try {
    const response = await useMyFetch('/connections', { method: 'GET' })
    connections.value = (response.data.value || []) as Connection[]

    const forcedNew = String(route.query.mode || '') === 'new_connection'
    if (forcedNew) {
      selectedOption.value = NEW_CONNECTION_OPTION
    } else if (connections.value.length === 0) {
      // No connections - auto-select "New"
      selectedOption.value = NEW_CONNECTION_OPTION
    } else if (connections.value.length === 1) {
      // Single connection - auto-select it
      selectedOption.value = connections.value[0]
    }
  } catch (err) {
    console.error('Failed to load connections:', err)
    selectedOption.value = NEW_CONNECTION_OPTION
  } finally {
    loadingConnections.value = false
  }
}

watch(selectedOption, (opt) => {
  // Reset data source selection when switching away from "New"
  if (opt?.id !== '__new__') {
    selectedDataSource.value = null
  }
})

async function createDomainFromExistingConnection() {
  if (!selectedOption.value || selectedOption.value.id === '__new__' || !domainName.value.trim()) return
  creatingFromConnection.value = true
  errorMessage.value = ''

  try {
    const payload = {
      name: domainName.value.trim(),
      connection_id: selectedOption.value.id,
      use_llm_sync: useLlmSync.value,
      is_public: true,
      generate_summary: false,
      generate_conversation_starters: false,
      generate_ai_rules: false,
    }

    const response = await useMyFetch('/data_sources', {
      method: 'POST',
      body: payload,
    })

    if (response.error.value) {
      const errData = (response.error.value as any).data as any
      errorMessage.value = errData?.detail || 'Failed to create domain'
      return
    }

    const result = response.data.value as any
    if (result?.id) {
      navigateTo(`/data/new/${result.id}/schema`)
    } else {
      navigateTo('/data')
    }
  } catch (err: any) {
    errorMessage.value = err?.message || 'An error occurred'
  } finally {
    creatingFromConnection.value = false
  }
}

onMounted(async () => {
  await loadConnections()

  // Check if type was passed via query param (for backward compatibility)
  const typeParam = route.query.type as string
  if (typeParam) {
    // Fetch available data sources to find the matching type
    const response = await useMyFetch('/available_data_sources', { method: 'GET' })
    if (response.data.value) {
      const availableDs = response.data.value as any[]
      const matchingDs = availableDs.find((ds: any) => ds.type === typeParam)
      if (matchingDs) {
        selectedOption.value = NEW_CONNECTION_OPTION
        selectedDataSource.value = matchingDs
      }
    }
  }
})
</script>
