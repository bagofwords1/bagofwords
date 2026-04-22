<template>
  <UModal v-model="isOpen" :ui="{ width: 'sm:max-w-lg' }">
    <div class="p-6">
      <h2 class="text-lg font-semibold mb-4">{{ isEditMode ? $t('settings.mcpModal.editTitle') : $t('settings.mcpModal.connectTitle') }}</h2>

      <form @submit.prevent="handleSubmit" class="space-y-4">
        <!-- Name -->
        <div>
          <label class="block text-xs font-medium text-gray-700 mb-1">{{ $t('settings.mcpModal.nameLabel') }}</label>
          <input
            v-model="form.name"
            type="text"
            :placeholder="$t('settings.mcpModal.namePlaceholder')"
            class="w-full border border-gray-300 rounded-md px-3 py-2 text-sm focus:outline-none focus:ring-1 focus:ring-blue-500"
          />
        </div>

        <!-- Server URL -->
        <div>
          <label class="block text-xs font-medium text-gray-700 mb-1">{{ $t('settings.mcpModal.urlLabel') }}</label>
          <input
            v-model="form.server_url"
            type="text"
            :placeholder="$t('settings.mcpModal.urlPlaceholder')"
            class="w-full border border-gray-300 rounded-md px-3 py-2 text-sm focus:outline-none focus:ring-1 focus:ring-blue-500"
          />
        </div>

        <!-- Transport -->
        <div>
          <label class="block text-xs font-medium text-gray-700 mb-1">{{ $t('settings.mcpModal.transportLabel') }}</label>
          <select
            v-model="form.transport"
            class="w-full border border-gray-300 rounded-md px-3 py-2 text-sm focus:outline-none focus:ring-1 focus:ring-blue-500"
          >
            <option value="sse">{{ $t('settings.mcpModal.transportSse') }}</option>
            <option value="streamable_http">{{ $t('settings.mcpModal.transportHttp') }}</option>
          </select>
        </div>

        <!-- Auth -->
        <div>
          <label class="block text-xs font-medium text-gray-700 mb-1">{{ $t('settings.mcpModal.authLabel') }}</label>
          <select
            v-model="form.auth_type"
            class="w-full border border-gray-300 rounded-md px-3 py-2 text-sm focus:outline-none focus:ring-1 focus:ring-blue-500"
          >
            <option value="none">{{ $t('settings.mcpModal.authNone') }}</option>
            <option value="bearer">{{ $t('settings.mcpModal.authBearer') }}</option>
            <option value="api_key">{{ $t('settings.mcpModal.authApiKey') }}</option>
          </select>
        </div>

        <!-- Bearer Token -->
        <div v-if="form.auth_type === 'bearer'">
          <label class="block text-xs font-medium text-gray-700 mb-1">{{ $t('settings.mcpModal.bearerLabel') }}</label>
          <input
            v-model="form.token"
            type="password"
            :placeholder="isEditMode ? $t('settings.mcpModal.unchanged') : $t('settings.mcpModal.bearerPlaceholder')"
            class="w-full border border-gray-300 rounded-md px-3 py-2 text-sm focus:outline-none focus:ring-1 focus:ring-blue-500"
          />
        </div>

        <!-- API Key -->
        <div v-if="form.auth_type === 'api_key'" class="space-y-3">
          <div>
            <label class="block text-xs font-medium text-gray-700 mb-1">{{ $t('settings.mcpModal.apiKeyLabel') }}</label>
            <input
              v-model="form.api_key"
              type="password"
              :placeholder="isEditMode ? $t('settings.mcpModal.unchanged') : $t('settings.mcpModal.apiKeyPlaceholder')"
              class="w-full border border-gray-300 rounded-md px-3 py-2 text-sm focus:outline-none focus:ring-1 focus:ring-blue-500"
            />
          </div>
          <div>
            <label class="block text-xs font-medium text-gray-700 mb-1">{{ $t('settings.mcpModal.headerNameLabel') }}</label>
            <input
              v-model="form.api_key_header"
              type="text"
              placeholder="X-API-Key"
              class="w-full border border-gray-300 rounded-md px-3 py-2 text-sm focus:outline-none focus:ring-1 focus:ring-blue-500"
            />
          </div>
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
            :disabled="testing || !form.server_url"
            class="text-xs text-blue-600 hover:text-blue-800 disabled:opacity-50"
          >
            <Spinner v-if="testing" class="w-3 h-3 inline me-1" />
            {{ $t('settings.mcpModal.testConnection') }}
          </button>

          <div class="flex items-center gap-2">
            <UButton color="gray" variant="ghost" size="sm" @click="isOpen = false">{{ $t('settings.mcpModal.cancel') }}</UButton>
            <UButton type="submit" color="blue" size="sm" :loading="submitting" :disabled="!form.server_url || !form.name">
              {{ isEditMode ? $t('settings.mcpModal.save') : $t('settings.mcpModal.connect') }}
            </UButton>
          </div>
        </div>
      </form>
    </div>
  </UModal>
</template>

<script setup lang="ts">
import Spinner from '~/components/Spinner.vue'

const { t } = useI18n()
const isOpen = defineModel<boolean>({ default: false })
const props = defineProps<{
  editConnection?: any
}>()
const emit = defineEmits(['created'])
const toast = useToast()

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

// Fetch full connection detail and populate form when editing
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
    // Fallback if detail fetch fails
    form.name = conn.name || ''
    form.auth_type = 'none'
  }
}, { immediate: true })

// Reset form when modal closes
watch(isOpen, (open) => {
  if (!open && !props.editConnection) {
    resetForm()
  }
})

function resetForm() {
  Object.assign(form, { name: '', server_url: '', transport: 'sse', auth_type: 'none', token: '', api_key: '', api_key_header: 'X-API-Key' })
  testResult.value = null
}

function buildCredentials(): Record<string, any> | undefined {
  if (form.auth_type === 'bearer' && form.token) return { token: form.token }
  if (form.auth_type === 'api_key' && form.api_key) return { api_key: form.api_key, api_key_header: form.api_key_header }
  return undefined
}

const testing = ref(false)
const submitting = ref(false)
const testResult = ref<{ success: boolean; message: string } | null>(null)

async function testConnection() {
  testing.value = true
  testResult.value = null
  try {
    const config = { server_url: form.server_url, transport: form.transport, auth_type: form.auth_type }
    const credentials = buildCredentials() || {}
    const response = await useMyFetch('/connections/test-params', {
      method: 'POST',
      body: { name: 'test', type: 'mcp', config, credentials },
    })
    testResult.value = response.data.value as any
  } catch (e: any) {
    testResult.value = { success: false, message: e?.data?.detail || t('settings.mcpModal.testFailed') }
  } finally {
    testing.value = false
  }
}

async function handleSubmit() {
  submitting.value = true
  try {
    const config = { server_url: form.server_url, transport: form.transport, auth_type: form.auth_type }
    const credentials = buildCredentials()

    if (isEditMode.value && props.editConnection) {
      // Update existing connection
      const response = await useMyFetch(`/connections/${props.editConnection.id}`, {
        method: 'PUT',
        body: { name: form.name, config, credentials },
      })
      if (response.data.value) {
        toast.add({ title: t('settings.mcpModal.updated'), color: 'green' })
        isOpen.value = false
        emit('created', response.data.value)
      }
    } else {
      // Create new connection
      const response = await useMyFetch('/connections', {
        method: 'POST',
        body: { name: form.name, type: 'mcp', config, credentials, auth_policy: 'system_only' },
      })
      if (response.data.value) {
        toast.add({ title: t('settings.mcpModal.connected'), color: 'green' })
        isOpen.value = false
        emit('created', response.data.value)
        resetForm()
      }
    }
  } catch (e: any) {
    toast.add({ title: isEditMode.value ? t('settings.mcpModal.failedUpdate') : t('settings.mcpModal.failedConnect'), description: e?.data?.detail, color: 'red' })
  } finally {
    submitting.value = false
  }
}
</script>
