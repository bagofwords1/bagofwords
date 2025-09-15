<template>
    <UModal v-model="providerModalOpen" :ui="{ width: 'sm:max-w-xl' }">
        <div class="p-4 relative">
            <button @click="providerModalOpen = false" class="absolute top-2 right-2 text-gray-500 hover:text-gray-700">
                <Icon name="heroicons:x-mark" class="w-5 h-5" />
            </button>
            <h1 class="text-lg font-semibold">Integrate Models</h1>
            <p class="text-sm text-gray-500">Configure and manage LLM models and providers</p>
            <hr class="my-4" />

            <form @submit.prevent class="space-y-4">
                <!-- Providers list (always open) -->
                <div v-if="!selectedProvider" class="flex flex-col gap-2">
                    <div
                        v-for="option in providersWithNewOption"
                        :key="option.name"
                        @click="selectOption(option)"
                        class="flex items-center gap-3 px-3 py-2 border border-gray-200 rounded-md hover:bg-gray-50 cursor-pointer"
                    >
                        <Icon v-if="option.type === 'new_provider'" name="heroicons:plus-circle" class="w-5 h-5 text-blue-600" />
                        <LLMProviderIcon v-else :icon="true" :provider="option.type" class="w-6 h-6" />
                        <span class="text-sm" :class="option.type === 'new_provider' ? 'text-blue-600' : 'text-gray-800'">{{ option.name }}</span>
                    </div>
                </div>

                <!-- Back to providers + details -->
                <div v-if="selectedProvider">
                    <div class="flex items-center gap-2 mb-2">
                        <button type="button" @click="goBackToProviderList" class="flex items-center gap-1 text-sm text-gray-500 hover:text-gray-700">
                            <Icon name="heroicons:chevron-left" class="w-4 h-4" />
                            Providers
                        </button>
                        <span class="text-sm text-gray-800">/
                            <span v-if="selectedProvider.type !== 'new_provider'">{{ selectedProvider.name }}</span>
                            <span v-else>New Provider</span>
                        </span>
                    </div>
                    <div v-if="selectedProvider.type !== 'new_provider'" class="space-y-4">
                        <div class="">
                            <label class="text-sm font-medium text-gray-700 mb-2">
                                API Key
                            </label>
                            <input 
                                v-model="selectedProvider.credentials.api_key" 
                                type="text" 
                                placeholder="Keep blank to use stored key"
                                class="mt-2 border border-gray-300 rounded-lg px-4 py-2 w-full h-9 text-sm focus:outline-none focus:border-blue-500" 
                            />
                        </div>
                        <div class="" v-if="selectedProvider?.provider_type === 'azure' || selectedProvider?.type === 'azure'">
                            <label class="text-sm font-medium text-gray-700 mb-2">
                                Endpoint URL
                            </label>
                            <input 
                                v-model="selectedProvider.credentials.endpoint_url" 
                                type="text" 
                                placeholder="e.g. https://<resource>.openai.azure.com"
                                class="mt-2 border border-gray-300 rounded-lg px-4 py-2 w-full h-9 text-sm focus:outline-none focus:border-blue-500" 
                            />
                        </div>
                        <div class="" v-if="selectedProvider?.provider_type === 'openai' || selectedProvider?.type === 'openai'">
                            <div class="mt-1">
                                <button type="button" @click="toggleBaseUrl" class="text-xs text-blue-600 hover:underline">
                                    {{ showBaseUrl ? 'Use default base URL' : 'Set custom base URL' }}
                                </button>
                            </div>
                            <div v-if="showBaseUrl" class="mt-2">
                                <label class="text-sm font-medium text-gray-700 mb-2">
                                    Base URL (optional)
                                </label>
                                <input 
                                    v-model="selectedProvider.credentials.base_url" 
                                    type="text" 
                                    placeholder="e.g. https://my-openai-proxy.example.com/v1"
                                    class="mt-2 border border-gray-300 rounded-lg px-4 py-2 w-full h-9 text-sm focus:outline-none focus:border-blue-500" 
                                />
                            </div>
                        </div>
                        <div class="">
                            <label class="text-sm font-medium text-gray-700 mb-2">
                                Models
                            </label>
                            <div class="space-y-2">
                                <!-- Existing Models -->
                                <div v-for="model in selectedProvider.models" :key="model.id" class="flex items-center gap-2 p-2 border border-gray-200 rounded-lg">
                                    <UCheckbox v-model="model.is_enabled" />
                                    <div class="flex-1">
                                        <div class="text-sm font-medium text-gray-900">{{ model.name }}</div>
                                        <div class="text-xs text-gray-500">Model ID: {{ model.model_id }}</div>
                                    </div>
                                    <button 
                                        v-if="model.is_custom"
                                        type="button"
                                        @click="removeExistingCustomModel(model.id)"
                                        class="text-red-500 hover:text-red-700 hidden"
                                    >
                                        <Icon name="heroicons:trash" class="w-4 h-4" />
                                    </button>
                                </div>
                                
                                <!-- Custom Models for existing provider -->
                                <div v-for="(customModel, index) in existingProviderCustomModels" :key="`existing-custom-${index}`" class="flex items-center gap-2 p-2 border border-blue-200 rounded-lg bg-blue-50">
                                    <UCheckbox v-model="customModel.is_enabled" />
                                    <div class="flex-1">
                                        <input 
                                            v-model="customModel.model_id" 
                                            type="text" 
                                            placeholder="Model ID"
                                            class="text-sm border border-gray-300 rounded px-2 py-1 w-full focus:outline-none focus:border-blue-500"
                                        />
                                    </div>
                                    <button 
                                        type="button"
                                        @click="removeExistingProviderCustomModel(index)"
                                        class="text-red-500 hover:text-red-700"
                                    >
                                        <Icon name="heroicons:trash" class="w-4 h-4" />
                                    </button>
                                </div>
                                
                                <!-- Add Custom Model Button for existing provider -->
                                <div class="pt-2">
                                    <button 
                                        type="button"
                                        @click="addExistingProviderCustomModel"
                                        class="text-sm text-blue-500 hover:text-blue-700 underline flex items-center gap-1"
                                    >
                                        <Icon name="heroicons:plus-circle" class="w-4 h-4" />
                                        Add Custom Model
                                    </button>
                                </div>
                            </div>
                        </div>
                        <div class="" v-if="selectedProvider?.type !== 'new_provider'">
                            <div>
                                <button 
                                    type="button"
                                    class="flex items-center gap-2 text-sm font-medium text-gray-700 mb-2"
                                    @click="showDangerZone = !showDangerZone" >
                                    <span class="transform transition-transform mt-1" :class="{ 'rotate-90': showDangerZone }">
                                        <Icon name="heroicons:chevron-right" class="w-3" />
                                    </span>
                                    Danger Zone
                                </button>
                                <div v-if="showDangerZone" class="mt-2">
                                    <UButton 
                                        type="button"
                                        color="red" 
                                        variant="soft"
                                        class="inline-block"
                                        @click="deleteProvider(selectedProvider.id)"
                                    >
                                        Delete Provider
                                    </UButton>
                                </div>
                            </div>
                        </div>
                    </div>

                    <div v-else class="space-y-4">
                        <div class="flex flex-col">
                            <div class="flex flex-row gap-4 mt-2">
                                <button v-for="provider in providers" 
                                    @click="providerForm.provider_type = provider.type" 
                                    :key="provider.type" 
                                    class="bg-gray-50 hover:bg-gray-100 border border-gray-300 p-4 rounded-lg flex flex-col items-center justify-center min-w-[120px]"
                                    type="button"
                                    :class="{ '!border-blue-500 border-2 bg-white': providerForm.provider_type === provider.type }"
                                >
                                    <LLMProviderIcon :provider="provider.type" class="w-16" />
                                </button>
                            </div>
                        </div>

                        <div v-if="providerForm.provider_type">
                            <div class="flex flex-col mb-4">
                                <label class="text-sm font-medium text-gray-700 mb-2">Name</label>
                                <input v-model="providerForm.name" type="text" required 
                                    :placeholder="`Provider Name (e.g. ${providerForm.provider_type} production)`"
                                    class="border border-gray-300 rounded-lg px-4 py-2 w-full h-9 text-sm focus:outline-none focus:border-blue-500" />
                            </div>
                            <div v-for="(field, index) in credentialFieldsForNewProvider" :key="field.key">
                                <label class="text-sm font-medium text-gray-700 mb-2 mt-2">{{ field.title }}</label>
                                <input v-model="providerForm.credentials[field.key]" type="text" :required="!!field.required"
                                    :placeholder="field.description || ''"
                                    class="border border-gray-300 rounded-lg px-4 py-2 w-full h-9 text-sm focus:outline-none focus:border-blue-500" />
                            </div>
                            <div v-if="providerForm.provider_type === 'openai'" class="mt-1">
                                <button type="button" @click="toggleBaseUrlNewProvider" class="text-xs text-blue-600 hover:underline">
                                    {{ showBaseUrlNew ? 'Use default base URL' : 'Set custom base URL' }}
                                </button>
                                <div v-if="showBaseUrlNew" class="mt-2">
                                    <label class="text-sm font-medium text-gray-700 mb-2">Base URL (optional)</label>
                                    <input v-model="providerForm.credentials.base_url" type="text"
                                        placeholder="e.g. https://my-openai-proxy.example.com/v1"
                                        class="border border-gray-300 rounded-lg px-4 py-2 w-full h-9 text-sm focus:outline-none focus:border-blue-500" />
                                </div>
                            </div>
                        </div>
                    </div>
                </div>

                                        <div v-if="providerForm.provider_type">
                            <label class="text-sm font-medium text-gray-700 mb-2">
                                Models
                            </label>
                            <div class="space-y-2">
                                <!-- Preset Models (if any) -->
                                <template v-if="filteredModels.length > 0">
                                    <div v-for="model in filteredModels" :key="model.id" class="flex items-center gap-2 p-2 border border-gray-200 rounded-lg">
                                        <UCheckbox v-model="model.is_enabled" />
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
                                    <UCheckbox v-model="customModel.is_enabled" />
                                    <div class="flex-1">
                                        <input 
                                            v-model="customModel.model_id" 
                                            type="text" 
                                            placeholder="Model ID"
                                            class="text-sm border border-gray-300 rounded px-2 py-1 w-full focus:outline-none focus:border-blue-500"
                                        />
                                    </div>
                                    <button 
                                        type="button"
                                        @click="removeCustomModel(index)"
                                        class="text-red-500 hover:text-red-700"
                                    >
                                        <Icon name="heroicons:trash" class="w-4 h-4" />
                                    </button>
                                </div>
                                
                                <!-- Add Custom Model Button -->
                                <div class="pt-2">
                                    <button 
                                        type="button"
                                        @click="addCustomModel"
                                        class="text-sm text-blue-500 hover:text-blue-700 underline flex items-center gap-1"
                                    >
                                        <Icon name="heroicons:plus-circle" class="w-4 h-4" />
                                        Add Custom Model
                                    </button>
                                </div>
                            </div>
                        </div>

                <div class="flex items-center pt-4">
                    <div v-if="isNewProviderSelected && providerForm.provider_type">
                        <UTooltip text="Regular charges may occur">
                            <UButton 
                                variant="soft" 
                                color="gray"
                                class="bg-white border border-gray-300 rounded-lg px-4 py-2 text-sm hover:bg-gray-50 mr-2"
                                :disabled="isTestingConnection || !canTestConnection"
                                @click="testConnection"
                                title="Regular charges may occur"
                            >
                                <template v-if="isTestingConnection">
                                    <Spinner class="w-4 h-4 mr-2 inline-block align-[-0.125em]" />
                                    Testing...
                                </template>
                                <template v-else>
                                    Test Connection
                                </template>
                            </UButton>
                        </UTooltip>
                    </div>
                    <div class="ml-auto space-x-2">
                        <UButton label="Cancel" color="gray" variant="soft" @click="providerModalOpen = false" />
                        <UButton 
                            type="submit" 
                            :label="selectedProvider?.type === 'new_provider' ? 'Save Provider' : 'Update Provider'"  
                            class="!bg-blue-500 !text-white" 
                            @click="selectedProvider?.type === 'new_provider' ? createProvider() : updateProvider()"
                        />
                    </div>
                </div>
            </form>
        </div>
    </UModal>
</template>

<script setup lang="ts">
import { ref, computed, watch, onMounted } from 'vue';
import Spinner from './Spinner.vue';

const props = defineProps<{
    modelValue: boolean;
    editProviderId?: string | null;
}>();

const emit = defineEmits(['update:modelValue']);

const toast = useToast();

const showDangerZone = ref(false);

const providerModalOpen = computed({
    get: () => props.modelValue,
    set: (value) => emit('update:modelValue', value)
});

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
const providers = ref<AvailableProvider[]>([]);
const organizationProviders = ref<OrgProvider[]>([]);
const models = ref<AvailableModel[]>([]);

onMounted(async () => {
  try {
    const [providersRes, orgProvidersRes, modelsRes] = await Promise.all([
      useMyFetch('/api/llm/available_providers'),
      useMyFetch('/api/llm/providers'),
      useMyFetch('/api/llm/available_models')
    ]);
    
    providers.value = (providersRes.data.value as unknown as AvailableProvider[]) || [];
    organizationProviders.value = (orgProvidersRes.data.value as unknown as OrgProvider[]) || [];
    models.value = ((modelsRes.data.value as unknown) as AvailableModel[]).map((model: any) => ({
      ...model,
      is_enabled: false
    }));
  } catch (error) {
    console.error('Failed to fetch data:', error);
  }
});

const selectedProvider = ref<any | null>(null);
const selectedModel = ref<any | null>(null);
const customModels = ref<{ model_id: string; is_enabled: boolean }[]>([]);
const existingProviderCustomModels = ref<{ model_id: string; is_enabled: boolean }[]>([]);
const providerForm = ref<{ name: string; provider_type: string; credentials: Record<string, any>; models?: any[]}>({
    name: '',
    provider_type: '',
    credentials: {}
});

const providersWithNewOption = computed(() => {
    return [
        ...(organizationProviders.value || []).map(p => ({
            ...p,
            type: p.provider_type
        })), 
        { name: 'New Provider', type: 'new_provider' }
    ];
});

const showBaseUrl = ref(false);
const showBaseUrlNew = ref(false);
const isTestingConnection = ref(false);
const canTestConnection = computed(() => {
    if (selectedProvider.value && selectedProvider.value.type !== 'new_provider') {
        // Existing provider: must have provider_type and some credential (api_key may be blank to use stored)
        return !!selectedProvider.value.provider_type;
    }
    // New provider form: need type and at least api_key
    return !!providerForm.value.provider_type && !!providerForm.value.credentials && typeof providerForm.value.credentials.api_key !== 'undefined';
});

function fieldsForProvider(providerType: string): CredentialField[] {
    const provider = providers.value.find(p => p.type === providerType) as any;
    const props: Record<string, { title: string; description?: string }> = (provider?.credentials?.properties || {}) as any;
    const requiredKeys: string[] = (provider?.credentials?.required || []) as string[];
    return Object.entries(props).map(([key, val]: any) => ({ key, title: val.title, description: val.description, required: requiredKeys?.includes(key) }));
}

const credentialFieldsForNewProvider = computed<CredentialField[]>(() => {
    const providerType = providerForm.value.provider_type;
    const all = fieldsForProvider(providerType);
    if (providerType === 'openai') {
        return all.filter(f => f.key !== 'base_url');
    }
    return all;
});

  function selectOption(option: any) {
    if (option.type === 'new_provider') {
      selectedProvider.value = { type: 'new_provider', name: 'New Provider' } as any;
      providerForm.value = { name: '', provider_type: '', credentials: {}, models: [] } as any;
      customModels.value = [];
      showBaseUrlNew.value = false;
    } else {
      selectedProvider.value = option;
    }
  }

  function goBackToProviderList() {
    selectedProvider.value = null;
    providerForm.value = { name: '', provider_type: '', credentials: {} } as any;
    customModels.value = [];
    existingProviderCustomModels.value = [];
    showDangerZone.value = false;
    showBaseUrl.value = false;
    showBaseUrlNew.value = false;
  }

const isNewProviderSelected = computed(() => {
    return selectedProvider.value?.type === 'new_provider';
});

// fieldsForProvider moved above to include required keys

const filteredModels = computed<AvailableModel[]>(() => {
    const providerType = isNewProviderSelected.value 
        ? providerForm.value.provider_type 
        : selectedProvider.value?.type;
    return models.value.filter((model: AvailableModel) => model.provider_type === providerType);
});

const resetForm = () => {
    selectedProvider.value = null;
    selectedModel.value = null;
    customModels.value = [];
    providerForm.value = {
        name: '',
        provider_type: '',
        credentials: {}
    };
    showDangerZone.value = false;
    // Reset any selected models
    models.value.forEach((model: any) => {
        model.selected = false;
        model.is_enabled = false;
    });
};

watch(providerModalOpen, (newValue) => {
    if (!newValue) {
        resetForm();
    }
    if (newValue && props.editProviderId) {
        // Select provider for editing
        const provider = (organizationProviders.value || []).find((p: OrgProvider) => p.id === props.editProviderId);
        if (provider) {
            selectedProvider.value = {
                ...provider,
                type: provider.provider_type
            };
            showDangerZone.value = false;
            existingProviderCustomModels.value = [];
            // Initialize credentials container and base_url toggle for OpenAI
            if (!selectedProvider.value.credentials) {
                selectedProvider.value.credentials = { api_key: null } as any;
            }
            if ((selectedProvider.value.provider_type === 'openai' || selectedProvider.value.type === 'openai')) {
                // Hydrate base_url from additional_config if present
                const existingBaseUrl = selectedProvider.value.additional_config?.base_url;
                if (existingBaseUrl && (!selectedProvider.value.credentials.base_url || selectedProvider.value.credentials.base_url === '')) {
                    (selectedProvider.value.credentials as any).base_url = existingBaseUrl;
                }
                if (selectedProvider.value.credentials.base_url === undefined) {
                    (selectedProvider.value.credentials as any).base_url = null;
                }
                showBaseUrl.value = !!selectedProvider.value.credentials.base_url;
            } else {
                showBaseUrl.value = false;
            }
            // Hydrate Azure endpoint_url for edit
            if ((selectedProvider.value.provider_type === 'azure' || selectedProvider.value.type === 'azure')) {
                const existingEndpoint = selectedProvider.value.additional_config?.endpoint_url;
                if (existingEndpoint && (!selectedProvider.value.credentials.endpoint_url || selectedProvider.value.credentials.endpoint_url === '')) {
                    (selectedProvider.value.credentials as any).endpoint_url = existingEndpoint;
                }
                if (selectedProvider.value.credentials.endpoint_url === undefined) {
                    (selectedProvider.value.credentials as any).endpoint_url = null;
                }
            }
        }
    }
});

watch(() => props.editProviderId, (newId) => {
    if (providerModalOpen.value && newId) {
        const provider = (organizationProviders.value || []).find((p: OrgProvider) => p.id === newId);
        if (provider) {
            selectedProvider.value = {
                ...provider,
                type: provider.provider_type
            };
            showDangerZone.value = false;
            existingProviderCustomModels.value = [];
            if (!selectedProvider.value.credentials) {
                selectedProvider.value.credentials = { api_key: null } as any;
            }
            if ((selectedProvider.value.provider_type === 'openai' || selectedProvider.value.type === 'openai')) {
                const existingBaseUrl = selectedProvider.value.additional_config?.base_url;
                if (existingBaseUrl && (!selectedProvider.value.credentials.base_url || selectedProvider.value.credentials.base_url === '')) {
                    (selectedProvider.value.credentials as any).base_url = existingBaseUrl;
                }
                if (selectedProvider.value.credentials.base_url === undefined) {
                    (selectedProvider.value.credentials as any).base_url = null;
                }
                showBaseUrl.value = !!selectedProvider.value.credentials.base_url;
            } else {
                showBaseUrl.value = false;
            }
            // Hydrate Azure endpoint_url for edit
            if ((selectedProvider.value.provider_type === 'azure' || selectedProvider.value.type === 'azure')) {
                const existingEndpoint = selectedProvider.value.additional_config?.endpoint_url;
                if (existingEndpoint && (!selectedProvider.value.credentials.endpoint_url || selectedProvider.value.credentials.endpoint_url === '')) {
                    (selectedProvider.value.credentials as any).endpoint_url = existingEndpoint;
                }
                if (selectedProvider.value.credentials.endpoint_url === undefined) {
                    (selectedProvider.value.credentials as any).endpoint_url = null;
                }
            }
        }
    }
});

// When creating a new provider and choosing a type, default-enable all preset models for that provider.
watch(() => providerForm.value.provider_type, (providerType: string) => {
    if (isNewProviderSelected.value && providerType) {
        models.value
            .filter((m: AvailableModel) => m.provider_type === providerType)
            .forEach((m: AvailableModel) => { m.is_enabled = true; });
    }
    // Reset base URL toggle for new provider on provider type changes
    if (isNewProviderSelected.value) {
        if (providerType === 'openai') {
            showBaseUrlNew.value = false;
            // ensure we don't carry over base_url unless toggled on
            if (providerForm.value.credentials && 'base_url' in providerForm.value.credentials) {
                delete (providerForm.value.credentials as any).base_url;
            }
        } else {
            showBaseUrlNew.value = false;
            if (providerForm.value.credentials && 'base_url' in providerForm.value.credentials) {
                delete (providerForm.value.credentials as any).base_url;
            }
        }
    }
});

async function createProvider() {
    // Gather selected preset models
    const selectedPresetModels = models.value
        .filter((model: AvailableModel) => model.provider_type === providerForm.value.provider_type && !!model.is_enabled)
        .map((model: AvailableModel) => ({
            model_id: model.model_id,
            name: model.name,
            is_custom: false,
            is_enabled: true,
            is_preset: true
        }));

    // Gather selected custom models
    const selectedCustomModels = customModels.value
        .filter(model => model.is_enabled)
        .map(model => ({
            model_id: model.model_id,
            name: model.model_id, // Use model_id as the name for custom models
            is_custom: true,
            is_enabled: true,
            is_preset: false
        }));

    // Combine all selected models
    providerForm.value.models = [...selectedPresetModels, ...selectedCustomModels];

    const response = await useMyFetch('/api/llm/providers', {
        method: 'POST',
        body: providerForm.value
    }).then(async response => {
        if (response.status.value === 'success') {
            // Refresh providers list so future edits reflect latest data
            const orgProvidersRes = await useMyFetch('/api/llm/providers');
            organizationProviders.value = (orgProvidersRes.data.value as unknown as OrgProvider[]) || [];
            resetForm();
            providerModalOpen.value = false;
            toast.add({
                title: 'Success',
                description: `Provider ${providerForm.value.name} added successfully`,
                color: 'green'
            });
        }
        else {
            toast.add({
                title: 'Error',
                description: String((response.error as any)?.value || 'Request failed'),
                color: 'red'
            });
        }
    });
}

async function updateProvider() {
    // Prepare new custom models for creation (no ID means create new)
    const newCustomModels = existingProviderCustomModels.value
        .filter((model: { model_id: string; is_enabled: boolean }) => model.is_enabled && model.model_id.trim() !== '')
        .map((model: { model_id: string; is_enabled: boolean }) => ({
            // No id field - this signals to backend to create new model
            model_id: model.model_id,
            name: model.model_id,
            is_custom: true,
            is_enabled: true,
            is_preset: false
        }));

    // Existing models keep their IDs for updates
    if (!selectedProvider.value) return;
    const existingModels = selectedProvider.value.models.map((model: any) => ({
        id: model.id, // Keep existing ID for updates
        model_id: model.model_id,
        name: model.name,
        is_custom: model.is_custom,
        is_enabled: model.is_enabled,
        is_preset: model.is_preset
    }));

    // Prepare update payload with existing models + new custom models
    const updatePayload = {
        name: selectedProvider.value?.name,
        provider_type: selectedProvider.value?.provider_type,
        credentials: selectedProvider.value?.credentials,
        models: [...existingModels, ...newCustomModels]
    };

    // Update selectedProvider with new models
    const response = await useMyFetch(`/api/llm/providers/${selectedProvider.value?.id}`, {
        method: 'PUT',
        body: updatePayload
    }).then(async response => {
        if (response.status.value === 'success') {
            // Refresh providers list to ensure additional_config updates are reflected
            const orgProvidersRes = await useMyFetch('/api/llm/providers');
            organizationProviders.value = (orgProvidersRes.data.value as unknown as OrgProvider[]) || [];
            resetForm();
            providerModalOpen.value = false;
            toast.add({
                title: 'Success',
                description: `Provider updated successfully`,
                color: 'green'
            });
        }
        else {
            toast.add({
                title: 'Error',
                description: String((response.error as any)?.value || 'Request failed'),
                color: 'red'
            });
        }
    });
}

watch(selectedProvider, (newValue) => {
    // Reset showDangerZone when switching providers
    showDangerZone.value = false;
    
    if (newValue && newValue?.type !== 'new_provider') {
        // Initialize credentials if null
        if (!newValue.credentials) {
            newValue.credentials = { api_key: null } as any;
        }
        // Ensure base_url field exists for OpenAI so users can set/clear it
        if ((newValue.provider_type === 'openai' || newValue.type === 'openai')) {
            const existingBaseUrl = (newValue as any)?.additional_config?.base_url;
            if (existingBaseUrl && (!newValue.credentials.base_url || newValue.credentials.base_url === '')) {
                (newValue.credentials as any).base_url = existingBaseUrl;
            }
            if (newValue.credentials.base_url === undefined) {
                (newValue.credentials as any).base_url = null;
            }
            showBaseUrl.value = !!newValue.credentials.base_url;
        } else {
            showBaseUrl.value = false;
        }
        // Ensure endpoint_url field exists for Azure so users can view/update it
        if ((newValue.provider_type === 'azure' || newValue.type === 'azure')) {
            const existingEndpoint = (newValue as any)?.additional_config?.endpoint_url;
            if (existingEndpoint && (!newValue.credentials.endpoint_url || newValue.credentials.endpoint_url === '')) {
                (newValue.credentials as any).endpoint_url = existingEndpoint;
            }
            if (newValue.credentials.endpoint_url === undefined) {
                (newValue.credentials as any).endpoint_url = null;
            }
        }
        providerForm.value = {
            name: '',
            provider_type: '',
            credentials: {}
        };
    }
});

async function deleteProvider(providerId: string) {
    if (confirm('Are you sure you want to delete this provider? This action cannot be undone.')) {
        try {
            const response = await useMyFetch(`/api/llm/providers/${providerId}`, {
                method: 'DELETE'
            });
            
            if (response.status.value === 'success') {
                resetForm();
                providerModalOpen.value = false;
                toast.add({
                    title: 'Success',
                    description: 'Provider deleted successfully',
                    color: 'green'
                });
                // Refresh the providers list
                const orgProvidersRes = await useMyFetch('/api/llm/providers');
                organizationProviders.value = (orgProvidersRes.data.value as unknown as OrgProvider[]) || [];
            } else {
                toast.add({
                    title: 'Error',
                    description: 'Failed to delete provider that has a default model',
                    color: 'red'
                });
            }
        } catch (error) {
            toast.add({
                title: 'Error',
                description: 'Failed to delete provider',
                color: 'red'
            });
        }
    }
}

function addCustomModel() {
    customModels.value.push({
        model_id: '',
        is_enabled: true
    });
}

function removeCustomModel(index: number) {
    customModels.value.splice(index, 1);
}

function addExistingProviderCustomModel() {
    existingProviderCustomModels.value.push({
        model_id: '',
        is_enabled: true
    });
}

function removeExistingProviderCustomModel(index: number) {
    existingProviderCustomModels.value.splice(index, 1);
}

function removeExistingCustomModel(modelId: string) {
    if (confirm('Are you sure you want to remove this custom model?')) {
        selectedProvider.value!.models = selectedProvider.value!.models.filter((model: any) => model.id !== modelId);
    }
}

function toggleBaseUrl() {
    showBaseUrl.value = !showBaseUrl.value;
    if (!showBaseUrl.value && selectedProvider.value && selectedProvider.value.credentials) {
        // Clear to signal backend to use default
        (selectedProvider.value.credentials as any).base_url = '';
    }
}

function toggleBaseUrlNewProvider() {
    showBaseUrlNew.value = !showBaseUrlNew.value;
    if (!showBaseUrlNew.value) {
        if ('base_url' in providerForm.value.credentials) {
            delete (providerForm.value.credentials as any).base_url;
        }
    } else {
        if (providerForm.value.credentials.base_url === undefined) {
            (providerForm.value.credentials as any).base_url = '';
        }
    }
}

async function testConnection() {
    try {
        isTestingConnection.value = true;
        // Build payload from either existing-selected provider (edit mode) or new form
        let payload: any;
        if (selectedProvider.value && selectedProvider.value.type !== 'new_provider') {
            // Use selectedProvider fields
            const modelsPayload = (selectedProvider.value.models || []).map((m: any) => ({
                id: m.id,
                name: m.name,
                model_id: m.model_id,
                is_preset: m.is_preset,
                is_custom: m.is_custom,
                is_enabled: m.is_enabled,
                is_default: m.is_default
            }));

            payload = {
                name: selectedProvider.value.name,
                provider_type: selectedProvider.value.provider_type || selectedProvider.value.type,
                credentials: selectedProvider.value.credentials || {},
                models: modelsPayload
            };
        } else {
            // Build from new provider form + selected models in UI
            const selectedPresetModels = models.value
                .filter((model: any) => model.provider_type === providerForm.value.provider_type && !!model.is_enabled)
                .map((model: any) => ({
                    model_id: model.model_id,
                    name: model.name,
                    is_custom: false,
                    is_enabled: true,
                    is_preset: true
                }));
            const selectedCustomModelsPayload = customModels.value
                .filter(model => model.is_enabled && model.model_id.trim() !== '')
                .map(model => ({
                    model_id: model.model_id,
                    name: model.model_id,
                    is_custom: true,
                    is_enabled: true,
                    is_preset: false
                }));

            payload = {
                name: providerForm.value.name || `${providerForm.value.provider_type} (temp)` ,
                provider_type: providerForm.value.provider_type,
                credentials: providerForm.value.credentials,
                models: [...selectedPresetModels, ...selectedCustomModelsPayload]
            };
        }

        const res = await useMyFetch('/api/llm/test_connection', {
            method: 'POST',
            body: payload
        });
        if (res.status.value === 'success') {
            const data = (res.data as any)?.value as any;
            const ok = data?.success;
            toast.add({
                title: ok ? 'Connection successful' : 'Connection failed',
                description: data?.message || (ok ? 'Connected' : 'No response'),
                color: ok ? 'green' : 'red'
            });
        } else {
            toast.add({
                title: 'Error',
                description: String((res.error as any)?.value || 'Request failed'),
                color: 'red'
            });
        }
    } catch (e: any) {
        toast.add({ title: 'Error', description: String(e?.message || e), color: 'red' });
    } finally {
        isTestingConnection.value = false;
    }
}
</script>
