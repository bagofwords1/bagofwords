<template>
  <div class="flex pl-2 md:pl-4 text-sm mx-auto md:w-1/2 md:pt-10">
    <div class="w-full px-4 pl-0 py-4">
      <div>
        <h1 class="text-lg font-semibold text-center">New Data</h1>
        <p class="mt-2 text-gray-500 text-center">Connect a data source and define its business context for querying        </p>
      </div>

      <WizardSteps class="mt-7" current="connect" />

      <!-- Loading connections -->
      <div v-if="loadingConnections" class="flex flex-col items-center justify-center py-16">
        <Spinner class="h-4 w-4 text-gray-400" />
        <p class="text-sm text-gray-500 mt-2">Loading connections...</p>
      </div>

      <div v-else class="mt-6 bg-white rounded-lg border border-gray-200 p-4">
        <!-- Domain name -->
        <div class="mb-4">
          <label class="block text-sm font-medium text-gray-700 mb-1">
            Name <span class="text-red-500">*</span>
          </label>
          <UInput
            v-model="domainName"
            placeholder="e.g., Sales, Marketing, Finance"
            size="lg"
            :disabled="creatingFromConnection"
          />
        </div>

        <!-- Connection selector (multi-select for existing connections) -->
        <div class="mb-4">
          <label class="block text-sm font-medium text-gray-700 mb-1">
            Connections <span class="text-red-500">*</span>
          </label>
          <USelectMenu
            v-model="selectedConnections"
            :options="connections"
            placeholder="Select connections"
            size="lg"
            :disabled="creatingFromConnection"
            by="id"
            multiple
            searchable
            searchable-placeholder="Search connections..."
          >
            <template #label>
              <div v-if="selectedConnections.length > 0" class="flex items-center gap-1.5 flex-wrap">
                <template v-for="conn in selectedConnections" :key="conn.id">
                  <div class="flex items-center gap-1 bg-gray-100 rounded px-1.5 py-0.5">
                    <DataSourceIcon :type="conn.type" class="h-3.5 w-3.5 flex-shrink-0" />
                    <span class="text-xs truncate max-w-[100px]">{{ conn.name }}</span>
                  </div>
                </template>
              </div>
              <span v-else class="text-gray-400">Select connections</span>
            </template>
            <template #option="{ option }">
              <div class="flex items-center gap-2 w-full">
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
          <button
            type="button"
            class="mt-2 inline-flex items-center gap-1.5 text-xs text-blue-600 hover:text-blue-700"
            :disabled="creatingFromConnection"
            @click="handleCreateNewConnection"
          >
            <UIcon name="heroicons-plus-circle" class="h-3.5 w-3.5" />
            <span>Create new connection</span>
          </button>
        </div>

        <!-- New connection flow (DataSourceGrid + ConnectForm) -->
        <div v-if="showNewConnectionFlow">
          <div v-if="!selectedDataSource" class="mt-2">
            <div class="flex items-center gap-2 mb-4">
              <button type="button" @click="showNewConnectionFlow = false" class="text-gray-500 hover:text-gray-700">
                <UIcon name="heroicons-chevron-left" class="w-5 h-5" />
              </button>
              <span class="text-sm font-medium text-gray-800">Select connection type</span>
            </div>
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
              @success="handleNewConnectionSuccess"
              :initialType="selectedDataSource.type"
              :forceShowSystemCredentials="true"
              :showRequireUserAuthToggle="true"
              :initialRequireUserAuth="false"
              :showTestButton="true"
              :showLLMToggle="false"
              :hideHeader="true"
              mode="create_connection_only"
            />
          </div>

          <div class="mt-6 text-center">
            <button type="button" @click="showNewConnectionFlow = false" class="text-sm text-gray-500 hover:text-gray-700">
              ← Back
            </button>
          </div>
        </div>

        <!-- Existing connection flow (main form) -->
        <div v-else-if="selectedConnections.length > 0">
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

const connections = ref<Connection[]>([])
const loadingConnections = ref(true)
const selectedConnections = ref<Connection[]>([])
const domainName = ref('')
const useLlmSync = ref(true)
const creatingFromConnection = ref(false)
const errorMessage = ref('')
const showNewConnectionFlow = ref(false)

const selectedDataSource = ref<any | null>(null)

const existingConnections = computed<Connection[]>(() => {
  return connections.value
})

function handleCreateNewConnection() {
  showNewConnectionFlow.value = true
  selectedDataSource.value = null
}

function selectDataSource(ds: any) {
  selectedDataSource.value = ds
}

function backToGrid() {
  selectedDataSource.value = null
}

async function handleNewConnectionSuccess(connectionData: any) {
  // Add the new connection to selections and refresh list
  await loadConnections()

  // Find and select the newly created connection
  if (connectionData?.id) {
    const newConn = connections.value.find(c => c.id === connectionData.id)
    if (newConn && !selectedConnections.value.some(c => c.id === newConn.id)) {
      selectedConnections.value = [...selectedConnections.value, newConn]
    }
  }

  // Exit new connection flow
  showNewConnectionFlow.value = false
  selectedDataSource.value = null
}

const canSubmitExisting = computed(() => {
  return (
    selectedConnections.value.length > 0 &&
    domainName.value.trim().length > 0 &&
    !creatingFromConnection.value
  )
})

async function loadConnections() {
  loadingConnections.value = true
  try {
    const response = await useMyFetch('/connections', { method: 'GET' })
    connections.value = (response.data.value || []) as Connection[]
  } catch (err) {
    console.error('Failed to load connections:', err)
  } finally {
    loadingConnections.value = false
  }
}

async function createDomainFromExistingConnection() {
  if (selectedConnections.value.length === 0 || !domainName.value.trim()) return
  creatingFromConnection.value = true
  errorMessage.value = ''

  try {
    const payload = {
      name: domainName.value.trim(),
      connection_ids: selectedConnections.value.map(c => c.id),
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

  const forcedNew = String(route.query.mode || '') === 'new_connection'
  if (forcedNew || connections.value.length === 0) {
    showNewConnectionFlow.value = true
  } else if (connections.value.length === 1) {
    // Single connection - auto-select it
    selectedConnections.value = [connections.value[0]]
  }

  // Check if type was passed via query param (for backward compatibility)
  const typeParam = route.query.type as string
  if (typeParam) {
    // Fetch available data sources to find the matching type
    const response = await useMyFetch('/available_data_sources', { method: 'GET' })
    if (response.data.value) {
      const availableDs = response.data.value as any[]
      const matchingDs = availableDs.find((ds: any) => ds.type === typeParam)
      if (matchingDs) {
        showNewConnectionFlow.value = true
        selectedDataSource.value = matchingDs
      }
    }
  }
})
</script>
