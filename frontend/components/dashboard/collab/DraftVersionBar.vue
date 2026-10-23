<template>
    <!-- The draft page's preview bar (collab01): which version is on screen
         and whose, pick another version or draft, publish this one, see
         what changed, the history. Editors only: the draft page opens for
         no one else. -->
    <div class="flex-shrink-0 min-h-9 bg-gray-50 dark:bg-gray-800/60 border-b border-gray-200 dark:border-gray-700 px-2 sm:px-4 py-1 flex flex-wrap items-center gap-x-3 gap-y-1 text-xs"
        data-testid="version-bar">
        <div class="flex items-center gap-1.5 min-w-0 text-gray-600 dark:text-gray-300">
            <Icon name="heroicons:eye" class="w-3.5 h-3.5 shrink-0 text-gray-400" />
            <span class="truncate" data-testid="version-bar-label">
                <template v-if="meta.version_number">{{ $t('dashboardCollab.page.previewingVersion', { n: meta.version_number }) }}</template>
                <template v-else>{{ $t('dashboardCollab.page.previewingDraft') }}</template>
                <template v-if="authorName"> · {{ $t('dashboardCollab.page.byAuthor', { name: authorName }) }}</template>
                <template v-if="meta.version_is_mine"> · <span class="text-emerald-600 dark:text-emerald-400">{{ $t('dashboardCollab.page.yours') }}</span></template>
            </span>
        </div>

        <!-- Version picker: any version of a live editing conversation -->
        <label v-if="versionOptions.length" class="flex items-center gap-1 text-gray-500 dark:text-gray-400">
            <span class="sr-only">{{ $t('dashboardCollab.page.versionPicker') }}</span>
            <select
                :value="selectedVersion"
                data-testid="version-picker"
                :aria-label="$t('dashboardCollab.page.versionPicker')"
                class="h-6 max-w-[200px] rounded border border-gray-200 dark:border-gray-600 bg-white dark:bg-gray-900 text-gray-700 dark:text-gray-200 text-xs ps-1.5 pe-6 py-0"
                @change="onVersion(($event.target as HTMLSelectElement).value)"
            >
                <option value="">{{ $t('dashboardCollab.page.latest') }}</option>
                <option v-for="v in versionOptions" :key="v.id" :value="String(v.number)">{{ v.label }}</option>
            </select>
        </label>

        <!-- Draft picker: only once someone started a second draft -->
        <label v-if="drafts.length > 1" class="flex items-center gap-1 text-gray-500 dark:text-gray-400">
            <span class="sr-only">{{ $t('dashboardCollab.page.draftPicker') }}</span>
            <select
                :value="selectedDraft"
                data-testid="draft-picker"
                :aria-label="$t('dashboardCollab.page.draftPicker')"
                class="h-6 max-w-[180px] rounded border border-gray-200 dark:border-gray-600 bg-white dark:bg-gray-900 text-gray-700 dark:text-gray-200 text-xs ps-1.5 pe-6 py-0"
                @change="onDraft(($event.target as HTMLSelectElement).value)"
            >
                <option v-for="d in drafts" :key="d.id || 'main'" :value="d.id ? (d.slug || '') : ''">{{ d.id ? d.name : $t('dashboardCollab.page.main') }}</option>
            </select>
        </label>

        <button
            v-if="canEdit"
            type="button"
            data-testid="new-draft"
            class="text-gray-400 hover:text-gray-600 dark:hover:text-gray-200 underline-offset-2 hover:underline"
            @click="$emit('new-draft')"
        >{{ $t('dashboardCollab.page.newDraft') }}</button>

        <!-- Someone else's version: the charts are yours to run -->
        <div v-if="meta.version_is_mine === false && (showingAuthorData || withheldCount)" class="flex items-center gap-1.5 text-amber-700 dark:text-amber-300">
            <Icon name="heroicons:user-circle" class="w-3.5 h-3.5 shrink-0" />
            <span class="hidden md:inline">{{ runHint }}</span>
            <button
                type="button"
                data-testid="run-as-me"
                :disabled="isRunning"
                class="flex items-center gap-1 rounded border border-amber-300 dark:border-amber-700 bg-white dark:bg-gray-900 px-1.5 py-0.5 hover:bg-amber-50 dark:hover:bg-amber-900/40 disabled:opacity-60"
                :title="runHint"
                @click="$emit('run-as-me')"
            >
                <Icon name="heroicons:arrow-path" :class="['w-3 h-3', isRunning ? 'animate-spin' : '']" />
                {{ isRunning ? $t('dashboardCollab.page.running') : $t('dashboardCollab.page.runAsMe') }}
            </button>
        </div>

        <div class="ms-auto flex items-center gap-1.5">
            <button
                type="button"
                data-testid="changes-toggle"
                :aria-pressed="changesOpen"
                :class="[
                    'flex items-center gap-1 rounded border px-2 py-1 transition-colors',
                    changesOpen
                        ? 'border-amber-300 dark:border-amber-700 bg-amber-50 dark:bg-amber-900/40 text-amber-800 dark:text-amber-200'
                        : 'border-gray-200 dark:border-gray-700 bg-white dark:bg-gray-900 text-gray-600 dark:text-gray-300 hover:bg-gray-100 dark:hover:bg-gray-800'
                ]"
                @click="$emit('toggle-changes')"
            >
                <Icon name="heroicons:arrows-right-left" class="w-3.5 h-3.5" />
                {{ $t('dashboardCollab.page.changes') }}
            </button>
            <button
                type="button"
                data-testid="history-button"
                class="flex items-center gap-1 rounded border border-gray-200 dark:border-gray-700 bg-white dark:bg-gray-900 text-gray-600 dark:text-gray-300 hover:bg-gray-100 dark:hover:bg-gray-800 px-2 py-1"
                @click="$emit('open-history')"
            >
                <Icon name="heroicons:clock" class="w-3.5 h-3.5" />
                {{ $t('dashboardCollab.page.history') }}
            </button>
            <PublishButton
                v-if="canPublish && meta.version_id"
                :artifact="{ id: meta.version_id, artifact_id: meta.id }"
                :label="$t('dashboardCollab.page.publishThisVersion')"
                @published="$emit('published')"
                @review="$emit('review')"
            />
        </div>
    </div>
</template>

<script setup lang="ts">
import PublishButton from '~/components/dashboard/PublishButton.vue';

const props = defineProps<{
    meta: any;
    capabilities: string[];
    changesOpen: boolean;
    isRunning: boolean;
    // Some chart shows the author's results rather than the reader's own run.
    showingAuthorData?: boolean;
    // Charts withheld from the reader until they run them (e.g. RLS).
    withheldCount?: number;
}>();
defineEmits<{
    (e: 'toggle-changes'): void;
    (e: 'open-history'): void;
    (e: 'run-as-me'): void;
    (e: 'published'): void;
    (e: 'review'): void;
    (e: 'new-draft'): void;
}>();

const { t } = useI18n();
const route = useRoute();
const { relativeTime } = useRelativeTime();

const canEdit = computed(() => props.capabilities.includes('edit'));
const canPublish = computed(() => props.capabilities.includes('publish'));
const authorName = computed(() => props.meta?.version_author?.name || '');
const runHint = computed(() => props.showingAuthorData
    ? t('dashboardCollab.page.notYourVersion', { name: authorName.value || '—' })
    : t('dashboardCollab.page.someNeedYourRun', { count: props.withheldCount || 0 }));
const drafts = computed<any[]>(() => props.meta?.drafts || []);

const selectedVersion = computed(() => (typeof route.query.version === 'string' ? route.query.version : ''));
const selectedDraft = computed(() => (typeof route.query.draft === 'string' ? route.query.draft : ''));

// Versions newest first, only those a live editing conversation still holds
// (the draft page answers "not found" for the rest).
const history = ref<any>(null);
async function loadHistory() {
    const id = props.meta?.id;
    if (!id) return;
    const { data, error } = await useMyFetch(`/api/dashboards/${encodeURIComponent(id)}/history`);
    if (!error.value) history.value = data.value;
}
onMounted(loadHistory);

const versionOptions = computed(() => {
    const h = history.value;
    if (!h) return [];
    const open = new Set((h.conversations || []).filter((c: any) => !c.ended_at).map((c: any) => String(c.report_id)));
    const versions = (h.versions || []) as any[];
    return versions
        .filter((v) => !open.size || open.has(String(v.report_id)))
        .slice()
        .sort((a, b) => b.number - a.number)
        .map((v) => {
            const parts = [t('dashboardCollab.page.versionShort', { n: v.number })];
            if (v.author?.name) parts.push(v.author.name);
            if (v.created_at) parts.push(relativeTime(v.created_at));
            if (v.draft && drafts.value.length > 1) parts.push(v.draft);
            if (v.published) parts.push(t('dashboardCollab.page.publishedTag'));
            return { id: v.id, number: v.number, draft: v.draft as string | null, label: parts.join(' · ') };
        });
});

function onVersion(value: string) {
    const query: Record<string, string> = {};
    if (value) {
        query.version = value;
        // Keep the draft the version belongs to, so the picker beside it agrees.
        const chosen = versionOptions.value.find((v) => String(v.number) === value);
        const draft = drafts.value.find((d) => d.id && d.name === chosen?.draft);
        if (draft?.slug) query.draft = draft.slug;
    } else if (selectedDraft.value) {
        query.draft = selectedDraft.value;
    }
    navigateTo({ path: route.path, query });
}

function onDraft(slug: string) {
    navigateTo({ path: route.path, query: slug ? { draft: slug } : {} });
}
</script>
