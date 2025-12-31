<template>
  <div class="flex pl-2 md:pl-4 text-sm mx-auto md:w-1/2 md:pt-10">
    <div class="w-full px-4 pl-0 py-4">
      <div>
        <h1 class="text-lg font-semibold text-center">Add Domain</h1>
        <p class="mt-4 text-gray-500 text-center">Create a new domain from an existing connection</p>
      </div>

      <!-- Loading -->
      <div v-if="loadingConnections" class="flex flex-col items-center justify-center py-20">
        <Spinner class="h-4 w-4 text-gray-400" />
        <p class="text-sm text-gray-500 mt-2">Loading connections...</p>
      </div>

      <!-- No Connections State -->
      <div v-else-if="connections.length === 0" class="text-center py-12">
        <UIcon name="heroicons-server-stack" class="h-12 w-12 text-gray-300 mx-auto mb-3" />
        <p class="text-gray-500 mb-4">No connections available</p>
        <UButton color="primary" @click="navigateTo('/data/new')">
          Add Connection First
        </UButton>
        <div class="mt-6 text-center">
          <NuxtLink to="/data" class="text-sm text-gray-500 hover:text-gray-700">
            ← Back to Data
          </NuxtLink>
        </div>
      </div>

      <!-- Form -->
      <div v-else>
        <WizardSteps class="mt-4" current="connect" mode="domain" />

        <div class="mt-6 bg-white rounded-lg border border-gray-200 p-4">
          <!-- Domain Name (first) -->
          <div class="mb-4">
            <label class="block text-sm font-medium text-gray-700 mb-1">
              Domain Name <span class="text-red-500">*</span>
            </label>
            <UInput 
              v-model="domainName" 
              placeholder="e.g., Sales Analytics" 
              size="lg"
              :disabled="creating"
            />
          </div>

          <!-- Connection Selector (second) -->
          <div class="mb-4">
            <label class="block text-sm font-medium text-gray-700 mb-1">
              Connection <span class="text-red-500">*</span>
            </label>
            <USelectMenu
              v-model="selectedConnection"
              :options="connections"
              placeholder="Select a connection"
              size="lg"
              :disabled="creating"
              by="id"
              searchable
              searchable-placeholder="Search connections..."
            >
              <template #label>
                <div v-if="selectedConnection" class="flex items-center gap-2">
                  <DataSourceIcon :type="selectedConnection.type" class="h-4 w-4 flex-shrink-0" />
                  <span class="truncate">{{ selectedConnection.name }}</span>
                  <span class="text-xs text-gray-400 ml-1">· {{ selectedConnection.table_count || 0 }} tables</span>
                </div>
                <span v-else class="text-gray-400">Select a connection</span>
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
          </div>

          <!-- LLM Toggle -->
          <div class="flex items-center gap-2 mb-4">
            <UToggle v-model="useLlmSync" :disabled="creating" size="xs" color="blue" />
            <span class="text-xs text-gray-700">Use LLM to learn domain</span>
          </div>

          <!-- Error Message -->
          <div v-if="errorMessage" class="p-3 bg-red-50 text-red-700 rounded-lg text-sm mb-4">
            {{ errorMessage }}
          </div>

          <!-- Actions -->
          <div class="flex justify-between items-center pt-4 border-t border-gray-100">
            <NuxtLink to="/data" class="text-sm text-gray-500 hover:text-gray-700">
              ← Cancel
            </NuxtLink>
            <UButton 
              color="blue"
              size="xs" 
              :loading="creating"
              :disabled="!canSubmit"
              @click="createDomain"
            >
              Save & Continue
            </UButton>
          </div>
        </div>
      </div>
    </div>
  </div>
</template>

<script lang="ts" setup>
import Spinner from '~/components/Spinner.vue'
import WizardSteps from '@/components/datasources/WizardSteps.vue'

definePageMeta({ auth: true })

interface Connection {
  id: string
  name: string
  type: string
  table_count?: number
  domain_count?: number
}

const connections = ref<Connection[]>([])
const loadingConnections = ref(true)
const selectedConnection = ref<Connection | null>(null)
const domainName = ref('')
const useLlmSync = ref(true)
const creating = ref(false)
const errorMessage = ref('')

const canSubmit = computed(() => {
  return selectedConnection.value && domainName.value.trim().length > 0 && !creating.value
})

async function loadConnections() {
  loadingConnections.value = true
  try {
    const response = await useMyFetch('/connections', { method: 'GET' })
    if (response.data.value) {
      connections.value = response.data.value as Connection[]
      
      // Auto-select first connection if only one exists
      if (connections.value.length === 1) {
        selectedConnection.value = connections.value[0]
        domainName.value = connections.value[0].name
      }
    }
  } catch (err) {
    console.error('Failed to load connections:', err)
  } finally {
    loadingConnections.value = false
  }
}

// Watch for connection selection to auto-fill domain name (only if empty)
watch(selectedConnection, (conn) => {
  if (conn && !domainName.value.trim()) {
    domainName.value = conn.name
  }
})

async function createDomain() {
  if (!selectedConnection.value || !domainName.value.trim()) return
  
  creating.value = true
  errorMessage.value = ''
  
  try {
    const payload = {
      name: domainName.value.trim(),
      connection_id: selectedConnection.value.id,
      use_llm_sync: useLlmSync.value,
      is_public: true,
      generate_summary: false,
      generate_conversation_starters: false,
    }
    
    const response = await useMyFetch('/data_sources', {
      method: 'POST',
      body: payload,
    })
    
    if (response.error.value) {
      const errData = response.error.value.data as any
      errorMessage.value = errData?.detail || 'Failed to create domain'
      return
    }
    
    const result = response.data.value as any
    if (result?.id) {
      // Navigate to schema selection step
      navigateTo(`/domains/new/${result.id}/schema`)
    } else {
      navigateTo('/data')
    }
  } catch (err: any) {
    errorMessage.value = err?.message || 'An error occurred'
  } finally {
    creating.value = false
  }
}

onMounted(() => {
  loadConnections()
})
</script>

