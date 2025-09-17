<template>
  <div class="min-h-screen bg-gray-50 flex items-center justify-center py-12 px-4">
    <div class="w-full max-w-6xl">
      <OnboardingView forcedStepKey="data_source_created" :hideNextButton="true">
        <template #data>
          <div>
            <div v-if="!selectedDataSource">
              <div class="mt-4 grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-4">
                <button
                  v-for="ds in available_ds"
                  :key="ds.type"
                  type="button"
                  @click="selectDataSource(ds)"
                  class="group rounded-lg p-4 bg-white hover:bg-gray-50 transition-colors w-full"
                >
                  <div class="flex flex-col items-center text-center">
                    <div class="p-2">
                      <DataSourceIcon class="h-8" :type="ds.type" />
                    </div>
                    <div class="text-xs text-gray-500">
                      {{ ds.title }}
                    </div>
                  </div>
                </button>
              </div>
            </div>

            <div v-else class="bg-white rounded-lg border border-gray-200 p-5">
              <div class="flex items-center gap-2 mb-4">
                <button type="button" @click="backToList" class="text-gray-500 hover:text-gray-700">
                  <Icon name="heroicons:chevron-left" class="w-5 h-5" />
                </button>
                <DataSourceIcon :type="selectedDataSource.type" class="h-5" />
                <span class="text-sm text-gray-800">{{ selectedDataSource.title || selectedDataSource.type }}</span>
              </div>

              <form @submit.prevent="handleSubmit" class="space-y-4">
                <div>
                  <label class="text-sm font-medium text-gray-700 mb-2">Name</label>
                  <input v-model="name" type="text" placeholder="Data source name" class="mt-2 border border-gray-300 rounded-lg px-4 py-2 w-full h-9 text-sm focus:outline-none focus:border-blue-500" />
                </div>

                <div v-if="fields.config" class="bg-gray-50 p-4 rounded border">
                  <div class="text-sm font-medium text-gray-700 mb-3">Configuration</div>
                  <div v-for="field in configFields" :key="field.field_name" class="mb-3" @change="clearTestResult()">
                    <label :for="field.field_name" class="block text-sm text-gray-700">{{ field.title || field.field_name }}</label>
                    <input v-if="field.type === 'string'" type="text" v-model="formData.config[field.field_name]" :id="field.field_name" class="mt-1 block w-full px-3 py-2 border border-gray-300 rounded-md focus:outline-none focus:border-blue-500 text-sm" :placeholder="field.title || field.field_name" />
                    <input v-else-if="field.type === 'integer'" type="number" v-model.number="formData.config[field.field_name]" :id="field.field_name" class="mt-1 block w-full px-3 py-2 border border-gray-300 rounded-md focus:outline-none focus:border-blue-500 text-sm" :placeholder="field.title || field.field_name" :min="field.minimum" :max="field.maximum" />
                  </div>
                </div>

                <div v-if="fields.credentials" class="bg-gray-50 p-4 rounded border">
                  <div class="text-sm font-medium text-gray-700 mb-3">Credentials</div>
                  <div v-for="field in credentialFields" :key="field.field_name" class="mb-3" @change="clearTestResult()">
                    <label :for="field.field_name" class="block text-sm text-gray-700">{{ field.title || field.field_name }}</label>
                    <input :type="isPasswordField(field.field_name) ? 'password' : 'text'" v-model="formData.credentials[field.field_name]" :id="field.field_name" class="mt-1 block w-full px-3 py-2 border border-gray-300 rounded-md focus:outline-none focus:border-blue-500 text-sm" :placeholder="field.title || field.field_name" />
                  </div>
                </div>

                <div class="pt-2">
                  <div v-if="testResultOk !== null" class="mb-2">
                    <div :class="testResultOk ? 'text-green-600' : 'text-red-600'" class="text-xs break-words line-clamp-2">
                      {{ testResultMessage }}
                    </div>
                  </div>
                  <div class="flex items-center justify-between gap-2">
                    <UTooltip text="Regular charges may occur">
                      <UButton variant="soft" color="gray" class="bg-white border border-gray-300 rounded-lg px-4 py-2 text-sm hover:bg-gray-50" :disabled="isTestingConnection" @click="testConnection">
                        <template v-if="isTestingConnection">
                          <Spinner />
                          Testing...
                        </template>
                        <template v-else>
                          Test Connection
                        </template>
                      </UButton>
                    </UTooltip>

                    <button type="submit" :disabled="isSubmitting || !connectionTestPassed" class="bg-gray-900 hover:bg-black text-white text-sm font-medium py-2.5 px-5 rounded-lg disabled:opacity-50">
                      <span v-if="isSubmitting">Saving...</span>
                      <span v-else>Save and Continue</span>
                    </button>
                  </div>

                </div>
              </form>
            </div>
          </div>
        </template>
      </OnboardingView>
      <div class="text-center mt-6">
        <button @click="skipForNow" class="text-gray-500 hover:text-gray-700 text-sm">Skip onboarding</button>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
definePageMeta({ auth: true, layout: 'onboarding' })
import OnboardingView from '@/components/onboarding/OnboardingView.vue'
import Spinner from '@/components/Spinner.vue'
const { updateOnboarding } = useOnboarding()
const router = useRouter()
async function skipForNow() { await updateOnboarding({ dismissed: true }); router.push('/') }

const available_ds = ref<any[]>([])
const selectedDataSource = ref<any | null>(null)
const name = ref('')
const fields = ref<{ config: any | null; credentials: any | null }>({ config: null, credentials: null })
const formData = reactive<{ config: Record<string, any>; credentials: Record<string, any> }>({ config: {}, credentials: {} })
const is_public = ref(true)
const isSubmitting = ref(false)
const isTestingConnection = ref(false)
const connectionTestPassed = ref(false)
const testResultMessage = ref('')
const testResultOk = ref<boolean | null>(null)

async function getAvailableDataSources() {
  const { data, error } = await useMyFetch('/available_data_sources', { method: 'GET' })
  if (error.value) {
    throw new Error('Could not fetch available data sources')
  }
  available_ds.value = (data.value as any[]) || []
}

onMounted(async () => {
  nextTick(async () => {
    getAvailableDataSources()
  })
})

function selectDataSource(ds: any) {
  selectedDataSource.value = ds
  name.value = ds.title || ds.type
  fetchFieldsForSelected()
  connectionTestPassed.value = false
  testResultMessage.value = ''
  testResultOk.value = null
}

function backToList() {
  selectedDataSource.value = null
  fields.value = { config: null, credentials: null }
  formData.config = {}
  formData.credentials = {}
  connectionTestPassed.value = false
  testResultMessage.value = ''
  testResultOk.value = null
}

function continueNext() {
  // Placeholder action after configuring the selected data source
  // Keep user on the same step for now
}

const configFields = computed(() => {
  if (!fields.value.config?.properties) return [] as any[]
  return Object.entries(fields.value.config.properties).map(([field_name, schema]: any) => ({ field_name, ...schema }))
})

const credentialFields = computed(() => {
  if (!fields.value.credentials?.properties) return [] as any[]
  return Object.entries(fields.value.credentials.properties).map(([field_name, schema]: any) => ({ field_name, ...schema }))
})

function isPasswordField(fieldName: string) {
  const s = String(fieldName).toLowerCase()
  return s.includes('password') || s.includes('secret') || s.includes('token') || s.includes('key')
}

async function fetchFieldsForSelected() {
  if (!selectedDataSource.value) return
  try {
    const res = await useMyFetch(`/data_sources/${selectedDataSource.value.type}/fields`, { method: 'GET' })
    fields.value = (res.data.value as any) || { config: null, credentials: null }
    initFormDefaults()
  } catch (e) {
    console.error('Failed to fetch fields', e)
    fields.value = { config: null, credentials: null }
  }
}

function initFormDefaults() {
  if (fields.value.config?.properties) {
    Object.entries(fields.value.config.properties).forEach(([field_name, schema]: any) => {
      formData.config[field_name] = schema?.default ?? ''
    })
  }
  if (fields.value.credentials?.properties) {
    Object.entries(fields.value.credentials.properties).forEach(([field_name, schema]: any) => {
      formData.credentials[field_name] = schema?.default ?? ''
    })
  }
}

async function handleSubmit() {
  if (!selectedDataSource.value || isSubmitting.value) return
  isSubmitting.value = true
  try {
    const payload = {
      name: name.value || selectedDataSource.value.type,
      type: selectedDataSource.value.type,
      config: formData.config,
      credentials: formData.credentials,
      is_public: is_public.value
    }
    const response = await useMyFetch('/data_sources', { method: 'POST', body: JSON.stringify(payload), headers: { 'Content-Type': 'application/json' } })
    if (response.status.value === 'success') {
      const created = (response.data as any)?.value as any
      const dsId = created?.id
      await updateOnboarding({ current_step: 'schema_selected' as any })
      navigateTo(dsId ? `/onboarding/data/${dsId}/schema` : '/onboarding/data/schema')
    } else {
      console.error('Failed to create data source', (response.error as any)?.value)
    }
  } finally {
    isSubmitting.value = false
  }
}

async function testConnection() {
  if (!selectedDataSource.value || isTestingConnection.value) return
  isTestingConnection.value = true
  connectionTestPassed.value = false
  try {
    const payload = {
      name: name.value || selectedDataSource.value.type,
      type: selectedDataSource.value.type,
      config: formData.config,
      credentials: formData.credentials,
      is_public: is_public.value
    }
    const res = await useMyFetch('/data_sources/test_connection', { method: 'POST', body: JSON.stringify(payload), headers: { 'Content-Type': 'application/json' } })
    const data = (res.data as any)?.value as any
    const ok = data?.success ?? false
    const msg = data?.message || (ok ? 'Connection successful' : 'Connection failed')
    connectionTestPassed.value = !!ok
    testResultOk.value = !!ok
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
</script>


