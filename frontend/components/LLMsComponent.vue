<template>
    <div>
        <!-- Only show controls when there are models -->
        <div v-if="models.length > 0" class="flex justify-between items-center mb-2">
            <div class="w-1/2">
                <input
                    type="text"
                    v-model="searchQuery"
                    placeholder="Search LLMs..."
                    class="border border-gray-300 rounded-md px-3 py-1.5 text-sm focus:ring-blue-500 focus:border-blue-500 w-full"
                >
            </div>
            <div class="space-x-2">
                <button 
                    v-if="useCan('manage_llm_settings')"
                    @click="providerModalOpen = true" 
                    class="bg-blue-500 text-white text-sm px-3 py-1.5 rounded-md"
                >
                    Integrate Models
                </button>
            </div>
        </div>
        <div v-if="models.length > 0" class="bg-white rounded-lg shadow">
            <table class="min-w-full divide-y divide-gray-200">
                <thead class="bg-gray-50">
                    <tr>
                        <th class="px-6 py-3 text-left text-xs font-medium text-gray-500 uppercase tracking-wider">Model</th>
                        <th class="px-6 py-3 text-left text-xs font-medium text-gray-500 uppercase tracking-wider">Provider</th>
                        <th class="px-6 py-3 text-left text-xs font-medium text-gray-500 uppercase tracking-wider">Status</th>
                        <th class="px-6 py-3 text-left text-xs font-medium text-gray-500 uppercase tracking-wider" v-if="useCan('manage_llm_settings')">Actions</th>
                    </tr>
                </thead>
                <tbody class="bg-white divide-y divide-gray-200">
                    <tr v-for="model in filteredModels" :key="model.id" class="hover:bg-gray-50">
                        <td class="px-6 py-4 whitespace-nowrap">
                            <div class="flex items-center">
                                <div class="flex-shrink-0 h-10 w-10">
                                    <LLMProviderIcon :provider="model.provider.provider_type" class="h-8 w-8 text-gray-500" />
                                </div>
                                <div class="ml-4">
                                    <div class="text-sm font-medium text-gray-900">
                                        {{ model.name }}
                                        <span v-if="model.is_default" class="text-xs bg-blue-500 text-white px-1.5 py-0.5 rounded-md">Default</span>
                                    </div>
                                    <div v-if="model.model_id !== model.name" class="text-xs text-gray-500">
                                        Model ID: {{ model.model_id }}
                                    </div>
                                </div>
                            </div>
                        </td>
                        <td class="px-6 py-4 whitespace-nowrap text-sm text-gray-500">
                            {{ model.provider.name }}
                        </td>
                        <td class="px-6 py-4 whitespace-nowrap text-sm">
                            <UToggle v-model="model.is_enabled" @change="toggleModel(model.id, $event)" :disabled="!useCan('manage_llm_settings')" />
                        </td>
                        <td class="px-6 py-4 whitespace-nowrap text-sm" v-if="useCan('manage_llm_settings')">
                            <UDropdown :items="getDropdownItems(model)">
                                <UButton class="text-gray-500 hover:text-gray-900 font-medium transition-colors duration-150" color="white" label="" trailing-icon="i-heroicons-ellipsis-vertical" />
                            </UDropdown>
                        </td>
                    </tr>
                </tbody>
            </table>
        </div>

        <!-- Empty state -->
        <div v-else class="text-center py-12 bg-white rounded-lg mt-20">
            <div class="w-48 mx-auto mb-4 flex items-center justify-center">
                <UIcon name="heroicons-cube-transparent" class="w-12 h-12 text-gray-400" />
            </div>
            <h3 class="text-lg font-medium text-gray-900 mb-2">No LLMs Integrated</h3>
            <p class="text-sm text-gray-500 mb-6">Get started by integrating your LLM provider and models</p>
            <button 
                v-if="useCan('manage_llm_settings')"
                @click="providerModalOpen = true" 
                class="bg-blue-500 text-white text-sm px-4 py-2 rounded-md hover:bg-blue-600 transition-colors"
            >
                Integrate Models
            </button>
        </div>

        <!-- Provider Modal -->
        <LLMProviderModalComponent 
            v-model="providerModalOpen"
            :edit-provider-id="editProviderId"
            @update:modelValue="handleProviderModalClose"
        />

        
    </div>
</template>

<script setup lang="ts">
const props = defineProps({
    organization: {
        type: Object,
        required: true,
    },
});

const toast = useToast();
const searchQuery = ref('');

type Provider = { id: string; name: string; provider_type: string };
type Model = {
  id: string;
  name: string;
  model_id: string;
  is_default: boolean;
  is_enabled: boolean;
  provider: Provider;
};

const models = ref<Model[]>([]);
const providers = ref<Provider[]>([]);

const providerModalOpen = ref(false);
const editProviderId = ref<string | null>(null);

const filteredModels = computed<Model[]>(() => {
    const query = searchQuery.value.toLowerCase();
    if (!query) return models.value;
    
    return models.value.filter(model => {
        return model.name.toLowerCase().includes(query) || 
               model.provider.name.toLowerCase().includes(query);
    });
});

const getModels = async () => {
  const response = await useMyFetch<Model[]>('/llm/models', {
      method: 'GET',
  });

  models.value = (response.data.value as unknown as Model[]) || [];
}

const getProviders = async () => {
    const response = await useMyFetch<Provider[]>('/llm/providers', {
        method: 'GET',
    });

    providers.value = (response.data.value as unknown as Provider[]) || [];
}

onMounted(async () => {
    await getModels();
    //await getProviders();
});

const handleProviderModalClose = async (value: boolean) => {
    providerModalOpen.value = value;
    if (!value) {  // Modal is closing
        await getModels();
        editProviderId.value = null;
    }
};

const setDefaultModel = async (modelId: string) => {
    const response = await useMyFetch(`/llm/models/${modelId}/set_default`, {
        method: 'POST',
    });
    if (response.status.value === 'success') {
        await getModels();
        toast.add({
            title: 'Model updated',
            description: 'Model has been updated successfully',
            color: 'green'
        });
    }
    else {
        toast.add({
            title: 'Error',
            description: 'Could not update model',
            color: 'red'
        });
    }
};

const toggleModel = async (modelId: string, enabled: boolean) => {
    const response = await useMyFetch(`/llm/models/${modelId}/toggle`, {
        method: 'POST',
        query: { enabled }
    });
    if (response.status.value === 'success') {
        await getModels();
        toast.add({
            title: 'Model updated',
            description: 'Model has been updated successfully',
            color: 'green'
        });
    }
    else {
        toast.add({
            title: 'Error',
            description: 'Could not update model',
            color: 'red'
        });
    }
};

const openManageProvider = (providerId: string) => {
    editProviderId.value = providerId;
    providerModalOpen.value = true;
};

const getDropdownItems = (model: Model) => {
    const items: any[][] = [[
        {
            label: 'Make Default',
            click: () => {
                setDefaultModel(model.id);
            }
        }
    ]];
    if (useCan('manage_llm_settings')) {
        items[0].push({
            label: 'Manage Provider',
            click: () => {
                openManageProvider(model.provider.id);
            }
        });
    }
    return items;
};
</script>
