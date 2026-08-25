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
                <!-- Model ID. Editable only for custom models on providers whose
                     ids the admin owns (Azure deployments, Bedrock model ids,
                     self-hosted OpenAI-compatible models) — see
                     EDITABLE_MODEL_ID_PROVIDER_TYPES on the backend. Everywhere
                     else the id is the model-catalog key, so editing it would
                     detach the row from its pricing and context window. -->
                <div v-if="canEditModelId" class="flex items-center justify-between py-2.5 gap-4">
                    <UTooltip :text="$t('settings.llms.modelIdEditTooltip')">
                        <span class="text-sm text-gray-700 dark:text-gray-300 underline decoration-dotted decoration-gray-300 underline-offset-2">{{ $t('settings.llms.modelIdLabel') }}</span>
                    </UTooltip>
                    <input
                        v-model="modelIdDraft"
                        type="text"
                        maxlength="200"
                        :placeholder="model.model_id"
                        data-testid="card-model-id-input"
                        class="border border-gray-300 dark:border-gray-600 dark:bg-gray-800 rounded px-2 py-1 w-48 text-sm text-end focus:outline-none focus:border-blue-500"
                    />
                </div>
                <!-- Display name. Custom models only — a preset's name is owned by
                     the model catalog and is re-applied on every models-list load,
                     so the backend rejects renaming one. Empty = fall back to the
                     model_id (what custom models were always named before). -->
                <div v-if="model.is_custom" class="flex items-center justify-between py-2.5 gap-4">
                    <UTooltip :text="$t('settings.llms.displayNameTooltip')">
                        <span class="text-sm text-gray-700 dark:text-gray-300 underline decoration-dotted decoration-gray-300 underline-offset-2">{{ $t('settings.llms.displayNameLabel') }}</span>
                    </UTooltip>
                    <input
                        v-model="nameDraft"
                        type="text"
                        maxlength="120"
                        :placeholder="effectiveModelId"
                        data-testid="card-name-input"
                        class="border border-gray-300 dark:border-gray-600 dark:bg-gray-800 rounded px-2 py-1 w-48 text-sm text-end focus:outline-none focus:border-blue-500"
                    />
                </div>
                <!-- Enabled -->
                <div class="flex items-center justify-between py-2.5">
                    <span class="text-sm text-gray-700 dark:text-gray-300">{{ $t('settings.llms.enabledLabel') }}</span>
                    <UTooltip :text="model.is_default || model.is_small_default ? $t('settings.llms.smallDefaultTooltip') : ''" :prevent="!(model.is_default || model.is_small_default)">
                        <UToggle
                            v-model="enabledDraft"
                            :disabled="model.is_default || model.is_small_default"
                            data-testid="card-enabled-toggle"
                        />
                    </UTooltip>
                </div>
                <!-- Vision -->
                <div class="flex items-center justify-between py-2.5">
                    <UTooltip :text="$t('settings.llms.visionTooltip')">
                        <span class="text-sm text-gray-700 dark:text-gray-300 underline decoration-dotted decoration-gray-300 underline-offset-2">{{ $t('settings.llms.colVision') }}</span>
                    </UTooltip>
                    <UToggle v-model="visionDraft" data-testid="card-vision-toggle" />
                </div>
                <!-- Image generation. Disabled for default models: the backend would
                     silently strip the default flags (image models can't be defaults),
                     leaving the org without a default model. -->
                <div class="flex items-center justify-between py-2.5">
                    <UTooltip :text="$t('settings.llms.imageGenTooltip')">
                        <span class="text-sm text-gray-700 dark:text-gray-300 underline decoration-dotted decoration-gray-300 underline-offset-2">{{ $t('settings.llms.colImageGen') }}</span>
                    </UTooltip>
                    <UTooltip :text="$t('settings.llms.imageGenDefaultBlocked')" :prevent="!(model.is_default || model.is_small_default) || imageGenDraft">
                        <UToggle
                            v-model="imageGenDraft"
                            :disabled="(model.is_default || model.is_small_default) && !imageGenDraft"
                            data-testid="card-imagegen-toggle"
                        />
                    </UTooltip>
                </div>
                <!-- Defaults — promotion actions, applied immediately (same as the
                     Actions menu). set_default has no "unset", only promotion of
                     another model, so these are buttons rather than draft toggles. -->
                <div class="flex items-center justify-between py-2.5">
                    <span class="text-sm text-gray-700 dark:text-gray-300">{{ $t('settings.llms.badgeDefault') }}</span>
                    <span v-if="model.is_default" class="text-xs bg-blue-500 text-white px-1.5 py-0.5 rounded-md">{{ $t('settings.llms.badgeDefault') }}</span>
                    <UButton
                        v-else
                        size="2xs" color="gray" variant="soft"
                        :disabled="!model.is_enabled || model.supports_image_generation || settingDefault"
                        data-testid="card-make-default"
                        @click="setDefault(false)"
                    >
                        {{ $t('settings.llms.makeDefault') }}
                    </UButton>
                </div>
                <div class="flex items-center justify-between py-2.5">
                    <UTooltip :text="$t('settings.llms.smallDefaultTooltip')">
                        <span class="text-sm text-gray-700 dark:text-gray-300 underline decoration-dotted decoration-gray-300 underline-offset-2">{{ $t('settings.llms.badgeSmallDefault') }}</span>
                    </UTooltip>
                    <span v-if="model.is_small_default" class="text-xs bg-green-500 text-white px-1.5 py-0.5 rounded-md">{{ $t('settings.llms.badgeSmallDefault') }}</span>
                    <UButton
                        v-else
                        size="2xs" color="gray" variant="soft"
                        :disabled="!model.is_enabled || model.supports_image_generation || settingDefault"
                        data-testid="card-make-small-default"
                        @click="setDefault(true)"
                    >
                        {{ $t('settings.llms.makeSmallDefault') }}
                    </UButton>
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
                            class="border border-gray-300 dark:border-gray-600 dark:bg-gray-800 rounded px-2 py-1 w-36 text-sm text-end focus:outline-none focus:border-blue-500"
                        />
                    </div>
                </div>
                <!-- Temperature. Deliberately tri-state: empty = no temperature
                     parameter is sent and the provider's default applies. The
                     input must never be pre-filled with a number — saving an
                     untouched dialog must not silently set one. -->
                <div class="flex items-center justify-between py-2.5">
                    <UTooltip :text="$t('settings.llms.temperatureTooltip')">
                        <span class="text-sm text-gray-700 dark:text-gray-300 underline decoration-dotted decoration-gray-300 underline-offset-2">{{ $t('settings.llms.colTemperature') }}</span>
                    </UTooltip>
                    <div class="flex items-center gap-1.5">
                        <UTooltip v-if="model.config?.temperature != null" :text="$t('settings.llms.temperatureResetTooltip')">
                            <button type="button" class="text-gray-400 hover:text-gray-600" data-testid="card-temperature-reset" @click="resetTemperature">
                                <UIcon name="i-heroicons-arrow-uturn-left" class="w-3.5 h-3.5" />
                            </button>
                        </UTooltip>
                        <input
                            v-model="temperatureDraft"
                            type="number" min="0" max="2" step="0.1"
                            :placeholder="$t('settings.llms.temperaturePlaceholder')"
                            data-testid="card-temperature-input"
                            class="border border-gray-300 dark:border-gray-600 dark:bg-gray-800 rounded px-2 py-1 w-36 text-sm text-end focus:outline-none focus:border-blue-500"
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
                        <UButton size="sm" class="!bg-blue-500 !text-white" :disabled="!isDirty || saving" :loading="saving" data-testid="card-save-button" @click="save">
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
// Minimal per-model settings card. Every control is a local draft — Save
// applies all changes (each field maps to its own endpoint), Cancel/X
// discards them.
const props = defineProps<{
    modelValue: boolean;
    model: any | null;
}>();

const emit = defineEmits(['update:modelValue', 'updated', 'deleted']);

const { t } = useI18n();
const { getErrorMessage } = useErrorMessage();
const toast = useToast();

const open = computed({
    get: () => props.modelValue,
    set: (value: boolean) => emit('update:modelValue', value),
});

// Custom models only; blank means "use the model_id" (see effectiveName).
const nameDraft = ref<string>('');
const enabledDraft = ref(false);
const visionDraft = ref(false);
const imageGenDraft = ref(false);
const modelIdDraft = ref('');
const contextDraft = ref<number | null>(null);
// String draft so "" (provider default) stays distinct from 0 (an explicit,
// valid temperature).
const temperatureDraft = ref<string>('');
const costInDraft = ref<number | null>(null);
const costOutDraft = ref<number | null>(null);
const confirmingDelete = ref(false);
const deleting = ref(false);
const saving = ref(false);

const deleteBlocked = computed(() => !!props.model && (props.model.is_default || props.model.is_small_default));

// Mirrors EDITABLE_MODEL_ID_PROVIDER_TYPES in backend/app/models/llm_provider.py.
const EDITABLE_MODEL_ID_PROVIDER_TYPES = ['azure', 'custom', 'bedrock'];
const canEditModelId = computed(() =>
    !!props.model?.is_custom
    && EDITABLE_MODEL_ID_PROVIDER_TYPES.includes(props.model?.provider?.provider_type)
);

// Same blank-resets-to-current gesture as the name field: an empty box means
// "leave it alone" rather than an attempt to save an empty id.
const effectiveModelId = computed(() =>
    modelIdDraft.value.trim() || props.model?.model_id || ''
);
const modelIdChanged = computed(() =>
    canEditModelId.value && effectiveModelId.value !== props.model?.model_id
);

// A blank input resolves to the model_id, so clearing the field is the "reset
// to default" gesture and never produces a phantom dirty state. It resolves
// against the id being saved, so clearing the name during an id edit does not
// pin the model to the id it is moving away from.
const effectiveName = computed(() =>
    nameDraft.value.trim() || effectiveModelId.value
);

const syncDrafts = () => {
    nameDraft.value = props.model?.name ?? '';
    modelIdDraft.value = props.model?.model_id ?? '';
    enabledDraft.value = !!props.model?.is_enabled;
    visionDraft.value = !!props.model?.supports_vision;
    imageGenDraft.value = !!props.model?.supports_image_generation;
    contextDraft.value = props.model?.context_window_tokens ?? null;
    temperatureDraft.value = props.model?.config?.temperature != null ? String(props.model.config.temperature) : '';
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
    return modelIdChanged.value
        || (props.model.is_custom && effectiveName.value !== props.model.name)
        || enabledDraft.value !== !!props.model.is_enabled
        || visionDraft.value !== !!props.model.supports_vision
        || imageGenDraft.value !== !!props.model.supports_image_generation
        || norm(contextDraft.value) !== norm(props.model.context_window_tokens)
        || norm(temperatureDraft.value) !== norm(props.model.config?.temperature)
        || norm(costInDraft.value) !== norm(props.model.input_cost_per_million_tokens_usd)
        || norm(costOutDraft.value) !== norm(props.model.output_cost_per_million_tokens_usd);
});

const fail = (description: string) => toast.add({ title: 'Error', description, color: 'red' });

const settingDefault = ref(false);
const setDefault = async (small: boolean) => {
    settingDefault.value = true;
    try {
        const response = await useMyFetch(`/llm/models/${props.model.id}/set_default`, {
            method: 'POST', query: { small }
        });
        if (response.status.value === 'success') {
            emit('updated');
            toast.add({ title: 'Model updated', color: 'green' });
        } else {
            fail('Could not update model');
        }
    } finally {
        settingDefault.value = false;
    }
};

const resetTemperature = async () => {
    // No query param = clear the override; the client stops sending temperature.
    const response = await useMyFetch(`/llm/models/${props.model.id}/set_temperature`, { method: 'POST' });
    if (response.status.value === 'success') {
        emit('updated');
        nextTick(() => { temperatureDraft.value = ''; });
    } else fail('Could not update temperature');
};

const resetContextWindow = async () => {
    const response = await useMyFetch(`/llm/models/${props.model.id}/set_context_window`, { method: 'POST' });
    if (response.status.value === 'success') {
        emit('updated');
        // Re-sync only the context draft from the refreshed model (the catalog
        // value comes back on refresh) — other pending drafts stay untouched.
        nextTick(() => { contextDraft.value = props.model?.context_window_tokens ?? null; });
    } else fail('Could not update context window');
};

const save = async () => {
    const model = props.model;
    const norm = (v: any) => (v == null || v === '' ? null : Number(v));

    // Validate before writing anything, so a partial save can't happen.
    const tokens = norm(contextDraft.value);
    const contextChanged = tokens !== norm(model.context_window_tokens);
    if (contextChanged && (tokens == null || !Number.isFinite(tokens) || tokens <= 0)) {
        fail('Context window must be a positive number of tokens');
        return;
    }
    const temp = norm(temperatureDraft.value);
    const temperatureChanged = temp !== norm(model.config?.temperature);
    if (temperatureChanged && temp != null && (!Number.isFinite(temp) || temp < 0 || temp > 2)) {
        fail('Temperature must be between 0 and 2');
        return;
    }
    const inC = norm(costInDraft.value), outC = norm(costOutDraft.value);
    const pricingChanged = inC !== norm(model.input_cost_per_million_tokens_usd) || outC !== norm(model.output_cost_per_million_tokens_usd);
    if (pricingChanged && ((inC != null && inC < 0) || (outC != null && outC < 0))) {
        fail('Cost must be non-negative');
        return;
    }

    const nameChanged = !!model.is_custom && effectiveName.value !== model.name;
    const idChanged = modelIdChanged.value;

    saving.value = true;
    try {
        // One PATCH for both fields: the backend resolves the name fallback
        // against the incoming model_id, so sending them together keeps a
        // model named after its id in sync with the new id.
        if (nameChanged || idChanged) {
            const response = await useMyFetch(`/llm/models/${model.id}`, {
                method: 'PATCH',
                body: {
                    ...(nameChanged ? { name: effectiveName.value } : {}),
                    ...(idChanged ? { model_id: effectiveModelId.value } : {}),
                },
            });
            if (response.status.value !== 'success') {
                // The model_id guards ship an error_code, so getErrorMessage
                // resolves them against the locale catalog. Anything untagged
                // (the rename guard, a 500) still falls back to the server
                // string and then to the per-field message below.
                const errAny = response.error as any;
                const err = (errAny && (errAny.value || errAny)) || {};
                fail(getErrorMessage(err, idChanged ? t('settings.llms.modelIdError') : t('settings.llms.displayNameError')));
                return;
            }
        }
        if (enabledDraft.value !== !!model.is_enabled) {
            const response = await useMyFetch(`/llm/models/${model.id}/toggle`, { method: 'POST', query: { enabled: enabledDraft.value } });
            if (response.status.value !== 'success') { fail('Could not update model status'); return; }
        }
        if (visionDraft.value !== !!model.supports_vision) {
            const response = await useMyFetch(`/llm/models/${model.id}/toggle_vision`, { method: 'POST', query: { enabled: visionDraft.value } });
            if (response.status.value !== 'success') { fail('Could not update vision setting'); return; }
        }
        if (imageGenDraft.value !== !!model.supports_image_generation) {
            const response = await useMyFetch(`/llm/models/${model.id}/toggle_image_generation`, { method: 'POST', query: { enabled: imageGenDraft.value } });
            if (response.status.value !== 'success') { fail('Could not update image-generation setting'); return; }
        }
        if (contextChanged) {
            const response = await useMyFetch(`/llm/models/${model.id}/set_context_window`, {
                method: 'POST', query: { tokens: Math.floor(tokens as number) }
            });
            if (response.status.value !== 'success') { fail('Could not update context window'); return; }
        }
        if (temperatureChanged) {
            // Empty draft clears the override (no query param); the client then
            // sends no temperature at all.
            const response = await useMyFetch(`/llm/models/${model.id}/set_temperature`, {
                method: 'POST', ...(temp != null ? { query: { temperature: temp } } : {})
            });
            if (response.status.value !== 'success') { fail('Could not update temperature'); return; }
        }
        if (pricingChanged) {
            const response = await useMyFetch(`/llm/models/${model.id}/pricing`, {
                method: 'POST',
                body: { input_cost_per_million_tokens_usd: inC, output_cost_per_million_tokens_usd: outC },
            });
            if (response.status.value !== 'success') { fail('Could not update pricing'); return; }
        }

        emit('updated');
        toast.add({ title: 'Model updated', color: 'green' });
        open.value = false;
    } finally {
        saving.value = false;
    }
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
