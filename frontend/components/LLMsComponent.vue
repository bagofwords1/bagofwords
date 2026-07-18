<template>
    <div>
        <!-- Only show controls when there are models -->
        <div v-if="models.length > 0" class="flex justify-between items-center mb-2">
            <div class="w-1/2">
                <input
                    type="text"
                    v-model="searchQuery"
                    :placeholder="$t('settings.llms.searchPlaceholder')"
                    class="border border-gray-300 dark:border-gray-600 rounded-md px-3 py-1.5 text-sm focus:ring-blue-500 focus:border-blue-500 w-full"
                >
            </div>
            <div class="flex items-center space-x-3">
                <div class="flex items-center gap-1.5" data-testid="auto-router-toggle">
                    <span class="text-sm text-gray-600 dark:text-gray-300">{{ $t('settings.llms.autoRouter') }}</span>
                    <UPopover mode="hover" :popper="{ placement: 'bottom' }">
                        <UIcon name="i-heroicons-question-mark-circle" class="w-4 h-4 text-gray-400 hover:text-gray-600 cursor-help" />
                        <template #panel>
                            <div class="p-3 max-w-xs text-xs text-gray-600 dark:text-gray-300 space-y-1.5">
                                <p class="font-semibold text-gray-900 dark:text-white">{{ $t('settings.llms.autoRouterHowTitle') }}</p>
                                <p>• {{ $t('settings.llms.autoRouterHow1') }}</p>
                                <p>• {{ $t('settings.llms.autoRouterHow2') }}</p>
                                <p>• {{ $t('settings.llms.autoRouterHow3') }}</p>
                                <p>• {{ $t('settings.llms.autoRouterHow4') }}</p>
                            </div>
                        </template>
                    </UPopover>
                    <UToggle
                        v-model="autoRouterOn"
                        :disabled="!useCan('manage_llm_settings')"
                        @update:model-value="saveAutoRouter"
                    />
                </div>
                <button
                    v-if="useCan('manage_llm_settings')"
                    @click="providerModalOpen = true"
                    class="bg-blue-500 text-white text-sm px-3 py-1.5 rounded-md"
                >
                    {{ $t('settings.llms.integrateModels') }}
                </button>
            </div>
        </div>
        <div v-if="models.length > 0" class="bg-white dark:bg-gray-900 rounded-lg shadow">
            <table class="min-w-full divide-y divide-gray-200 dark:divide-gray-700">
                <thead class="bg-gray-50 dark:bg-gray-900">
                    <tr>
                        <th class="px-4 py-2 text-start text-xs font-medium text-gray-500 dark:text-gray-400 uppercase tracking-wider">{{ $t('settings.llms.colModel') }}</th>
                        <th v-if="autoRouterOn" class="px-4 py-2 text-start text-xs font-medium text-gray-500 dark:text-gray-400 uppercase tracking-wider">{{ $t('settings.llms.colRouting') }}</th>
                        <th class="px-4 py-2 text-start text-xs font-medium text-gray-500 dark:text-gray-400 uppercase tracking-wider">{{ $t('settings.llms.colCost') }}</th>
                        <th class="px-4 py-2 text-start text-xs font-medium text-gray-500 dark:text-gray-400 uppercase tracking-wider">{{ $t('settings.llms.colStatus') }}</th>
                        <th class="px-4 py-2 text-start text-xs font-medium text-gray-500 dark:text-gray-400 uppercase tracking-wider">
                            <UTooltip :text="$t('settings.llms.visionTooltip')">{{ $t('settings.llms.colVision') }}</UTooltip>
                        </th>
                        <th class="px-4 py-2 text-start text-xs font-medium text-gray-500 dark:text-gray-400 uppercase tracking-wider">
                            <UTooltip :text="$t('settings.llms.contextTooltip')">{{ $t('settings.llms.colContext') }}</UTooltip>
                        </th>
                        <th class="px-4 py-2 text-start text-xs font-medium text-gray-500 dark:text-gray-400 uppercase tracking-wider" v-if="canManageAccess">Access</th>
                        <th class="px-4 py-2 text-start text-xs font-medium text-gray-500 dark:text-gray-400 uppercase tracking-wider" v-if="useCan('manage_llm_settings')">{{ $t('settings.llms.colActions') }}</th>
                    </tr>
                </thead>
                <tbody class="bg-white dark:bg-gray-900 divide-y divide-gray-200 dark:divide-gray-700">
                    <tr v-for="model in filteredModels" :key="model.id" class="hover:bg-gray-50 dark:hover:bg-gray-800">
                        <td class="px-4 py-2.5 whitespace-nowrap">
                            <div class="flex items-center">
                                <div class="flex-shrink-0 h-10 w-10 flex items-center justify-center">
                                    <LLMProviderIcon :provider="model.provider.provider_type" :icon="true" class="h-6 w-6" />
                                </div>
                                <div class="ms-4">
                                    <div class="text-sm font-medium text-gray-900 dark:text-white flex items-center gap-1.5">
                                        {{ model.name }}
                                        <span v-if="model.is_default" class="text-xs bg-blue-500 text-white px-1.5 py-0.5 rounded-md">{{ $t('settings.llms.badgeDefault') }}</span>
                                        <UTooltip v-if="model.is_small_default" :text="$t('settings.llms.smallDefaultTooltip')">
                                            <span class="text-xs bg-green-500 text-white px-1.5 py-0.5 rounded-md">{{ $t('settings.llms.badgeSmallDefault') }}</span>
                                        </UTooltip>
                                    </div>
                                    <div v-if="model.model_id !== model.name" class="text-xs text-gray-500 dark:text-gray-400">
                                        {{ $t('settings.llms.modelIdLabel') }}: {{ model.model_id }}
                                    </div>
                                </div>
                            </div>
                        </td>
                        <td v-if="autoRouterOn" class="px-4 py-2.5 text-sm align-middle" data-testid="llm-routing-cell">
                            <div class="min-w-[13rem]">
                                <span v-if="!model.is_enabled" class="text-xs text-gray-400 italic">{{ $t('settings.llms.routingNotAvailable') }}</span>
                                <!-- Routing guidance (only editable target when enabled) -->
                                <div v-else>
                                    <div v-if="editingHintId === model.id" class="flex items-start gap-1">
                                        <textarea
                                            v-model="hintDraft"
                                            rows="2"
                                            :placeholder="$t('settings.llms.routingHintPlaceholder')"
                                            class="border border-gray-300 dark:border-gray-600 dark:bg-gray-800 rounded px-2 py-1 w-full text-xs focus:outline-none focus:border-blue-500"
                                            @keyup.escape="editingHintId = null"
                                        />
                                        <button type="button" class="text-blue-500 hover:text-blue-700 mt-1" @click="saveHint(model)">
                                            <UIcon name="i-heroicons-check" class="w-4 h-4" />
                                        </button>
                                        <button type="button" class="text-gray-400 hover:text-gray-600 mt-1" @click="editingHintId = null">
                                            <UIcon name="i-heroicons-x-mark" class="w-4 h-4" />
                                        </button>
                                    </div>
                                    <UTooltip v-else :text="$t('settings.llms.routingHintTooltip')">
                                        <button
                                            v-if="useCan('manage_llm_settings')"
                                            type="button"
                                            class="group inline-flex items-start gap-1 text-xs text-start"
                                            :class="modelHint(model) ? 'text-gray-600 dark:text-gray-300 hover:text-blue-600' : 'text-gray-400 hover:text-blue-600 italic'"
                                            @click="startHintEdit(model)"
                                        >
                                            <span class="underline decoration-dotted underline-offset-2">{{ modelHint(model) || $t('settings.llms.routingHintPlaceholder') }}</span>
                                            <UIcon name="i-heroicons-pencil-square" class="w-3.5 h-3.5 flex-shrink-0 mt-0.5 opacity-0 group-hover:opacity-100 transition-opacity" />
                                        </button>
                                        <span v-else class="text-xs text-gray-500">{{ modelHint(model) || '—' }}</span>
                                    </UTooltip>
                                </div>
                            </div>
                        </td>
                        <td class="px-4 py-2.5 whitespace-nowrap text-sm text-gray-600 dark:text-gray-300 tabular-nums" data-testid="llm-cost-cell">
                            <div v-if="editingCostId === model.id" class="flex items-center gap-1">
                                <input v-model.number="costInDraft" type="number" min="0" step="0.01"
                                    class="border border-gray-300 dark:border-gray-600 dark:bg-gray-800 rounded px-1.5 py-1 w-16 text-xs focus:outline-none focus:border-blue-500"
                                    placeholder="in" @keyup.enter="saveCost(model)" @keyup.escape="editingCostId = null" />
                                <span class="text-gray-400">/</span>
                                <input v-model.number="costOutDraft" type="number" min="0" step="0.01"
                                    class="border border-gray-300 dark:border-gray-600 dark:bg-gray-800 rounded px-1.5 py-1 w-16 text-xs focus:outline-none focus:border-blue-500"
                                    placeholder="out" @keyup.enter="saveCost(model)" @keyup.escape="editingCostId = null" />
                                <button type="button" class="text-blue-500 hover:text-blue-700" @click="saveCost(model)"><UIcon name="i-heroicons-check" class="w-4 h-4" /></button>
                                <button type="button" class="text-gray-400 hover:text-gray-600" @click="editingCostId = null"><UIcon name="i-heroicons-x-mark" class="w-4 h-4" /></button>
                            </div>
                            <UTooltip v-else :text="$t('settings.llms.costEditTooltip')">
                                <button v-if="useCan('manage_llm_settings')" type="button"
                                    class="hover:text-blue-600 underline decoration-dotted underline-offset-2"
                                    @click="startCostEdit(model)">
                                    {{ formatCost(model) }}
                                </button>
                                <span v-else>{{ formatCost(model) }}</span>
                            </UTooltip>
                        </td>
                        <td class="px-4 py-2.5 whitespace-nowrap text-sm">
                            <UToggle
                                v-model="model.is_enabled"
                                @change="toggleModel(model.id, $event)"
                                :disabled="!useCan('manage_llm_settings') || model.is_default || model.is_small_default"
                            />
                        </td>
                        <td class="px-4 py-2.5 whitespace-nowrap text-sm">
                            <UTooltip :text="$t('settings.llms.visionTooltip')">
                                <UToggle
                                    v-model="model.supports_vision"
                                    @change="toggleVision(model.id, $event)"
                                    :disabled="!useCan('manage_llm_settings')"
                                />
                            </UTooltip>
                        </td>
                        <td class="px-4 py-2.5 whitespace-nowrap text-sm" data-testid="llm-context-cell">
                            <div v-if="editingContextId === model.id" class="flex items-center gap-1">
                                <input
                                    v-model.number="contextDraft"
                                    type="number"
                                    min="1"
                                    step="1000"
                                    :placeholder="$t('settings.llms.contextPlaceholder')"
                                    class="border border-gray-300 dark:border-gray-600 rounded px-2 py-1 w-28 text-sm focus:outline-none focus:border-blue-500"
                                    @keyup.enter="saveContextWindow(model)"
                                    @keyup.escape="editingContextId = null"
                                />
                                <button type="button" class="text-blue-500 hover:text-blue-700" @click="saveContextWindow(model)">
                                    <UIcon name="i-heroicons-check" class="w-4 h-4" />
                                </button>
                                <button type="button" class="text-gray-400 hover:text-gray-600" @click="editingContextId = null">
                                    <UIcon name="i-heroicons-x-mark" class="w-4 h-4" />
                                </button>
                            </div>
                            <div v-else class="flex items-center gap-1.5">
                                <UTooltip :text="$t('settings.llms.contextTooltip')">
                                    <button
                                        v-if="useCan('manage_llm_settings')"
                                        type="button"
                                        class="text-gray-700 dark:text-gray-300 hover:text-blue-600 underline decoration-dotted underline-offset-2"
                                        @click="startContextEdit(model)"
                                    >
                                        {{ formatTokens(model.context_window_tokens) }}
                                    </button>
                                    <span v-else class="text-gray-700 dark:text-gray-300">{{ formatTokens(model.context_window_tokens) }}</span>
                                </UTooltip>
                                <UTooltip
                                    v-if="useCan('manage_llm_settings') && model.context_window_tokens_override != null"
                                    :text="$t('settings.llms.contextResetTooltip')"
                                >
                                    <button type="button" class="text-gray-400 hover:text-gray-600" @click="resetContextWindow(model)">
                                        <UIcon name="i-heroicons-arrow-uturn-left" class="w-3.5 h-3.5" />
                                    </button>
                                </UTooltip>
                            </div>
                        </td>
                        <td class="px-4 py-2.5 whitespace-nowrap text-sm" v-if="canManageAccess">
                            <button
                                type="button"
                                class="inline-flex items-center gap-1 text-sm"
                                :class="accessIsRestricted(model) ? 'text-amber-600 dark:text-amber-400' : 'text-gray-500 dark:text-gray-400'"
                                @click="openAccess(model)"
                                data-testid="llm-access-cell"
                            >
                                <UIcon :name="accessIsRestricted(model) ? 'i-heroicons-lock-closed' : 'i-heroicons-user-group'" class="w-4 h-4" />
                                <span>{{ accessLabel(model) }}</span>
                            </button>
                        </td>
                        <td class="px-4 py-2.5 whitespace-nowrap text-sm" v-if="useCan('manage_llm_settings')">
                            <UDropdown :items="dropdownItemsByModel[model.id]" :popper="{ strategy: 'fixed' }">
                                <UButton class="text-gray-500 dark:text-gray-400 hover:text-gray-900 dark:hover:text-white font-medium transition-colors duration-150" color="white" label="" trailing-icon="i-heroicons-ellipsis-vertical" />
                            </UDropdown>
                        </td>
                    </tr>
                </tbody>
            </table>
        </div>

        <!-- Empty state -->
        <div v-else class="text-center py-12 bg-white dark:bg-gray-900 rounded-lg mt-20">
            <div class="w-48 mx-auto mb-4 flex items-center justify-center">
                <UIcon name="heroicons-cube-transparent" class="w-12 h-12 text-gray-400" />
            </div>
            <h3 class="text-lg font-medium text-gray-900 dark:text-white mb-2">{{ $t('settings.llms.emptyTitle') }}</h3>
            <p class="text-sm text-gray-500 dark:text-gray-400 mb-6">{{ $t('settings.llms.emptyHint') }}</p>
            <button
                v-if="useCan('manage_llm_settings')"
                @click="providerModalOpen = true"
                class="bg-blue-500 text-white text-sm px-4 py-2 rounded-md hover:bg-blue-600 transition-colors"
            >
                {{ $t('settings.llms.integrateModels') }}
            </button>
        </div>

        <!-- Provider Modal -->
        <LLMProviderModalComponent
            v-model="providerModalOpen"
            :edit-provider-id="editProviderId"
            @update:modelValue="handleProviderModalClose"
        />

        <!-- Per-model Access Modal (Enterprise) -->
        <LLMModelAccessModal
            v-if="canManageAccess && accessModel"
            v-model="accessModalOpen"
            :model="accessModel"
            :organization-id="organization.id"
            @updated="getModels"
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

const { t } = useI18n();
const toast = useToast();
const searchQuery = ref('');

type Provider = { id: string; name: string; provider_type: string };
type Model = {
  id: string;
  name: string;
  model_id: string;
  is_default: boolean;
  is_small_default: boolean;
  is_enabled: boolean;
  supports_vision: boolean;
  supports_vision_override?: boolean | null;
  context_window_tokens?: number | null;
  context_window_tokens_override?: number | null;
  is_restricted?: boolean;
  input_cost_per_million_tokens_usd?: number | null;
  output_cost_per_million_tokens_usd?: number | null;
  config?: Record<string, any> | null;
  provider: Provider;
};

const models = ref<Model[]>([]);
const providers = ref<Provider[]>([]);

// ── Auto model router (org setting model_routing) ──────────────────────────
const autoRouterOn = ref(false);

const loadAutoRouter = async () => {
    const response = await useMyFetch<any>('/organization/settings', { method: 'GET' });
    const cfg = (response.data.value as any)?.config;
    autoRouterOn.value = !!cfg?.model_routing?.value;
};

const saveAutoRouter = async (val: boolean) => {
    const response = await useMyFetch('/organization/settings', {
        method: 'PUT',
        body: { config: { model_routing: { value: val } } },
    });
    if (response.status.value === 'success') {
        toast.add({
            title: val ? t('settings.llms.autoRouterOn') : t('settings.llms.autoRouterOff'),
            color: 'green',
        });
    } else {
        autoRouterOn.value = !val; // revert optimistic toggle
        toast.add({ title: 'Error', description: 'Could not update auto router', color: 'red' });
    }
};

const modelHint = (model: Model): string => (model.config?.routing_hint as string) || '';

const formatCost = (model: Model): string => {
    const i = model.input_cost_per_million_tokens_usd;
    const o = model.output_cost_per_million_tokens_usd;
    if (i == null && o == null) return '—';
    const fmt = (n?: number | null) => (n == null ? '—' : `$${parseFloat(Number(n).toFixed(2))}`);
    return `${fmt(i)} / ${fmt(o)}`;
};

// ── Per-model pricing (inline edit) ────────────────────────────────────────
const editingCostId = ref<string | null>(null);
const costInDraft = ref<number | null>(null);
const costOutDraft = ref<number | null>(null);

const startCostEdit = (model: Model) => {
    costInDraft.value = model.input_cost_per_million_tokens_usd ?? null;
    costOutDraft.value = model.output_cost_per_million_tokens_usd ?? null;
    editingCostId.value = model.id;
};

const saveCost = async (model: Model) => {
    const inC = costInDraft.value, outC = costOutDraft.value;
    if ((inC != null && inC < 0) || (outC != null && outC < 0)) {
        toast.add({ title: 'Error', description: 'Cost must be non-negative', color: 'red' });
        return;
    }
    const response = await useMyFetch(`/llm/models/${model.id}/pricing`, {
        method: 'POST',
        body: {
            input_cost_per_million_tokens_usd: inC,
            output_cost_per_million_tokens_usd: outC,
        },
    });
    if (response.status.value === 'success') {
        editingCostId.value = null;
        await getModels();
        toast.add({ title: 'Pricing updated', color: 'green' });
    } else {
        toast.add({ title: 'Error', description: 'Could not update pricing', color: 'red' });
    }
};

const editingHintId = ref<string | null>(null);
const hintDraft = ref<string>('');

const startHintEdit = (model: Model) => {
    hintDraft.value = modelHint(model);
    editingHintId.value = model.id;
};

const saveHint = async (model: Model) => {
    const response = await useMyFetch(`/llm/models/${model.id}/routing_hint`, {
        method: 'POST',
        body: { hint: hintDraft.value },
    });
    if (response.status.value === 'success') {
        editingHintId.value = null;
        await getModels();
        toast.add({ title: 'Routing guidance updated', color: 'green' });
    } else {
        toast.add({ title: 'Error', description: 'Could not update routing guidance', color: 'red' });
    }
};

const providerModalOpen = ref(false);
const editProviderId = ref<string | null>(null);

const { hasFeature } = useEnterprise();
const canManageAccess = computed(() => hasFeature('llm_access_control') && useCan('manage_llm_settings'));

const accessModalOpen = ref(false);
const accessModel = ref<Model | null>(null);

const accessIsRestricted = (model: Model) => !!model.is_restricted && !model.is_default && !model.is_small_default;
const accessLabel = (model: Model) => {
    if (model.is_default || model.is_small_default) return 'Everyone (default)';
    return model.is_restricted ? 'Restricted' : 'Everyone';
};
const openAccess = (model: Model) => {
    accessModel.value = model;
    accessModalOpen.value = true;
};

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
    await loadAutoRouter();
    //await getProviders();
});

const handleProviderModalClose = async (value: boolean) => {
    providerModalOpen.value = value;
    if (!value) {  // Modal is closing
        await getModels();
        editProviderId.value = null;
    }
};

const setDefaultModel = async (modelId: string, small = false) => {
    const response = await useMyFetch(`/llm/models/${modelId}/set_default`, {
        method: 'POST',
        query: { small }
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

const toggleVision = async (modelId: string, enabled: boolean) => {
    const response = await useMyFetch(`/llm/models/${modelId}/toggle_vision`, {
        method: 'POST',
        query: { enabled }
    });
    if (response.status.value === 'success') {
        await getModels();
        toast.add({
            title: 'Model updated',
            description: enabled ? 'Vision enabled for this model' : 'Vision disabled for this model',
            color: 'green'
        });
    }
    else {
        // Revert optimistic toggle on failure
        const model = models.value.find(m => m.id === modelId);
        if (model) model.supports_vision = !enabled;
        toast.add({
            title: 'Error',
            description: 'Could not update vision setting',
            color: 'red'
        });
    }
};

const editingContextId = ref<string | null>(null);
const contextDraft = ref<number | null>(null);

const formatTokens = (n?: number | null) => {
    if (!n) return '—';
    if (n >= 1_000_000) return `${parseFloat((n / 1_000_000).toFixed(2))}M`;
    if (n >= 1_000) return `${parseFloat((n / 1_000).toFixed(1))}K`;
    return String(n);
};

const startContextEdit = (model: Model) => {
    contextDraft.value = model.context_window_tokens ?? null;
    editingContextId.value = model.id;
};

const setContextWindow = async (model: Model, tokens: number | null) => {
    const response = await useMyFetch(`/llm/models/${model.id}/set_context_window`, {
        method: 'POST',
        query: tokens != null ? { tokens } : {}
    });
    if (response.status.value === 'success') {
        editingContextId.value = null;
        await getModels();
        toast.add({
            title: 'Model updated',
            description: tokens != null ? 'Context window updated for this model' : 'Context window reset to default',
            color: 'green'
        });
    } else {
        toast.add({
            title: 'Error',
            description: 'Could not update context window',
            color: 'red'
        });
    }
};

const saveContextWindow = async (model: Model) => {
    const tokens = Number(contextDraft.value);
    if (!Number.isFinite(tokens) || tokens <= 0) {
        toast.add({ title: 'Error', description: 'Context window must be a positive number of tokens', color: 'red' });
        return;
    }
    await setContextWindow(model, Math.floor(tokens));
};

const resetContextWindow = async (model: Model) => {
    await setContextWindow(model, null);
};

const openManageProvider = (providerId: string) => {
    editProviderId.value = providerId;
    providerModalOpen.value = true;
};

const buildDropdownItems = (model: Model) => {
    const items: any[][] = [[
        {
            label: t('settings.llms.makeDefault'),
            click: () => {
                setDefaultModel(model.id, false);
            }
        },
        {
            label: t('settings.llms.makeSmallDefault'),
            click: () => {
                setDefaultModel(model.id, true);
            }
        }
    ]];
    if (useCan('manage_llm_settings')) {
        items[0].push({
            label: t('settings.llms.manageProvider'),
            click: () => {
                openManageProvider(model.provider.id);
            }
        });
    }
    return items;
};

// Memoize dropdown items per model. Building them inline in the template
// (`:items="getDropdownItems(model)"`) returns a fresh array on every render —
// so each row hover (which toggles the hover background) re-created the items
// and thrashed the dropdown popper, making the Actions menu feel frozen.
const dropdownItemsByModel = computed<Record<string, any[][]>>(() => {
    const map: Record<string, any[][]> = {};
    for (const m of models.value) map[m.id] = buildDropdownItems(m);
    return map;
});
</script>
