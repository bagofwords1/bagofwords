<template>
    <!-- Publish the version open in the editor (docs/design/dashboards-first-class.md,
         phase 5). Shown to whoever may publish this dashboard -- the status
         call answers only them. Same look as the Share button beside it; the
         dot says the open draft is not what readers see. -->
    <UTooltip v-if="status" :text="tooltip">
        <button
            type="button"
            data-testid="publish-dashboard"
            :disabled="publishing"
            @click="publish"
            class="items-center flex gap-1 hover:bg-gray-100 dark:hover:bg-gray-700 rounded border border-gray-200 dark:border-gray-700 bg-gray-50 dark:bg-gray-900 text-gray-600 dark:text-gray-400 px-2 py-1 text-xs disabled:opacity-60 disabled:cursor-not-allowed"
        >
            <div class="relative">
                <Spinner v-if="publishing" class="w-3.5 h-3.5" />
                <Icon v-else name="heroicons:rocket-launch" class="w-3.5 h-3.5" />
                <span v-if="!publishing && status.has_unpublished_changes"
                    data-testid="unpublished-dot"
                    class="absolute -top-0.5 -end-0.5 w-1.5 h-1.5 bg-amber-500 rounded-full"></span>
            </div>
            <span class="text-xs whitespace-nowrap">{{ publishing ? $t('artifactFrame.publishing') : $t('artifactFrame.publish') }}</span>
        </button>
    </UTooltip>
</template>

<script setup lang="ts">
import Spinner from '~/components/Spinner.vue';

// `artifact`: the version open in the editor; its artifact_id is the dashboard.
const props = defineProps<{ artifact: any }>();

const { t } = useI18n();
const toast = useToast();
const { getErrorMessage } = useErrorMessage();

const status = ref<any>(null);
const starting = ref(false);
const publishing = computed(() => starting.value || status.value?.latest?.status === 'publishing');
const dashboardId = computed(() => props.artifact?.artifact_id || null);

const tooltip = computed(() => {
    if (publishing.value) return t('artifactFrame.publishing');
    if (!status.value?.published) return t('artifactFrame.notPublishedYet');
    return status.value.has_unpublished_changes ? t('artifactFrame.unpublishedChanges') : t('artifactFrame.publishedUpToDate');
});

let poll: ReturnType<typeof setTimeout> | null = null;
let alive = true;
function stopPolling() {
    if (poll) clearTimeout(poll);
    poll = null;
}

// One answer about the dashboard. While a publish runs (here or elsewhere),
// ask again until it settles; `announce` toasts the outcome of ours.
async function load(announce = false) {
    const id = dashboardId.value;
    if (!id) { status.value = null; return; }
    const { data, error } = await useMyFetch(`/api/dashboards/${encodeURIComponent(id)}/publish`);
    if (!alive || id !== dashboardId.value) return; // left, or switched dashboards meanwhile
    if (error.value) { status.value = null; return; } // not theirs to publish
    const previous = status.value?.latest?.status;
    status.value = data.value;
    const latest = status.value?.latest;
    if (latest?.status === 'publishing') {
        stopPolling();
        if (alive) poll = setTimeout(() => load(true), 2000);
        return;
    }
    if (announce && previous === 'publishing') {
        if (latest?.status === 'ready') {
            toast.add({ title: t('artifactFrame.published'), color: 'green' });
        } else if (latest?.status === 'failed') {
            toast.add({ title: t('artifactFrame.publishFailed'), description: latest.error || undefined, color: 'red' });
        }
    }
}

async function publish() {
    if (publishing.value || !dashboardId.value) return;
    starting.value = true;
    try {
        const { data, error } = await useMyFetch(`/api/dashboards/${encodeURIComponent(dashboardId.value)}/publish`, {
            method: 'POST',
            body: { version_id: props.artifact?.id || null },
        });
        if (error.value) {
            toast.add({ title: t('artifactFrame.publishFailed'), description: getErrorMessage(error.value), color: 'red' });
            return;
        }
        status.value = data.value;
        stopPolling();
        if (alive) poll = setTimeout(() => load(true), 2000);
    } finally {
        starting.value = false;
    }
}

// A new version (the agent's next edit) or another dashboard: ask again.
watch(() => [dashboardId.value, props.artifact?.id], () => { stopPolling(); load(); }, { immediate: true });
// A request still in flight must not start polling again once we are gone.
onBeforeUnmount(() => { alive = false; stopPolling(); });
</script>
