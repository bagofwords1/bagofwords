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
            @click="publish()"
            class="items-center flex gap-1 hover:bg-gray-100 dark:hover:bg-gray-700 rounded border border-gray-200 dark:border-gray-700 bg-gray-50 dark:bg-gray-900 text-gray-600 dark:text-gray-400 px-2 py-1 text-xs disabled:opacity-60 disabled:cursor-not-allowed"
        >
            <div class="relative">
                <Spinner v-if="publishing" class="w-3.5 h-3.5" />
                <Icon v-else name="heroicons:rocket-launch" class="w-3.5 h-3.5" />
                <span v-if="!publishing && status.has_unpublished_changes"
                    data-testid="unpublished-dot"
                    class="absolute -top-0.5 -end-0.5 w-1.5 h-1.5 bg-amber-500 rounded-full"></span>
            </div>
            <span class="text-xs whitespace-nowrap">{{ publishing ? $t('artifactFrame.publishing') : (label || $t('artifactFrame.publish')) }}</span>
        </button>
    </UTooltip>

    <!-- Someone published since this version's draft started (collab01):
         ask before replacing what readers see with a version that lacks
         their changes. -->
    <UModal v-model="sinceOpen" :ui="{ width: 'sm:max-w-md' }">
        <div class="p-5 space-y-3" data-testid="published-since-dialog">
            <div class="flex items-start gap-3">
                <Icon name="heroicons:exclamation-triangle" class="w-5 h-5 text-amber-500 shrink-0 mt-0.5" />
                <div class="space-y-1">
                    <h3 class="text-sm font-semibold text-gray-900 dark:text-gray-100">{{ $t('dashboardCollab.publishSince.title') }}</h3>
                    <p class="text-sm text-gray-600 dark:text-gray-400">{{ $t('dashboardCollab.publishSince.body', { name: since?.by || $t('dashboardCollab.publishSince.someone'), when: sinceWhen }) }}</p>
                </div>
            </div>
            <div class="flex flex-wrap justify-end gap-2 pt-2">
                <UButton color="gray" variant="ghost" size="xs" @click="sinceOpen = false">{{ $t('dashboardCollab.publishSince.cancel') }}</UButton>
                <UButton color="gray" variant="soft" size="xs" icon="i-heroicons-arrows-right-left" data-testid="published-since-review" @click="review">{{ $t('dashboardCollab.publishSince.review') }}</UButton>
                <UButton color="amber" size="xs" icon="i-heroicons-rocket-launch" data-testid="published-since-force" @click="publishAnyway">{{ $t('dashboardCollab.publishSince.publishAnyway') }}</UButton>
            </div>
        </div>
    </UModal>
</template>

<script setup lang="ts">
import Spinner from '~/components/Spinner.vue';

// `artifact`: the version open in the editor; its artifact_id is the dashboard.
// `label`: the button's text, when not "Publish" ("Publish this version").
const props = defineProps<{ artifact: any; label?: string }>();
const emit = defineEmits<{ (e: 'published'): void; (e: 'review'): void }>();

const { t } = useI18n();
const toast = useToast();
const { getErrorMessage, getErrorCode } = useErrorMessage();
const { relativeTime } = useRelativeTime();

// The 409 "published since your draft started" (collab01).
const sinceOpen = ref(false);
const since = ref<{ by?: string | null; at?: string | null } | null>(null);
const sinceWhen = computed(() => (since.value?.at ? relativeTime(since.value.at) : ''));

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
        if (latest?.status === 'ready') emit('published');
    }
}

async function publish(force = false) {
    if (publishing.value || !dashboardId.value) return;
    starting.value = true;
    try {
        const { data, error } = await useMyFetch(`/api/dashboards/${encodeURIComponent(dashboardId.value)}/publish`, {
            method: 'POST',
            body: { version_id: props.artifact?.id || null, force },
        });
        if (error.value) {
            if (getErrorCode(error.value) === 'dashboard.published_since') {
                const body = (error.value as any)?.data || {};
                since.value = { by: body?.params?.by || null, at: body?.params?.at || null };
                sinceOpen.value = true;
                return;
            }
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

function publishAnyway() {
    sinceOpen.value = false;
    publish(true);
}

function review() {
    sinceOpen.value = false;
    emit('review');
}

// A new version (the agent's next edit) or another dashboard: ask again.
watch(() => [dashboardId.value, props.artifact?.id], () => { stopPolling(); load(); }, { immediate: true });
// A request still in flight must not start polling again once we are gone.
onBeforeUnmount(() => { alive = false; stopPolling(); });
</script>
