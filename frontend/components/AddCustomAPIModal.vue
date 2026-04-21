<template>
  <UModal v-model="isOpen" :ui="{ width: 'sm:max-w-lg' }">
    <div class="p-6">
      <h2 class="text-lg font-semibold mb-4">{{ isEditMode ? 'Edit Custom API Connection' : 'Connect Custom API' }}</h2>

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
            :placeholder="isEditMode ? '(unchanged)' : 'Enter token'"
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
              :placeholder="isEditMode ? '(unchanged)' : 'Enter API key'"
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

        <!-- Custom Headers -->
        <div>
          <label class="block text-xs font-medium text-gray-700 mb-1">
            Custom Headers
            <span class="text-gray-400 font-normal">(sent with every request)</span>
          </label>
          <div class="space-y-1.5">
            <div v-for="(header, idx) in customHeaders" :key="idx" class="flex items-center gap-2">
              <input
                v-model="header.key"
                type="text"
                placeholder="Header name"
                class="flex-1 border border-gray-300 rounded-md px-2 py-1.5 text-xs focus:outline-none focus:ring-1 focus:ring-blue-500"
              />
              <input
                v-model="header.value"
                type="text"
                placeholder="Value"
                class="flex-1 border border-gray-300 rounded-md px-2 py-1.5 text-xs focus:outline-none focus:ring-1 focus:ring-blue-500"
              />
              <button type="button" @click="customHeaders.splice(idx, 1)" class="text-gray-400 hover:text-red-500 p-0.5">
                <UIcon name="heroicons-x-mark" class="w-3.5 h-3.5" />
              </button>
            </div>
          </div>
          <button
            type="button"
            @click="customHeaders.push({ key: '', value: '' })"
            class="mt-1.5 text-[11px] text-blue-600 hover:text-blue-800"
          >
            + Add header
          </button>
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
            <Spinner v-if="testing" class="w-3 h-3 inline me-1" />
            Test Connection
          </button>

          <div class="flex items-center gap-2">
            <UButton color="gray" variant="ghost" size="sm" @click="isOpen = false">Cancel</UButton>
            <UButton type="submit" color="blue" size="sm" :loading="submitting" :disabled="!form.base_url || !form.name">
              {{ isEditMode ? 'Save' : 'Connect' }}
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
const props = defineProps<{
  editConnection?: any
}>()
const emit = defineEmits(['created'])
const toast = useToast()

const isEditMode = computed(() => !!props.editConnection)

const form = reactive({
  name: '',
  base_url: '',
  auth_type: 'none',
  token: '',
  api_key: '',
  api_key_header: 'X-API-Key',
  endpoints_json: '[]',
})

const customHeaders = reactive<{ key: string; value: string }[]>([])

// Populate form when editing
watch(() => props.editConnection, async (conn) => {
  if (conn) {
    try {
      const response = await useMyFetch(`/connections/${conn.id}`, { method: 'GET' })
      const detail = response.data.value as any
      if (detail) {
        const config = detail.config || {}
        form.name = detail.name || ''
        form.base_url = config.base_url || ''
        form.auth_type = config.auth_type || (detail.has_credentials ? 'api_key' : 'none')
        form.token = ''
        form.api_key = ''
        form.api_key_header = config.api_key_header || 'X-API-Key'
        form.endpoints_json = config.endpoints ? JSON.stringify(config.endpoints, null, 2) : '[]'
        // Populate custom headers
        customHeaders.splice(0)
        const hdrs = config.headers || {}
        for (const [key, value] of Object.entries(hdrs)) {
          customHeaders.push({ key, value: String(value) })
        }
        return
      }
    } catch {}
    // Fallback
    form.name = conn.name || ''
    form.auth_type = 'none'
  }
}, { immediate: true })

// Reset form when modal closes (only for create mode)
watch(isOpen, (open) => {
  if (!open && !props.editConnection) {
    resetForm()
  }
})

function resetForm() {
  Object.assign(form, { name: '', base_url: '', auth_type: 'none', token: '', api_key: '', api_key_header: 'X-API-Key', endpoints_json: '[]' })
  customHeaders.splice(0)
  testResult.value = null
}

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
  if (form.auth_type === 'bearer' && form.token) return { token: form.token }
  if (form.auth_type === 'api_key' && form.api_key) return { api_key: form.api_key, api_key_header: form.api_key_header }
  return undefined
}

function buildHeaders(): Record<string, string> {
  const h: Record<string, string> = {}
  for (const { key, value } of customHeaders) {
    if (key.trim()) h[key.trim()] = value
  }
  return h
}

function buildConfig(): Record<string, any> {
  let endpoints: any[] = []
  try { endpoints = JSON.parse(form.endpoints_json) } catch { /* empty */ }
  return { base_url: form.base_url, auth_type: form.auth_type, headers: buildHeaders(), endpoints }
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
    const config = buildConfig()
    const credentials = buildCredentials()

    if (isEditMode.value && props.editConnection) {
      const response = await useMyFetch(`/connections/${props.editConnection.id}`, {
        method: 'PUT',
        body: { name: form.name, config, credentials },
      })
      if (response.data.value) {
        toast.add({ title: 'Connection updated', color: 'green' })
        isOpen.value = false
        emit('created', response.data.value)
      }
    } else {
      const response = await useMyFetch('/connections', {
        method: 'POST',
        body: {
          name: form.name,
          type: 'custom_api',
          config,
          credentials,
          auth_policy: 'system_only',
        },
      })
      if (response.data.value) {
        toast.add({ title: 'Custom API connected', color: 'green' })
        isOpen.value = false
        emit('created', response.data.value)
        resetForm()
      }
    }
  } catch (e: any) {
    toast.add({ title: isEditMode.value ? 'Failed to update' : 'Failed to create connection', description: e?.data?.detail, color: 'red' })
  } finally {
    submitting.value = false
  }
}
</script>
