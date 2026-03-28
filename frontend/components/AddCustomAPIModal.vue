<template>
  <UModal v-model="isOpen" :ui="{ width: 'sm:max-w-lg' }">
    <div class="p-6">
      <h2 class="text-lg font-semibold mb-4">Connect Custom API</h2>

      <form @submit.prevent="handleSubmit" class="space-y-4">
        <!-- Name -->
        <div>
          <label class="block text-xs font-medium text-gray-700 mb-1">Connection Name</label>
          <input
            v-model="form.name"
            type="text"
            placeholder="e.g., Internal API"
            class="w-full border border-gray-300 rounded-md px-3 py-2 text-sm focus:outline-none focus:ring-1 focus:ring-blue-500"
          />
        </div>

        <!-- Base URL -->
        <div>
          <label class="block text-xs font-medium text-gray-700 mb-1">Base URL</label>
          <input
            v-model="form.base_url"
            type="text"
            placeholder="https://api.example.com/v1"
            class="w-full border border-gray-300 rounded-md px-3 py-2 text-sm focus:outline-none focus:ring-1 focus:ring-blue-500"
          />
        </div>

        <!-- Auth -->
        <div>
          <label class="block text-xs font-medium text-gray-700 mb-1">Authentication</label>
          <select
            v-model="form.auth_type"
            class="w-full border border-gray-300 rounded-md px-3 py-2 text-sm focus:outline-none focus:ring-1 focus:ring-blue-500"
          >
            <option value="none">No Auth</option>
            <option value="bearer">Bearer Token</option>
            <option value="api_key">API Key</option>
          </select>
        </div>

        <!-- Bearer Token -->
        <div v-if="form.auth_type === 'bearer'">
          <label class="block text-xs font-medium text-gray-700 mb-1">Bearer Token</label>
          <input
            v-model="form.token"
            type="password"
            placeholder="Enter token"
            class="w-full border border-gray-300 rounded-md px-3 py-2 text-sm focus:outline-none focus:ring-1 focus:ring-blue-500"
          />
        </div>

        <!-- API Key -->
        <div v-if="form.auth_type === 'api_key'" class="space-y-3">
          <div>
            <label class="block text-xs font-medium text-gray-700 mb-1">API Key</label>
            <input
              v-model="form.api_key"
              type="password"
              placeholder="Enter API key"
              class="w-full border border-gray-300 rounded-md px-3 py-2 text-sm focus:outline-none focus:ring-1 focus:ring-blue-500"
            />
          </div>
          <div>
            <label class="block text-xs font-medium text-gray-700 mb-1">Header Name</label>
            <input
              v-model="form.api_key_header"
              type="text"
              placeholder="X-API-Key"
              class="w-full border border-gray-300 rounded-md px-3 py-2 text-sm focus:outline-none focus:ring-1 focus:ring-blue-500"
            />
          </div>
        </div>

        <!-- Endpoints (JSON) -->
        <div>
          <label class="block text-xs font-medium text-gray-700 mb-1">
            Endpoints
            <span class="text-gray-400 font-normal">(JSON array of endpoint definitions)</span>
          </label>
          <textarea
            v-model="form.endpoints_json"
            rows="8"
            placeholder='[
  {
    "name": "get_customers",
    "method": "GET",
    "path": "/customers",
    "description": "List customers",
    "parameters": [
      {"name": "status", "in": "query", "type": "string", "description": "Filter by status"}
    ]
  }
]'
            class="w-full border border-gray-300 rounded-md px-3 py-2 text-xs font-mono focus:outline-none focus:ring-1 focus:ring-blue-500"
          />
          <p v-if="endpointsError" class="text-xs text-red-500 mt-1">{{ endpointsError }}</p>
        </div>

        <!-- Test result -->
        <div v-if="testResult" :class="['text-xs px-3 py-2 rounded', testResult.success ? 'bg-green-50 text-green-700' : 'bg-red-50 text-red-700']">
          {{ testResult.message }}
        </div>

        <!-- Actions -->
        <div class="flex items-center justify-between pt-2">
          <button
            type="button"
            @click="testConnection"
            :disabled="testing || !form.base_url"
            class="text-xs text-blue-600 hover:text-blue-800 disabled:opacity-50"
          >
            <Spinner v-if="testing" class="w-3 h-3 inline mr-1" />
            Test Connection
          </button>

          <div class="flex items-center gap-2">
            <UButton color="gray" variant="ghost" size="sm" @click="isOpen = false">Cancel</UButton>
            <UButton type="submit" color="blue" size="sm" :loading="submitting" :disabled="!form.base_url || !form.name">
              Connect
            </UButton>
          </div>
        </div>
      </form>
    </div>
  </UModal>
</template>

<script setup lang="ts">
import Spinner from '~/components/Spinner.vue'

const isOpen = defineModel<boolean>({ default: false })
const emit = defineEmits(['created'])
const toast = useToast()

const form = reactive({
  name: '',
  base_url: '',
  auth_type: 'none',
  token: '',
  api_key: '',
  api_key_header: 'X-API-Key',
  endpoints_json: '[]',
})

const testing = ref(false)
const submitting = ref(false)
const testResult = ref<{ success: boolean; message: string } | null>(null)

const endpointsError = computed(() => {
  try {
    const parsed = JSON.parse(form.endpoints_json)
    if (!Array.isArray(parsed)) return 'Must be a JSON array'
    return null
  } catch {
    return 'Invalid JSON'
  }
})

function buildCredentials(): Record<string, any> | undefined {
  if (form.auth_type === 'bearer') return { token: form.token }
  if (form.auth_type === 'api_key') return { api_key: form.api_key, api_key_header: form.api_key_header }
  return undefined
}

function buildConfig(): Record<string, any> {
  let endpoints: any[] = []
  try { endpoints = JSON.parse(form.endpoints_json) } catch { /* empty */ }
  return { base_url: form.base_url, endpoints }
}

async function testConnection() {
  testing.value = true
  testResult.value = null
  try {
    const response = await useMyFetch('/connections/test-params', {
      method: 'POST',
      body: { name: 'test', type: 'custom_api', config: buildConfig(), credentials: buildCredentials() || {} },
    })
    testResult.value = response.data.value as any
  } catch (e: any) {
    testResult.value = { success: false, message: e?.data?.detail || 'Connection test failed' }
  } finally {
    testing.value = false
  }
}

async function handleSubmit() {
  if (endpointsError.value) return
  submitting.value = true
  try {
    const response = await useMyFetch('/connections', {
      method: 'POST',
      body: {
        name: form.name,
        type: 'custom_api',
        config: buildConfig(),
        credentials: buildCredentials(),
        auth_policy: 'system_only',
      },
    })

    if (response.data.value) {
      toast.add({ title: 'Custom API connected', color: 'green' })
      isOpen.value = false
      emit('created', response.data.value)
      Object.assign(form, { name: '', base_url: '', auth_type: 'none', token: '', api_key: '', api_key_header: 'X-API-Key', endpoints_json: '[]' })
      testResult.value = null
    }
  } catch (e: any) {
    toast.add({ title: 'Failed to create connection', description: e?.data?.detail, color: 'red' })
  } finally {
    submitting.value = false
  }
}
</script>
