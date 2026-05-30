<template>
  <UModal v-model="open" :ui="{ width: 'sm:max-w-lg' }">
    <div class="p-5 relative">
      <button @click="emit('update:modelValue', false)" class="absolute top-2 end-2 text-gray-500 hover:text-gray-700 outline-none">
        <Icon name="heroicons:x-mark" class="w-5 h-5" />
      </button>

      <div class="mb-4">
        <h1 class="text-base font-semibold flex items-center">
            <DataSourceIcon :type="connectionType" class="h-4 me-2" />
            {{ $t('data.connectNamed', { name: ds?.name }) }}</h1>
        <p class="mt-1 text-xs text-gray-500">{{ $t('data.provideCredentials') }}</p>
      </div>

      <!-- Per-user catalog auto-redirects to OAuth; show a clean handoff state -->
      <div v-if="isPerUserCatalog" class="mt-4 text-center py-6">
        <div v-if="oauthLoading" class="flex flex-col items-center gap-2 text-sm text-gray-600">
          <UIcon name="heroicons-arrow-path" class="w-5 h-5 animate-spin text-blue-500" />
          <div>Redirecting to {{ providerName }}…</div>
        </div>
        <div v-else class="flex flex-col gap-3 items-center">
          <p class="text-xs text-gray-500">Sign in to {{ providerName }} to access your files.</p>
          <UButton size="sm" color="blue" variant="solid" :loading="oauthLoading" @click="onOAuthSignIn">
            {{ currentAuthTitle || $t('data.signIn') }}
          </UButton>
        </div>
      </div>

      <div v-else-if="authOptions.length > 1" class="mb-3">
        <label class="text-xs text-gray-600">{{ $t('data.authMethod') }}</label>
        <USelectMenu v-model="authMode" :options="authOptions" option-attribute="label" value-attribute="value" />
      </div>

      <!-- OAuth mode (shared catalog with OAuth option): standard sign-in button -->
      <div v-if="!isPerUserCatalog && isOAuthMode" class="mt-4">
        <UButton
          size="sm"
          color="blue"
          variant="solid"
          block
          :loading="oauthLoading"
          @click="onOAuthSignIn"
        >
          {{ currentAuthTitle || $t('data.signIn') }}
        </UButton>
      </div>

      <!-- Standard credential form (shared catalog with non-OAuth auth) -->
      <template v-else-if="!isPerUserCatalog">
        <div class="space-y-3">
          <div v-for="field in credentialFields" :key="field.key" class="flex flex-col">
            <label class="text-xs text-gray-600 mb-1">{{ field.title }}</label>
            <input v-if="field.type === 'string'" :type="field.format === 'password' ? 'password' : 'text'" v-model="form.credentials[field.key]" class="border rounded px-2 py-1 text-sm" />
            <input v-else-if="field.type === 'integer'" type="number" v-model.number="form.credentials[field.key]" class="border rounded px-2 py-1 text-sm" />
            <UCheckbox v-else-if="field.type === 'boolean'" v-model="form.credentials[field.key]">{{ field.title }}</UCheckbox>
            <textarea v-else-if="field.type === 'text' || field.type === 'textarea'" v-model="form.credentials[field.key]" class="border rounded px-2 py-1 text-sm"></textarea>
            <input v-else v-model="form.credentials[field.key]" class="border rounded px-2 py-1 text-sm" />
          </div>
        </div>

        <div class="flex justify-between mt-5">
          <UButton size="xs" color="gray" variant="soft" :loading="testing" @click="onTest">{{ $t('data.testConnection') }}</UButton>
          <div class="space-x-2">
            <UButton size="xs" color="gray" variant="soft" @click="emit('update:modelValue', false)">{{ $t('data.cancel') }}</UButton>
            <UButton size="xs" color="blue" variant="solid" :loading="saving" @click="onSave">{{ $t('data.save') }}</UButton>
          </div>
        </div>
      </template>

      <div v-if="testResult" class="mt-3 text-xs">
        <span :class="testResult.success ? 'text-green-600' : 'text-red-600'">
          {{ testResult.success ? $t('data.connectedSuccess') : $t('data.connectionFailed') }}
        </span>
        <span v-if="testResult.message" class="text-gray-500"> - {{ testResult.message }}</span>
      </div>
    </div>
  </UModal>
  
</template>

<script lang="ts" setup>
import { computed, watch, ref } from 'vue'

const props = defineProps<{ modelValue: boolean, dataSource: any }>()
const emit = defineEmits(['update:modelValue', 'saved'])

const { t } = useI18n()

const open = computed({
  get: () => props.modelValue,
  set: (v: boolean) => emit('update:modelValue', v)
})

const ds = computed(() => props.dataSource)
// Use nested connection type (Option A architecture)
const connectionType = computed(() => ds.value?.connection?.type || ds.value?.type)
const connectionId = computed(() => ds.value?.connection?.id || ds.value?.connection_id || ds.value?.connections?.[0]?.id)
const authMode = ref<string>('')
const form = ref<{ auth_mode: string, credentials: Record<string, any>, is_primary?: boolean }>({ auth_mode: '', credentials: {}, is_primary: true })
const authOptions = ref<{ label: string, value: string }[]>([])
const fieldsByAuth = ref<Record<string, any>>({})
const credentialFields = computed(() => {
  const schema = fieldsByAuth.value[authMode.value]
  if (!schema) return []
  const props = (schema.properties || {})
  const req = schema.required || []
  return Object.keys(props).map((k) => {
    const f = props[k] || {}
    return {
      key: k,
      title: f.title || k,
      type: f.type || 'string',
      format: f.format,
      required: req.includes(k)
    }
  })
})

watch(open, async (val) => {
  if (val && connectionType.value) {
    await loadFields()
  }
})

async function loadFields() {
  const { data } = await useMyFetch(`/data_sources/${connectionType.value}/fields`, { method: 'GET', query: { auth_policy: 'user_required' } })
  const payload = data.value as any
  const byAuth = (payload?.credentials_by_auth) || {}
  fieldsByAuth.value = byAuth
  catalogOwnership.value = payload?.catalog_ownership || 'shared'
  // build options
  const names = Object.keys((payload?.auth?.by_auth) || {})
  authOptions.value = names.map((n) => ({ label: payload.auth.by_auth[n]?.title || n, value: n }))
  // For per-user catalogs (OneDrive, personal Drive): the user should never
  // see admin app credential fields. Force "oauth" mode if available — it's
  // the only auth that makes sense at the user level.
  const defaultAuth = payload?.auth?.default
  const preferred = (catalogOwnership.value === 'per_user' && names.includes('oauth'))
    ? 'oauth'
    : (defaultAuth && names.includes(defaultAuth)) ? defaultAuth : names[0] || ''
  authMode.value = preferred
  form.value.auth_mode = authMode.value
  form.value.credentials = {}

  // Auto-trigger OAuth for per-user catalogs — the modal becomes purely a
  // loading indicator while we redirect to the provider. No credential form,
  // no "Sign in" button to click. Matches Claude / ChatGPT connector UX.
  if (catalogOwnership.value === 'per_user' && authMode.value === 'oauth' && connectionId.value) {
    onOAuthSignIn()
  }
}

const catalogOwnership = ref<string>('shared')

watch(authMode, (v) => {
  form.value.auth_mode = v || ''
  form.value.credentials = {}
})

const isOAuthMode = computed(() => authMode.value === 'oauth')
const isPerUserCatalog = computed(() => catalogOwnership.value === 'per_user')
const currentAuthTitle = computed(() => {
  const opt = authOptions.value.find(o => o.value === authMode.value)
  return opt?.label || t('data.signIn')
})
const providerName = computed(() => {
  // Friendly provider name for the redirecting-state copy.
  const t = connectionType.value
  if (t === 'onedrive' || t === 'sharepoint') return 'Microsoft'
  if (t === 'google_drive' || t === 'bigquery') return 'Google'
  if (t === 'powerbi') return 'Power BI'
  if (t === 'ms_fabric') return 'Microsoft Fabric'
  return 'the provider'
})
const oauthLoading = ref(false)

async function onOAuthSignIn() {
  if (!connectionId.value) {
    testResult.value = { success: false, message: t('data.noConnectionForSource') }
    return
  }
  try {
    oauthLoading.value = true
    const { data, error } = await useMyFetch(`/connections/${connectionId.value}/oauth/authorize`, { method: 'GET' })
    if (error.value) throw error.value
    const result = data.value as any
    if (result?.authorization_url) {
      window.location.href = result.authorization_url
    }
  } catch (e: any) {
    testResult.value = { success: false, message: e?.message || t('data.oauthStartFailed') }
  } finally {
    oauthLoading.value = false
  }
}

const saving = ref(false)
const testing = ref(false)
const testResult = ref<{ success: boolean, message?: string } | null>(null)

async function onSave() {
  try {
    saving.value = true
    const { error } = await useMyFetch(`/data_sources/${ds.value.id}/my-credentials`, { method: 'POST', body: form.value })
    if (error.value) throw error.value
    emit('saved')
    emit('update:modelValue', false)
  } catch (e) {
    // optionally toast
  } finally {
    saving.value = false
  }
}

async function onTest() {
  try {
    testing.value = true
    const { data, error } = await useMyFetch(`/data_sources/${ds.value.id}/my-credentials/test`, { method: 'POST', body: form.value })
    if (error.value) throw error.value
    testResult.value = data.value as any
  } catch (e: any) {
    testResult.value = { success: false, message: e?.message || t('data.failed') }
  } finally {
    testing.value = false
  }
}

</script>


