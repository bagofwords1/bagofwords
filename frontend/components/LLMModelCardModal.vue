<template>
    <UModal v-model="open" :ui="{ width: 'sm:max-w-md' }">
        <div v-if="model" class="p-5" data-testid="model-card">
            <button @click="open = false" class="absolute top-2 end-2 text-gray-500 dark:text-gray-400 hover:text-gray-700 dark:hover:text-gray-300">
                <Icon name="heroicons:x-mark" class="w-5 h-5" />
            </button>

            <!-- Header -->
            <div class="flex items-center gap-3">
                <div class="flex-shrink-0 h-9 w-9 flex items-center justify-center">
                    <LLMProviderIcon :provider="model.provider.provider_type" :model="`${model.name} ${model.model_id}`" :icon="true" class="h-7 w-7" />
                </div>
                <div class="leading-tight min-w-0">
                    <div class="text-base font-semibold text-gray-900 dark:text-white flex items-center gap-1.5 flex-wrap">
                        <span class="truncate">{{ model.name }}</span>
                        <span v-if="model.is_default" class="text-xs bg-blue-500 text-white px-1.5 py-0.5 rounded-md">{{ $t('settings.llms.badgeDefault') }}</span>
                        <span v-if="model.is_small_default" class="text-xs bg-green-500 text-white px-1.5 py-0.5 rounded-md">{{ $t('settings.llms.badgeSmallDefault') }}</span>
                    </div>
                    <div class="text-xs text-gray-500 dark:text-gray-400 truncate">
                        {{ $t('settings.llms.modelIdLabel') }}: {{ model.model_id }} · {{ model.provider.name }}
                    </div>
                </div>
            </div>

            <div class="mt-4 divide-y divide-gray-100 dark:divide-gray-800 border-y border-gray-100 dark:border-gray-800">
                <!-- Enabled -->
                <div class="flex items-center justify-between py-2.5">
                    <span class="text-sm text-gray-700 dark:text-gray-300">{{ $t('settings.llms.enabledLabel') }}</span>
                    <UTooltip :text="model.is_default || model.is_small_default ? $t('settings.llms.smallDefaultTooltip') : ''" :prevent="!(model.is_default || model.is_small_default)">
                        <UToggle
                            :model-value="model.is_enabled"
                            :disabled="model.is_default || model.is_small_default"
                            data-testid="card-enabled-toggle"
                            @update:model-value="toggleEnabled"
                        />
                    </UTooltip>
                </div>
                <!-- Vision -->
                <div class="flex items-center justify-between py-2.5">
                    <UTooltip :text="$t('settings.llms.visionTooltip')">
                        <span class="text-sm text-gray-700 dark:text-gray-300 underline decoration-dotted decoration-gray-300 underline-offset-2">{{ $t('settings.llms.colVision') }}</span>
                    </UTooltip>
                    <UToggle :model-value="model.supports_vision" data-testid="card-vision-toggle" @update:model-value="toggleVision" />
                </div>
                <!-- Image generation. Disabled for default models: the backend would
                     silently strip the default flags (image models can't be defaults),
                     leaving the org without a default model. -->
                <div class="flex items-center justify-between py-2.5">
                    <UTooltip :text="$t('settings.llms.imageGenTooltip')">
                        <span class="text-sm text-gray-700 dark:text-gray-300 underline decoration-dotted decoration-gray-300 underline-offset-2">{{ $t('settings.llms.colImageGen') }}</span>
                    </UTooltip>
                    <UTooltip :text="$t('settings.llms.imageGenDefaultBlocked')" :prevent="!(model.is_default || model.is_small_default) || model.supports_image_generation">
                        <UToggle
                            :model-value="model.supports_image_generation"
                            :disabled="(model.is_default || model.is_small_default) && !model.supports_image_generation"
                            data-testid="card-imagegen-toggle"
                            @update:model-value="toggleImageGeneration"
                        />
                    </UTooltip>
                </div>
                <!-- Context window -->
                <div class="flex items-center justify-between py-2.5">
                    <UTooltip :text="$t('settings.llms.contextTooltip')">
                        <span class="text-sm text-gray-700 dark:text-gray-300 underline decoration-dotted decoration-gray-300 underline-offset-2">{{ $t('settings.llms.colContext') }}</span>
                    </UTooltip>
                    <div class="flex items-center gap-1.5">
                        <UTooltip v-if="model.context_window_tokens_override != null" :text="$t('settings.llms.contextResetTooltip')">
                            <button type="button" class="text-gray-400 hover:text-gray-600" @click="resetContextWindow">
                                <UIcon name="i-heroicons-arrow-uturn-left" class="w-3.5 h-3.5" />
                            </button>
                        </UTooltip>
                        <input
                            v-model.number="contextDraft"
                            type="number" min="1" step="1000"
                            :placeholder="$t('settings.llms.contextPlaceholder')"
                            data-testid="card-context-input"
                            class="border border-gray-300 dark:border-gray-600 dark:bg-gray-800 rounded px-2 py-1 w-28 text-sm text-end focus:outline-none focus:border-blue-500"
                        />
                    </div>
                </div>
                <!-- Cost -->
                <div class="flex items-center justify-between py-2.5">
                    <UTooltip :text="$t('settings.llms.costEditTooltip')">
                        <span class="text-sm text-gray-700 dark:text-gray-300 underline decoration-dotted decoration-gray-300 underline-offset-2">{{ $t('settings.llms.colCost') }}</span>
                    </UTooltip>
                    <div class="flex items-center gap-2">
                        <label class="flex items-center gap-1 text-[10px] text-gray-400 uppercase tracking-wide">
                            {{ $t('settings.llms.costInput') }}
                            <input v-model.number="costInDraft" type="number" min="0" step="0.01"
                                data-testid="card-cost-input"
                                class="border border-gray-300 dark:border-gray-600 dark:bg-gray-800 rounded px-1.5 py-1 w-16 text-xs text-gray-700 dark:text-gray-200 text-end normal-case focus:outline-none focus:border-blue-500" />
                        </label>
                        <label class="flex items-center gap-1 text-[10px] text-gray-400 uppercase tracking-wide">
                            {{ $t('settings.llms.costOutput') }}
                            <input v-model.number="costOutDraft" type="number" min="0" step="0.01"
                                data-testid="card-cost-output"
                                class="border border-gray-300 dark:border-gray-600 dark:bg-gray-800 rounded px-1.5 py-1 w-16 text-xs text-gray-700 dark:text-gray-200 text-end normal-case focus:outline-none focus:border-blue-500" />
                        </label>
                    </div>
                </div>
            </div>

            <!-- Footer -->
            <div class="mt-4 flex items-center">
                <template v-if="!confirmingDelete">
                    <UTooltip :text="deleteBlocked ? $t('settings.llms.deleteModelBlockedDefault') : ''" :prevent="!deleteBlocked">
                        <UButton
                            color="red" variant="ghost" size="xs" icon="i-heroicons-trash"
                            :disabled="deleteBlocked"
                            data-testid="card-delete-button"
                            @click="confirmingDelete = true"
                        >
                            {{ $t('settings.llms.deleteModel') }}
                        </UButton>
                    </UTooltip>
                    <div class="ms-auto space-x-2">
                        <UButton color="gray" variant="soft" size="sm" @click="open = false">{{ $t('settings.llms.cancel') }}</UButton>
                        <UButton size="sm" class="!bg-blue-500 !text-white" :disabled="!isDirty" data-testid="card-save-button" @click="save">
                            {{ $t('settings.llms.save') }}
                        </UButton>
                    </div>
                </template>
                <template v-else>
                    <div class="flex items-center justify-between w-full gap-2" data-testid="card-delete-confirm">
                        <span class="text-sm text-red-600 dark:text-red-400">{{ $t('settings.llms.deleteModelConfirm', { name: model.name }) }}</span>
                        <div class="flex items-center gap-2 flex-shrink-0">
                            <UButton color="gray" variant="ghost" size="xs" @click="confirmingDelete = false">{{ $t('settings.llms.cancel') }}</UButton>
                            <UButton color="red" size="xs" :loading="deleting" data-testid="card-delete-confirm-button" @click="deleteModel">
                                {{ $t('settings.llms.deleteModel') }}
                            </UButton>
                        </div>
                    </div>
                </template>
            </div>
        </div>
    </UModal>
</template>

<script setup lang="ts">
// Minimal per-model settings card. Toggles apply immediately (each maps to its
// own endpoint); context window and pricing are drafts committed by Save.
const props = defineProps<{
    modelValue: boolean;
    model: any | null;
}>();

const emit = defineEmits(['update:modelValue', 'updated', 'deleted']);

const { t } = useI18n();
const toast = useToast();

const open = computed({
    get: () => props.modelValue,
    set: (value: boolean) => emit('update:modelValue', value),
});

const contextDraft = ref<number | null>(null);
const costInDraft = ref<number | null>(null);
const costOutDraft = ref<number | null>(null);
const confirmingDelete = ref(false);
const deleting = ref(false);

const deleteBlocked = computed(() => !!props.model && (props.model.is_default || props.model.is_small_default));

const syncDrafts = () => {
    contextDraft.value = props.model?.context_window_tokens ?? null;
    costInDraft.value = props.model?.input_cost_per_million_tokens_usd ?? null;
    costOutDraft.value = props.model?.output_cost_per_million_tokens_usd ?? null;
};

watch(() => [props.modelValue, props.model?.id], () => {
    if (props.modelValue) {
        syncDrafts();
        confirmingDelete.value = false;
    }
}, { immediate: true });

const isDirty = computed(() => {
    if (!props.model) return false;
    const norm = (v: any) => (v == null || v === '' ? null : Number(v));
    return norm(contextDraft.value) !== norm(props.model.context_window_tokens)
        || norm(costInDraft.value) !== norm(props.model.input_cost_per_million_tokens_usd)
        || norm(costOutDraft.value) !== norm(props.model.output_cost_per_million_tokens_usd);
});

const fail = (description: string) => toast.add({ title: 'Error', description, color: 'red' });

const toggleEnabled = async (enabled: boolean) => {
    const response = await useMyFetch(`/llm/models/${props.model.id}/toggle`, { method: 'POST', query: { enabled } });
    if (response.status.value === 'success') emit('updated');
    else fail('Could not update model');
};

const toggleVision = async (enabled: boolean) => {
    const response = await useMyFetch(`/llm/models/${props.model.id}/toggle_vision`, { method: 'POST', query: { enabled } });
    if (response.status.value === 'success') emit('updated');
    else fail('Could not update vision setting');
};

const toggleImageGeneration = async (enabled: boolean) => {
    const response = await useMyFetch(`/llm/models/${props.model.id}/toggle_image_generation`, { method: 'POST', query: { enabled } });
    if (response.status.value === 'success') emit('updated');
    else fail('Could not update image-generation setting');
};

const resetContextWindow = async () => {
    const response = await useMyFetch(`/llm/models/${props.model.id}/set_context_window`, { method: 'POST' });
    if (response.status.value === 'success') {
        emit('updated');
        // The effective value comes back from the catalog on refresh; clear the
        // draft so it re-syncs from the refreshed model prop.
        nextTick(() => syncDrafts());
    } else fail('Could not update context window');
};

const save = async () => {
    const model = props.model;
    const norm = (v: any) => (v == null || v === '' ? null : Number(v));

    // Context window
    if (norm(contextDraft.value) !== norm(model.context_window_tokens)) {
        const tokens = norm(contextDraft.value);
        if (tokens == null || !Number.isFinite(tokens) || tokens <= 0) {
            fail('Context window must be a positive number of tokens');
            return;
        }
        const response = await useMyFetch(`/llm/models/${model.id}/set_context_window`, {
            method: 'POST', query: { tokens: Math.floor(tokens) }
        });
        if (response.status.value !== 'success') { fail('Could not update context window'); return; }
    }

    // Pricing
    const inC = norm(costInDraft.value), outC = norm(costOutDraft.value);
    if (inC !== norm(model.input_cost_per_million_tokens_usd) || outC !== norm(model.output_cost_per_million_tokens_usd)) {
        if ((inC != null && inC < 0) || (outC != null && outC < 0)) {
            fail('Cost must be non-negative');
            return;
        }
        const response = await useMyFetch(`/llm/models/${model.id}/pricing`, {
            method: 'POST',
            body: { input_cost_per_million_tokens_usd: inC, output_cost_per_million_tokens_usd: outC },
        });
        if (response.status.value !== 'success') { fail('Could not update pricing'); return; }
    }

    emit('updated');
    toast.add({ title: 'Model updated', color: 'green' });
    open.value = false;
};

const deleteModel = async () => {
    deleting.value = true;
    try {
        const response = await useMyFetch(`/llm/models/${props.model.id}`, { method: 'DELETE' });
        if (response.status.value === 'success') {
            toast.add({ title: t('settings.llms.modelDeleted'), color: 'green' });
            emit('deleted');
            open.value = false;
        } else {
            const errAny = (response.error as any);
            const err = (errAny && (errAny.value || errAny)) || {};
            const detail = err?.data?.detail || err?.data?.message || err?.message || 'Could not delete model';
            fail(String(detail));
        }
    } finally {
        deleting.value = false;
    }
};
</script>
