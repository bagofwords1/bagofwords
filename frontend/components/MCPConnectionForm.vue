<template>
  <div>
    <!-- Use existing connection (create mode only) -->
    <div v-if="!isEditMode && existingConnections.length > 0" class="mb-4">
      <label class="block text-xs font-medium text-gray-700 mb-1">{{ $t('settings.mcpModal.useExistingLabel') }}</label>
      <USelectMenu
        v-model="selectedExisting"
        :options="existingConnectionOptions"
        option-attribute="name"
        :placeholder="$t('settings.mcpModal.selectExistingPlaceholder')"
        size="sm"
        class="w-full"
      />
      <div v-if="!selectedExistingId" class="relative my-4">
        <div class="absolute inset-0 flex items-center"><div class="w-full border-t border-gray-200" /></div>
        <div class="relative flex justify-center"><span class="bg-white px-2 text-xs text-gray-400">{{ $t('settings.mcpModal.orCreateNew') }}</span></div>
      </div>
    </div>

    <form @submit.prevent="handleSubmit" class="space-y-4">
      <template v-if="!selectedExistingId">
        <div>
          <label class="block text-xs font-medium text-gray-700 mb-1">{{ $t('settings.mcpModal.nameLabel') }}</label>
          <input v-model="form.name" type="text" :placeholder="$t('settings.mcpModal.namePlaceholder')" class="w-full border border-gray-300 rounded-md px-3 py-2 text-sm focus:outline-none focus:ring-1 focus:ring-blue-500" />
        </div>

        <div>
          <label class="block text-xs font-medium text-gray-700 mb-1">{{ $t('settings.mcpModal.urlLabel') }}</label>
          <input v-model="form.server_url" type="text" :placeholder="$t('settings.mcpModal.urlPlaceholder')" class="w-full border border-gray-300 rounded-md px-3 py-2 text-sm focus:outline-none focus:ring-1 focus:ring-blue-500" />
        </div>

        <div>
          <label class="block text-xs font-medium text-gray-700 mb-1">{{ $t('settings.mcpModal.transportLabel') }}</label>
          <select v-model="form.transport" class="w-full border border-gray-300 rounded-md px-3 py-2 text-sm focus:outline-none focus:ring-1 focus:ring-blue-500">
            <option value="sse">{{ $t('settings.mcpModal.transportSse') }}</option>
            <option value="streamable_http">{{ $t('settings.mcpModal.transportHttp') }}</option>
          </select>
        </div>

        <div>
          <label class="block text-xs font-medium text-gray-700 mb-1">{{ $t('settings.mcpModal.authLabel') }}</label>
          <select v-model="form.auth_type" class="w-full border border-gray-300 rounded-md px-3 py-2 text-sm focus:outline-none focus:ring-1 focus:ring-blue-500">
            <option value="none">{{ $t('settings.mcpModal.authNone') }}</option>
            <option value="bearer">{{ $t('settings.mcpModal.authBearer') }}</option>
            <option value="api_key">{{ $t('settings.mcpModal.authApiKey') }}</option>
          </select>
        </div>

        <div v-if="form.auth_type === 'bearer'">
          <label class="block text-xs font-medium text-gray-700 mb-1">{{ $t('settings.mcpModal.bearerLabel') }}</label>
          <input v-model="form.token" type="password" :placeholder="isEditMode ? $t('settings.mcpModal.unchanged') : $t('settings.mcpModal.bearerPlaceholder')" class="w-full border border-gray-300 rounded-md px-3 py-2 text-sm focus:outline-none focus:ring-1 focus:ring-blue-500" />
        </div>

        <div v-if="form.auth_type === 'api_key'" class="space-y-3">
          <div>
            <label class="block text-xs font-medium text-gray-700 mb-1">{{ $t('settings.mcpModal.apiKeyLabel') }}</label>
            <input v-model="form.api_key" type="password" :placeholder="isEditMode ? $t('settings.mcpModal.unchanged') : $t('settings.mcpModal.apiKeyPlaceholder')" class="w-full border border-gray-300 rounded-md px-3 py-2 text-sm focus:outline-none focus:ring-1 focus:ring-blue-500" />
          </div>
          <div>
            <label class="block text-xs font-medium text-gray-700 mb-1">{{ $t('settings.mcpModal.headerNameLabel') }}</label>
            <input v-model="form.api_key_header" type="text" placeholder="X-API-Key" class="w-full border border-gray-300 rounded-md px-3 py-2 text-sm focus:outline-none focus:ring-1 focus:ring-blue-500" />
          </div>
        </div>

        <div v-if="testResult" :class="['text-xs px-3 py-2 rounded', testResult.success ? 'bg-green-50 text-green-700' : 'bg-red-50 text-red-700']">
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
  return undefined
}

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
      const response = await useMyFetch(`/connections/${props.editConnection.id}`, {
        method: 'PUT',
        body: { name: form.name, config, credentials },
      })
      if (response.data.value) emit('saved', response.data.value)
    } else {
      const response = await useMyFetch('/connections', {
        method: 'POST',
        body: { name: form.name, type: 'mcp', config, credentials, auth_policy: 'system_only' },
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
