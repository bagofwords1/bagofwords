<template>
    <UModal v-model="open" :ui="{ width: 'sm:max-w-md' }">
        <div class="p-5" data-testid="add-model-modal">
            <button @click="open = false" class="absolute top-2 end-2 text-gray-500 dark:text-gray-400 hover:text-gray-700 dark:hover:text-gray-300">
                <Icon name="heroicons:x-mark" class="w-5 h-5" />
            </button>
            <h1 class="text-lg font-semibold">{{ $t('settings.llms.addModelTitle') }}</h1>
            <hr class="my-4" />

            <div class="space-y-4">
                <!-- Provider -->
                <div>
                    <label class="text-sm font-medium text-gray-700 dark:text-gray-300">{{ $t('settings.llms.providerLabel') }}</label>
                    <select
                        v-model="selectedProviderId"
                        data-testid="add-model-provider-select"
                        class="mt-2 border border-gray-300 dark:border-gray-600 dark:bg-gray-800 rounded-lg px-3 py-2 w-full text-sm focus:outline-none focus:border-blue-500"
                    >
                        <option v-for="p in providers" :key="p.id" :value="p.id">{{ p.name }}</option>
                    </select>
                </div>

                <!-- Catalog models not yet installed for this provider -->
                <div v-if="selectedProvider">
                    <label class="text-sm font-medium text-gray-700 dark:text-gray-300">{{ $t('settings.llms.addModelCatalog') }}</label>
                    <div class="mt-2 space-y-2 max-h-64 overflow-y-auto">
                        <div
                            v-for="m in catalogChoices"
                            :key="m.model_id"
                            class="flex items-center gap-2 p-2 border border-gray-200 dark:border-gray-700 rounded-lg cursor-pointer hover:bg-gray-50 dark:hover:bg-gray-800"
                            @click="toggleChoice(m.model_id)"
                        >
                            <UCheckbox :model-value="checked.has(m.model_id)" @click.stop @update:model-value="toggleChoice(m.model_id)" />
                            <div class="flex-1 leading-tight">
                                <div class="text-sm font-medium text-gray-900 dark:text-white">{{ m.name }}</div>
                                <div class="text-xs text-gray-500 dark:text-gray-400">{{ $t('settings.llms.modelIdLabel') }}: {{ m.model_id }}</div>
                            </div>
                        </div>
                        <div v-if="catalogChoices.length === 0" class="text-xs text-gray-500 dark:text-gray-400 italic">
                            {{ $t('settings.llms.addModelCatalogEmpty') }}
                        </div>
                    </div>
                </div>

                <!-- Custom model -->
                <div v-if="selectedProvider">
                    <label class="text-sm font-medium text-gray-700 dark:text-gray-300">{{ $t('settings.llms.addModelCustom') }}</label>
                    <input
                        v-model="customModelId"
                        type="text"
                        :placeholder="$t('settings.llms.addModelCustomPlaceholder')"
                        data-testid="add-model-custom-input"
                        class="mt-2 border border-gray-300 dark:border-gray-600 dark:bg-gray-800 rounded-lg px-3 py-2 w-full text-sm focus:outline-none focus:border-blue-500"
                    />
                </div>
            </div>

            <div class="mt-5 flex justify-end space-x-2">
                <UButton color="gray" variant="soft" @click="open = false">{{ $t('settings.llms.cancel') }}</UButton>
                <UButton
                    class="!bg-blue-500 !text-white"
                    :disabled="!canSubmit || isSubmitting"
                    :loading="isSubmitting"
                    data-testid="add-model-submit"
                    @click="submit"
                >
                    {{ $t('settings.llms.addModelSubmit') }}
                </UButton>
            </div>
        </div>
    </UModal>
</template>

<script setup lang="ts">
// Add one or more models to an existing provider: pick from the catalog models
// not yet installed on it, and/or type a custom model id. Each selection is
// created via POST /llm/models.
type OrgProvider = { id: string; name: string; provider_type: string };

const props = defineProps<{
    modelValue: boolean;
    providers: OrgProvider[];
    // Installed models (across providers) — used to hide already-added catalog entries.
    models: any[];
}>();

const emit = defineEmits(['update:modelValue', 'added']);

const { t } = useI18n();
const toast = useToast();

const open = computed({
    get: () => props.modelValue,
    set: (value: boolean) => emit('update:modelValue', value),
});

const selectedProviderId = ref<string | null>(null);
const checked = ref<Set<string>>(new Set());
const customModelId = ref('');
const isSubmitting = ref(false);

type CatalogModel = { name: string; model_id: string; provider_type: string; supports_vision?: boolean; supports_image_generation?: boolean; context_window_tokens?: number | null; max_output_tokens?: number | null; input_cost_per_million_tokens_usd?: number | null; output_cost_per_million_tokens_usd?: number | null };
const catalog = ref<CatalogModel[]>([]);

const loadCatalog = async () => {
    if (catalog.value.length) return;
    const response = await useMyFetch<CatalogModel[]>('/llm/available_models', { method: 'GET' });
    catalog.value = (response.data.value as unknown as CatalogModel[]) || [];
};

watch(open, (isOpen) => {
    if (isOpen) {
        checked.value = new Set();
        customModelId.value = '';
        if (!selectedProviderId.value || !props.providers.some(p => p.id === selectedProviderId.value)) {
            selectedProviderId.value = props.providers[0]?.id ?? null;
        }
        loadCatalog();
    }
});

// Switching provider invalidates the selection (catalog is per provider type).
watch(selectedProviderId, () => { checked.value = new Set(); });

const selectedProvider = computed(() => props.providers.find(p => p.id === selectedProviderId.value) || null);

const catalogChoices = computed<CatalogModel[]>(() => {
    const provider = selectedProvider.value;
    if (!provider) return [];
    const installed = new Set(
        props.models
            .filter((m: any) => m.provider?.id === provider.id)
            .map((m: any) => m.model_id)
    );
    return catalog.value.filter(m => m.provider_type === provider.provider_type && !installed.has(m.model_id));
});

const toggleChoice = (modelId: string) => {
    const next = new Set(checked.value);
    if (next.has(modelId)) next.delete(modelId);
    else next.add(modelId);
    checked.value = next;
};

const canSubmit = computed(() => !!selectedProvider.value && (checked.value.size > 0 || customModelId.value.trim() !== ''));

const submit = async () => {
    if (!selectedProvider.value) return;
    isSubmitting.value = true;
    try {
        const providerId = selectedProvider.value.id;
        const payloads: any[] = [];
        for (const m of catalogChoices.value.filter(m => checked.value.has(m.model_id))) {
            payloads.push({
                provider_id: providerId,
                model_id: m.model_id,
                name: m.name,
                is_custom: false,
                supports_vision: !!m.supports_vision,
                supports_image_generation: !!m.supports_image_generation,
                context_window_tokens: m.context_window_tokens ?? null,
                max_output_tokens: m.max_output_tokens ?? null,
                input_cost_per_million_tokens_usd: m.input_cost_per_million_tokens_usd ?? null,
                output_cost_per_million_tokens_usd: m.output_cost_per_million_tokens_usd ?? null,
            });
        }
        const custom = customModelId.value.trim();
        if (custom) {
            payloads.push({ provider_id: providerId, model_id: custom, name: custom, is_custom: true });
        }

        let created = 0;
        for (const body of payloads) {
            const response = await useMyFetch('/llm/models', { method: 'POST', body });
            if (response.status.value === 'success') {
                created++;
            } else {
                const errAny = (response.error as any);
                const err = (errAny && (errAny.value || errAny)) || {};
                const detail = err?.data?.detail || err?.data?.message || err?.message || 'Request failed';
                toast.add({ title: 'Error', description: String(detail), color: 'red' });
            }
        }
        if (created > 0) {
            toast.add({ title: t('settings.llms.modelAdded'), color: 'green' });
            emit('added');
            open.value = false;
        }
    } finally {
        isSubmitting.value = false;
    }
};
</script>
