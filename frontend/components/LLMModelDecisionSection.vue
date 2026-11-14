<template>
    <!-- Decision-model settings (wire format, path, wire model id, limits,
         timeout, keep_alive, caching) + a one-question connection test.
         Drafts only; the parent card calls save() on Save. -->
    <div class="py-2.5" data-testid="card-decision-section">
        <div class="flex items-center justify-between">
            <UTooltip :text="$t('settings.llms.decision.sectionTooltip')">
                <span class="text-sm font-medium text-gray-700 dark:text-gray-300 underline decoration-dotted decoration-gray-300 underline-offset-2">{{ $t('settings.llms.decision.sectionTitle') }}</span>
            </UTooltip>
            <UButton size="2xs" color="gray" variant="soft" :loading="testing" :disabled="isDirty" data-testid="card-decision-test" @click="runTest">
                {{ isDirty ? $t('settings.llms.decision.saveToTest') : $t('settings.llms.decision.test') }}
            </UButton>
        </div>
        <p v-if="testResult" class="mt-1.5 text-xs" :class="testResult.ok ? 'text-green-700 dark:text-green-400' : 'text-red-600 dark:text-red-400'" data-testid="card-decision-test-result">
            {{ testResult.text }}
        </p>

        <div class="mt-2 grid grid-cols-[1fr,auto] gap-x-4 gap-y-2 items-center text-sm">
            <span class="text-gray-700 dark:text-gray-300">{{ $t('settings.llms.decision.formatLabel') }}</span>
            <select v-model="draft.decision_format" class="bow-native-control w-48" data-testid="card-decision-format">
                <option value="openai">{{ $t('settings.llms.decision.formatOpenai') }}</option>
                <option value="systemone">{{ $t('settings.llms.decision.formatSystemone') }}</option>
            </select>

            <UTooltip :text="$t('settings.llms.decision.pathTooltip')">
                <span class="text-gray-700 dark:text-gray-300 underline decoration-dotted decoration-gray-300 underline-offset-2">{{ $t('settings.llms.decision.pathLabel') }}</span>
            </UTooltip>
            <input v-model="draft.decision_path" type="text" maxlength="200" :placeholder="defaultPath"
                data-testid="card-decision-path"
                class="border border-gray-300 dark:border-gray-600 dark:bg-gray-800 rounded px-2 py-1 w-48 text-sm text-end font-mono focus:outline-none focus:border-blue-500" />

            <UTooltip :text="$t('settings.llms.decision.apiModelTooltip')">
                <span class="text-gray-700 dark:text-gray-300 underline decoration-dotted decoration-gray-300 underline-offset-2">{{ $t('settings.llms.decision.apiModelLabel') }}</span>
            </UTooltip>
            <input v-model="draft.api_model_id" type="text" maxlength="300" :placeholder="model.model_id"
                data-testid="card-decision-api-model"
                class="border border-gray-300 dark:border-gray-600 dark:bg-gray-800 rounded px-2 py-1 w-48 text-sm text-end font-mono focus:outline-none focus:border-blue-500" />

            <UTooltip :text="$t('settings.llms.decision.maxQuestionsTooltip')">
                <span class="text-gray-700 dark:text-gray-300 underline decoration-dotted decoration-gray-300 underline-offset-2">{{ $t('settings.llms.decision.maxQuestionsLabel') }}</span>
            </UTooltip>
            <input v-model.number="draft.max_questions" type="number" min="1" max="1000" step="1"
                data-testid="card-decision-max-questions"
                class="border border-gray-300 dark:border-gray-600 dark:bg-gray-800 rounded px-2 py-1 w-48 text-sm text-end focus:outline-none focus:border-blue-500" />

            <UTooltip :text="$t('settings.llms.decision.timeoutTooltip')">
                <span class="text-gray-700 dark:text-gray-300 underline decoration-dotted decoration-gray-300 underline-offset-2">{{ $t('settings.llms.decision.timeoutLabel') }}</span>
            </UTooltip>
            <input v-model.number="draft.timeout_ms" type="number" min="100" max="120000" step="100"
                data-testid="card-decision-timeout"
                class="border border-gray-300 dark:border-gray-600 dark:bg-gray-800 rounded px-2 py-1 w-48 text-sm text-end focus:outline-none focus:border-blue-500" />

            <template v-if="draft.decision_format === 'systemone'">
                <UTooltip :text="$t('settings.llms.decision.keepAliveTooltip')">
                    <span class="text-gray-700 dark:text-gray-300 underline decoration-dotted decoration-gray-300 underline-offset-2">{{ $t('settings.llms.decision.keepAliveLabel') }}</span>
                </UTooltip>
                <input v-model="draft.keep_alive" type="text" maxlength="20" placeholder="5m"
                    data-testid="card-decision-keep-alive"
                    class="border border-gray-300 dark:border-gray-600 dark:bg-gray-800 rounded px-2 py-1 w-48 text-sm text-end font-mono focus:outline-none focus:border-blue-500" />
            </template>

            <UTooltip :text="$t('settings.llms.decision.cachingTooltip')">
                <span class="text-gray-700 dark:text-gray-300 underline decoration-dotted decoration-gray-300 underline-offset-2">{{ $t('settings.llms.decision.cachingLabel') }}</span>
            </UTooltip>
            <div class="flex justify-end"><UToggle v-model="draft.prompt_caching" data-testid="card-decision-caching" /></div>
        </div>
    </div>
</template>

<script setup lang="ts">
const props = defineProps<{ model: any }>();
const { t } = useI18n();

const KEYS = ['decision_format', 'decision_path', 'api_model_id', 'max_questions', 'timeout_ms', 'keep_alive', 'prompt_caching'] as const;
type Draft = Record<(typeof KEYS)[number], any>;

const effective = computed<Draft>(() => {
    const d = props.model?.decision || {};
    return {
        decision_format: d.decision_format ?? 'systemone',
        decision_path: d.decision_path ?? '',
        api_model_id: d.api_model_id ?? props.model?.model_id ?? '',
        max_questions: d.max_questions ?? 64,
        timeout_ms: d.timeout_ms ?? 1000,
        keep_alive: d.keep_alive ?? '',
        prompt_caching: d.prompt_caching ?? true,
    };
});
const draft = reactive<Draft>({ ...effective.value });
const defaultPath = computed(() => (draft.decision_format === 'openai' ? '/decisions' : '/systemone'));

const sync = () => Object.assign(draft, effective.value);
watch(() => props.model?.id, sync);
watch(effective, sync, { deep: true });

const norm = (v: any) => (v === '' || v === undefined ? null : v);
const changed = computed<Partial<Draft>>(() => {
    const out: Partial<Draft> = {};
    for (const k of KEYS) {
        if (norm(draft[k]) !== norm(effective.value[k])) out[k] = norm(draft[k]);
    }
    return out;
});
const isDirty = computed(() => Object.keys(changed.value).length > 0);

// Returns an error string, or null on success / nothing to save.
const save = async (): Promise<string | null> => {
    if (!isDirty.value) return null;
    const response = await useMyFetch(`/llm/models/${props.model.id}/decision`, { method: 'PUT', body: changed.value });
    if (response.status.value === 'success') return null;
    const errAny = response.error as any;
    const err = (errAny && (errAny.value || errAny)) || {};
    return String(err?.data?.detail || err?.message || t('settings.llms.decision.saveError'));
};

const testing = ref(false);
const testResult = ref<{ ok: boolean; text: string } | null>(null);
const runTest = async () => {
    testing.value = true;
    testResult.value = null;
    try {
        const response = await useMyFetch<any>(`/llm/models/${props.model.id}/test_decision`, { method: 'POST' });
        if (response.status.value === 'success') {
            const r = response.data.value as any;
            testResult.value = {
                ok: true,
                text: t('settings.llms.decision.testOk', { pct: Math.round((r?.probability ?? 0) * 100), ms: r?.latency_ms ?? 0 }),
            };
        } else {
            const errAny = response.error as any;
            const err = (errAny && (errAny.value || errAny)) || {};
            testResult.value = { ok: false, text: String(err?.data?.detail || t('settings.llms.decision.testFailed')) };
        }
    } finally {
        testing.value = false;
    }
};

defineExpose({ isDirty, save });
</script>
