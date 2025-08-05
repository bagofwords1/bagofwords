<template>
    <UModal v-model="providerModalOpen">
        <div class="p-4 relative">
            <button @click="providerModalOpen = false" class="absolute top-2 right-2 text-gray-500 hover:text-gray-700">
                <Icon name="heroicons:x-mark" class="w-5 h-5" />
            </button>
            <h1 class="text-lg font-semibold">Integrate Models</h1>
            <p class="text-sm text-gray-500">Configure and manage LLM models and providers</p>
            <hr class="my-4" />

            <form @submit.prevent="submitProviderForm" class="space-y-4">
                <div class="flex flex-col">
                    <USelectMenu v-model="selectedProvider" :options="providersWithNewOption" option-attribute="name" class="w-full">

                        <UButton color="gray" class="flex-1" :class="{ '!text-blue-500': selectedProvider?.type === 'new_provider' }">
                            <Icon name="heroicons:plus-circle" class="w-5 h-5" v-if="selectedProvider?.type === 'new_provider'" />
                            <LLMProviderIcon :provider="selectedProvider?.type" class="w-8 h-8" v-if="selectedProvider && selectedProvider?.type !== 'new_provider'" />
                            <span v-if="selectedProvider" class="font-medium">{{ selectedProvider?.name }}</span>
                            <span v-else>Choose Provider</span>
                        </UButton>



                        <template #option="{ option }">
                            <div class="flex items-center gap-3" :class="{ 'text-blue-500': selectedProvider?.type === option.type }">
                            <Icon name="heroicons:plus-circle" class="w-5 h-5" v-if="option.type === 'new_provider'" />
                            <LLMProviderIcon :provider="option.type" class="w-8 h-8" v-else />
                            <span class="font-medium">{{ option.name }}</span>
                        </div>
                        </template>
                    </USelectMenu>
                </div>
                <div v-if="selectedProvider">
                    <div v-if="selectedProvider.type !== 'new_provider'" class="space-y-4">
                        <div class="">
                            <label class="text-sm font-medium text-gray-700 mb-2">
                                API Key
                            </label>
                            <input 
                                v-model="selectedProvider.credentials.api_key" 
                                type="text" 
                                placeholder="Keep blank to use default"
                                class="mt-2 border border-gray-300 rounded-lg px-4 py-2 w-full h-9 text-sm focus:outline-none focus:border-blue-500" 
                            />
                        </div>
                        <div class="">
                            <label class="text-sm font-medium text-gray-700 mb-2">
                                Models
                            </label>
                            <ul>
                                <li class="text-sm text-gray-500 mt-2" v-for="model in selectedProvider.models" :key="model.id">
                                    <span class="flex items-center gap-2">
                                        <UCheckbox v-model="model.is_enabled" /> {{ model.name }} 
                                    </span>
                                </li>
                            </ul>
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
                            <div v-for="(field, key) in fieldsForProvider(providerForm.provider_type)" :key="key">
                                <label class="text-sm font-medium text-gray-700 mb-2">{{ field.title }}</label>
                                <input v-model="providerForm.credentials[key]" type="text" required
                                    :placeholder="field.description || ''"
                                    class="border border-gray-300 rounded-lg px-4 py-2 w-full h-9 text-sm focus:outline-none focus:border-blue-500" />
                            </div>
                        </div>
                    </div>
                </div>

                                        <div v-if="providerForm.provider_type && filteredModels.length > 0">
                            <label class="text-sm font-medium text-gray-700 mb-2">
                                Models
                            </label>
                            <div class="space-y-2">
                                <!-- Preset Models -->
                                <div v-for="model in filteredModels" :key="model.id" class="flex items-center gap-2 p-2 border border-gray-200 rounded-lg">
                                    <UCheckbox v-model="model.is_enabled" />
                                    <div class="flex-1">
                                        <div class="text-sm font-medium text-gray-900">{{ model.name }}</div>
                                        <div class="text-xs text-gray-500">Model ID: {{ model.model_id }}</div>
                                    </div>
                                </div>
                                
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

                <div class="flex justify-end space-x-2 pt-4">
                    <UButton label="Cancel" color="gray" variant="soft" @click="providerModalOpen = false" />
                    <UButton 
                        type="submit" 
                        :label="selectedProvider?.type === 'new_provider' ? 'Save Provider' : 'Update Provider'"  
                        class="!bg-blue-500 !text-white" 
                        @click="selectedProvider?.type === 'new_provider' ? createProvider() : updateProvider()"
                    />
                </div>
            </form>
        </div>
    </UModal>
</template>

<script setup lang="ts">
import { ref, computed, watch, onMounted } from 'vue';

const props = defineProps<{
    modelValue: boolean;
}>();

const emit = defineEmits(['update:modelValue']);

const toast = useToast();

const showDangerZone = ref(false);

const providerModalOpen = computed({
    get: () => props.modelValue,
    set: (value) => emit('update:modelValue', value)
});

const providers = ref([]);
const organizationProviders = ref([]);
const models = ref([]);

onMounted(async () => {
  try {
    const [providersRes, orgProvidersRes, modelsRes] = await Promise.all([
      useMyFetch('/api/llm/available_providers'),
      useMyFetch('/api/llm/providers'),
      useMyFetch('/api/llm/available_models')
    ]);
    
    providers.value = providersRes.data.value;
    organizationProviders.value = orgProvidersRes.data.value;
    models.value = modelsRes.data.value.map(model => ({
      ...model,
      is_enabled: false
    }));
  } catch (error) {
    console.error('Failed to fetch data:', error);
  }
});

const selectedProvider = ref(null);
const selectedModel = ref(null);
const customModels = ref([]);
const providerForm = ref({
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

const isNewProviderSelected = computed(() => {
    return selectedProvider.value?.type === 'new_provider';
});

function fieldsForProvider(providerType) {
    const provider = providers.value.find(p => p.type === providerType);
    return provider?.credentials?.properties || [];
}

const filteredModels = computed(() => {
    const providerType = isNewProviderSelected.value 
        ? providerForm.value.provider_type 
        : selectedProvider.value?.type;
    return models.value.filter(model => model.provider_type === providerType);
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
    models.value.forEach(model => {
        model.selected = false;
        model.is_enabled = false;
    });
};

watch(providerModalOpen, (newValue) => {
    if (!newValue) {
        resetForm();
    }
});

async function createProvider() {
    // Gather selected preset models
    const selectedPresetModels = models.value
        .filter(model => model.provider_type === providerForm.value.provider_type && model.is_enabled)
        .map(model => ({
            model_id: model.model_id,
            name: model.name,
            is_custom: false
        }));

    // Gather selected custom models
    const selectedCustomModels = customModels.value
        .filter(model => model.is_enabled)
        .map(model => ({
            model_id: model.model_id,
            name: model.model_id, // Use model_id as the name for custom models
            is_custom: true
        }));

    // Combine all selected models
    providerForm.value.models = [...selectedPresetModels, ...selectedCustomModels];

    const response = await useMyFetch('/api/llm/providers', {
        method: 'POST',
        body: providerForm.value
    }).then(response => {
        if (response.status.value === 'success') {
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
                description: response.error,
                color: 'red'
            });
        }
    });
}

async function updateProvider() {
    // Update selectedProvider with new models
    const response = await useMyFetch(`/api/llm/providers/${selectedProvider.value.id}`, {
        method: 'PUT',
        body: selectedProvider.value
    }).then(response => {
        if (response.status.value === 'success') {
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
                description: response.error,
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
            newValue.credentials = { api_key: null };
        }
        providerForm.value = {
            name: '',
            provider_type: '',
            credentials: {}
        };
    }
});

async function deleteProvider(providerId) {
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
                organizationProviders.value = orgProvidersRes.data.value;
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

function removeCustomModel(index) {
    customModels.value.splice(index, 1);
}
</script>
