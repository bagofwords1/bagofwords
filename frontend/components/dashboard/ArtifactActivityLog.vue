<template>
    <!-- The app activity log: what the app did while its editors (and the
         agent's own preview) used it. Read-only; the same log the agent reads
         through <app_activity> and read_artifact_activity. Editors only. -->
    <UModal v-model="isOpen" :ui="{ width: 'sm:max-w-3xl' }">
        <div class="flex flex-col max-h-[80vh]" data-testid="artifact-activity-log">
            <div class="flex items-center justify-between gap-3 px-5 py-4 border-b border-line">
                <div class="min-w-0">
                    <h2 class="text-sm font-semibold text-ink truncate">{{ $t('artifactActivity.title') }}</h2>
                    <p class="text-2xs text-ink-muted mt-0.5">
                        {{ $t('artifactActivity.retention', { days: retentionDays, count: keepPerReport }) }}
                    </p>
                </div>
                <div class="flex items-center gap-2 shrink-0">
                    <USelectMenu v-model="source" :options="sourceOptions" value-attribute="value" option-attribute="label"
                                 size="xs" class="min-w-[7rem]" data-testid="artifact-activity-source" />
                    <label class="flex items-center gap-1.5 text-xs text-ink-2 cursor-pointer select-none">
                        <UToggle v-model="onlyErrors" size="xs" data-testid="artifact-activity-errors-only" />
                        {{ $t('artifactActivity.errorsOnly') }}
                    </label>
                    <UButton size="xs" color="gray" variant="ghost" icon="i-heroicons-arrow-path" :loading="loading"
                             :aria-label="$t('artifactActivity.refresh')" data-testid="artifact-activity-refresh" @click="load" />
                    <UButton size="xs" color="gray" variant="ghost" icon="i-heroicons-x-mark" :aria-label="$t('common.close')"
                             @click="isOpen = false" />
                </div>
            </div>

            <div class="overflow-y-auto">
                <div v-if="loadError" class="px-5 py-8 text-center text-xs text-red-600 dark:text-red-400" data-testid="artifact-activity-error">
                    {{ loadError }}
                </div>
                <div v-else-if="!loading && events.length === 0" class="px-5 py-10 text-center" data-testid="artifact-activity-empty">
                    <Icon name="heroicons:clipboard-document-list" class="w-6 h-6 mx-auto text-ink-subtle" />
                    <p class="text-sm text-ink-2 mt-2">{{ onlyErrors ? $t('artifactActivity.emptyErrors') : $t('artifactActivity.empty') }}</p>
                    <p class="text-xs text-ink-muted mt-1">{{ $t('artifactActivity.emptyHint') }}</p>
                </div>
                <ul v-else class="divide-y divide-line">
                    <li v-for="e in events" :key="e.id" class="flex items-start gap-3 px-5 py-2" data-testid="artifact-activity-row"
                        :data-failed="e.failed ? 'true' : 'false'">
                        <span class="nums text-2xs text-ink-muted shrink-0 w-24 pt-0.5 whitespace-nowrap" :title="e.created_at || ''">{{ when(e.created_at) }}</span>
                        <span class="text-2xs shrink-0 w-14 pt-0.5 font-medium"
                              :class="e.source === 'preview' ? 'text-violet-700 dark:text-violet-300' : 'text-ink-muted'">
                            {{ e.source === 'preview' ? $t('artifactActivity.sources.preview') : $t('artifactActivity.sources.editor') }}
                        </span>
                        <span class="nums text-2xs text-ink-muted shrink-0 w-8 pt-0.5">{{ e.version != null ? `v${e.version}` : '' }}</span>
                        <span class="min-w-0 flex-1 text-xs break-words" dir="auto"
                              :class="e.failed ? 'text-red-700 dark:text-red-400' : 'text-ink-2'">
                            <Icon v-if="e.failed" name="heroicons:exclamation-circle" class="w-3.5 h-3.5 inline-block align-text-bottom me-1" />{{ e.text }}
                        </span>
                    </li>
                </ul>
            </div>
        </div>
    </UModal>
</template>

<script setup lang="ts">
const props = defineProps<{ artifactId: string, reportId: string }>();

const { t } = useI18n();
const { getErrorMessage } = useErrorMessage();

type Row = {
    id: string, created_at: string | null, kind: string, source: string, version: number | null,
    status: string | null, error_code: string | null, failed: boolean, text: string,
};

const isOpen = ref(false);
const loading = ref(false);
const loadError = ref<string | null>(null);
const events = ref<Row[]>([]);
const onlyErrors = ref(false);
const source = ref<'all' | 'editor' | 'preview'>('all');
const retentionDays = ref(7);
const keepPerReport = ref(500);

const sourceOptions = computed(() => [
    { value: 'all', label: t('artifactActivity.sources.all') },
    { value: 'editor', label: t('artifactActivity.sources.editor') },
    { value: 'preview', label: t('artifactActivity.sources.preview') },
]);

// The time of day; with its date when it was not today, so a log spanning
// days still reads newest-first at a glance.
function when(iso: string | null) {
    if (!iso) return '';
    const d = new Date(iso);
    if (Number.isNaN(d.getTime())) return '';
    const time = d.toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false });
    const today = new Date();
    const sameDay = d.getFullYear() === today.getFullYear() && d.getMonth() === today.getMonth() && d.getDate() === today.getDate();
    return sameDay ? time : `${d.toLocaleDateString(undefined, { month: 'short', day: 'numeric' })} ${time}`;
}

async function load() {
    loading.value = true;
    loadError.value = null;
    try {
        const { data, error } = await useMyFetch(`/api/artifacts/${encodeURIComponent(props.artifactId)}/runtime/activity`, {
            query: {
                report_id: props.reportId,
                only_errors: onlyErrors.value ? 'true' : 'false',
                ...(source.value !== 'all' ? { source: source.value } : {}),
                limit: 100,
            },
        });
        if (error.value) throw error.value;
        const body: any = data.value || {};
        events.value = Array.isArray(body.events) ? body.events : [];
        if (body.retention_days) retentionDays.value = body.retention_days;
        if (body.keep_per_report) keepPerReport.value = body.keep_per_report;
    } catch (e: any) {
        loadError.value = getErrorMessage(e);
    } finally {
        loading.value = false;
    }
}

watch([onlyErrors, source], () => { if (isOpen.value) load(); });

function open() {
    isOpen.value = true;
    load();
}

defineExpose({ open });
</script>
