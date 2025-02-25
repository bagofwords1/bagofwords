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
                            <Icon name="heroicons:plus-circle" class="w-4 h-4" v-if="selectedProvider?.type === 'new_provider'" />
                            <LLMProviderIcon :provider="selectedProvider?.type" class="w-4" v-if="selectedProvider && selectedProvider?.type !== 'new_provider'" />
                            <span v-if="selectedProvider">{{ selectedProvider?.name }}</span>
                            <span v-else>Choose Provider</span>
                        </UButton>



                        <template #option="{ option }">
                            <div class="flex items-center gap-2" :class="{ 'text-blue-500': selectedProvider?.type === option.type }">
                            <Icon name="heroicons:plus-circle" class="w-4 h-4" v-if="option.type === 'new_provider'" />
                            <LLMProviderIcon :provider="option.type" class="w-4" v-else />
                            <span>{{ option.name }}</span>
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
                            <div class="flex flex-row gap-3 mt-2">
                                <button v-for="provider in providers" 
                                    @click="providerForm.provider_type = provider.type" 
                                    :key="provider.type" 
                                    class="bg-gray-50 hover:bg-gray-100 border border-gray-300 p-2 rounded-lg"
                                    type="button"
                                    :class="{ '!border-blue-500 border-2 bg-white': providerForm.provider_type === provider.type }"
                                >
                                    <LLMProviderIcon :provider="provider.type" class="w-4" />
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
                                <label class="text-sm font-medium uppercase text-gray-700 mb-2">{{ field.title }}</label>
                                <input v-model="providerForm.credentials[key]" type="text" required
                                    class="border border-gray-300 rounded-lg px-4 py-2 w-full h-9 text-sm focus:outline-none focus:border-blue-500" />
                            </div>
                        </div>
                    </div>
                </div>

                <div v-if="providerForm.provider_type && filteredModels.length > 0">
                    <label class="text-sm font-medium text-gray-700 mb-2">
                        Models
                    </label>
                    <ul>
                        <li class="text-sm text-gray-500" v-for="model in filteredModels" :key="model.id">
                            <span class="flex items-center gap-2">
                                 {{ model.name }}
                            </span>
                        </li>
                    </ul>
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
    models.value = modelsRes.data.value;
  } catch (error) {
    console.error('Failed to fetch data:', error);
  }
});

const selectedProvider = ref(null);
const selectedModel = ref(null);
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
    providerForm.value = {
        name: '',
        provider_type: '',
        credentials: {}
    };
    showDangerZone.value = false;
    // Reset any selected models
    models.value.forEach(model => {
        model.selected = false;
    });
};

watch(providerModalOpen, (newValue) => {
    if (!newValue) {
        resetForm();
    }
});

async function createProvider() {
    // Gather selected models
    const selectedModels = models.value
        .filter(model => model.provider_type === providerForm.value.provider_type)
        .map(model => ({
            model_id: model.model_id,
            name: model.name,
            is_custom: false
        }));

    // Add selected models to providerForm
    providerForm.value.models = selectedModels;

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
</script>
