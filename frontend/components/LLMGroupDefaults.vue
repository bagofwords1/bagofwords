<template>
    <div class="mt-10" data-testid="llm-group-defaults">
        <div class="mb-4">
            <h2 class="text-base font-semibold text-ink">{{ $t('settings.llms.groupDefaults.title') }}</h2>
            <p class="mt-1 text-sm text-ink-muted leading-relaxed">{{ $t('settings.llms.groupDefaults.subtitle') }}</p>
        </div>

        <!-- Members left without a default (e.g. org default restricted, no group default) -->
        <div
            v-if="uncovered.length > 0"
            class="mb-3 rounded-md border border-amber-200 dark:border-amber-900 bg-amber-50 dark:bg-amber-950/40 p-3"
            data-testid="llm-coverage-warning"
        >
            <div class="flex items-start gap-2">
                <UIcon name="i-heroicons-exclamation-triangle" class="w-4 h-4 text-amber-600 dark:text-amber-400 mt-0.5 flex-shrink-0" />
                <div class="min-w-0">
                    <div class="text-sm font-medium text-amber-800 dark:text-amber-300">
                        {{ $t('settings.llms.groupDefaults.coverageTitle', { count: uncovered.length }) }}
                    </div>
                    <p class="text-xs text-amber-700 dark:text-amber-400 mt-0.5">{{ $t('settings.llms.groupDefaults.coverageHint') }}</p>
                    <ul class="mt-1.5 text-xs text-amber-800 dark:text-amber-300 space-y-0.5">
                        <li v-for="u in uncovered.slice(0, 8)" :key="u.user_id" data-testid="llm-coverage-member">
                            <span class="font-medium">{{ u.name }}</span>
                            <span class="text-amber-600 dark:text-amber-500">
                                — {{ u.fallback_model ? $t('settings.llms.groupDefaults.fallsBackTo', { model: u.fallback_model.name }) : $t('settings.llms.groupDefaults.noModel') }}
                            </span>
                        </li>
                        <li v-if="uncovered.length > 8" class="text-amber-600 dark:text-amber-500">
                            {{ $t('settings.llms.groupDefaults.andMore', { count: uncovered.length - 8 }) }}
                        </li>
                    </ul>
                </div>
            </div>
        </div>

        <div class="bow-table-card">
            <div v-if="loading" class="px-5 py-8 text-sm text-ink-muted text-center">{{ $t('common.loading') }}</div>
            <div v-else-if="rows.length === 0" class="px-5 py-8 text-sm text-ink-muted text-center">
                {{ $t('settings.llms.groupDefaults.empty') }}
            </div>
            <div v-else class="overflow-x-auto">
                <table class="bow-table">
                    <thead>
                        <tr>
                            <th>{{ $t('settings.llms.groupDefaults.colGroup') }}</th>
                            <th>{{ $t('settings.llms.groupDefaults.colDefault') }}</th>
                            <th>{{ $t('settings.llms.groupDefaults.colSmall') }}</th>
                            <th>
                                <UTooltip :text="$t('settings.llms.groupDefaults.priorityTooltip')">
                                    <span class="inline-flex items-center gap-1">
                                        {{ $t('settings.llms.groupDefaults.colPriority') }}
                                        <UIcon name="i-heroicons-question-mark-circle" class="w-3.5 h-3.5 text-ink-muted" />
                                    </span>
                                </UTooltip>
                            </th>
                        </tr>
                    </thead>
                    <tbody>
                        <tr v-for="row in rows" :key="row.group_id" data-testid="llm-group-default-row">
                            <td class="whitespace-nowrap">
                                <span class="inline-flex items-center gap-2 bow-cell-primary">
                                    <UIcon name="i-heroicons-user-group" class="w-4 h-4 text-ink-muted" />
                                    {{ row.group_name }}
                                </span>
                            </td>
                            <td>
                                <select
                                    class="bow-native-control min-w-[13rem]"
                                    :value="row.default_llm_model_id || ''"
                                    :disabled="savingId === row.group_id"
                                    data-testid="llm-group-default-select"
                                    @change="save(row, { default_llm_model_id: ($event.target as HTMLSelectElement).value || null })"
                                >
                                    <option value="">{{ $t('settings.llms.groupDefaults.inherit') }}</option>
                                    <option v-for="m in modelOptions" :key="m.id" :value="m.id">{{ m.label }}</option>
                                </select>
                            </td>
                            <td>
                                <select
                                    class="bow-native-control min-w-[13rem]"
                                    :value="row.small_default_llm_model_id || ''"
                                    :disabled="savingId === row.group_id"
                                    data-testid="llm-group-small-default-select"
                                    @change="save(row, { small_default_llm_model_id: ($event.target as HTMLSelectElement).value || null })"
                                >
                                    <option value="">{{ $t('settings.llms.groupDefaults.inherit') }}</option>
                                    <option v-for="m in modelOptions" :key="m.id" :value="m.id">{{ m.label }}</option>
                                </select>
                            </td>
                            <td>
                                <input
                                    type="number"
                                    min="0"
                                    class="bow-native-control w-20 tabular-nums"
                                    :value="row.default_llm_priority"
                                    :disabled="savingId === row.group_id"
                                    data-testid="llm-group-priority"
                                    @change="onPriority(row, ($event.target as HTMLInputElement).value)"
                                >
                            </td>
                        </tr>
                    </tbody>
                </table>
            </div>
        </div>
    </div>
</template>

<script setup lang="ts">
const props = defineProps<{ models: any[] }>();
const emit = defineEmits(['updated', 'rows']);

const { t } = useI18n();
const toast = useToast();
const { getErrorMessage } = useErrorMessage();

type Row = {
    group_id: string;
    group_name: string;
    default_llm_model_id: string | null;
    small_default_llm_model_id: string | null;
    default_llm_priority: number;
};
type Uncovered = { user_id: string; name: string; email: string; fallback_model: { id: string; name: string } | null };

const rows = ref<Row[]>([]);
const uncovered = ref<Uncovered[]>([]);
const loading = ref(true);
const savingId = ref<string | null>(null);

// Enabled chat models only — the backend rejects anything else.
const modelOptions = computed(() =>
    (props.models || [])
        .filter((m: any) => m.is_enabled && !m.supports_image_generation)
        .map((m: any) => ({ id: m.id, label: `${m.name} · ${m.provider?.name || ''}` }))
);

async function loadRows() {
    const { data } = await useMyFetch('/llm/group-defaults', { method: 'GET' });
    rows.value = (data.value as Row[]) || [];
    emit('rows', rows.value);
}

async function loadCoverage() {
    const { data } = await useMyFetch('/llm/default-coverage', { method: 'GET' });
    uncovered.value = ((data.value as any)?.members_without_default as Uncovered[]) || [];
}

async function refresh() {
    await Promise.all([loadRows(), loadCoverage()]);
}

async function save(row: Row, body: Record<string, any>) {
    savingId.value = row.group_id;
    const { data, error } = await useMyFetch(`/llm/group-defaults/${row.group_id}`, { method: 'PUT', body });
    savingId.value = null;
    if (error.value) {
        toast.add({
            title: t('settings.llms.groupDefaults.saveFailed'),
            description: getErrorMessage(error.value),
            color: 'red',
        });
        await loadRows();
        return;
    }
    Object.assign(row, data.value as Row);
    emit('rows', rows.value);
    toast.add({ title: t('settings.llms.groupDefaults.saved'), color: 'green' });
    await loadCoverage();
    emit('updated');
}

function onPriority(row: Row, raw: string) {
    const n = parseInt(raw, 10);
    if (Number.isNaN(n) || n < 0 || n === row.default_llm_priority) return;
    save(row, { default_llm_priority: n });
}

onMounted(async () => {
    try {
        await refresh();
    } finally {
        loading.value = false;
    }
});

defineExpose({ refresh });
</script>
