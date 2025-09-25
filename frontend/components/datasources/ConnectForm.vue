<template>
  <div class="w-full">
    <div v-if="selectedType" class="bg-white rounded-lg p-4">
      <div class="flex items-center gap-2 mb-3">
        <DataSourceIcon :type="selectedType" class="h-5" />
        <span class="text-sm text-gray-800">{{ selectedTitle }}</span>
      </div>

      <form @submit.prevent="onSubmit" class="space-y-3">
        <div>
          <label class="text-sm font-medium text-gray-700 mb-1 block">Name</label>
          <input v-model="name" type="text" placeholder="Data source name" class="border border-gray-300 rounded-lg px-3 py-1.5 w-full text-sm focus:outline-none focus:border-blue-500" />
        </div>

        <div v-if="fields.config" class="p-3 rounded border">
          <div class="text-sm font-medium text-gray-700 mb-2">Configuration</div>
          <div v-for="field in configFields" :key="field.field_name" class="mb-2" @change="clearTestResult()">
            <label :for="field.field_name" class="block text-xs text-gray-700 mb-1">{{ field.title || field.field_name }}</label>
            <input v-if="field.type === 'string' && field.uiType !== 'textarea' && field.uiType !== 'password'" type="text" v-model="formData.config[field.field_name]" :id="field.field_name" class="block w-full px-3 py-1.5 border border-gray-300 rounded-md focus:outline-none focus:border-blue-500 text-sm" :placeholder="field.title || field.field_name" />
            <input v-else-if="field.type === 'integer' || field.uiType === 'number'" type="number" v-model.number="formData.config[field.field_name]" :id="field.field_name" class="block w-full px-3 py-1.5 border border-gray-300 rounded-md focus:outline-none focus:border-blue-500 text-sm" :placeholder="field.title || field.field_name" :min="field.minimum" :max="field.maximum" />
            <textarea v-else-if="field.uiType === 'textarea'" v-model="formData.config[field.field_name]" :id="field.field_name" class="mt-1 block w-full px-3 py-2 border border-gray-300 rounded-md shadow-sm focus:outline-none focus:ring-blue-500 focus:border-blue-500 sm:text-sm" :placeholder="field.title || field.field_name" rows="3" />
            <input v-else-if="field.uiType === 'password' || field.type === 'password'" type="password" v-model="formData.config[field.field_name]" :id="field.field_name" class="block w-full px-3 py-1.5 border border-gray-300 rounded-md focus:outline-none focus:border-blue-500 text-sm" :placeholder="field.title || field.field_name" />
            <input v-else type="text" v-model="formData.config[field.field_name]" :id="field.field_name" class="block w-full px-3 py-1.5 border border-gray-300 rounded-md focus:outline-none focus:border-blue-500 text-sm" :placeholder="field.title || field.field_name" />
          </div>
        </div>

        <div v-if="true" class="p-3 rounded border">
          <div class="text-sm font-medium text-gray-700 mb-2">Credentials</div>
          <div v-if="authOptions.length" class="w-48 mb-2">
            <USelectMenu v-if="authOptions.length > 1" v-model="selectedAuth" :options="authOptions" option-attribute="label" value-attribute="value" @change="handleAuthChange" />
          </div>
          <div v-if="auth_policy === 'user_required'" class="text-xs text-gray-600 bg-white border border-dashed rounded p-3 mb-2">
            Users will connect their own credentials after creation.
          </div>
          <div v-if="showSystemCredentialFields" v-for="field in credentialFields" :key="field.field_name" class="mb-2" @change="clearTestResult()">
            <label :for="field.field_name" class="block text-xs text-gray-700 mb-1">{{ field.title || field.field_name }}</label>
            <input :type="isPasswordField(field.field_name) ? 'password' : (field.uiType === 'number' ? 'number' : 'text')" v-model="formData.credentials[field.field_name]" :id="field.field_name" class="block w-full px-3 py-1.5 border border-gray-300 rounded-md focus:outline-none focus:border-blue-500 text-sm" :placeholder="field.title || field.field_name" />
          </div>
        </div>

        <div class="pt-1">
          <div class="flex items-center gap-2 mb-2">
            <UToggle color="blue" v-model="generate_summary" />
            <span class="text-xs text-gray-700">Use LLM to onboard data source (runs after schema save)</span>
          </div>
          <div v-if="testResultOk !== null" class="mb-2">
            <div :class="testResultOk ? 'text-green-600' : 'text-red-600'" class="text-xs break-words line-clamp-2">
              {{ testResultMessage }}
            </div>
          </div>
          <div class="flex items-center justify-end gap-2 mt-3">
            <UTooltip text="Regular charges may occur">
              <UButton variant="soft" color="gray" class="bg-white border border-gray-300 rounded-lg px-3 py-1.5 text-xs hover:bg-gray-50" :disabled="isTestingConnection" @click="testConnection">
                <template v-if="isTestingConnection">
                  <Spinner />
                  Testing...
                </template>
                <template v-else>
                  Test Connection
                </template>
              </UButton>
            </UTooltip>

            <UTooltip :text="!connectionTestPassed ? 'Pass the connection test first' : ''">
              <button type="submit" :disabled="submitting || !connectionTestPassed" class="bg-blue-500 hover:bg-blue-600 text-white text-xs font-medium py-1.5 px-3 rounded disabled:opacity-50">
                <span v-if="submitting">Saving...</span>
                <span v-else>Save and Continue</span>
              </button>
            </UTooltip>
          </div>
        </div>
      </form>
    </div>
  </div>
  
</template>

<script setup lang="ts">
import Spinner from '@/components/Spinner.vue'
function selectProvider(ds: any) {
  selectedType.value = String(ds?.type || '')
  name.value = ds?.title || ds?.type || ''
  handleTypeChange()
}
const props = defineProps<{ mode?: 'onboarding'|'create'|'edit', initialType?: string }>()
const emit = defineEmits<{ (e: 'submitted', payload: any): void; (e: 'success', dataSource: any): void; (e: 'change:type', type: string): void; (e: 'change:auth', authType: string | null): void }>()

const toast = useToast()
const route = useRoute()

const available_ds = ref<any[]>([])
const selectedType = ref<string>(String(props.initialType || (typeof route.query.type === 'string' ? route.query.type : '')))
const name = ref('')
const fields = ref<any>({ config: null, credentials: null, auth: null, credentials_by_auth: null })
const formData = reactive<{ config: Record<string, any>; credentials: Record<string, any> }>({ config: {}, credentials: {} })
const selectedAuth = ref<string | undefined>(undefined)
const is_public = ref(true)
const require_user_auth = ref(false)
const generate_summary = ref(true)
const generate_conversation_starters = ref(true)
const generate_ai_rules = ref(true)
const submitting = ref(false)
const isTestingConnection = ref(false)
const connectionTestPassed = ref(false)
const testResultMessage = ref('')
const testResultOk = ref<boolean | null>(null)

const auth_policy = computed(() => (require_user_auth.value ? 'user_required' : 'system_only'))

const typeOptions = computed(() => available_ds.value || [])

const configFields = computed(() => {
  if (!fields.value?.config?.properties) return [] as any[]
  return Object.entries(fields.value.config.properties).map(([field_name, schema]: any) => ({ field_name, ...schema }))
})

const authOptions = computed(() => {
  const authMeta = fields.value?.auth
  if (!authMeta) return [] as Array<{ label: string; value: string }>
  const opts: Array<{ label: string; value: string }> = []
  const byAuth = authMeta.by_auth || {}
  for (const key of Object.keys(byAuth)) {
    const label = (byAuth[key]?.title as string) || key
    opts.push({ label, value: key })
  }
  return opts
})

const showSystemCredentialFields = computed(() => auth_policy.value !== 'user_required')

const credentialFields = computed(() => {
  const byAuth = fields.value?.credentials_by_auth
  const active = byAuth && selectedAuth.value ? byAuth[selectedAuth.value] : null
  const credsSchema = active || fields.value?.credentials
  if (!credsSchema?.properties) return [] as any[]
  return Object.entries(credsSchema.properties).map(([field_name, schema]: any) => ({ field_name, ...schema }))
})

const selectedTitle = computed(() => {
  const match = (available_ds.value || []).find((x: any) => String(x.type) === String(selectedType.value))
  return match?.title || selectedType.value
})

function isPasswordField(fieldName: string) {
  const s = String(fieldName).toLowerCase()
  return s.includes('password') || s.includes('secret') || s.includes('token') || s.includes('key')
}

async function fetchAvailable() {
  const res = await useMyFetch('/available_data_sources', { method: 'GET' })
  available_ds.value = (res.data as any)?.value || []
  if (!selectedType.value && available_ds.value.length) selectedType.value = String(available_ds.value[0]?.type || '')
  if (selectedType.value) await fetchFields()
}

async function fetchFields() {
  if (!selectedType.value) return
  try {
    const res = await useMyFetch(`/data_sources/${selectedType.value}/fields?auth_policy=${auth_policy.value}` as any, { method: 'GET' })
    fields.value = (res.data as any)?.value || { config: null, credentials: null }
    // set default auth
    const authMeta = fields.value?.auth
    if (authMeta && !selectedAuth.value) selectedAuth.value = authMeta.default || undefined
    initFormDefaults()
    emit('change:type', selectedType.value)
  } catch (e) {
    fields.value = { config: null, credentials: null }
  }
}

function initFormDefaults() {
  formData.config = {} as any
  formData.credentials = {} as any
  if (fields.value?.config?.properties) {
    Object.entries(fields.value.config.properties).forEach(([k, v]: any) => { (formData.config as any)[k] = v?.default ?? '' })
  }
  const byAuth = fields.value?.credentials_by_auth
  const active = byAuth && selectedAuth.value ? byAuth[selectedAuth.value] : null
  const credsSchema = active || fields.value?.credentials
  if (credsSchema?.properties) {
    Object.entries(credsSchema.properties).forEach(([k, v]: any) => {
      const t = v?.type
      if (t === 'boolean') (formData.credentials as any)[k] = typeof v.default === 'boolean' ? v.default : false
      else if (t === 'integer' || v?.['ui:type'] === 'number') (formData.credentials as any)[k] = typeof v.default === 'number' ? v.default : undefined
      else (formData.credentials as any)[k] = v?.default ?? ''
    })
  }
}

function handleTypeChange() {
  fields.value = { config: null, credentials: null, auth: null, credentials_by_auth: null }
  selectedAuth.value = undefined
  name.value = selectedType.value || ''
  fetchFields()
}

function handleAuthChange() {
  formData.credentials = {} as any
  initFormDefaults()
  emit('change:auth', selectedAuth.value ?? null)
}

const canSubmit = computed(() => !!selectedType.value && !submitting.value)

async function onSubmit() {
  if (submitting.value || !selectedType.value) return
  submitting.value = true
  try {
    const payload: any = {
      name: name.value || selectedType.value,
      type: selectedType.value,
      config: { ...formData.config, auth_type: selectedAuth.value || undefined },
      credentials: showSystemCredentialFields.value ? formData.credentials : {},
      is_public: is_public.value,
      auth_policy: auth_policy.value,
      generate_summary: generate_summary.value,
      generate_conversation_starters: generate_conversation_starters.value,
      generate_ai_rules: generate_ai_rules.value
    }
    emit('submitted', payload)
    const res = await useMyFetch('/data_sources', { method: 'POST', body: JSON.stringify(payload), headers: { 'Content-Type': 'application/json' } })
    if ((res.status as any)?.value === 'success') {
      const created = (res.data as any)?.value
      emit('success', created)
    } else {
      const detail = (res.error as any)?.value?.data?.detail || 'Failed to create data source'
      toast.add({ title: 'Failed to create data source', description: String(detail), icon: 'i-heroicons-x-circle', color: 'red' })
    }
  } catch (e: any) {
    toast.add({ title: 'Error', description: e?.message || 'Unexpected error', icon: 'i-heroicons-x-circle', color: 'red' })
  } finally {
    submitting.value = false
  }
}

async function testConnection() {
  if (!selectedType.value || isTestingConnection.value) return
  isTestingConnection.value = true
  connectionTestPassed.value = false
  try {
    const payload = {
      name: name.value || selectedType.value,
      type: selectedType.value,
      config: formData.config,
      credentials: showSystemCredentialFields.value ? formData.credentials : {},
      is_public: is_public.value
    }
    const res = await useMyFetch('/data_sources/test_connection', { method: 'POST', body: JSON.stringify(payload), headers: { 'Content-Type': 'application/json' } })
    const data: any = (res.data as any)?.value
    const ok = !!(data?.success)
    const msg = data?.message || (ok ? 'Connection successful' : 'Connection failed')
    connectionTestPassed.value = ok
    testResultOk.value = ok
    testResultMessage.value = String(msg)
  } catch (e) {
    connectionTestPassed.value = false
    testResultOk.value = false
    testResultMessage.value = 'Request failed'
  } finally {
    isTestingConnection.value = false
  }
}

function clearTestResult() {
  connectionTestPassed.value = false
  testResultMessage.value = ''
  testResultOk.value = null
}

onMounted(() => { fetchAvailable() })
</script>

<style scoped>
</style>


