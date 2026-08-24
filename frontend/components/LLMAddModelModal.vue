<template>
    <UModal v-model="open" :ui="{ width: 'sm:max-w-md' }">
        <div class="p-5" data-testid="add-model-modal">
            <button @click="open = false" class="absolute top-2 end-2 text-gray-500 dark:text-gray-400 hover:text-gray-700 dark:hover:text-gray-300">
                <Icon name="heroicons:x-mark" class="w-5 h-5" />
            </button>

            <!-- Header (mirrors the model card) -->
            <div class="flex items-center gap-3">
                <div class="flex-shrink-0 h-9 w-9 flex items-center justify-center">
                    <LLMProviderIcon v-if="selectedProvider" :provider="selectedProvider.provider_type" :model="modelId" :icon="true" class="h-7 w-7" />
                    <UIcon v-else name="i-heroicons-cpu-chip" class="h-7 w-7 text-gray-400" />
                </div>
                <div class="leading-tight min-w-0">
                    <div class="text-base font-semibold text-gray-900 dark:text-white">{{ $t('settings.llms.addModelTitle') }}</div>
                    <div class="text-xs text-gray-500 dark:text-gray-400 truncate">{{ selectedProvider?.name || '—' }}</div>
                </div>
            </div>

            <div class="mt-4 divide-y divide-gray-100 dark:divide-gray-800 border-y border-gray-100 dark:border-gray-800">
                <!-- Provider (select with provider icon) -->
                <div class="flex items-center justify-between py-2.5">
                    <span class="text-sm text-gray-700 dark:text-gray-300">{{ $t('settings.llms.providerLabel') }}</span>
                    <div class="relative">
                        <button
                            type="button"
                            data-testid="add-model-provider-select"
                            class="inline-flex items-center gap-2 border border-gray-300 dark:border-gray-600 dark:bg-gray-800 rounded-lg ps-2.5 pe-2 py-1.5 text-sm text-gray-800 dark:text-gray-200 hover:border-blue-400 focus:outline-none focus:border-blue-500 min-w-[10rem] justify-between"
                            @click="providerListOpen = !providerListOpen"
                        >
                            <span class="inline-flex items-center gap-2 min-w-0">
                                <LLMProviderIcon v-if="selectedProvider" :provider="selectedProvider.provider_type" :icon="true" class="h-4 w-4" />
                                <span class="truncate">{{ selectedProvider?.name || '—' }}</span>
                            </span>
                            <UIcon name="i-heroicons-chevron-down" class="w-4 h-4 text-gray-400 flex-shrink-0" />
                        </button>
                        <div
                            v-if="providerListOpen"
                            class="absolute end-0 z-20 mt-1 w-full min-w-[10rem] bg-white dark:bg-gray-800 border border-gray-200 dark:border-gray-700 rounded-lg shadow-lg py-1"
                            data-testid="add-model-provider-options"
                        >
                            <button
                                v-for="p in providers"
                                :key="p.id"
                                type="button"
                                class="w-full flex items-center gap-2 px-2.5 py-1.5 text-sm text-start text-gray-800 dark:text-gray-200 hover:bg-gray-50 dark:hover:bg-gray-700"
                                @click="selectProvider(p.id)"
                            >
                                <LLMProviderIcon :provider="p.provider_type" :icon="true" class="h-4 w-4" />
                                <span class="truncate">{{ p.name }}</span>
                                <UIcon v-if="p.id === selectedProviderId" name="i-heroicons-check" class="w-4 h-4 ms-auto text-blue-500 flex-shrink-0" />
                            </button>
                        </div>
                    </div>
                </div>
                <!-- Model ID -->
                <div class="flex items-center justify-between py-2.5 gap-4">
                    <span class="text-sm text-gray-700 dark:text-gray-300 flex-shrink-0">{{ $t('settings.llms.modelIdLabel') }}</span>
                    <input
                        v-model="modelId"
                        type="text"
                        :placeholder="$t('settings.llms.addModelCustomPlaceholder')"
                        data-testid="add-model-custom-input"
                        class="border border-gray-300 dark:border-gray-600 dark:bg-gray-800 rounded-lg px-2.5 py-1.5 w-56 text-sm text-end focus:outline-none focus:border-blue-500"
                    />
                </div>
                <!-- Display name. Optional: blank keeps the old behavior of showing
                     the model_id as the name. Uses displayNameHint, not
                     displayNameTooltip: everything added through this dialog is a
                     custom model (is_custom: true) whatever provider is selected —
                     a fine-tuned OpenAI id belongs here too — so the "custom models
                     only" caveat would just confuse. -->
                <div class="flex items-center justify-between py-2.5 gap-4">
                    <UTooltip :text="$t('settings.llms.displayNameHint')">
                        <span class="text-sm text-gray-700 dark:text-gray-300 underline decoration-dotted decoration-gray-300 underline-offset-2">{{ $t('settings.llms.displayNameLabel') }}</span>
                    </UTooltip>
                    <input
                        v-model="displayName"
                        type="text"
                        maxlength="120"
                        :placeholder="modelId.trim() || $t('settings.llms.displayNamePlaceholder')"
                        data-testid="add-model-name-input"
                        class="border border-gray-300 dark:border-gray-600 dark:bg-gray-800 rounded-lg px-2.5 py-1.5 w-56 text-sm text-end focus:outline-none focus:border-blue-500"
                    />
                </div>
                <!-- Vision -->
                <div class="flex items-center justify-between py-2.5">
                    <UTooltip :text="$t('settings.llms.visionTooltip')">
                        <span class="text-sm text-gray-700 dark:text-gray-300 underline decoration-dotted decoration-gray-300 underline-offset-2">{{ $t('settings.llms.colVision') }}</span>
                    </UTooltip>
                    <UToggle v-model="supportsVision" data-testid="add-model-vision-toggle" />
                </div>
                <!-- Image generation -->
                <div class="flex items-center justify-between py-2.5">
                    <UTooltip :text="$t('settings.llms.imageGenTooltip')">
                        <span class="text-sm text-gray-700 dark:text-gray-300 underline decoration-dotted decoration-gray-300 underline-offset-2">{{ $t('settings.llms.colImageGen') }}</span>
                    </UTooltip>
                    <UToggle v-model="supportsImageGeneration" data-testid="add-model-imagegen-toggle" />
                </div>
                <!-- Context window -->
                <div class="flex items-center justify-between py-2.5">
                    <UTooltip :text="$t('settings.llms.contextTooltip')">
                        <span class="text-sm text-gray-700 dark:text-gray-300 underline decoration-dotted decoration-gray-300 underline-offset-2">{{ $t('settings.llms.colContext') }}</span>
                    </UTooltip>
                    <input
                        v-model.number="contextWindowTokens"
                        type="number" min="1" step="1000"
                        :placeholder="$t('settings.llms.contextPlaceholder')"
                        data-testid="add-model-context-input"
                        class="border border-gray-300 dark:border-gray-600 dark:bg-gray-800 rounded px-2 py-1 w-36 text-sm text-end focus:outline-none focus:border-blue-500"
                    />
                </div>
                <!-- Temperature. Tri-state on purpose: empty = no temperature
                     parameter is ever sent (provider default). Never pre-fill. -->
                <div class="flex items-center justify-between py-2.5">
                    <UTooltip :text="$t('settings.llms.temperatureTooltip')">
                        <span class="text-sm text-gray-700 dark:text-gray-300 underline decoration-dotted decoration-gray-300 underline-offset-2">{{ $t('settings.llms.colTemperature') }}</span>
                    </UTooltip>
                    <input
                        v-model="temperature"
                        type="number" min="0" max="2" step="0.1"
                        :placeholder="$t('settings.llms.temperaturePlaceholder')"
                        data-testid="add-model-temperature-input"
                        class="border border-gray-300 dark:border-gray-600 dark:bg-gray-800 rounded px-2 py-1 w-36 text-sm text-end focus:outline-none focus:border-blue-500"
                    />
                </div>
                <!-- Cost -->
                <div class="flex items-center justify-between py-2.5">
                    <UTooltip :text="$t('settings.llms.costEditTooltip')">
                        <span class="text-sm text-gray-700 dark:text-gray-300 underline decoration-dotted decoration-gray-300 underline-offset-2">{{ $t('settings.llms.colCost') }}</span>
                    </UTooltip>
                    <div class="flex items-center gap-2">
                        <label class="flex items-center gap-1 text-[10px] text-gray-400 uppercase tracking-wide">
                            {{ $t('settings.llms.costInput') }}
                            <input v-model.number="costIn" type="number" min="0" step="0.01"
                                data-testid="add-model-cost-input"
                                class="border border-gray-300 dark:border-gray-600 dark:bg-gray-800 rounded px-1.5 py-1 w-16 text-xs text-gray-700 dark:text-gray-200 text-end normal-case focus:outline-none focus:border-blue-500" />
                        </label>
                        <label class="flex items-center gap-1 text-[10px] text-gray-400 uppercase tracking-wide">
                            {{ $t('settings.llms.costOutput') }}
                            <input v-model.number="costOut" type="number" min="0" step="0.01"
                                data-testid="add-model-cost-output"
                                class="border border-gray-300 dark:border-gray-600 dark:bg-gray-800 rounded px-1.5 py-1 w-16 text-xs text-gray-700 dark:text-gray-200 text-end normal-case focus:outline-none focus:border-blue-500" />
                        </label>
                    </div>
                </div>
            </div>

            <!-- Footer -->
            <div class="mt-4 flex justify-end space-x-2">
                <UButton color="gray" variant="soft" size="sm" @click="open = false">{{ $t('settings.llms.cancel') }}</UButton>
                <UButton
                    size="sm"
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
// Add a model to an existing provider. Same minimal card layout as the model
// settings modal: provider (icon select), model id, vision/image-gen toggles,
// context window and cost. Creates via POST /llm/models.
type OrgProvider = { id: string; name: string; provider_type: string };

const props = defineProps<{
    modelValue: boolean;
    providers: OrgProvider[];
    // Installed models — used to catch duplicates client-side before the 409.
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
const providerListOpen = ref(false);
const modelId = ref('');
// Optional display label. Blank falls back to the model_id at submit time,
// which is what custom models were always named before this field existed.
const displayName = ref('');
const supportsVision = ref(false);
const supportsImageGeneration = ref(false);
const contextWindowTokens = ref<number | null>(null);
// String draft so "" (provider default) stays distinct from 0 (explicit).
const temperature = ref<string>('');
const costIn = ref<number | null>(null);
const costOut = ref<number | null>(null);
const isSubmitting = ref(false);

watch(open, (isOpen) => {
    if (isOpen) {
        modelId.value = '';
        displayName.value = '';
        supportsVision.value = false;
        supportsImageGeneration.value = false;
        contextWindowTokens.value = null;
        temperature.value = '';
        costIn.value = null;
        costOut.value = null;
        providerListOpen.value = false;
        if (!selectedProviderId.value || !props.providers.some(p => p.id === selectedProviderId.value)) {
            selectedProviderId.value = props.providers[0]?.id ?? null;
        }
    }
});

const selectProvider = (id: string) => {
    selectedProviderId.value = id;
    providerListOpen.value = false;
};

const selectedProvider = computed(() => props.providers.find(p => p.id === selectedProviderId.value) || null);

const canSubmit = computed(() => !!selectedProvider.value && modelId.value.trim() !== '');

const submit = async () => {
    const provider = selectedProvider.value;
    if (!provider) return;
    const id = modelId.value.trim();

    const dup = props.models.some((m: any) => m.provider?.id === provider.id && m.model_id === id);
    if (dup) {
        toast.add({ title: 'Error', description: `Model '${id}' already exists on this provider`, color: 'red' });
        return;
    }

    const norm = (v: any) => (v == null || v === '' ? null : Number(v));
    const temp = norm(temperature.value);
    if (temp != null && (!Number.isFinite(temp) || temp < 0 || temp > 2)) {
        toast.add({ title: 'Error', description: 'Temperature must be between 0 and 2', color: 'red' });
        return;
    }
    isSubmitting.value = true;
    try {
        const response = await useMyFetch('/llm/models', {
            method: 'POST',
            body: {
                provider_id: provider.id,
                model_id: id,
                name: displayName.value.trim() || id,
                is_custom: true,
                supports_vision: supportsVision.value,
                supports_vision_override: supportsVision.value ? true : null,
                supports_image_generation: supportsImageGeneration.value,
                supports_image_generation_override: supportsImageGeneration.value ? true : null,
                context_window_tokens: norm(contextWindowTokens.value),
                context_window_tokens_override: norm(contextWindowTokens.value),
                input_cost_per_million_tokens_usd: norm(costIn.value),
                output_cost_per_million_tokens_usd: norm(costOut.value),
                // Empty field → no config key → no temperature is ever sent.
                config: temp != null ? { temperature: temp } : null,
            },
        });
        if (response.status.value === 'success') {
            toast.add({ title: t('settings.llms.modelAdded'), color: 'green' });
            emit('added');
            open.value = false;
        } else {
            const errAny = (response.error as any);
            const err = (errAny && (errAny.value || errAny)) || {};
            const detail = err?.data?.detail || err?.data?.message || err?.message || 'Request failed';
            toast.add({ title: 'Error', description: String(detail), color: 'red' });
        }
    } finally {
        isSubmitting.value = false;
    }
};
</script>
