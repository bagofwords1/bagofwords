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
                <Icon v-else-if="openIsLive" name="heroicons:check-circle" class="w-3.5 h-3.5 text-green-600 dark:text-green-400" />
                <Icon v-else name="heroicons:rocket-launch" class="w-3.5 h-3.5" />
                <span v-if="!publishing && !openIsLive && !label"
                    data-testid="unpublished-dot"
                    class="absolute -top-0.5 -end-0.5 w-1.5 h-1.5 bg-amber-500 rounded-full"></span>
            </div>
            <span class="text-xs whitespace-nowrap">{{ buttonText }}</span>
        </button>
    </UTooltip>

    <!-- What is live is not in this version's history (collab01): ask
         before replacing it. Names both versions, and "You" when the one who
         published it is the one asking. -->
    <UModal v-model="sinceOpen" :ui="{ width: 'sm:max-w-md' }">
        <div class="p-5 space-y-3" data-testid="published-since-dialog">
            <div class="flex items-start gap-3">
                <Icon name="heroicons:exclamation-triangle" class="w-5 h-5 text-amber-500 shrink-0 mt-0.5" />
                <div class="space-y-1">
                    <h3 class="text-sm font-semibold text-gray-900 dark:text-gray-100">{{ $t('dashboardCollab.publishSince.title') }}</h3>
                    <p class="text-sm text-gray-600 dark:text-gray-400" data-testid="published-since-body">{{ sinceBody }}</p>
                </div>
            </div>
            <div class="flex flex-wrap justify-end gap-2 pt-2">
                <UButton color="gray" variant="ghost" size="xs" @click="sinceOpen = false">{{ $t('dashboardCollab.publishSince.cancel') }}</UButton>
                <UButton v-if="since?.liveVersionId && since?.liveVersion != null" color="gray" variant="soft" size="xs" icon="i-heroicons-eye"
                    data-testid="published-since-open" @click="openLive">{{ $t('dashboardCollab.publishSince.openLive', { n: since.liveVersion }) }}</UButton>
                <UButton v-else color="gray" variant="soft" size="xs" icon="i-heroicons-arrows-right-left" data-testid="published-since-review" @click="review">{{ $t('dashboardCollab.publishSince.review') }}</UButton>
                <UButton color="amber" size="xs" icon="i-heroicons-rocket-launch" data-testid="published-since-force" @click="publishAnyway">
                    {{ openNumber != null ? $t('dashboardCollab.publishSince.publishNAnyway', { n: openNumber }) : $t('dashboardCollab.publishSince.publishAnyway') }}
                </UButton>
            </div>
        </div>
    </UModal>
</template>

<script setup lang="ts">
import Spinner from '~/components/Spinner.vue';

// `artifact`: the version open in the editor; its artifact_id is the dashboard.
// `label`: the button's text, when not "Publish" ("Publish this version").
const props = defineProps<{ artifact: any; label?: string }>();
const emit = defineEmits<{
    (e: 'published'): void; (e: 'review'): void; (e: 'status', status: any): void
    (e: 'open-version', versionId: string, number: number | null): void
}>();

const { t } = useI18n();
const toast = useToast();
const { getErrorMessage, getErrorCode } = useErrorMessage();
const { relativeTime } = useRelativeTime();

// The 409 "published since your draft started" (collab01).
const sinceOpen = ref(false);
const since = ref<{ by?: string | null; byMe?: boolean; at?: string | null; liveVersionId?: string | null; liveVersion?: number | null } | null>(null);
const sinceWhen = computed(() => (since.value?.at ? relativeTime(since.value.at) : ''));
const { data: sessionUser } = useAuth();
const sinceBody = computed(() => {
    const s = since.value;
    if (!s) return '';
    const n = openNumber.value;
    if (s.liveVersion == null || n == null) {
        return t('dashboardCollab.publishSince.body', { name: s.by || t('dashboardCollab.publishSince.someone'), when: sinceWhen.value });
    }
    return s.byMe
        ? t('dashboardCollab.publishSince.bodyYou', { live: s.liveVersion, n, when: sinceWhen.value })
        : t('dashboardCollab.publishSince.bodyOther', { name: s.by || t('dashboardCollab.publishSince.someone'), live: s.liveVersion, n, when: sinceWhen.value });
});

const status = ref<any>(null);
const starting = ref(false);
const publishing = computed(() => starting.value || status.value?.latest?.status === 'publishing');
const dashboardId = computed(() => props.artifact?.artifact_id || null);

// One vocabulary with the version picker and Settings › Versions:
// "Publish v7", "Live", "Viewers see v5".
const openNumber = computed(() => props.artifact?.version ?? null);
const liveNumber = computed(() => status.value?.published?.source_version_number ?? null);
const openIsLive = computed(() => !!status.value?.published?.source_version_id
    && status.value.published.source_version_id === props.artifact?.id);
const buttonText = computed(() => {
    if (publishing.value) return t('artifactFrame.publishing');
    if (props.label) return props.label;
    if (openIsLive.value) return t('dashboardCollab.versions.live');
    return openNumber.value != null ? t('dashboardCollab.versions.publishN', { n: openNumber.value }) : t('artifactFrame.publish');
});
const tooltip = computed(() => {
    if (publishing.value) return t('artifactFrame.publishing');
    if (!status.value?.published) return t('dashboardCollab.versions.tipNotPublished');
    if (openIsLive.value) return t('dashboardCollab.versions.tipLive');
    return liveNumber.value != null && openNumber.value != null
        ? t('dashboardCollab.versions.tipReplace', { live: liveNumber.value, n: openNumber.value })
        : t('artifactFrame.unpublishedChanges');
});
watch(status, (value) => emit('status', value));

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
                const params = (error.value as any)?.data?.params || {};
                const me = (sessionUser.value as any)?.id;
                since.value = {
                    by: params.by || null, byMe: !!me && params.by_id === me, at: params.at || null,
                    liveVersionId: params.live_version_id || null, liveVersion: params.live_version ?? null,
                };
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

function openLive() {
    sinceOpen.value = false;
    if (since.value?.liveVersionId) emit('open-version', since.value.liveVersionId, since.value.liveVersion ?? null);
}

// A new version (the agent's next edit) or another dashboard: ask again.
watch(() => [dashboardId.value, props.artifact?.id], () => { stopPolling(); load(); }, { immediate: true });
// A request still in flight must not start polling again once we are gone.
onBeforeUnmount(() => { alive = false; stopPolling(); });
</script>
