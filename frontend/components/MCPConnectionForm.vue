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
        <div>
          <label class="block text-xs font-medium text-gray-700 dark:text-gray-300 mb-1">{{ $t('settings.mcpModal.nameLabel') }}</label>
          <input v-model="form.name" type="text" :placeholder="$t('settings.mcpModal.namePlaceholder')" class="w-full border border-gray-300 dark:border-gray-600 rounded-md px-3 py-2 text-sm focus:outline-none focus:ring-1 focus:ring-blue-500" />
        </div>

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

        <div>
          <label class="block text-xs font-medium text-gray-700 dark:text-gray-300 mb-1">{{ $t('settings.mcpModal.authLabel') }}</label>
          <select v-model="form.auth_type" class="w-full border border-gray-300 dark:border-gray-600 rounded-md px-3 py-2 text-sm focus:outline-none focus:ring-1 focus:ring-blue-500">
            <option value="none">{{ $t('settings.mcpModal.authNone') }}</option>
            <option value="bearer">{{ $t('settings.mcpModal.authBearer') }}</option>
            <option value="api_key">{{ $t('settings.mcpModal.authApiKey') }}</option>
            <option value="dcr">Sign in (auto-register / DCR)</option>
            <option value="oauth_app">OAuth (admin-registered app)</option>
          </select>
        </div>

        <div v-if="form.auth_type === 'dcr'" class="text-xs text-gray-600 dark:text-gray-400 border border-gray-200 dark:border-gray-700 rounded-md p-3 bg-gray-50 dark:bg-gray-900">
          No setup needed. We discover the server's authorization server and register a client
          automatically (RFC 9728/8414/7591). Each user signs in with their own account; their
          token is stored encrypted and sent on every tool call. Only the server URL above is required.
        </div>

        <div v-if="form.auth_type === 'bearer'">
          <label class="block text-xs font-medium text-gray-700 dark:text-gray-300 mb-1">{{ $t('settings.mcpModal.bearerLabel') }}</label>
          <input v-model="form.token" type="password" :placeholder="isEditMode ? $t('settings.mcpModal.unchanged') : $t('settings.mcpModal.bearerPlaceholder')" class="w-full border border-gray-300 dark:border-gray-600 rounded-md px-3 py-2 text-sm focus:outline-none focus:ring-1 focus:ring-blue-500" />
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
            Register an OAuth client at the identity provider that fronts this MCP server. Users will sign in
            individually; their tokens are stored encrypted and sent on every tool call.
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
      </template>

      <div class="flex items-center justify-between pt-2">
        <button v-if="!selectedExistingId" type="button" @click="testConnection" :disabled="testing || !form.server_url" class="text-xs text-blue-600 hover:text-blue-800 disabled:opacity-50">
          <Spinner v-if="testing" class="w-3 h-3 inline me-1" />
          {{ $t('settings.mcpModal.testConnection') }}
        </button>
        <span v-else />
        <div class="flex items-center gap-2">
          <UButton color="gray" variant="ghost" size="sm" @click="emit('cancel')">{{ $t('settings.mcpModal.cancel') }}</UButton>
          <UButton type="submit" color="blue" size="sm" :loading="submitting" :disabled="selectedExistingId ? false : (!form.server_url || !form.name)">
            {{ isEditMode ? $t('settings.mcpModal.save') : $t('settings.mcpModal.connect') }}
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
}>()
const emit = defineEmits<{
  (e: 'saved', connection: any): void
  (e: 'cancel'): void
}>()

const toast = useToast()
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

const testing = ref(false)
const submitting = ref(false)
const testResult = ref<{ success: boolean; message: string } | null>(null)

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
const authPolicy = computed(() => PER_USER_OAUTH.includes(form.auth_type) ? 'user_required' : 'system_only')
const allowedUserAuthModes = computed(() => PER_USER_OAUTH.includes(form.auth_type) ? ['oauth'] : undefined)

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
  try {
    const config = { server_url: form.server_url, transport: form.transport, auth_type: form.auth_type }
    const credentials = buildCredentials()

    if (isEditMode.value && props.editConnection) {
      const body: Record<string, any> = { name: form.name, config, credentials, auth_policy: authPolicy.value }
      if (allowedUserAuthModes.value) body.allowed_user_auth_modes = allowedUserAuthModes.value
      const response = await useMyFetch(`/connections/${props.editConnection.id}`, {
        method: 'PUT',
        body,
      })
      if (response.data.value) emit('saved', response.data.value)
    } else {
      const body: Record<string, any> = {
        name: form.name, type: 'mcp', config, credentials,
        auth_policy: authPolicy.value,
      }
      if (allowedUserAuthModes.value) body.allowed_user_auth_modes = allowedUserAuthModes.value
      const response = await useMyFetch('/connections', {
        method: 'POST',
        body,
      })
      if (response.data.value) emit('saved', response.data.value)
    }
  } catch (e: any) {
    toast.add({ title: isEditMode.value ? t('settings.mcpModal.failedUpdate') : t('settings.mcpModal.failedConnect'), description: e?.data?.detail, color: 'red' })
  } finally {
    submitting.value = false
  }
}
</script>
