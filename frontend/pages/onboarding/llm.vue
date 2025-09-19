<template>
  <div class="min-h-screen bg-gray-50 flex items-center justify-center py-12 px-4">
    <div class="w-full max-w-6xl">
      <OnboardingView forcedStepKey="llm_configured" :hideNextButton="true">
        <template #llm>
          <div class="space-y-6">
            <!-- Providers types (first screen) -->
            <div v-if="!selectedProvider" class="flex flex-col gap-2">
              <div
                v-for="option in providerTypeOptions"
                :key="option.type"
                @click="selectProviderType(option.type)"
                class="flex items-center gap-3 px-3 py-2 border border-gray-200 rounded-md hover:bg-gray-50 cursor-pointer"
              >
                <LLMProviderIcon :icon="true" :provider="option.type" class="w-6 h-6" />
                <span class="text-sm text-gray-800">{{ option.name }}</span>
              </div>
            </div>

            <!-- Back + details header -->
            <div v-else>
              <div class="flex items-center gap-2 mb-2">
                <button type="button" @click="goBackToProviderList" class="flex items-center gap-1 text-sm text-gray-500 hover:text-gray-700">
                  <Icon name="heroicons:chevron-left" class="w-4 h-4" />
                </button>
                <LLMProviderIcon v-if="providerForm.provider_type" :provider="providerForm.provider_type" class="w-5 h-5" />
                <span class="text-sm text-gray-800">{{ selectedProviderDisplayName }}</span>
              </div>

              <!-- Existing provider form -->
              <div v-if="selectedProvider.type !== 'new_provider'" class="space-y-4">
                <div>
                  <label class="text-sm font-medium text-gray-700 mb-2">API Key</label>
                  <input v-model="selectedProvider.credentials.api_key" type="text" placeholder="Keep blank to use stored key" class="mt-2 border border-gray-300 rounded-lg px-4 py-2 w-full h-9 text-sm focus:outline-none focus:border-blue-500" @change="clearTestResult()" />
                </div>

                <div v-if="selectedProvider?.provider_type === 'azure' || selectedProvider?.type === 'azure'">
                  <label class="text-sm font-medium text-gray-700 mb-2">Endpoint URL</label>
                  <input v-model="selectedProvider.credentials.endpoint_url" type="text" placeholder="e.g. https://[resource].openai.azure.com" class="mt-2 border border-gray-300 rounded-lg px-4 py-2 w-full h-9 text-sm focus:outline-none focus:border-blue-500" @change="clearTestResult()" />
                </div>

                <div v-if="selectedProvider?.provider_type === 'openai' || selectedProvider?.type === 'openai'">
                  <div class="mt-1">
                    <button type="button" @click="toggleBaseUrl" class="text-xs text-blue-600 hover:underline">
                      {{ showBaseUrl ? 'Use default base URL' : 'Set custom base URL' }}
                    </button>
                  </div>
                  <div v-if="showBaseUrl" class="mt-2">
                    <label class="text-sm font-medium text-gray-700 mb-2">Base URL (optional)</label>
                    <input v-model="selectedProvider.credentials.base_url" type="text" placeholder="e.g. https://my-openai-proxy.example.com/v1" class="mt-2 border border-gray-300 rounded-lg px-4 py-2 w-full h-9 text-sm focus:outline-none focus:border-blue-500" @change="clearTestResult()" />
                  </div>
                </div>

                <!-- Models -->
          <div>
                  <label class="text-sm font-medium text-gray-700 mb-2">Models</label>
                  <div class="space-y-2">
                    <div v-for="model in selectedProvider.models" :key="model.id" class="flex items-center gap-2 p-2 border border-gray-200 rounded-lg">
                      <UCheckbox v-model="model.is_enabled" @change="clearTestResult()" />
                      <div class="flex-1">
                        <div class="text-sm font-medium text-gray-900">{{ model.name }}</div>
                        <div class="text-xs text-gray-500">Model ID: {{ model.model_id }}</div>
                      </div>
                    </div>

                    <!-- Custom models for existing provider -->
                    <div v-for="(customModel, index) in existingProviderCustomModels" :key="`existing-custom-${index}`" class="flex items-center gap-2 p-2 border border-blue-200 rounded-lg bg-blue-50">
                      <UCheckbox v-model="customModel.is_enabled" @change="clearTestResult()" />
                      <div class="flex-1">
                        <input v-model="customModel.model_id" type="text" placeholder="Model ID" class="text-sm border border-gray-300 rounded px-2 py-1 w-full focus:outline-none focus:border-blue-500" @change="clearTestResult()" />
                      </div>
                      <button type="button" @click="removeExistingProviderCustomModel(index)" class="text-red-500 hover:text-red-700">
                        <Icon name="heroicons:trash" class="w-4 h-4" />
                      </button>
                    </div>

                    <div class="pt-2">
                      <button type="button" @click="addExistingProviderCustomModel" class="text-sm text-blue-500 hover:text-blue-700 underline flex items-center gap-1">
                        <Icon name="heroicons:plus-circle" class="w-4 h-4" />
                        Add Custom Model
                      </button>
                    </div>
                  </div>
                </div>
              </div>

              <!-- New provider form -->
              <div v-else class="space-y-4">
                <!-- Provider selection buttons removed on config screen -->

                <div v-if="providerForm.provider_type">
                  <div class="flex flex-col mb-4">
                    <label class="text-sm font-medium text-gray-700 mb-2">Name</label>
                    <input v-model="providerForm.name" type="text" required :placeholder="`Provider Name (e.g. ${providerForm.provider_type} production)`" class="border border-gray-300 rounded-lg px-4 py-2 w-full h-9 text-sm focus:outline-none focus:border-blue-500" @change="clearTestResult()" />
                  </div>
                  <div v-for="(field, index) in credentialFieldsForNewProvider" :key="field.key">
                    <label class="text-sm font-medium text-gray-700 mb-2 mt-2">{{ field.title }}</label>
                    <input v-model="providerForm.credentials[field.key]" type="text" :required="!!field.required" :placeholder="field.description || ''" class="border border-gray-300 rounded-lg px-4 py-2 w-full h-9 text-sm focus:outline-none focus:border-blue-500" @change="clearTestResult()" />
                  </div>
                  <div v-if="providerForm.provider_type === 'openai'" class="mt-1">
                    <button type="button" @click="toggleBaseUrlNewProvider" class="text-xs text-blue-600 hover:underline">{{ showBaseUrlNew ? 'Use default base URL' : 'Set custom base URL' }}</button>
                    <div v-if="showBaseUrlNew" class="mt-2">
                      <label class="text-sm font-medium text-gray-700 mb-2">Base URL (optional)</label>
                      <input v-model="providerForm.credentials.base_url" type="text" placeholder="e.g. https://my-openai-proxy.example.com/v1" class="border border-gray-300 rounded-lg px-4 py-2 w-full h-9 text-sm focus:outline-none focus:border-blue-500" @change="clearTestResult()" />
                    </div>
                  </div>
                </div>
              </div>

              <!-- Models block for new provider -->
              <div v-if="providerForm.provider_type">
                <label class="text-sm font-medium text-gray-700 mb-2">Models</label>
                <div class="space-y-2">
                  <template v-if="filteredModels.length > 0">
                    <div v-for="model in filteredModels" :key="model.id" class="flex items-center gap-2 p-2 border border-gray-200 rounded-lg">
                      <UCheckbox v-model="model.is_enabled" @change="clearTestResult()" />
                      <div class="flex-1">
                        <div class="text-sm font-medium text-gray-900">{{ model.name }}</div>
                        <div class="text-xs text-gray-500">Model ID: {{ model.model_id }}</div>
                      </div>
                    </div>
                  </template>
                  <template v-else>
                    <div class="text-xs text-gray-500 italic">No preset models available for this provider. Add a custom model below.</div>
                  </template>

                  <!-- Custom Models -->
                  <div v-for="(customModel, index) in customModels" :key="`custom-${index}`" class="flex items-center gap-2 p-2 border border-blue-200 rounded-lg bg-blue-50">
                    <UCheckbox v-model="customModel.is_enabled" @change="clearTestResult()" />
                    <div class="flex-1">
                      <input v-model="customModel.model_id" type="text" placeholder="Model ID" class="text-sm border border-gray-300 rounded px-2 py-1 w-full focus:outline-none focus:border-blue-500" @change="clearTestResult()" />
                    </div>
                    <button type="button" @click="removeCustomModel(index)" class="text-red-500 hover:text-red-700">
                      <Icon name="heroicons:trash" class="w-4 h-4" />
                    </button>
                  </div>

                  <div class="pt-2">
                    <button type="button" @click="addCustomModel" class="text-sm text-blue-500 hover:text-blue-700 underline flex items-center gap-1">
                      <Icon name="heroicons:plus-circle" class="w-4 h-4" />
                      Add Custom Model
                    </button>
                  </div>
                </div>
              </div>

              <!-- Test result message -->
              <div v-if="testResultOk !== null" class="pt-2">
                <div :class="testResultOk ? 'text-green-600' : 'text-red-600'" class="text-xs break-words overflow-hidden">
                  <div class="line-clamp-3 max-w-full">
                    {{ testResultMessage }}
                  </div>
                </div>
              </div>

              <!-- Actions: Test + Save -->
              <div class="flex items-center justify-end gap-2 pt-4">
                <UTooltip text="Regular charges may occur">
                  <UButton variant="soft" color="gray" class="bg-white border border-gray-300 rounded-lg px-3 py-1.5 text-xs hover:bg-gray-50" :disabled="isTestingConnection || !canTestConnection" @click="testConnection" title="Regular charges may occur">
                    <template v-if="isTestingConnection">
                      <Spinner class="w-4 h-4 mr-2 inline-block align-[-0.125em]" />
                      Testing...
                    </template>
                    <template v-else>
                      Test Connection
                    </template>
                  </UButton>
                </UTooltip>
                
                <UTooltip :text="!connectionTestPassed ? 'Pass the connection test first' : ''">
                  <UButton type="button" class="!bg-blue-500 !text-white text-xs py-1.5 px-3" :disabled="isSaving || !providerForm.provider_type || !connectionTestPassed" @click="handleSave">
                    <template v-if="isSaving">
                      <Spinner class="w-4 h-4 mr-2 inline-block align-[-0.125em]" />
                      Saving...
                    </template>
                    <template v-else>
                      Save and Next
                    </template>
                  </UButton>
                </UTooltip>
              </div>
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
import LLMProviderIcon from '@/components/LLMProviderIcon.vue'

const { updateOnboarding } = useOnboarding()
const router = useRouter()
const toast = useToast()

type OrgProvider = {
  id: string;
  name: string;
  provider_type: string;
  additional_config?: any;
  credentials?: any;
  models: any[];
};
type AvailableProvider = { type: string; name: string; credentials?: { properties?: Record<string, { title: string; description?: string }>} };
type AvailableModel = { id?: string; name: string; model_id: string; provider_type: string; is_preset?: boolean; is_enabled?: boolean; selected?: boolean };
type CredentialField = { key: string; title: string; description?: string; required?: boolean };

const providers = ref<AvailableProvider[]>([])
const organizationProviders = ref<OrgProvider[]>([])
const models = ref<AvailableModel[]>([])

const selectedProvider = ref<any | null>(null)
const selectedModel = ref<any | null>(null)
const customModels = ref<{ model_id: string; is_enabled: boolean }[]>([])
const existingProviderCustomModels = ref<{ model_id: string; is_enabled: boolean }[]>([])
const providerForm = ref<{ name: string; provider_type: string; credentials: Record<string, any>; models?: any[]}>({
  name: '',
  provider_type: '',
  credentials: {}
})

const showBaseUrl = ref(false)
const showBaseUrlNew = ref(false)
const isTestingConnection = ref(false)
const isSaving = ref(false)
const connectionTestPassed = ref(false)
const testResultMessage = ref('')
const testResultOk = ref<boolean | null>(null)

onMounted(async () => {
  try {
    const [providersRes, orgProvidersRes, modelsRes] = await Promise.all([
      useMyFetch('/api/llm/available_providers'),
      useMyFetch('/api/llm/providers'),
      useMyFetch('/api/llm/available_models')
    ])
    providers.value = (providersRes.data.value as unknown as AvailableProvider[]) || []
    organizationProviders.value = (orgProvidersRes.data.value as unknown as OrgProvider[]) || []
    models.value = ((modelsRes.data.value as unknown) as AvailableModel[]).map((model: any) => ({
      ...model,
      is_enabled: false
    }))
  } catch (error) {
    console.error('Failed to fetch data:', error)
  }
})

const providersWithNewOption = computed(() => [])
const providerTypeOptions = computed(() => providers.value.map(p => ({ type: p.type, name: p.name })))
const selectedProviderDisplayName = computed(() => {
  const type = providerForm.value.provider_type
  const match = providers.value.find(p => p.type === type)
  return match?.name || type || 'Provider'
})

const isNewProviderSelected = computed(() => selectedProvider.value?.type === 'new_provider')

function selectProviderType(type: string) {
  selectedProvider.value = { type: 'new_provider' }
  providerForm.value.provider_type = type
  // Set default name to the provider type display name
  const provider = providers.value.find(p => p.type === type)
  providerForm.value.name = provider?.name || type
  clearTestResult()
}

function fieldsForProvider(providerType: string): CredentialField[] {
  const provider = providers.value.find(p => p.type === providerType) as any
  const props: Record<string, { title: string; description?: string }> = (provider?.credentials?.properties || {}) as any
  const requiredKeys: string[] = (provider?.credentials?.required || []) as string[]
  return Object.entries(props).map(([key, val]: any) => ({ key, title: val.title, description: val.description, required: requiredKeys?.includes(key) }))
}

const credentialFieldsForNewProvider = computed<CredentialField[]>(() => {
  const providerType = providerForm.value.provider_type
  const all = fieldsForProvider(providerType)
  if (providerType === 'openai') return all.filter(f => f.key !== 'base_url')
  return all
})

function selectOption(option: any) {
  if (option.type === 'new_provider') {
    selectedProvider.value = { type: 'new_provider', name: 'New Provider' } as any
    providerForm.value = { name: '', provider_type: '', credentials: {}, models: [] } as any
    customModels.value = []
    showBaseUrlNew.value = false
  } else {
    selectedProvider.value = option
  }
}

function goBackToProviderList() {
  selectedProvider.value = null
  providerForm.value = { name: '', provider_type: '', credentials: {} } as any
  customModels.value = []
  existingProviderCustomModels.value = []
  showBaseUrl.value = false
  showBaseUrlNew.value = false
}

const filteredModels = computed<AvailableModel[]>(() => {
  const providerType = isNewProviderSelected.value ? providerForm.value.provider_type : selectedProvider.value?.type
  return models.value.filter((model: AvailableModel) => model.provider_type === providerType)
})

const canTestConnection = computed(() => {
  if (selectedProvider.value && selectedProvider.value.type !== 'new_provider') {
    return !!selectedProvider.value.provider_type
  }
  return !!providerForm.value.provider_type && !!providerForm.value.credentials && typeof providerForm.value.credentials.api_key !== 'undefined'
})

watch(() => providerForm.value.provider_type, (providerType: string) => {
  if (isNewProviderSelected.value && providerType) {
    models.value
      .filter((m: AvailableModel) => m.provider_type === providerType)
      .forEach((m: AvailableModel) => { m.is_enabled = true })
  }
  if (isNewProviderSelected.value) {
    if (providerType === 'openai') {
      showBaseUrlNew.value = false
      if (providerForm.value.credentials && 'base_url' in providerForm.value.credentials) {
        delete (providerForm.value.credentials as any).base_url
      }
    } else {
      showBaseUrlNew.value = false
      if (providerForm.value.credentials && 'base_url' in providerForm.value.credentials) {
        delete (providerForm.value.credentials as any).base_url
      }
    }
  }
  clearTestResult()
})

watch(selectedProvider, (newValue) => {
  if (newValue && newValue?.type !== 'new_provider') {
    if (!newValue.credentials) newValue.credentials = { api_key: null } as any
    if ((newValue.provider_type === 'openai' || newValue.type === 'openai')) {
      const existingBaseUrl = (newValue as any)?.additional_config?.base_url
      if (existingBaseUrl && (!newValue.credentials.base_url || newValue.credentials.base_url === '')) {
        (newValue.credentials as any).base_url = existingBaseUrl
      }
      if (newValue.credentials.base_url === undefined) (newValue.credentials as any).base_url = null
      showBaseUrl.value = !!newValue.credentials.base_url
    } else {
      showBaseUrl.value = false
    }
    if ((newValue.provider_type === 'azure' || newValue.type === 'azure')) {
      const existingEndpoint = (newValue as any)?.additional_config?.endpoint_url
      if (existingEndpoint && (!newValue.credentials.endpoint_url || newValue.credentials.endpoint_url === '')) {
        (newValue.credentials as any).endpoint_url = existingEndpoint
      }
      if (newValue.credentials.endpoint_url === undefined) (newValue.credentials as any).endpoint_url = null
    }
    providerForm.value = { name: '', provider_type: '', credentials: {} }
  }
  clearTestResult()
})

function addCustomModel() {
  customModels.value.push({ model_id: '', is_enabled: true })
}
function removeCustomModel(index: number) {
  customModels.value.splice(index, 1)
}
function addExistingProviderCustomModel() {
  existingProviderCustomModels.value.push({ model_id: '', is_enabled: true })
}
function removeExistingProviderCustomModel(index: number) {
  existingProviderCustomModels.value.splice(index, 1)
}
function toggleBaseUrl() {
  showBaseUrl.value = !showBaseUrl.value
  if (!showBaseUrl.value && selectedProvider.value && selectedProvider.value.credentials) {
    (selectedProvider.value.credentials as any).base_url = ''
  }
  clearTestResult()
}
function toggleBaseUrlNewProvider() {
  showBaseUrlNew.value = !showBaseUrlNew.value
  if (!showBaseUrlNew.value) {
    if ('base_url' in providerForm.value.credentials) delete (providerForm.value.credentials as any).base_url
  } else {
    if (providerForm.value.credentials.base_url === undefined) (providerForm.value.credentials as any).base_url = ''
  }
  clearTestResult()
}

async function testConnection() {
  try {
    isTestingConnection.value = true
    let payload: any
    if (selectedProvider.value && selectedProvider.value.type !== 'new_provider') {
      const modelsPayload = (selectedProvider.value.models || []).map((m: any) => ({
        id: m.id,
        name: m.name,
        model_id: m.model_id,
        is_preset: m.is_preset,
        is_custom: m.is_custom,
        is_enabled: m.is_enabled,
        is_default: m.is_default
      }))
      payload = {
        name: selectedProvider.value.name,
        provider_type: selectedProvider.value.provider_type || selectedProvider.value.type,
        credentials: selectedProvider.value.credentials || {},
        models: modelsPayload
      }
    } else {
      const selectedPresetModels = models.value
        .filter((model: any) => model.provider_type === providerForm.value.provider_type && !!model.is_enabled)
        .map((model: any) => ({ model_id: model.model_id, name: model.name, is_custom: false, is_enabled: true, is_preset: true }))
      const selectedCustomModelsPayload = customModels.value
        .filter(model => model.is_enabled && model.model_id.trim() !== '')
        .map(model => ({ model_id: model.model_id, name: model.model_id, is_custom: true, is_enabled: true, is_preset: false }))
      payload = {
        name: providerForm.value.name || `${providerForm.value.provider_type} (temp)`,
        provider_type: providerForm.value.provider_type,
        credentials: providerForm.value.credentials,
        models: [...selectedPresetModels, ...selectedCustomModelsPayload]
      }
    }
    const res = await useMyFetch('/api/llm/test_connection', { method: 'POST', body: payload })
    if (res.status.value === 'success') {
      const data = (res.data as any)?.value as any
      const ok = data?.success
      const msg = data?.message || (ok ? 'Connection successful' : 'Connection failed')
      connectionTestPassed.value = !!ok
      testResultOk.value = !!ok
      testResultMessage.value = truncateMessage(String(msg))
    } else {
      clearTestResult()
      testResultOk.value = false
      testResultMessage.value = truncateMessage(String((res.error as any)?.value || 'Request failed'))
    }
  } catch (e: any) {
    clearTestResult()
    testResultOk.value = false
    testResultMessage.value = truncateMessage(String(e?.message || e || 'Request failed'))
  } finally {
    isTestingConnection.value = false
  }
}

function clearTestResult() {
  connectionTestPassed.value = false
  testResultMessage.value = ''
  testResultOk.value = null
}

function truncateMessage(message: string, maxLength: number = 200): string {
  if (message.length <= maxLength) return message
  return message.substring(0, maxLength) + '...'
}

async function createProvider() {
  const selectedPresetModels = models.value
    .filter((model: AvailableModel) => model.provider_type === providerForm.value.provider_type && !!model.is_enabled)
    .map((model: AvailableModel) => ({ model_id: model.model_id, name: model.name, is_custom: false, is_enabled: true, is_preset: true }))
  const selectedCustomModels = customModels.value
    .filter(model => model.is_enabled)
    .map(model => ({ model_id: model.model_id, name: model.model_id, is_custom: true, is_enabled: true, is_preset: false }))
  providerForm.value.models = [...selectedPresetModels, ...selectedCustomModels]
  const response = await useMyFetch('/api/llm/providers', { method: 'POST', body: providerForm.value })
  return response
}

async function updateProvider() {
  if (!selectedProvider.value) return null
  const newCustomModels = existingProviderCustomModels.value
    .filter((model: { model_id: string; is_enabled: boolean }) => model.is_enabled && model.model_id.trim() !== '')
    .map((model: { model_id: string; is_enabled: boolean }) => ({ model_id: model.model_id, name: model.model_id, is_custom: true, is_enabled: true, is_preset: false }))
  const existingModels = selectedProvider.value.models.map((model: any) => ({ id: model.id, model_id: model.model_id, name: model.name, is_custom: model.is_custom, is_enabled: model.is_enabled, is_preset: model.is_preset }))
  const updatePayload = { name: selectedProvider.value?.name, provider_type: selectedProvider.value?.provider_type, credentials: selectedProvider.value?.credentials, models: [...existingModels, ...newCustomModels] }
  const response = await useMyFetch(`/api/llm/providers/${selectedProvider.value?.id}`, { method: 'PUT', body: updatePayload })
  return response
}

async function handleSave() {
  try {
    isSaving.value = true
    if (isNewProviderSelected.value) {
      const res = await createProvider()
      if (res?.status.value !== 'success') {
        toast.add({ title: 'Error', description: String((res?.error as any)?.value || 'Failed to save provider'), color: 'red' })
        isSaving.value = false
        return
      }
    } else if (selectedProvider.value) {
      const res = await updateProvider()
      if (res?.status.value !== 'success') {
        toast.add({ title: 'Error', description: String((res?.error as any)?.value || 'Failed to update provider'), color: 'red' })
        isSaving.value = false
        return
      }
    }
    await updateOnboarding({ current_step: 'data_source_created' as any })
    router.push('/onboarding/data')
  } finally {
    isSaving.value = false
  }
}

async function skipForNow() { await updateOnboarding({ dismissed: true }); router.push('/') }
</script>


