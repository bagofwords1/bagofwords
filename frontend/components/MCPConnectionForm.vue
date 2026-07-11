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
        <!-- Connector overview: description + sample of tools (illustrative). -->
        <div v-if="presetDescription || sampleTools.length" class="rounded-md border border-gray-200 dark:border-gray-700 p-3 bg-gray-50 dark:bg-gray-900">
          <p v-if="presetDescription" class="text-xs text-gray-600 dark:text-gray-300">{{ presetDescription }}</p>
          <div v-if="sampleTools.length" class="mt-2">
            <div class="text-[11px] uppercase tracking-wide text-gray-400 mb-1">Example tools</div>
            <div class="flex flex-wrap gap-1">
              <span v-for="tool in sampleTools" :key="tool" class="text-[11px] font-mono px-1.5 py-0.5 rounded bg-white dark:bg-gray-800 border border-gray-200 dark:border-gray-700 text-gray-600 dark:text-gray-300">{{ tool }}</span>
            </div>
            <p class="mt-1 text-[11px] text-gray-400">The full tool set is discovered automatically after connecting.</p>
          </div>
          <p v-else-if="presetDescription" class="mt-2 text-[11px] text-gray-400">Tools are discovered automatically after connecting.</p>
        </div>

        <div>
          <label class="block text-xs font-medium text-gray-700 dark:text-gray-300 mb-1">{{ $t('settings.mcpModal.nameLabel') }}</label>
          <input v-model="form.name" type="text" :placeholder="$t('settings.mcpModal.namePlaceholder')" class="w-full border border-gray-300 dark:border-gray-600 rounded-md px-3 py-2 text-sm focus:outline-none focus:ring-1 focus:ring-blue-500" />
        </div>

        <div>
          <label class="block text-xs font-medium text-gray-700 dark:text-gray-300 mb-1">{{ $t('settings.mcpModal.urlLabel') }}</label>
          <input v-model="form.server_url" type="text" :placeholder="$t('settings.mcpModal.urlPlaceholder')" class="w-full border border-gray-300 dark:border-gray-600 rounded-md px-3 py-2 text-sm focus:outline-none focus:ring-1 focus:ring-blue-500" />
        </div>

        <div v-if="showTransport">
          <label class="block text-xs font-medium text-gray-700 dark:text-gray-300 mb-1">{{ $t('settings.mcpModal.transportLabel') }}</label>
          <select v-model="form.transport" class="w-full border border-gray-300 dark:border-gray-600 rounded-md px-3 py-2 text-sm focus:outline-none focus:ring-1 focus:ring-blue-500">
            <option value="sse">{{ $t('settings.mcpModal.transportSse') }}</option>
            <option value="streamable_http">{{ $t('settings.mcpModal.transportHttp') }}</option>
          </select>
        </div>

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
          No admin setup — this connector auto-registers (DCR, RFC 9728/8414/7591). You're adding a
          connection for your org; <strong>each user signs in with their own account</strong> when they
          first use it. Only the server URL above is required.
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

        <div v-if="form.auth_type === 'oauth_app'" class="space-y-3 border border-gray-200 dark:border-gray-700 rounded-md p-3 bg-gray-50 dark:bg-gray-900">
          <div class="text-xs text-gray-600 dark:text-gray-400">
            You're registering an OAuth app for your whole org. After you save, <strong>each user signs in
            individually</strong> — their tokens are stored encrypted and sent on every tool call.
            <span v-if="hasOauthDefaults">The endpoints below are pre-filled for this provider; you only need the Client ID and Secret.</span>
          </div>
          <div>
            <label class="block text-xs font-medium text-gray-700 dark:text-gray-300 mb-1">Authorize URL</label>
            <input v-model="form.authorize_url" type="text" placeholder="https://idp.example.com/oauth/authorize" class="w-full border border-gray-300 dark:border-gray-600 rounded-md px-3 py-2 text-sm focus:outline-none focus:ring-1 focus:ring-blue-500" />
          </div>
          <div>
            <label class="block text-xs font-medium text-gray-700 dark:text-gray-300 mb-1">Token URL</label>
            <input v-model="form.token_url" type="text" placeholder="https://idp.example.com/oauth/token" class="w-full border border-gray-300 dark:border-gray-600 rounded-md px-3 py-2 text-sm focus:outline-none focus:ring-1 focus:ring-blue-500" />
          </div>
          <div>
            <label class="block text-xs font-medium text-gray-700 dark:text-gray-300 mb-1">Client ID</label>
            <input v-model="form.client_id" type="text" class="w-full border border-gray-300 dark:border-gray-600 rounded-md px-3 py-2 text-sm focus:outline-none focus:ring-1 focus:ring-blue-500" />
          </div>
          <div>
            <label class="block text-xs font-medium text-gray-700 dark:text-gray-300 mb-1">Client Secret</label>
            <input v-model="form.client_secret" type="password" :placeholder="isEditMode ? $t('settings.mcpModal.unchanged') : ''" class="w-full border border-gray-300 dark:border-gray-600 rounded-md px-3 py-2 text-sm focus:outline-none focus:ring-1 focus:ring-blue-500" />
          </div>
          <div>
            <label class="block text-xs font-medium text-gray-700 dark:text-gray-300 mb-1">Scopes</label>
            <input v-model="form.scopes" type="text" placeholder="openid profile offline_access" class="w-full border border-gray-300 dark:border-gray-600 rounded-md px-3 py-2 text-sm focus:outline-none focus:ring-1 focus:ring-blue-500" />
          </div>
          <div>
            <label class="block text-xs font-medium text-gray-700 dark:text-gray-300 mb-1">Resource (audience, optional)</label>
            <input v-model="form.audience" type="text" placeholder="https://mcp.example.com" class="w-full border border-gray-300 dark:border-gray-600 rounded-md px-3 py-2 text-sm focus:outline-none focus:ring-1 focus:ring-blue-500" />
          </div>
        </div>

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
    oauth_defaults?: { authorize_url?: string; token_url?: string; scopes?: string; audience?: string } | null
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

// Transport is a property of a known server — hide the picker for presets and
// only surface it for a hand-entered (custom) MCP URL.
const showTransport = computed(() => !isPreset.value)

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
    if (response.data.value) emit('saved', response.data.value)
  } catch (e: any) {
    submitError.value = errorMessage(e, fallback)
  } finally {
    submitting.value = false
  }
}
</script>
