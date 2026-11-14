<template>
    <UModal v-model="open" :ui="{ width: 'sm:max-w-lg' }">
        <div class="p-5" data-testid="auto-router-modal">
            <h3 class="text-base font-semibold text-ink">{{ $t('settings.llms.autoRouterModal.title') }}</h3>
            <p class="mt-1 text-sm text-ink-muted">{{ $t('settings.llms.autoRouterModal.question') }}</p>

            <div class="mt-4 space-y-3">
                <!-- Small model (today's behavior) — always preselected on first enable. -->
                <label
                    class="flex items-start gap-3 rounded-lg border p-3 cursor-pointer transition-colors"
                    :class="method === 'small_model' ? 'border-blue-500 bg-blue-50/50 dark:bg-blue-950/30' : 'border-line hover:bg-ink/[0.02]'"
                    data-testid="auto-router-method-small"
                >
                    <input v-model="method" type="radio" name="auto-router-method" value="small_model" class="mt-1" />
                    <span>
                        <span class="block text-sm font-medium text-ink">{{ $t('settings.llms.autoRouterModal.smallTitle') }}</span>
                        <span class="block text-xs text-ink-muted mt-0.5">{{ $t('settings.llms.autoRouterModal.smallBody') }}</span>
                    </span>
                </label>

                <!-- Decision model: picks the starting model upfront. -->
                <label
                    class="flex items-start gap-3 rounded-lg border p-3 transition-colors"
                    :class="[
                        !hasDecisionModels ? 'opacity-60 cursor-not-allowed' : 'cursor-pointer',
                        method === 'decision' ? 'border-blue-500 bg-blue-50/50 dark:bg-blue-950/30' : 'border-line hover:bg-ink/[0.02]',
                    ]"
                    data-testid="auto-router-method-decision"
                >
                    <input
                        v-model="method"
                        type="radio"
                        name="auto-router-method"
                        value="decision"
                        class="mt-1"
                        :disabled="!hasDecisionModels"
                    />
                    <span class="flex-1 min-w-0">
                        <span class="block text-sm font-medium text-ink">{{ $t('settings.llms.autoRouterModal.decisionTitle') }}</span>
                        <span class="block text-xs text-ink-muted mt-0.5">{{ $t('settings.llms.autoRouterModal.decisionBody') }}</span>

                        <span v-if="!hasDecisionModels" class="block text-xs mt-2">
                            <button type="button" class="text-blue-600 hover:underline" data-testid="auto-router-add-decision" @click.prevent="$emit('add-decision-model')">
                                {{ $t('settings.llms.autoRouterModal.addDecisionModel') }}
                            </button>
                        </span>

                        <span v-else-if="method === 'decision'" class="mt-3 grid grid-cols-1 sm:grid-cols-[auto,1fr] gap-x-3 gap-y-2 items-center">
                            <span class="text-xs text-ink-2">{{ $t('settings.llms.autoRouterModal.modelLabel') }}</span>
                            <select v-model="decisionModelId" class="bow-native-control" data-testid="auto-router-decision-model">
                                <option v-for="m in decisionModels" :key="m.id" :value="m.id">
                                    {{ m.name }} ({{ m.provider?.name }})
                                </option>
                            </select>
                            <span class="text-xs text-ink-2">{{ $t('settings.llms.autoRouterModal.minConfidenceLabel') }}</span>
                            <span class="flex items-center gap-2">
                                <input
                                    v-model.number="minConfidencePct"
                                    type="number"
                                    min="0"
                                    max="100"
                                    step="5"
                                    class="bow-native-control w-20"
                                    data-testid="auto-router-min-confidence"
                                />
                                <span class="text-xs text-ink-muted">%</span>
                                <UTooltip :text="$t('settings.llms.autoRouterModal.minConfidenceTooltip')">
                                    <UIcon name="i-heroicons-question-mark-circle" class="w-4 h-4 text-ink-muted" />
                                </UTooltip>
                            </span>
                        </span>
                    </span>
                </label>
            </div>

            <p v-if="!alreadyOn" class="mt-4 text-xs text-ink-muted">{{ $t('settings.llms.autoRouterModal.hintsNote') }}</p>

            <div class="mt-5 flex justify-end gap-2">
                <UButton color="gray" variant="ghost" size="sm" @click="open = false">{{ $t('settings.llms.cancel') }}</UButton>
                <UButton
                    color="blue"
                    size="sm"
                    :loading="saving"
                    :disabled="method === 'decision' && !decisionModelId"
                    data-testid="auto-router-confirm"
                    @click="confirm"
                >
                    {{ alreadyOn ? $t('settings.llms.save') : $t('settings.llms.autoRouterModal.turnOn') }}
                </UButton>
            </div>
        </div>
    </UModal>
</template>

<script setup lang="ts">
type DecisionModel = { id: string; name: string; is_enabled: boolean; provider?: { name: string } };
type RoutingDecision = { method: 'small_model' | 'decision'; decision_model_id: string | null; min_confidence: number };

const props = defineProps<{
    modelValue: boolean;
    decisionModels: DecisionModel[];
    current: RoutingDecision | null;
    alreadyOn: boolean;
    saving?: boolean;
}>();
const emit = defineEmits<{
    'update:modelValue': [value: boolean];
    confirm: [value: RoutingDecision];
    'add-decision-model': [];
}>();

const open = computed({
    get: () => props.modelValue,
    set: (v: boolean) => emit('update:modelValue', v),
});

const decisionModels = computed(() => (props.decisionModels || []).filter((m) => m.is_enabled));
const hasDecisionModels = computed(() => decisionModels.value.length > 0);

const method = ref<'small_model' | 'decision'>('small_model');
const decisionModelId = ref<string | null>(null);
const minConfidencePct = ref<number>(70);

// Reset from the saved settings each time the modal opens. Turning the router
// on always preselects the small model: switching to a decision model is an
// explicit admin choice, never a default.
watch(open, (isOpen) => {
    if (!isOpen) return;
    const cur = props.current;
    method.value = props.alreadyOn && cur?.method === 'decision' && hasDecisionModels.value ? 'decision' : 'small_model';
    const savedId = cur?.decision_model_id && decisionModels.value.some((m) => m.id === cur.decision_model_id)
        ? cur.decision_model_id : null;
    decisionModelId.value = savedId || decisionModels.value[0]?.id || null;
    minConfidencePct.value = Math.round(((cur?.min_confidence ?? 0.7) as number) * 100);
}, { immediate: true });

watch(method, (m) => {
    if (m === 'decision' && !decisionModelId.value) decisionModelId.value = decisionModels.value[0]?.id || null;
});

const confirm = () => {
    const pct = Math.min(Math.max(Number(minConfidencePct.value) || 0, 0), 100);
    emit('confirm', {
        method: method.value,
        decision_model_id: method.value === 'decision' ? decisionModelId.value : (props.current?.decision_model_id ?? null),
        min_confidence: pct / 100,
    });
};
</script>
