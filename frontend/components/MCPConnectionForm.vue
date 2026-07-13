<template>
  <div>
    <!-- Use existing connection (create mode only) -->
    <div v-if="!isEditMode && existingConnections.length > 0" class="mb-4">
      <label class="block text-xs font-medium text-gray-700 dark:text-gray-300 mb-1">{{ $t('settings.mcpModal.useExistingLabel') }}</label>
      <USelectMenu
        v-model="selectedExisting"
        :options="existingConnectionOptions"
        option-attribute="name"
        :placeholder="$t('settings.mcpModal.selectExistingPlaceholder')"
        size="sm"
        class="w-full"
      />
      <div v-if="!selectedExistingId" class="relative my-4">
        <div class="absolute inset-0 flex items-center"><div class="w-full border-t border-gray-200 dark:border-gray-700" /></div>
        <div class="relative flex justify-center"><span class="bg-white dark:bg-gray-900 px-2 text-xs text-gray-400">{{ $t('settings.mcpModal.orCreateNew') }}</span></div>
      </div>
    </div>

    <form @submit.prevent="handleSubmit" class="space-y-4">
      <template v-if="!selectedExistingId">
        <!-- Connector header: description + example tools, as clean text (no box). -->
        <div v-if="presetDescription || sampleTools.length" class="-mt-2 space-y-2">
          <p v-if="presetDescription" class="text-sm text-gray-500 dark:text-gray-400">{{ presetDescription }}</p>
          <div v-if="sampleTools.length">
            <div class="text-[11px] uppercase tracking-wide text-gray-400 mb-1">Example tools</div>
            <div class="flex flex-wrap gap-1 items-center">
              <span v-for="tool in sampleTools.slice(0, 8)" :key="tool" class="text-[11px] font-mono px-1.5 py-0.5 rounded bg-gray-50 dark:bg-gray-800 border border-gray-200 dark:border-gray-700 text-gray-600 dark:text-gray-300">{{ tool }}</span>
              <span v-if="sampleTools.length > 8" class="text-[11px] text-gray-400">+{{ sampleTools.length - 8 }} more</span>
            </div>
            <p class="mt-1 text-[11px] text-gray-400">The full tool set is discovered automatically after connecting.</p>
          </div>
        </div>

        <div>
          <label class="block text-xs font-medium text-gray-700 dark:text-gray-300 mb-1">{{ $t('settings.mcpModal.nameLabel') }}</label>
          <input v-model="form.name" type="text" :placeholder="$t('settings.mcpModal.namePlaceholder')" class="w-full border border-gray-300 dark:border-gray-600 rounded-md px-3 py-2 text-sm focus:outline-none focus:ring-1 focus:ring-blue-500" />
        </div>

        <!-- Server URL + Transport are shown inline only for a custom (non-preset)
             MCP URL. For a known preset they live under Advanced (below). -->
        <template v-if="!isPreset">
          <div>
            <label class="block text-xs font-medium text-gray-700 dark:text-gray-300 mb-1">{{ $t('settings.mcpModal.urlLabel') }}</label>
            <input v-model="form.server_url" type="text" :placeholder="$t('settings.mcpModal.urlPlaceholder')" class="w-full border border-gray-300 dark:border-gray-600 rounded-md px-3 py-2 text-sm focus:outline-none focus:ring-1 focus:ring-blue-500" />
          </div>
          <div>
            <label class="block text-xs font-medium text-gray-700 dark:text-gray-300 mb-1">{{ $t('settings.mcpModal.transportLabel') }}</label>
            <select v-model="form.transport" class="w-full border border-gray-300 dark:border-gray-600 rounded-md px-3 py-2 text-sm focus:outline-none focus:ring-1 focus:ring-blue-500">
              <option value="sse">{{ $t('settings.mcpModal.transportSse') }}</option>
              <option value="streamable_http">{{ $t('settings.mcpModal.transportHttp') }}</option>
            </select>
          </div>
        </template>

        <div>
          <label class="block text-xs font-medium text-gray-700 dark:text-gray-300 mb-1">{{ $t('settings.mcpModal.authLabel') }}</label>
          <select v-model="form.auth_type" class="w-full border border-gray-300 dark:border-gray-600 rounded-md px-3 py-2 text-sm focus:outline-none focus:ring-1 focus:ring-blue-500">
            <option v-if="authAllowed('none')" value="none">{{ $t('settings.mcpModal.authNone') }}</option>
            <option v-if="authAllowed('bearer')" value="bearer">{{ $t('settings.mcpModal.authBearer') }}</option>
            <option v-if="authAllowed('api_key')" value="api_key">{{ $t('settings.mcpModal.authApiKey') }}</option>
            <option v-if="authAllowed('dcr')" value="dcr">Sign in (auto-register / DCR)</option>
            <option v-if="authAllowed('oauth_app')" value="oauth_app">OAuth (admin-registered app)</option>
          </select>
        </div>

        <div v-if="form.auth_type === 'dcr'" class="text-xs text-gray-600 dark:text-gray-400 border border-gray-200 dark:border-gray-700 rounded-md p-3 bg-gray-50 dark:bg-gray-900">
          This connector registers itself automatically (DCR, RFC 9728/8414/7591) — no client
          credentials to enter. <strong>Each user signs in with their own account</strong> the first
          time they use it.
        </div>

        <div v-if="form.auth_type === 'bearer'">
          <label class="block text-xs font-medium text-gray-700 dark:text-gray-300 mb-1">{{ $t('settings.mcpModal.bearerLabel') }}</label>
          <input v-model="form.token" type="password" :placeholder="isEditMode ? $t('settings.mcpModal.unchanged') : $t('settings.mcpModal.bearerPlaceholder')" class="w-full border border-gray-300 dark:border-gray-600 rounded-md px-3 py-2 text-sm focus:outline-none focus:ring-1 focus:ring-blue-500" />
          <p class="mt-1 text-xs text-gray-500 dark:text-gray-400">This token is shared by everyone who uses this connection.</p>
        </div>

        <div v-if="form.auth_type === 'api_key'" class="space-y-3">
          <div>
            <label class="block text-xs font-medium text-gray-700 dark:text-gray-300 mb-1">{{ $t('settings.mcpModal.apiKeyLabel') }}</label>
            <input v-model="form.api_key" type="password" :placeholder="isEditMode ? $t('settings.mcpModal.unchanged') : $t('settings.mcpModal.apiKeyPlaceholder')" class="w-full border border-gray-300 dark:border-gray-600 rounded-md px-3 py-2 text-sm focus:outline-none focus:ring-1 focus:ring-blue-500" />
          </div>
          <div>
            <label class="block text-xs font-medium text-gray-700 dark:text-gray-300 mb-1">{{ $t('settings.mcpModal.headerNameLabel') }}</label>
            <input v-model="form.api_key_header" type="text" placeholder="X-API-Key" class="w-full border border-gray-300 dark:border-gray-600 rounded-md px-3 py-2 text-sm focus:outline-none focus:ring-1 focus:ring-blue-500" />
          </div>
        </div>

        <div v-if="form.auth_type === 'oauth_app'" class="space-y-3">
          <p class="text-xs text-gray-600 dark:text-gray-400">
            You're registering an OAuth app for your whole org. After you save, <strong>each user signs in
            individually</strong> — their tokens are stored encrypted and sent on every tool call.
            <template v-if="isPreset && hasOauthDefaults">The provider endpoints are pre-filled (see Advanced) — you only need the Client ID and Secret.</template>
          </p>
          <!-- Endpoints inline for a custom URL; presets keep them under Advanced. -->
          <template v-if="!isPreset">
            <div>
              <label class="block text-xs font-medium text-gray-700 dark:text-gray-300 mb-1">Authorize URL</label>
              <input v-model="form.authorize_url" type="text" placeholder="https://idp.example.com/oauth/authorize" class="w-full border border-gray-300 dark:border-gray-600 rounded-md px-3 py-2 text-sm focus:outline-none focus:ring-1 focus:ring-blue-500" />
            </div>
            <div>
              <label class="block text-xs font-medium text-gray-700 dark:text-gray-300 mb-1">Token URL</label>
              <input v-model="form.token_url" type="text" placeholder="https://idp.example.com/oauth/token" class="w-full border border-gray-300 dark:border-gray-600 rounded-md px-3 py-2 text-sm focus:outline-none focus:ring-1 focus:ring-blue-500" />
            </div>
          </template>
          <div>
            <label class="block text-xs font-medium text-gray-700 dark:text-gray-300 mb-1">Client ID</label>
            <input v-model="form.client_id" type="text" class="w-full border border-gray-300 dark:border-gray-600 rounded-md px-3 py-2 text-sm focus:outline-none focus:ring-1 focus:ring-blue-500" />
          </div>
          <div>
            <label class="block text-xs font-medium text-gray-700 dark:text-gray-300 mb-1">Client Secret</label>
            <input v-model="form.client_secret" type="password" :placeholder="isEditMode ? $t('settings.mcpModal.unchanged') : ''" class="w-full border border-gray-300 dark:border-gray-600 rounded-md px-3 py-2 text-sm focus:outline-none focus:ring-1 focus:ring-blue-500" />
          </div>
          <template v-if="!isPreset">
            <div>
              <label class="block text-xs font-medium text-gray-700 dark:text-gray-300 mb-1">Scopes</label>
              <input v-model="form.scopes" type="text" placeholder="openid, profile, offline_access" class="w-full border border-gray-300 dark:border-gray-600 rounded-md px-3 py-2 text-sm focus:outline-none focus:ring-1 focus:ring-blue-500" />
            </div>
            <div>
              <label class="block text-xs font-medium text-gray-700 dark:text-gray-300 mb-1">Resource (audience, optional)</label>
              <input v-model="form.audience" type="text" placeholder="https://mcp.example.com" class="w-full border border-gray-300 dark:border-gray-600 rounded-md px-3 py-2 text-sm focus:outline-none focus:ring-1 focus:ring-blue-500" />
            </div>
          </template>
        </div>

        <!-- Advanced: known/prefilled fields for a preset (server URL, transport,
             OAuth endpoints). Collapsed by default; override for proxy/edge cases. -->
        <div v-if="isPreset">
          <button type="button" @click="advancedOpen = !advancedOpen" class="flex items-center gap-1 text-xs font-medium text-gray-500 dark:text-gray-400 hover:text-gray-700 dark:hover:text-gray-200">
            <UIcon :name="advancedOpen ? 'i-heroicons-chevron-down' : 'i-heroicons-chevron-right'" class="w-3.5 h-3.5" />
            Advanced
          </button>
          <div v-if="advancedOpen" class="mt-2 space-y-3 border border-gray-200 dark:border-gray-700 rounded-md p-3 bg-gray-50 dark:bg-gray-900">
            <p class="text-[11px] text-gray-400">Known for this connector — change only for a proxy or self-hosted endpoint.</p>
            <div>
              <label class="block text-xs font-medium text-gray-700 dark:text-gray-300 mb-1">{{ $t('settings.mcpModal.urlLabel') }}</label>
              <input v-model="form.server_url" type="text" class="w-full border border-gray-300 dark:border-gray-600 rounded-md px-3 py-2 text-sm focus:outline-none focus:ring-1 focus:ring-blue-500" />
            </div>
            <div>
              <label class="block text-xs font-medium text-gray-700 dark:text-gray-300 mb-1">{{ $t('settings.mcpModal.transportLabel') }}</label>
              <select v-model="form.transport" class="w-full border border-gray-300 dark:border-gray-600 rounded-md px-3 py-2 text-sm focus:outline-none focus:ring-1 focus:ring-blue-500">
                <option value="sse">{{ $t('settings.mcpModal.transportSse') }}</option>
                <option value="streamable_http">{{ $t('settings.mcpModal.transportHttp') }}</option>
              </select>
            </div>
            <template v-if="form.auth_type === 'oauth_app'">
              <div>
                <label class="block text-xs font-medium text-gray-700 dark:text-gray-300 mb-1">Authorize URL</label>
                <input v-model="form.authorize_url" type="text" class="w-full border border-gray-300 dark:border-gray-600 rounded-md px-3 py-2 text-sm focus:outline-none focus:ring-1 focus:ring-blue-500" />
              </div>
              <div>
                <label class="block text-xs font-medium text-gray-700 dark:text-gray-300 mb-1">Token URL</label>
                <input v-model="form.token_url" type="text" class="w-full border border-gray-300 dark:border-gray-600 rounded-md px-3 py-2 text-sm focus:outline-none focus:ring-1 focus:ring-blue-500" />
              </div>
              <div>
                <label class="block text-xs font-medium text-gray-700 dark:text-gray-300 mb-1">Scopes</label>
                <input v-model="form.scopes" type="text" class="w-full border border-gray-300 dark:border-gray-600 rounded-md px-3 py-2 text-sm focus:outline-none focus:ring-1 focus:ring-blue-500" />
              </div>
              <div>
                <label class="block text-xs font-medium text-gray-700 dark:text-gray-300 mb-1">Resource (audience, optional)</label>
                <input v-model="form.audience" type="text" placeholder="https://mcp.example.com" class="w-full border border-gray-300 dark:border-gray-600 rounded-md px-3 py-2 text-sm focus:outline-none focus:ring-1 focus:ring-blue-500" />
              </div>
            </template>
          </div>
        </div>

        <!-- OBO connections (per-user OAuth/DCR): offer to spin up a public
             org-wide agent so users can sign in and use it immediately. -->
        <CreatePublicAgentToggle
          v-if="!isEditMode && isPerUser"
          v-model:enabled="createAgent"
          v-model:name="agentName"
          :title="form.name"
          noun="connection"
        />

        <div v-if="testResult" :class="['text-xs px-3 py-2 rounded', testResult.success ? 'bg-green-50 dark:bg-green-950 text-green-700' : 'bg-red-50 dark:bg-red-950 text-red-700']">
          {{ testResult.message }}
        </div>

        <div v-if="submitError" class="text-xs px-3 py-2 rounded bg-red-50 dark:bg-red-950 text-red-700">
          {{ submitError }}
        </div>
      </template>

      <div class="flex items-center justify-between pt-2">
        <button v-if="!selectedExistingId" type="button" @click="testConnection" :disabled="testing || !form.server_url" class="text-xs text-blue-600 hover:text-blue-800 disabled:opacity-50">
          <Spinner v-if="testing" class="w-3 h-3 inline me-1" />
          {{ testLabel }}
        </button>
        <span v-else />
        <div class="flex items-center gap-2">
          <UButton color="gray" variant="ghost" size="sm" @click="emit('cancel')">{{ $t('settings.mcpModal.cancel') }}</UButton>
          <UButton type="submit" color="blue" size="sm" :loading="submitting" :disabled="selectedExistingId ? false : (!form.server_url || !form.name)">
            {{ submitLabel }}
          </UButton>
        </div>
      </div>
    </form>
  </div>
</template>

<script setup lang="ts">
import Spinner from '~/components/Spinner.vue'

const { t } = useI18n()
const props = defineProps<{
  editConnection?: any
  existingConnections?: any[]
  // Prefill the create form from a catalog entry. `allowed_auth` gates which
  // auth modes the dropdown offers; `oauth_defaults` pre-fills the provider's
  // (invariant) OAuth endpoints/scopes so the admin only supplies client id/secret.
  prefill?: {
    name?: string; server_url?: string; transport?: string; auth_type?: string
    allowed_auth?: string[] | null
    oauth_defaults?: { authorize_url?: string; token_url?: string; scopes?: string; audience?: string; token_endpoint_auth_method?: string } | null
    description?: string
    sample_tools?: string[] | null
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
  server_url: '',
  transport: 'sse',
  auth_type: 'none',
  token: '',
  api_key: '',
  api_key_header: 'X-API-Key',
  // OAuth app fields (used when auth_type === 'oauth_app')
  authorize_url: '',
  token_url: '',
  client_id: '',
  client_secret: '',
  scopes: '',
  audience: '',
  // Provider constant (client_secret_basic for X, client_secret_post for
  // Microsoft/Google). Carried through, not shown as an input.
  token_endpoint_auth_method: '',
})

watch(() => props.editConnection, async (conn) => {
  if (conn) {
    try {
      const response = await useMyFetch(`/connections/${conn.id}`, { method: 'GET' })
      const detail = response.data.value as any
      if (detail) {
        const config = detail.config || {}
        form.name = detail.name || ''
        form.server_url = config.server_url || ''
        form.transport = config.transport || 'sse'
        form.auth_type = config.auth_type || (detail.has_credentials ? 'bearer' : 'none')
        form.token = ''
        form.api_key = ''
        form.api_key_header = config.api_key_header || 'X-API-Key'
        // OAuth app fields — non-secret values come back from the backend in
        // the `credentials_meta` blob so the admin can edit them without
        // re-entering everything. Secrets stay blank (unchanged-placeholder).
        const meta = detail.credentials_meta || {}
        form.authorize_url = meta.authorize_url || ''
        form.token_url = meta.token_url || ''
        form.client_id = meta.client_id || ''
        form.client_secret = ''
        form.scopes = meta.scopes || ''
        form.audience = meta.audience || ''
        form.token_endpoint_auth_method = meta.token_endpoint_auth_method || ''
        return
      }
    } catch {}
    form.name = conn.name || ''
    form.auth_type = 'none'
  }
}, { immediate: true })

// Apply catalog prefill in create mode (no edit connection). Runs immediately so
// the form opens populated; the user only needs to add client id/secret (oauth_app)
// or just click the primary button (DCR/bearer presets).
watch(() => props.prefill, (p) => {
  if (!p || props.editConnection) return
  if (p.name) form.name = p.name
  if (p.server_url) form.server_url = p.server_url
  if (p.transport) form.transport = p.transport
  if (p.auth_type) form.auth_type = p.auth_type
  // Provider OAuth constants — pre-filled, still editable (proxy/edge cases).
  const d = p.oauth_defaults
  if (d) {
    if (d.authorize_url) form.authorize_url = d.authorize_url
    if (d.token_url) form.token_url = d.token_url
    if (d.scopes) form.scopes = d.scopes
    if (d.audience) form.audience = d.audience
    if (d.token_endpoint_auth_method) form.token_endpoint_auth_method = d.token_endpoint_auth_method
  }
}, { immediate: true })

// Resolve the catalog preset by server_url so EDIT mode (which has no prefill)
// shows the same description / tool preview / auth gating / known transport as
// the create-from-catalog flow. Create passes the full entry via `prefill`.
const catalog = ref<any[]>([])
onMounted(async () => {
  try {
    const r = await useMyFetch('/connectors/catalog', { method: 'GET' })
    if (r.data.value) catalog.value = r.data.value as any[]
  } catch {}
})
const matchedPreset = computed<any>(() =>
  (catalog.value || []).find((p: any) => p.server_url && p.server_url === form.server_url) || null
)
const presetSpec = computed<any>(() => props.prefill || matchedPreset.value)
const isPreset = computed(() => !!presetSpec.value)

const presetDescription = computed<string>(() => presetSpec.value?.description || '')
const sampleTools = computed<string[]>(() => presetSpec.value?.sample_tools || [])

// Which auth modes this tile offers (null → offer all, e.g. arbitrary URL).
const allowedAuth = computed<string[] | null>(() => presetSpec.value?.allowed_auth || null)
function authAllowed(mode: string): boolean {
  const a = allowedAuth.value
  return !a || a.includes(mode)
}
const hasOauthDefaults = computed(() => !!presetSpec.value?.oauth_defaults)

// For a known preset, the server URL / transport / OAuth endpoints are constants
// — collapse them under an "Advanced" disclosure (prefilled, still overridable
// for a proxy or self-hosted endpoint). Custom URLs show them inline.
const advancedOpen = ref(false)

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

function buildCredentials(): Record<string, any> | undefined {
  if (form.auth_type === 'bearer' && form.token) return { token: form.token }
  if (form.auth_type === 'api_key' && form.api_key) return { api_key: form.api_key, api_key_header: form.api_key_header }
  if (form.auth_type === 'oauth_app') {
    // Don't send empty strings; backend wants either a complete OAuth app or
    // an updated subset (edit mode).
    const c: Record<string, any> = {}
    if (form.authorize_url) c.authorize_url = form.authorize_url
    if (form.token_url) c.token_url = form.token_url
    if (form.client_id) c.client_id = form.client_id
    if (form.client_secret) c.client_secret = form.client_secret
    if (form.scopes) c.scopes = form.scopes
    if (form.audience) c.audience = form.audience
    if (form.token_endpoint_auth_method) c.token_endpoint_auth_method = form.token_endpoint_auth_method
    return Object.keys(c).length ? c : undefined
  }
  return undefined
}

// MCP OAuth (admin app) and DCR both imply per-user authentication — each user
// signs in themselves and their access token gates tool calls. DCR needs no
// admin client (the backend registers one dynamically); oauth_app uses the
// admin-entered client.
const PER_USER_OAUTH = ['oauth_app', 'dcr']
const isPerUser = computed(() => PER_USER_OAUTH.includes(form.auth_type))
const authPolicy = computed(() => isPerUser.value ? 'user_required' : 'system_only')
const allowedUserAuthModes = computed(() => isPerUser.value ? ['oauth'] : undefined)

// "Create a public agent" — offered for per-user OAuth (OBO) connections in
// create mode. Default on: the common case is enabling the connector so the org
// can use it immediately; each user signs in individually.
const createAgent = ref(true)
const agentName = ref('')
const toast = useToast()
// Prefill the agent name from the connection name so the field shows real text
// (not just a placeholder); still editable, and won't clobber a manual edit.
watch(() => form.name, (n) => { if (n && !agentName.value) agentName.value = n }, { immediate: true })

// For per-user OAuth the admin isn't authenticating here — they're saving config
// and verifying reachability. Reflect that in the button copy.
const submitLabel = computed(() => {
  if (isEditMode.value) return t('settings.mcpModal.save')
  return isPerUser.value ? 'Add connection' : t('settings.mcpModal.connect')
})
const testLabel = computed(() => isPerUser.value ? 'Verify' : t('settings.mcpModal.testConnection'))

async function testConnection() {
  testing.value = true
  testResult.value = null
  try {
    const config = { server_url: form.server_url, transport: form.transport, auth_type: form.auth_type }
    const response = await useMyFetch('/connections/test-params', {
      method: 'POST',
      body: { name: 'test', type: 'mcp', config, credentials: buildCredentials() || {} },
    })
    testResult.value = response.data.value as any
  } catch (e: any) {
    testResult.value = { success: false, message: e?.data?.detail || t('settings.mcpModal.testFailed') }
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

  submitting.value = true
  submitError.value = null
  const fallback = isEditMode.value ? t('settings.mcpModal.failedUpdate') : t('settings.mcpModal.failedConnect')
  try {
    const config = { server_url: form.server_url, transport: form.transport, auth_type: form.auth_type }
    const credentials = buildCredentials()

    let response: any
    if (isEditMode.value && props.editConnection) {
      const body: Record<string, any> = { name: form.name, config, credentials, auth_policy: authPolicy.value }
      if (allowedUserAuthModes.value) body.allowed_user_auth_modes = allowedUserAuthModes.value
      response = await useMyFetch(`/connections/${props.editConnection.id}`, {
        method: 'PUT',
        body,
      })
    } else {
      const body: Record<string, any> = {
        name: form.name, type: 'mcp', config, credentials,
        auth_policy: authPolicy.value,
      }
      if (allowedUserAuthModes.value) body.allowed_user_auth_modes = allowedUserAuthModes.value
      response = await useMyFetch('/connections', {
        method: 'POST',
        body,
      })
    }

    // useMyFetch resolves (never throws) on HTTP errors — a non-2xx response
    // surfaces via response.error, so surface it inline instead of silently
    // doing nothing (e.g. a duplicate connection name → 409).
    if (response.error?.value) {
      submitError.value = errorMessage(response.error.value, fallback)
      return
    }
    const saved = response.data.value
    if (saved) {
      // Best-effort: for an OBO connection, optionally spin up a public org-wide
      // agent so users can sign in and use it right away. A failure here (e.g. a
      // duplicate agent name) must not block the successful connection save.
      if (!isEditMode.value && isPerUser.value && createAgent.value) {
        try {
          await createPublicAgent((saved as any).id, { name: agentName.value || form.name, type: 'mcp' })
        } catch (e: any) {
          toast.add({ title: 'Connection saved, but agent creation failed', description: errorMessage(e, 'Agent creation failed'), color: 'yellow' })
        }
      }
      emit('saved', saved)
    }
  } catch (e: any) {
    submitError.value = errorMessage(e, fallback)
  } finally {
    submitting.value = false
  }
}
</script>
