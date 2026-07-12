<template>
  <div>
    <!-- Use existing connection (create mode only) -->
    <div v-if="!isEditMode && existingConnections.length > 0" class="mb-4">
      <label class="block text-xs font-medium text-gray-700 dark:text-gray-300 mb-1">Use existing connection</label>
      <USelectMenu
        v-model="selectedExisting"
        :options="existingConnectionOptions"
        option-attribute="name"
        placeholder="Select existing…"
        size="sm"
        class="w-full"
      />
      <div v-if="!selectedExistingId" class="relative my-4">
        <div class="absolute inset-0 flex items-center"><div class="w-full border-t border-gray-200 dark:border-gray-800" /></div>
        <div class="relative flex justify-center"><span class="bg-white dark:bg-gray-900 px-2 text-xs text-gray-400 dark:text-gray-500">— or create new —</span></div>
      </div>
    </div>

    <form @submit.prevent="handleSubmit" class="space-y-4">
      <template v-if="!selectedExistingId">
        <div>
          <label class="block text-xs font-medium text-gray-700 dark:text-gray-300 mb-1">Connection Name</label>
          <input v-model="form.name" type="text" placeholder="e.g., Internal API" class="w-full border border-gray-300 dark:border-gray-700 dark:bg-gray-800 dark:text-gray-100 dark:placeholder-gray-500 rounded-md px-3 py-2 text-sm focus:outline-none focus:ring-1 focus:ring-blue-500" />
        </div>

        <div>
          <label class="block text-xs font-medium text-gray-700 dark:text-gray-300 mb-1">Base URL</label>
          <input v-model="form.base_url" type="text" placeholder="https://api.example.com/v1" class="w-full border border-gray-300 dark:border-gray-700 dark:bg-gray-800 dark:text-gray-100 dark:placeholder-gray-500 rounded-md px-3 py-2 text-sm focus:outline-none focus:ring-1 focus:ring-blue-500" />
        </div>

        <div>
          <label class="block text-xs font-medium text-gray-700 dark:text-gray-300 mb-1">Authentication</label>
          <select v-model="form.auth_type" class="w-full border border-gray-300 dark:border-gray-700 dark:bg-gray-800 dark:text-gray-100 dark:placeholder-gray-500 rounded-md px-3 py-2 text-sm focus:outline-none focus:ring-1 focus:ring-blue-500">
            <option value="none">No Auth</option>
            <option value="bearer">Bearer Token</option>
            <option value="api_key">API Key</option>
            <option value="oauth_app">OAuth (per-user sign-in)</option>
          </select>
        </div>

        <!-- OAuth app: admin registers the client; each user signs in and their
             access token is sent as Bearer on every call. Endpoints/base URL and
             the OAuth constants are pre-filled by the preset (e.g. X Write). -->
        <div v-if="form.auth_type === 'oauth_app'" class="space-y-3">
          <div class="grid grid-cols-2 gap-3">
            <div>
              <label class="block text-xs font-medium text-gray-700 dark:text-gray-300 mb-1">Client ID</label>
              <input v-model="form.client_id" type="text" placeholder="OAuth client ID" class="w-full border border-gray-300 dark:border-gray-700 dark:bg-gray-800 dark:text-gray-100 dark:placeholder-gray-500 rounded-md px-3 py-2 text-sm focus:outline-none focus:ring-1 focus:ring-blue-500" />
            </div>
            <div>
              <label class="block text-xs font-medium text-gray-700 dark:text-gray-300 mb-1">Client Secret</label>
              <input v-model="form.client_secret" type="password" :placeholder="isEditMode ? '(unchanged)' : 'OAuth client secret'" class="w-full border border-gray-300 dark:border-gray-700 dark:bg-gray-800 dark:text-gray-100 dark:placeholder-gray-500 rounded-md px-3 py-2 text-sm focus:outline-none focus:ring-1 focus:ring-blue-500" />
            </div>
          </div>
          <details class="text-xs">
            <summary class="cursor-pointer text-gray-500 dark:text-gray-400">Advanced OAuth settings</summary>
            <div class="mt-2 space-y-2">
              <div>
                <label class="block text-[11px] font-medium text-gray-600 dark:text-gray-400 mb-1">Authorize URL</label>
                <input v-model="form.authorize_url" type="text" class="w-full border border-gray-300 dark:border-gray-700 dark:bg-gray-800 dark:text-gray-100 rounded-md px-3 py-1.5 text-xs focus:outline-none focus:ring-1 focus:ring-blue-500" />
              </div>
              <div>
                <label class="block text-[11px] font-medium text-gray-600 dark:text-gray-400 mb-1">Token URL</label>
                <input v-model="form.token_url" type="text" class="w-full border border-gray-300 dark:border-gray-700 dark:bg-gray-800 dark:text-gray-100 rounded-md px-3 py-1.5 text-xs focus:outline-none focus:ring-1 focus:ring-blue-500" />
              </div>
              <div>
                <label class="block text-[11px] font-medium text-gray-600 dark:text-gray-400 mb-1">Scopes</label>
                <input v-model="form.scopes" type="text" class="w-full border border-gray-300 dark:border-gray-700 dark:bg-gray-800 dark:text-gray-100 rounded-md px-3 py-1.5 text-xs focus:outline-none focus:ring-1 focus:ring-blue-500" />
              </div>
            </div>
          </details>
        </div>

        <div v-if="form.auth_type === 'bearer'">
          <label class="block text-xs font-medium text-gray-700 dark:text-gray-300 mb-1">Bearer Token</label>
          <input v-model="form.token" type="password" :placeholder="isEditMode ? '(unchanged)' : 'Enter token'" class="w-full border border-gray-300 dark:border-gray-700 dark:bg-gray-800 dark:text-gray-100 dark:placeholder-gray-500 rounded-md px-3 py-2 text-sm focus:outline-none focus:ring-1 focus:ring-blue-500" />
        </div>

        <div v-if="form.auth_type === 'api_key'" class="space-y-3">
          <div>
            <label class="block text-xs font-medium text-gray-700 dark:text-gray-300 mb-1">API Key</label>
            <input v-model="form.api_key" type="password" :placeholder="isEditMode ? '(unchanged)' : 'Enter API key'" class="w-full border border-gray-300 dark:border-gray-700 dark:bg-gray-800 dark:text-gray-100 dark:placeholder-gray-500 rounded-md px-3 py-2 text-sm focus:outline-none focus:ring-1 focus:ring-blue-500" />
          </div>
          <div>
            <label class="block text-xs font-medium text-gray-700 dark:text-gray-300 mb-1">Header Name</label>
            <input v-model="form.api_key_header" type="text" placeholder="X-API-Key" class="w-full border border-gray-300 dark:border-gray-700 dark:bg-gray-800 dark:text-gray-100 dark:placeholder-gray-500 rounded-md px-3 py-2 text-sm focus:outline-none focus:ring-1 focus:ring-blue-500" />
          </div>
        </div>

        <div>
          <label class="block text-xs font-medium text-gray-700 dark:text-gray-300 mb-1">
            Custom Headers
            <span class="text-gray-400 dark:text-gray-500 font-normal">(sent with every request)</span>
          </label>
          <div class="space-y-1.5">
            <div v-for="(header, idx) in customHeaders" :key="idx" class="flex items-center gap-2">
              <input v-model="header.key" type="text" placeholder="Header name" class="flex-1 border border-gray-300 dark:border-gray-700 dark:bg-gray-800 dark:text-gray-100 dark:placeholder-gray-500 rounded-md px-2 py-1.5 text-xs focus:outline-none focus:ring-1 focus:ring-blue-500" />
              <input v-model="header.value" type="text" placeholder="Value" class="flex-1 border border-gray-300 dark:border-gray-700 dark:bg-gray-800 dark:text-gray-100 dark:placeholder-gray-500 rounded-md px-2 py-1.5 text-xs focus:outline-none focus:ring-1 focus:ring-blue-500" />
              <button type="button" @click="customHeaders.splice(idx, 1)" class="text-gray-400 dark:text-gray-500 hover:text-red-500 p-0.5">
                <UIcon name="heroicons-x-mark" class="w-3.5 h-3.5" />
              </button>
            </div>
          </div>
          <button type="button" @click="customHeaders.push({ key: '', value: '' })" class="mt-1.5 text-[11px] text-blue-600 hover:text-blue-800">
            + Add header
          </button>
        </div>

        <div>
          <div class="flex items-center justify-between mb-1">
            <label class="block text-xs font-medium text-gray-700 dark:text-gray-300">
              Endpoints
              <span class="text-gray-400 dark:text-gray-500 font-normal">(operations exposed as tools)</span>
            </label>
            <button type="button" @click="toggleRawMode" class="text-[11px] text-blue-600 hover:text-blue-800">
              {{ rawMode ? 'Visual editor' : 'Advanced (raw JSON)' }}
            </button>
          </div>

          <!-- Visual builder -->
          <div v-if="!rawMode" class="space-y-3">
            <div v-for="(ep, epIdx) in endpoints" :key="epIdx" class="border border-gray-200 dark:border-gray-800 rounded-md p-3 space-y-2">
              <div class="flex items-center gap-2">
                <select v-model="ep.method" class="border border-gray-300 dark:border-gray-700 dark:bg-gray-800 dark:text-gray-100 dark:placeholder-gray-500 rounded-md px-2 py-1.5 text-xs focus:outline-none focus:ring-1 focus:ring-blue-500">
                  <option v-for="m in methodOptions" :key="m" :value="m">{{ m }}</option>
                </select>
                <input v-model="ep.name" type="text" placeholder="tool_name" class="flex-1 border border-gray-300 dark:border-gray-700 dark:bg-gray-800 dark:text-gray-100 dark:placeholder-gray-500 rounded-md px-2 py-1.5 text-xs focus:outline-none focus:ring-1 focus:ring-blue-500" />
                <button type="button" @click="endpoints.splice(epIdx, 1)" class="text-gray-400 dark:text-gray-500 hover:text-red-500 p-0.5" title="Remove endpoint">
                  <UIcon name="heroicons-x-mark" class="w-3.5 h-3.5" />
                </button>
              </div>
              <input v-model="ep.path" type="text" placeholder="/customers/{id}" class="w-full border border-gray-300 dark:border-gray-700 dark:bg-gray-800 dark:text-gray-100 dark:placeholder-gray-500 rounded-md px-2 py-1.5 text-xs font-mono focus:outline-none focus:ring-1 focus:ring-blue-500" />
              <input v-model="ep.description" type="text" placeholder="Description (helps the agent decide when to use this)" class="w-full border border-gray-300 dark:border-gray-700 dark:bg-gray-800 dark:text-gray-100 dark:placeholder-gray-500 rounded-md px-2 py-1.5 text-xs focus:outline-none focus:ring-1 focus:ring-blue-500" />

              <div>
                <div class="text-[11px] font-medium text-gray-500 dark:text-gray-400 mb-1">Parameters</div>
                <div v-if="ep.parameters.length" class="space-y-1.5">
                  <div v-for="(p, pIdx) in ep.parameters" :key="pIdx" class="flex items-center gap-1.5">
                    <input v-model="p.name" type="text" placeholder="name" class="flex-1 border border-gray-300 dark:border-gray-700 dark:bg-gray-800 dark:text-gray-100 dark:placeholder-gray-500 rounded-md px-2 py-1 text-xs focus:outline-none focus:ring-1 focus:ring-blue-500" />
                    <select v-model="p.in" class="border border-gray-300 dark:border-gray-700 dark:bg-gray-800 dark:text-gray-100 dark:placeholder-gray-500 rounded-md px-1.5 py-1 text-xs focus:outline-none focus:ring-1 focus:ring-blue-500">
                      <option v-for="loc in inOptions" :key="loc" :value="loc">{{ loc }}</option>
                    </select>
                    <select v-model="p.type" class="border border-gray-300 dark:border-gray-700 dark:bg-gray-800 dark:text-gray-100 dark:placeholder-gray-500 rounded-md px-1.5 py-1 text-xs focus:outline-none focus:ring-1 focus:ring-blue-500">
                      <option v-for="t in typeOptions" :key="t" :value="t">{{ t }}</option>
                    </select>
                    <label class="flex items-center gap-1 text-[11px] text-gray-500 dark:text-gray-400 whitespace-nowrap">
                      <input type="checkbox" v-model="p.required" /> req
                    </label>
                    <button type="button" @click="ep.parameters.splice(pIdx, 1)" class="text-gray-400 dark:text-gray-500 hover:text-red-500 p-0.5" title="Remove parameter">
                      <UIcon name="heroicons-x-mark" class="w-3.5 h-3.5" />
                    </button>
                  </div>
                </div>
                <button type="button" @click="ep.parameters.push(newParam())" class="mt-1 text-[11px] text-blue-600 hover:text-blue-800">
                  + Add parameter
                </button>
              </div>
            </div>
            <button type="button" @click="endpoints.push(newEndpoint())" class="text-xs text-blue-600 hover:text-blue-800">
              + Add endpoint
            </button>
            <p v-if="endpointsError" class="text-xs text-red-500 mt-1">{{ endpointsError }}</p>
          </div>

          <!-- Raw JSON (advanced) -->
          <div v-else>
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
              class="w-full border border-gray-300 dark:border-gray-700 dark:bg-gray-800 dark:text-gray-100 dark:placeholder-gray-500 rounded-md px-3 py-2 text-xs font-mono focus:outline-none focus:ring-1 focus:ring-blue-500"
            />
            <p v-if="endpointsError" class="text-xs text-red-500 mt-1">{{ endpointsError }}</p>
          </div>
        </div>

        <div v-if="testResult" :class="['text-xs px-3 py-2 rounded', testResult.success ? 'bg-green-50 text-green-700' : 'bg-red-50 text-red-700']">
          {{ testResult.message }}
        </div>

        <div v-if="submitError" class="text-xs px-3 py-2 rounded bg-red-50 dark:bg-red-950 text-red-700">
          {{ submitError }}
        </div>
      </template>

      <div class="flex items-center justify-between pt-2">
        <button v-if="!selectedExistingId" type="button" @click="testConnection" :disabled="testing || !form.base_url" class="text-xs text-blue-600 hover:text-blue-800 disabled:opacity-50">
          <Spinner v-if="testing" class="w-3 h-3 inline me-1" />
          Test Connection
        </button>
        <span v-else />
        <div class="flex items-center gap-2">
          <UButton color="gray" variant="ghost" size="sm" @click="emit('cancel')">Cancel</UButton>
          <UButton type="submit" color="blue" size="sm" :loading="submitting" :disabled="selectedExistingId ? false : (!form.base_url || !form.name)">
            {{ isEditMode ? 'Save' : 'Connect' }}
          </UButton>
        </div>
      </div>
    </form>
  </div>
</template>

<script setup lang="ts">
import Spinner from '~/components/Spinner.vue'

const props = defineProps<{
  editConnection?: any
  existingConnections?: any[]
  // Prefill from a Custom API preset (e.g. X Write): base_url + endpoints +
  // OAuth defaults. The admin only supplies client id/secret.
  prefill?: {
    name?: string; base_url?: string; auth?: string
    headers?: Record<string, string>
    endpoints?: any[]
    oauth_defaults?: { authorize_url?: string; token_url?: string; scopes?: string; audience?: string; token_endpoint_auth_method?: string } | null
    description?: string
  } | null
}>()
const emit = defineEmits<{
  (e: 'saved', connection: any): void
  (e: 'cancel'): void
}>()

const selectedExisting = ref<any>(null)
const existingConnections = computed(() => props.existingConnections || [])
const existingConnectionOptions = computed(() =>
  existingConnections.value.map((c: any) => ({ id: c.id, name: c.name }))
)
const selectedExistingId = computed(() => selectedExisting.value?.id || '')
const isEditMode = computed(() => !!props.editConnection)

const form = reactive({
  name: '',
  base_url: '',
  auth_type: 'none',
  token: '',
  api_key: '',
  api_key_header: 'X-API-Key',
  endpoints_json: '[]',
  // OAuth app fields (auth_type === 'oauth_app')
  client_id: '',
  client_secret: '',
  authorize_url: '',
  token_url: '',
  scopes: '',
  token_endpoint_auth_method: '',
})

const customHeaders = reactive<{ key: string; value: string }[]>([])

// --- Endpoints: visual builder + synced raw-JSON view ---
type EndpointParam = { name: string; in: string; type: string; description: string; required: boolean }
type Endpoint = { name: string; method: string; path: string; description: string; parameters: EndpointParam[] }

const methodOptions = ['GET', 'POST', 'PUT', 'PATCH', 'DELETE']
const inOptions = ['query', 'path', 'body']
const typeOptions = ['string', 'number', 'integer', 'boolean']

const endpoints = reactive<Endpoint[]>([])
const rawMode = ref(false)

function newParam(): EndpointParam {
  return { name: '', in: 'query', type: 'string', description: '', required: false }
}
function newEndpoint(): Endpoint {
  return { name: '', method: 'GET', path: '', description: '', parameters: [] }
}

function setEndpoints(arr: Endpoint[]) {
  endpoints.splice(0, endpoints.length, ...arr)
}

// Coerce arbitrary parsed JSON into the builder shape so round-tripping is safe.
function normalizeEndpoints(arr: any[]): Endpoint[] {
  return arr.map((ep: any) => ({
    name: ep?.name || '',
    method: String(ep?.method || 'GET').toUpperCase(),
    path: ep?.path || '',
    description: ep?.description || '',
    parameters: Array.isArray(ep?.parameters)
      ? ep.parameters.map((p: any) => ({
          name: p?.name || '',
          in: p?.in || 'query',
          type: p?.type || 'string',
          description: p?.description || '',
          required: !!p?.required,
        }))
      : [],
  }))
}

// Serialize builder state into the backend endpoint shape, trimming empties.
function serializeEndpoints(): any[] {
  return endpoints.map((ep) => {
    const out: any = { name: ep.name.trim(), method: ep.method, path: ep.path.trim() }
    if (ep.description.trim()) out.description = ep.description.trim()
    const params = ep.parameters
      .filter((p) => p.name.trim())
      .map((p) => {
        const po: any = { name: p.name.trim(), in: p.in, type: p.type }
        if (p.description.trim()) po.description = p.description.trim()
        if (p.required) po.required = true
        return po
      })
    if (params.length) out.parameters = params
    return out
  })
}

function toggleRawMode() {
  if (rawMode.value) {
    // Raw -> visual: only switch if the JSON is a valid array.
    try {
      const parsed = JSON.parse(form.endpoints_json)
      if (!Array.isArray(parsed)) return
      setEndpoints(normalizeEndpoints(parsed))
      rawMode.value = false
    } catch {
      // Stay in raw; endpointsError surfaces the problem.
    }
  } else {
    // Visual -> raw: serialize current builder state.
    form.endpoints_json = JSON.stringify(serializeEndpoints(), null, 2)
    rawMode.value = true
  }
}

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
        setEndpoints(Array.isArray(config.endpoints) ? normalizeEndpoints(config.endpoints) : [])
        form.endpoints_json = config.endpoints ? JSON.stringify(config.endpoints, null, 2) : '[]'
        // OAuth app meta (non-secret) round-trips via credentials_meta.
        const meta = detail.credentials_meta || {}
        form.client_id = meta.client_id || ''
        form.client_secret = ''
        form.authorize_url = meta.authorize_url || ''
        form.token_url = meta.token_url || ''
        form.scopes = meta.scopes || ''
        form.token_endpoint_auth_method = meta.token_endpoint_auth_method || ''
        customHeaders.splice(0)
        const hdrs = config.headers || {}
        for (const [key, value] of Object.entries(hdrs)) {
          customHeaders.push({ key, value: String(value) })
        }
        return
      }
    } catch {}
    form.name = conn.name || ''
    form.auth_type = 'none'
  }
}, { immediate: true })

// Apply preset prefill in create mode (X Write etc). Runs immediately so the
// form opens populated; the admin only adds client id/secret.
watch(() => props.prefill, (p) => {
  if (!p || props.editConnection) return
  if (p.name && !form.name) form.name = p.name
  if (p.base_url) form.base_url = p.base_url
  if (p.auth) form.auth_type = p.auth
  if (Array.isArray(p.endpoints)) {
    setEndpoints(normalizeEndpoints(p.endpoints))
    form.endpoints_json = JSON.stringify(p.endpoints, null, 2)
  }
  if (p.headers) {
    customHeaders.splice(0)
    for (const [key, value] of Object.entries(p.headers)) customHeaders.push({ key, value: String(value) })
  }
  const d = p.oauth_defaults
  if (d) {
    if (d.authorize_url) form.authorize_url = d.authorize_url
    if (d.token_url) form.token_url = d.token_url
    if (d.scopes) form.scopes = d.scopes
    if (d.token_endpoint_auth_method) form.token_endpoint_auth_method = d.token_endpoint_auth_method
  }
}, { immediate: true })

const testing = ref(false)
const submitting = ref(false)
const testResult = ref<{ success: boolean; message: string } | null>(null)
const submitError = ref<string | null>(null)

// Extract a human-readable message from a useMyFetch error (FetchError) or a
// thrown exception. `detail` is what FastAPI returns for HTTPException.
function errorMessage(err: any, fallback: string): string {
  return err?.data?.detail || err?.message || fallback
}

// Clear the inline submit error as soon as the user edits any field.
watch(() => ({ ...form }), () => { submitError.value = null })

const endpointsError = computed(() => {
  if (rawMode.value) {
    try {
      const parsed = JSON.parse(form.endpoints_json)
      if (!Array.isArray(parsed)) return 'Must be a JSON array'
      return null
    } catch {
      return 'Invalid JSON'
    }
  }
  // Visual builder validation.
  for (const ep of endpoints) {
    if (!ep.name.trim()) return 'Every endpoint needs a name'
    if (!ep.path.trim()) return `Endpoint "${ep.name.trim()}" needs a path`
  }
  const names = endpoints.map((e) => e.name.trim())
  if (new Set(names).size !== names.length) return 'Endpoint names must be unique'
  return null
})

function buildCredentials(): Record<string, any> | undefined {
  if (form.auth_type === 'bearer' && form.token) return { token: form.token }
  if (form.auth_type === 'api_key' && form.api_key) return { api_key: form.api_key, api_key_header: form.api_key_header }
  if (form.auth_type === 'oauth_app') {
    // Send only non-empty fields; on edit, a blank client_secret is preserved
    // server-side (write-only placeholder).
    const c: Record<string, any> = {}
    if (form.authorize_url) c.authorize_url = form.authorize_url
    if (form.token_url) c.token_url = form.token_url
    if (form.client_id) c.client_id = form.client_id
    if (form.client_secret) c.client_secret = form.client_secret
    if (form.scopes) c.scopes = form.scopes
    if (form.token_endpoint_auth_method) c.token_endpoint_auth_method = form.token_endpoint_auth_method
    return Object.keys(c).length ? c : undefined
  }
  return undefined
}

function buildHeaders(): Record<string, string> {
  const h: Record<string, string> = {}
  for (const { key, value } of customHeaders) {
    if (key.trim()) h[key.trim()] = value
  }
  return h
}

function buildEndpoints(): any[] {
  if (rawMode.value) {
    try { return JSON.parse(form.endpoints_json) } catch { return [] }
  }
  return serializeEndpoints()
}

function buildConfig(): Record<string, any> {
  return { base_url: form.base_url, auth_type: form.auth_type, headers: buildHeaders(), endpoints: buildEndpoints() }
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
  if (selectedExisting.value) {
    const conn = existingConnections.value.find((c: any) => c.id === selectedExisting.value.id)
    if (conn) emit('saved', conn)
    return
  }

  if (endpointsError.value) return
  submitting.value = true
  submitError.value = null
  const fallback = isEditMode.value ? 'Failed to update connection' : 'Failed to create connection'
  try {
    const config = buildConfig()
    const credentials = buildCredentials()

    let response: any
    if (isEditMode.value && props.editConnection) {
      response = await useMyFetch(`/connections/${props.editConnection.id}`, {
        method: 'PUT',
        body: { name: form.name, config, credentials },
      })
    } else {
      // OAuth-app connections authenticate per user: each user signs in and
      // their token runs the calls. Everything else uses the shared system creds.
      const isOAuth = form.auth_type === 'oauth_app'
      response = await useMyFetch('/connections', {
        method: 'POST',
        body: {
          name: form.name, type: 'custom_api', config, credentials,
          auth_policy: isOAuth ? 'user_required' : 'system_only',
          ...(isOAuth ? { allowed_user_auth_modes: ['oauth'] } : {}),
        },
      })
    }

    // useMyFetch resolves (never throws) on HTTP errors — a non-2xx response
    // surfaces via response.error, so surface it inline instead of silently
    // doing nothing (e.g. a duplicate connection name → 409).
    if (response.error?.value) {
      submitError.value = errorMessage(response.error.value, fallback)
      return
    }
    if (response.data.value) emit('saved', response.data.value)
  } catch (e: any) {
    submitError.value = errorMessage(e, fallback)
  } finally {
    submitting.value = false
  }
}
</script>
