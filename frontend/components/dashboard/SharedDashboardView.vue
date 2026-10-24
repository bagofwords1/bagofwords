<template>
    <!-- Access error overlay -->
    <div v-if="accessError" class="h-dvh w-screen flex items-center justify-center bg-gray-50 dark:bg-gray-900">
        <div class="text-center max-w-md mx-auto px-6">
            <Icon :name="accessError === 'login' ? 'heroicons:lock-closed' : 'heroicons:shield-exclamation'" class="w-12 h-12 mx-auto mb-4 text-gray-400" />
            <h2 class="text-lg font-semibold text-gray-900 dark:text-white mb-2">
                {{ accessError === 'login' ? 'Sign in required' : 'Access denied' }}
            </h2>
            <p class="text-sm text-gray-500 dark:text-gray-400 mb-6">
                {{ accessError === 'login'
                    ? 'You need to sign in to view this dashboard.'
                    : 'You don\'t have permission to view this dashboard. Ask the owner to share it with you.' }}
            </p>
            <a v-if="accessError === 'login'" :href="`/users/sign-in?redirect=${api.pagePath}`"
                class="inline-flex items-center gap-2 px-4 py-2 text-sm font-medium text-white bg-blue-600 rounded-lg hover:bg-blue-700">
                Sign in
            </a>
            <a v-else href="/"
                class="inline-flex items-center gap-2 px-4 py-2 text-sm font-medium text-gray-700 dark:text-gray-300 bg-white dark:bg-gray-900 border border-gray-300 dark:border-gray-600 rounded-lg hover:bg-gray-50 dark:hover:bg-gray-800">
                Go home
            </a>
        </div>
    </div>

    <div v-else class="h-dvh w-screen relative bg-gray-50 dark:bg-gray-900 flex flex-col overflow-hidden">
        <!-- Top Bar -->
        <div v-if="showTopBar && reportLoaded" class="flex-shrink-0 h-10 bg-white dark:bg-gray-900 border-b border-gray-200 dark:border-gray-700 relative flex items-center">
            <!-- Left: Back to app (absolute; fits inside the start content gutter) -->
            <a
                href="/"
                class="absolute start-2 sm:start-4 top-1/2 -translate-y-1/2 flex items-center gap-1 text-xs text-gray-400 hover:text-gray-600 transition-colors"
            >
                <Icon name="heroicons:arrow-left" class="w-3.5 h-3.5 rtl-flip" />
                <span class="hidden sm:inline">Back to app</span>
            </a>

            <!-- Center: Tab Menu + Refreshed. Tabs keep the dashboard content
                 padding on the start side; the end side flexes against the
                 in-flow action cluster so nothing can overlap it. -->
            <div class="h-full flex-1 min-w-0 flex items-center">
                <div class="w-full min-w-0 flex items-center justify-between gap-2 ps-11 sm:ps-[200px] pe-2 sm:pe-4">
                    <!-- Tab Menu -->
                    <div class="flex items-center gap-1 flex-shrink-0">
                        <button
                            @click="activeTab = 'report'"
                            :class="[
                                'px-3 py-1.5 text-xs font-medium rounded transition-colors',
                                activeTab === 'report'
                                    ? 'bg-gray-100 dark:bg-gray-800 text-gray-900 dark:text-white'
                                    : 'text-gray-500 dark:text-gray-400 hover:text-gray-700 dark:hover:text-gray-300 hover:bg-gray-50 dark:hover:bg-gray-800'
                            ]"
                        >
                            Report
                        </button>
                        <button
                            v-if="showDataTab"
                            @click="activeTab = 'data'"
                            :class="[
                                'px-3 py-1.5 text-xs font-medium rounded transition-colors',
                                activeTab === 'data'
                                    ? 'bg-gray-100 dark:bg-gray-800 text-gray-900 dark:text-white'
                                    : 'text-gray-500 dark:text-gray-400 hover:text-gray-700 dark:hover:text-gray-300 hover:bg-gray-50 dark:hover:bg-gray-800'
                            ]"
                        >
                            Data ({{ visualizationsData.length }})
                        </button>
                    </div>

                    <!-- Refreshed text (hidden on mobile where the bar is too
                         crowded; truncates instead of overlapping the actions) -->
                    <span v-if="runError" class="hidden sm:inline min-w-0 text-[11px] text-red-400 truncate max-w-[300px]" :title="runError">
                        {{ runError }}
                    </span>
                    <span v-else-if="isRefreshingOnView" class="hidden sm:inline-flex items-center gap-1 min-w-0 text-[11px] text-gray-400">
                        <Icon name="heroicons:arrow-path" class="w-3 h-3 animate-spin flex-shrink-0" />
                        <span class="truncate">Refreshing...</span>
                    </span>
                    <span v-else-if="lastRefreshedAt" class="hidden sm:inline min-w-0 text-[11px] text-gray-400 truncate">
                        Refreshed {{ formatTime(lastRefreshedAt) }}
                    </span>
                </div>
            </div>

            <!-- Right: Run + Fork + Edit Report + Close (in normal flow so the
                 bar always reserves the cluster's width — no overlap however
                 many buttons are visible) -->
            <div class="flex-shrink-0 h-full pe-2 sm:pe-4 flex items-center gap-1 sm:gap-2">
                <!-- Who else is editing (collab01): editors only -->
                <PresenceAvatars v-if="presenceEnabled" :others="presenceOthers" class="hidden sm:flex" />
                <!-- Run button: signed-in non-owner viewers re-run the dashboard's
                     queries into their own per-user results (whose credentials run
                     is the owner's share setting; never touches the shared data).
                     The draft page's bar has its own "Run as me". -->
                <button
                    v-if="canRun && source !== 'draft'"
                    @click="handleRun"
                    :disabled="isRunning"
                    class="flex items-center gap-1 text-xs text-gray-400 hover:text-gray-600 transition-colors disabled:opacity-50"
                    title="Re-run this dashboard's queries for you"
                >
                    <Icon name="heroicons:arrow-path" :class="['w-3.5 h-3.5', isRunning ? 'animate-spin' : '']" />
                    <span class="hidden sm:inline">{{ isRunning ? 'Running...' : 'Run' }}</span>
                </button>
                <!-- Export PDF: server-side render of the shared snapshot,
                     same ReportPdfService pipeline as the emailed dashboard
                     share. Hidden for doc artifacts (no server renderer) and
                     when the snapshot is withheld (backend refuses anyway). -->
<!-- One button for both exports, from the same availability list the
                     in-app viewer uses. Hidden when the snapshot is withheld: the
                     file would carry that data past every gate we have. -->
                <ExportMenu
                    :options="availableExports"
                    :busy="isExportingPdf || isExportingHtml || isExportingPptx"
                    variant="subtle"
                    @select="handleExport"
                />
                                <!-- Fork button -->
                <button
                    v-if="forkEligibility?.can_fork"
                    @click="handleFork"
                    :disabled="isForking"
                    class="flex items-center gap-1 text-xs text-gray-400 hover:text-gray-600 transition-colors disabled:opacity-50"
                >
                    <Icon name="heroicons:arrow-path-rounded-square" class="w-3.5 h-3.5" />
                    <span class="hidden sm:inline">{{ isForking ? 'Forking...' : 'Fork' }}</span>
                </button>
                <span
                    v-else-if="forkEligibility && !forkEligibility.can_fork"
                    class="text-[10px] text-gray-300 dark:text-gray-600 cursor-default"
                    :title="forkReasonLabel"
                >
                    <Icon name="heroicons:arrow-path-rounded-square" class="w-3.5 h-3.5 inline" />
                </span>
                <!-- View analytics (collab01): the existing artifact analytics,
                     for whoever holds the `analytics` capability -->
                <button
                    v-if="hasCap('analytics')"
                    type="button"
                    data-testid="analytics-button"
                    @click="analyticsOpen = true"
                    class="flex items-center gap-1 text-xs text-gray-400 hover:text-gray-600 transition-colors"
                    :title="$t('artifactResources.analytics')"
                >
                    <Icon name="heroicons:chart-bar" class="w-3.5 h-3.5" />
                    <span class="hidden sm:inline">{{ $t('artifactResources.analytics') }}</span>
                </button>
                <NuxtLink
                    v-if="source === 'report' && isOwner && editPath"
                    :to="editPath"
                    class="flex items-center gap-1 text-xs text-gray-400 hover:text-gray-600 transition-colors"
                >
                    <Icon name="heroicons:pencil-square" class="w-3.5 h-3.5" />
                    <span class="hidden sm:inline">Edit Report</span>
                </NuxtLink>
                <!-- A dashboard's editors (collab01): Edit opens their own
                     conversation on it -- the one they have, else a new copy
                     of the draft (the named draft on screen, if any) -->
                <button
                    v-else-if="source !== 'report' && hasCap('edit')"
                    type="button"
                    data-testid="edit-in-my-conversation"
                    @click="handleEdit"
                    :disabled="isOpeningEditor"
                    class="flex items-center gap-1 text-xs text-gray-400 hover:text-gray-600 transition-colors disabled:opacity-50"
                    :title="$t('dashboardCollab.page.editInMyConversation')"
                >
                    <Icon :name="isOpeningEditor ? 'heroicons:arrow-path' : 'heroicons:pencil-square'" :class="['w-3.5 h-3.5', isOpeningEditor ? 'animate-spin' : '']" />
                    <span class="hidden sm:inline">{{ $t('dashboardCollab.page.editInMyConversation') }}</span>
                </button>
                <button
                    @click="showTopBar = false"
                    class="p-1 text-gray-400 hover:text-gray-600 hover:bg-gray-100 dark:hover:bg-gray-700 rounded transition-colors"
                >
                    <Icon name="heroicons:x-mark" class="w-4 h-4" />
                </button>
            </div>
        </div>

        <!-- The draft page: which version is previewed, and what to do with it -->
        <DraftVersionBar
            v-if="source === 'draft' && showTopBar && reportLoaded && dashboardMeta"
            :meta="dashboardMeta"
            :capabilities="capabilities"
            :changes-open="changesOpen"
            :is-running="isRunning"
            :showing-author-data="showingAuthorData"
            :withheld-count="withheldCharts.length"
            @toggle-changes="toggleChanges"
            @open-history="historyOpen = true"
            @run-as-me="handleRun"
            @published="emit('reload')"
            @review="openChanges"
        />

        <!-- Made with Bag of words badge -->
        <a v-if="report.general?.bow_credit !== false"
           href="https://bagofwords.com"
           target="_blank"
           :style="{ bottom: `${20 + cornerLift}px` }"
           class="fixed z-[1000] end-5 block bg-black text-gray-200 font-light px-2 py-1 rounded-md text-xs hover:bg-gray-800 transition-colors">
            Made with <span class="font-bold text-white">Bag of words</span>
        </a>

        <!-- Main Content Area -->
        <div class="flex-1 min-h-0 relative">
            <!-- Report Tab: Artifact/Dashboard Content -->
            <template v-if="activeTab === 'report'">
                <!-- Snapshot withheld: this dashboard's data is per-user, so the
                     page auto-runs the queries for the viewer (gate shows
                     'loading'); the other gate states are the fallback when
                     auto-run can't succeed (anonymous, missing connection,
                     run failure). -->
                <div v-if="showViewerGate" class="absolute inset-0 z-20 flex items-center justify-center bg-white/85 dark:bg-gray-900/85">
                    <ViewerRunGate :state="gateState" :report-id="id" :return-path="api.pagePath"
                        :is-running="isRunning" :source-errors="dataSourceErrors"
                        :error-message="gateErrorMessage" :source-type="gateSourceType" @run="handleRun" />
                </div>
                <!-- Some charts withheld from this viewer: the rest render; say
                     which are missing and why. (This page has no i18n context.) -->
                <div v-else-if="partiallyWithheld"
                     class="absolute top-2 left-1/2 -translate-x-1/2 z-20 max-w-[90%] flex items-start gap-1.5 px-3 py-1.5 rounded-lg bg-amber-50 dark:bg-amber-900/40 border border-amber-200 dark:border-amber-700 text-amber-800 dark:text-amber-200 shadow text-[11px]">
                    <Icon name="heroicons:lock-closed" class="w-3.5 h-3.5 shrink-0 mt-0.5" />
                    <div>
                        <div v-if="withheldCharts.some(c => c.noAccess)">
                            You don't have access to the data for: {{ withheldCharts.filter(c => c.noAccess).map(c => c.title).join(', ') }}
                        </div>
                        <div v-if="withheldCharts.some(c => !c.noAccess)">
                            Couldn't load: {{ withheldCharts.filter(c => !c.noAccess).map(c => c.title).join(', ') }}
                        </div>
                    </div>
                </div>

                <!-- Slides with Preview Images - Use SlideViewer -->
                <SlideViewer
                    v-if="hasSlidesWithPreviews && artifact"
                    :artifact-id="artifact.id"
                    :previews-base="api.previewsBase || undefined"
                    class="absolute inset-0"
                />

                <!-- Slides without previews: python-pptx source is not
                     browser-renderable — show a fallback, never the iframe -->
                <div v-else-if="slidesPreviewsMissing && artifact" class="absolute inset-0 flex items-center justify-center text-gray-400">
                    <div class="text-center">
                        <Icon name="heroicons:presentation-chart-bar" class="w-12 h-12 mx-auto mb-3 opacity-50" />
                        <p>Slide previews are not available for this presentation</p>
                    </div>
                </div>

                <!-- Shared Document - read-only DocViewer -->
                <DocViewer
                    v-else-if="isDocMode && artifact"
                    :markdown="artifact.content?.markdown || ''"
                    :visualizations="visualizationsData"
                    class="absolute inset-0"
                />

                <!-- Artifact Content - Full screen (modern reports with artifacts) -->
                <iframe
                    v-else-if="hasArtifacts && iframeSrcdoc && !hasSlidesWithPreviews && !isDocMode"
                    ref="artifactIframeRef"
                    :srcdoc="iframeSrcdoc"
                    @load="onArtifactIframeLoad"
                    sandbox="allow-scripts allow-downloads allow-forms"
                    class="absolute inset-0 w-full h-full border-0 bg-white"
                />

                <!-- Loading state -->
                <div v-else-if="!reportLoaded" class="absolute inset-0 flex items-center justify-center text-gray-400">
                    <div class="text-center">
                        <Icon name="heroicons:document-chart-bar" class="w-12 h-12 mx-auto mb-3 opacity-50" />
                        <p>Loading...</p>
                    </div>
                </div>

                <div v-else-if="artifactLoadFailed" role="alert" class="absolute inset-0 flex items-center justify-center text-gray-500">
                    <p>{{ $t('reports.artifactLoadFailed') }}</p>
                </div>

                <!-- Empty state (no artifacts) -->
                <div v-else class="absolute inset-0 flex items-center justify-center text-gray-400">
                    <div class="text-center">
                        <Icon name="heroicons:document-chart-bar" class="w-12 h-12 mx-auto mb-3 opacity-50" />
                        <p>No dashboard available</p>
                    </div>
                </div>
            </template>

            <!-- Data Tab: Visualizations List -->
            <div v-else-if="activeTab === 'data'" class="absolute inset-0 overflow-y-auto bg-gray-50 dark:bg-gray-900 p-4">
                <!-- Snapshot withheld: the data tables would be empty — show the
                     same viewer gate as the Report tab. -->
                <div v-if="showViewerGate" class="flex items-center justify-center h-full">
                    <ViewerRunGate :state="gateState" :report-id="id" :return-path="api.pagePath"
                        :is-running="isRunning" :source-errors="dataSourceErrors"
                        :error-message="gateErrorMessage" :source-type="gateSourceType" @run="handleRun" />
                </div>
                <div v-else-if="visualizationsData.length === 0" class="flex items-center justify-center h-full text-gray-400">
                    <p>No visualizations available</p>
                </div>
                <div v-else class="max-w-4xl mx-auto space-y-2">
                    <ToolWidgetPreview
                        v-for="viz in toolExecutions"
                        :key="viz.id"
                        :tool-execution="viz"
                        :readonly="true"
                        :initial-collapsed="true"
                    />
                </div>
            </div>

            <!-- Charts still running with this editor's credentials (a joined
                 copy): they fill in as they land -->
            <div v-if="activeTab === 'report' && pendingQueries.length"
                 data-testid="chart-pending"
                 :title="pendingQueries.map(q => q.title).filter(Boolean).join(', ')"
                 class="absolute bottom-4 left-1/2 -translate-x-1/2 z-20 flex items-center gap-1.5 px-3 py-1.5 rounded-full bg-sky-50 dark:bg-sky-900/50 border border-sky-200 dark:border-sky-700 text-sky-800 dark:text-sky-200 shadow text-[11px] whitespace-nowrap">
                <Icon name="heroicons:arrow-path" class="w-3.5 h-3.5 animate-spin shrink-0" />
                {{ $t('dashboardCollab.page.runningAsYou') }}
                <span class="tabular-nums text-sky-600 dark:text-sky-300">({{ pendingQueries.length }})</span>
            </div>

            <DashboardChangesPanel
                v-if="changesOpen"
                :changes="changes"
                :loading="changesLoading"
                :error="changesError"
                :located="locatedKeys"
                :active-key="activeChangeKey"
                @close="changesOpen = false"
                @highlight="activeChangeKey = $event"
            />
        </div>

        <!-- Floating chat (owner setting; the bubble itself resolves per-viewer
             availability — sign-in prompt for anonymous, member check inside) -->
        <ArtifactChatBubble
            v-if="reportLoaded && report?.artifact_chat_enabled && api.chatBase"
            :report-id="id"
            :base="api.chatBase"
            :raised="report.general?.bow_credit !== false"
            :lift="cornerLift"
        />

        <ArtifactResourceExplorer
            v-if="analyticsOpen && dashboardMeta?.id"
            :artifact-id="dashboardMeta.id"
            view="analytics"
            @close="analyticsOpen = false"
        />
        <DashboardHistoryModal
            v-if="source === 'draft' && dashboardMeta?.id"
            v-model="historyOpen"
            :dashboard-id="dashboardMeta.id"
            :capabilities="capabilities"
            @rolled-back="emit('reload')"
            @transferred="emit('reload')"
        />
        <!-- This page has no layout: Publish and Edit report through toasts -->
        <UNotifications v-if="source !== 'report'" />
    </div>
</template>

<script setup lang="ts">
import type { ExportFormat } from '~/composables/useArtifactExports'
import ToolWidgetPreview from '~/components/tools/ToolWidgetPreview.vue';
import SlideViewer from '~/components/dashboard/SlideViewer.vue';
import DocViewer from '~/components/dashboard/DocViewer.vue';
import ViewerRunGate from '~/components/dashboard/ViewerRunGate.vue';
import ArtifactChatBubble from '~/components/report/ArtifactChatBubble.vue';
import { buildArtifactIframeHtml, isHtmlSlidesCode } from '~/utils/artifactIframe';
import { exportsHidden } from '~/utils/dashboardExportsHidden';
import { sharedDashboardApi, type SharedDashboardSource } from '~/utils/sharedDashboardApi';
import ArtifactResourceExplorer from '~/components/ArtifactResourceExplorer.vue';
import DashboardHistoryModal from '~/components/dashboard/DashboardHistoryModal.vue';
import PresenceAvatars from '~/components/dashboard/collab/PresenceAvatars.vue';
import DraftVersionBar from '~/components/dashboard/collab/DraftVersionBar.vue';
import DashboardChangesPanel from '~/components/dashboard/collab/DashboardChangesPanel.vue';
import {
    chartOverlayScriptTag, vizIdsForKey, COLLAB_MARKS_MESSAGE, COLLAB_LOCATED_MESSAGE, type ChartMark,
} from '~/components/dashboard/collab/chartOverlay';

// The shared dashboard page: a conversation's /r/{id}, a published
// dashboard's /d/{id}, or its draft for its editors (/d/{id}/draft) -- the
// same page over each one's endpoints (utils/sharedDashboardApi).
const props = defineProps<{ source: SharedDashboardSource; id: string }>();
// `reload`: the page (keyed) mounts this view afresh -- after a publish, a
// roll back or a transfer changed what it shows or what the reader may do.
const emit = defineEmits<{ (e: 'reload'): void }>();
const api = sharedDashboardApi(props.source, props.id);
const route = useRoute();
const { data: currentUser } = useAuth();
const { t } = useI18n();
const toast = useToast();
const { getErrorMessage } = useErrorMessage();
const { relativeTime } = useRelativeTime();

// The draft page previews any version (?version=N) or named draft
// (?draft=slug, collab01). Every draft read takes the same two params, so
// they are threaded into each draft URL here, once (the page is keyed on its
// full path: another version is another mount).
const draftQuery = (() => {
    if (props.source !== 'draft') return '';
    const qs = new URLSearchParams();
    const version = route.query.version;
    if (typeof version === 'string' && /^\d{1,9}$/.test(version)) qs.set('version', version);
    const draft = route.query.draft;
    if (typeof draft === 'string' && draft) qs.set('draft', draft.slice(0, 120));
    return qs.toString();
})();
function withDraftQuery(url: string, query = draftQuery) {
    return query ? `${url}${url.includes('?') ? '&' : '?'}${query}` : url;
}
if (draftQuery) {
    const { queries, step, embedToken, paramRun } = api;
    api.meta = withDraftQuery(api.meta);
    if (api.content) api.content = withDraftQuery(api.content);
    api.queries = (artifactId?: string) => withDraftQuery(queries(artifactId));
    api.step = (queryId: string) => withDraftQuery(step(queryId));
    api.embedToken = (fileId: string) => withDraftQuery(embedToken(fileId));
    api.paramRun = (queryId: string) => withDraftQuery(paramRun(queryId));
}

// What the reader may do here (collab01): the page meta's capabilities, not
// ownership. A conversation's /r page has none of these buttons.
const capabilities = computed<string[]>(() =>
    (props.source === 'report' ? [] : (dashboardMeta.value?.capabilities || [])) as string[]);
function hasCap(cap: string) {
    return capabilities.value.includes(cap);
}
const analyticsOpen = ref(false);
const historyOpen = ref(false);

const report = ref<any>({
    title: '',
    id: '',
    user: { name: '' },
    general: {}
});

const artifact = ref<any>(null);
// Viewer identity for ARTIFACT_DATA.current_user. Stays null for anonymous
// viewers (401) or any fetch failure — the artifact renders anonymously.
const viewerContext = ref<any>(null);
async function fetchViewerContext() {
    try {
        const { data } = await useMyFetch('/api/users/me/viewer_context');
        viewerContext.value = data.value || null;
    } catch {
        viewerContext.value = null;
    }
}
const visualizationsData = ref<any[]>([]);
const filesData = ref<any[]>([]);
const hasArtifacts = ref(false);
const artifactLoadFailed = ref(false);
const reportLoaded = ref(false);
const dataReady = ref(false);

// Check if current user is the report owner. A dashboard says so itself
// (its owner, while still in its organization); its draft says whether the
// reader edits it (the organization's admin only reads it).
const dashboardOwner = ref(false);
// Whose rows the page shows: the owner sees the shared ones; anyone else
// (an editor who is not the owner, the organization's admin) runs it for
// their own. On a dashboard the same as isOwner; on a draft its own flag.
const draftDataOwner = ref(false);
const isOwner = computed(() => {
    if (props.source === 'draft' || props.source === 'dashboard') return dashboardOwner.value;
    const userId = (currentUser.value as any)?.user?.id || (currentUser.value as any)?.id;
    return userId && report.value?.user?.id === userId;
});
// The conversation the Edit button opens: the report itself, or the draft's
// editing conversation. A published dashboard asks the server (handleEdit).
const draftReportId = ref<string | null>(null);
const editPath = computed(() => {
    if (props.source === 'report') return `/reports/${props.id}`;
    // The editor opens on this dashboard, not on the conversation's newest.
    if (props.source === 'draft' && draftReportId.value) {
        const dashboard = dashboardMeta.value?.id;
        return `/reports/${draftReportId.value}${dashboard ? `?dashboard=${encodeURIComponent(dashboard)}` : ''}`;
    }
    return null;
});
const isOpeningEditor = ref(false);
// "Edit in my conversation" (collab01): the reader's own conversation on this
// dashboard -- theirs if they have one, else a new copy of the draft (the
// named draft on screen, when one is). A copy of someone else's work re-runs
// with the reader's credentials; the conversation shows that itself.
async function handleEdit() {
    if (isOpeningEditor.value) return;
    isOpeningEditor.value = true;
    try {
        const dashboardId = dashboardMeta.value?.id || report.value.id;
        const named = props.source === 'draft' ? dashboardMeta.value?.draft : null;
        const { data, error } = await useMyFetch(`/api/dashboards/${encodeURIComponent(dashboardId)}/edit`, {
            method: 'POST',
            ...(named?.id && named?.slug ? { body: { draft: named.slug } } : {}),
        });
        if (error.value) {
            const message = getErrorMessage(error.value);
            runError.value = message;
            toast.add({ title: t('dashboardCollab.page.editFailed'), description: message, color: 'red' });
            return;
        }
        const reportId = (data.value as any)?.report_id;
        if (reportId) {
            navigateTo(`/reports/${reportId}?dashboard=${encodeURIComponent(dashboardId)}`);
        }
    } catch (e) {
        console.error('Failed to open the editor:', e);
    } finally {
        isOpeningEditor.value = false;
    }
}

// The badge and chat bubble share the bottom-end corner with Intercom's
// launcher when a client-side navigation carried it over from the app (e.g.
// the post-sign-in redirect). Stack them above it, not under it: the chat
// panel opens upward from the bubble, so a launcher on top would cover it.
const intercomClearance = useIntercomLauncherClearance();
const cornerLift = computed(() => (intercomClearance.value ? intercomClearance.value - 8 : 0));

// Top bar state
const showTopBar = ref(true);
const activeTab = ref<'report' | 'data'>('report');
// The owner's "Include Data Tab" share setting. The report loads after mount,
// so this is true until proven otherwise — anything but an explicit false
// keeps today's behavior (tab shown), including on older reports.
const showDataTab = computed(() => report.value?.include_data_tab !== false);
// The report arrives asynchronously: if the viewer already opened the Data tab
// before it resolved, fall back to the report view rather than leaving them on
// a panel whose tab just disappeared.
watch(showDataTab, (visible) => {
    if (!visible && activeTab.value === 'data') activeTab.value = 'report';
});
const lastRefreshedAt = ref<Date | null>(null);
const isRefreshingOnView = ref(false);

// Viewer run state ("Run" button — signed-in non-owner viewers only)
const isRunning = ref(false);
const showingAuthorData = ref(false);
const runError = ref<string | null>(null);
// Newest per-viewer result timestamp seen while loading step data
const viewerLastRunAt = ref<Date | null>(null);
// True when the backend hid the owner's snapshot from this viewer
// (viewer-identity sharing on user-scoped data sources, or RLS)
const snapshotWithheld = ref(false);
// Per-chart view of the same policy (mirrors ArtifactFrame): a viewer refused
// ONE dataset keeps every chart they can read; only these are called out.
const withheldCharts = ref<Array<{ title: string; noAccess: boolean }>>([]);
const allWithheld = ref(false);
// Some own run was refused by the provider (server-classified 'no_access').
const viewerRunNoAccess = ref(false);
// Some own run stopped because the viewer must sign in to a connection
// first (server-classified 'credentials_required') -- they HAVE access.
const viewerRunNeedsConnect = ref(false);
// Some chart holds a successful run of the viewer's own.
const viewerHasOwnSuccess = ref(false);
// True when any step already carries a per-viewer result row for this user —
// auto-run only fires for a first-time viewer (no row at all); a failed
// earlier run becomes an explicit fallback state instead of a retry loop.
const hasOwnResult = ref(false);
// status_reason of the viewer's failed result row, if their last run errored
const viewerRunFailedReason = ref<string | null>(null);
// Data sources whose viewer client couldn't be built on the last run
// (e.g. a user_required source the viewer hasn't connected yet)
const dataSourceErrors = ref<Array<{ data_source: string; data_source_id?: string; code?: string; error: string }>>([]);
// What the gate's error state shows: the data source that could not be
// built names the cause; else the viewer's failed run reason.
const gateErrorMessage = computed(() =>
    dataSourceErrors.value[0]?.error || viewerRunFailedReason.value || null);

// Brand icon type for the gate's sign-in state: the report's identity-scoped
// connection (user_required) if any, else its first connection.
const gateSourceType = computed<string | null>(() => {
    const sources = (report.value?.data_sources || []) as any[];
    const conns = sources.flatMap((ds) => ds.connections || []);
    const userScoped = conns.find((c) => c.auth_policy && c.auth_policy !== 'system_only');
    return (userScoped || conns[0])?.type || null;
});
// Auto-run fires at most once per page load
const autoRunTried = ref(false);

const canRun = computed(() => {
    const userId = (currentUser.value as any)?.user?.id || (currentUser.value as any)?.id;
    const dataOwner = props.source === 'draft' ? draftDataOwner.value : isOwner.value;
    return !!api.run && !!userId && !dataOwner && hasArtifacts.value && reportLoaded.value;
});

// Which gate the withheld overlay shows. Auto-run makes 'loading' the state a
// signed-in viewer normally sees; the rest are fallbacks for when a run can't
// succeed on its own. Data-source errors carry a machine-readable code so the
// gate offers the right action: connect their credential ('connect'), ask an
// admin ('no_access'), or retry ('error').
// The gate covers the dashboard only when there is nothing of it to show:
// no run of the viewer's own yet (auto-run shows "loading"), a run in
// flight, or EVERY query withheld. Otherwise the charts the viewer can read
// render and a banner names the rest — the same rule as ArtifactFrame and
// as forking (keep what ran).
const showViewerGate = computed(() =>
    snapshotWithheld.value && (allWithheld.value || isRunning.value || !hasOwnResult.value));
const partiallyWithheld = computed(() =>
    snapshotWithheld.value && !showViewerGate.value && withheldCharts.value.length > 0);

const gateState = computed<'loading' | 'signin' | 'connect' | 'no_access' | 'error' | 'ready'>(() => {
    if (isRunning.value) return 'loading';
    if (!canRun.value) return 'signin';
    const errs = dataSourceErrors.value;
    if (errs.some((e) => e.code === 'credentials_required') || viewerRunNeedsConnect.value) return 'connect';
    // Refused for the whole source (no client) or for one dataset at query
    // time — both are "no access", never the provider's HTTP status.
    if (errs.some((e) => e.code === 'no_access') || viewerRunNoAccess.value) return 'no_access';
    if (errs.length > 0 || viewerRunFailedReason.value) return 'error';
    return 'ready';
});

async function handleRun() {
    if (isRunning.value) return;
    isRunning.value = true;
    runError.value = null;
    try {
        if (!api.run) return;
        const { data, error: fetchError } = await useMyFetch(api.run(artifact.value?.id), { method: 'POST' });
        if (fetchError.value) throw fetchError.value;
        const run = (data.value || {}) as any;
        dataSourceErrors.value = run.data_source_errors || [];
        await loadVisualizationData(artifact.value?.id);
        deliverArtifactData();
        lastRefreshedAt.value = new Date();
        if ((run.steps_failed ?? 0) > 0 || (run.data_source_errors || []).length > 0) {
            const dsError = (run.data_source_errors || [])[0]?.error;
            runError.value = dsError || `${run.steps_failed} of ${run.steps_total} queries failed`;
        }
    } catch (e: any) {
        runError.value = e?.data?.detail || e?.message || 'Run failed';
        // Surface the failure in the gate too (the run never produced results).
        if (!viewerRunFailedReason.value) viewerRunFailedReason.value = runError.value;
    } finally {
        isRunning.value = false;
    }
}

// Export PDF state. Docs have no server-side renderer (the backend refuses
// them) and a withheld snapshot means there is no shared data to render, so
// the button only shows for page/slides artifacts with a visible snapshot.
const isExportingPdf = ref(false);
const exportPdfError = ref<string | null>(null);
// The export button shows whenever the server would hand this reader a file
// (utils/dashboardExportsHidden.ts): on /d that file holds what they see.
const exportsWithheld = computed(() => exportsHidden({
    source: props.source,
    mode: artifact.value?.mode || 'page',
    snapshotWithheld: snapshotWithheld.value,
    exportsOwnRows: !!dashboardMeta.value?.exports_own_rows,
    hasOwnSuccess: viewerHasOwnSuccess.value,
}));
const { availableExports } = useArtifactExports(artifact, {
    snapshotWithheld: exportsWithheld,
    enabled: computed(() => !!api.exportUrl && reportLoaded.value && hasArtifacts.value),
});

function handleExport(format: ExportFormat) {
    if (format === 'pdf') return handleExportPdf();
    if (format === 'pptx') return handleExportPptx();
    return handleExportHtml();
}

const isExportingPptx = ref(false);

// The in-app PPTX export lives on the artifact router, which requires a
// signed-in user, so a share-link viewer goes through the report-scoped twin.
async function handleExportPptx() {
    if (isExportingPptx.value) return;
    isExportingPptx.value = true;
    try {
        // Pin the export to the artifact actually on screen rather than
        // letting the server pick "latest" — they can differ.
        const { data, error: fetchError } = await useMyFetch(
            api.exportUrl!('pptx', artifact.value?.id),
            {
                responseType: 'blob' as any,
                // Same reasoning as the other two: a silent ofetch retry on
                // 409/504 would repeat server-side work and double the spin.
                retry: 0,
                timeout: 360_000,
            } as any,
        );
        if (fetchError.value || !data.value) throw fetchError.value || new Error('PowerPoint export failed');

        const url = URL.createObjectURL(data.value as Blob);
        const a = document.createElement('a');
        a.href = url;
        // Strip filesystem-reserved characters only, so non-Latin titles
        // (e.g. Hebrew) survive as the download name.
        const base = String(pageTitle.value || 'presentation').replace(/[\\/:*?"<>|]+/g, ' ').trim();
        a.download = `${base || 'presentation'}.pptx`;
        document.body.appendChild(a);
        a.click();
        document.body.removeChild(a);
        URL.revokeObjectURL(url);
    } catch (e: any) {
        console.error('PowerPoint export failed:', e);
        // With responseType 'blob' the error body arrives as a Blob, so the
        // server's JSON {detail} has to be read out of it.
        let detail = e?.data?.detail;
        if (!detail && e?.data instanceof Blob) {
            try { detail = JSON.parse(await e.data.text())?.detail; } catch { /* not JSON */ }
        }
        runError.value = detail || e?.message || 'PowerPoint export failed';
    } finally {
        isExportingPptx.value = false;
    }
}

async function handleExportPdf() {
    if (isExportingPdf.value) return;
    isExportingPdf.value = true;
    exportPdfError.value = null;
    try {
        const { data, error: fetchError } = await useMyFetch(api.exportUrl!('pdf', artifact.value?.id), {
            responseType: 'blob' as any,
            // ofetch's default retry list includes 409/504 — exactly what a
            // failed/timed-out export returns — and a silent retry re-runs a
            // multi-minute server-side render, doubling how long the button
            // spins before the user sees the error. Fail once, fail visibly.
            retry: 0,
            // The server bounds an export at ~5.5 min (render cap + lock wait);
            // this backstop keeps the button from spinning forever if the
            // request itself goes into a black hole (dropped connection, proxy
            // buffering with no response). Without it the finally below never
            // runs and the UI is stuck at "Exporting...".
            timeout: 360_000,
        } as any);
        if (fetchError.value || !data.value) throw fetchError.value || new Error('PDF export failed');
        const blob = data.value as Blob;
        const url = URL.createObjectURL(blob);
        const a = document.createElement('a');
        a.href = url;
        // Strip filesystem-reserved characters only, so non-Latin titles
        // (e.g. Hebrew) survive as the download name.
        const base = String(pageTitle.value || 'report').replace(/[\\/:*?"<>|]+/g, ' ').trim();
        a.download = `${base || 'report'}.pdf`;
        document.body.appendChild(a);
        a.click();
        document.body.removeChild(a);
        URL.revokeObjectURL(url);
    } catch (e: any) {
        console.error('PDF export failed:', e);
        // With responseType 'blob' the error body arrives as a Blob, so the
        // server's JSON {detail} has to be read out of it — otherwise the user
        // sees the raw ofetch message instead of e.g. "PDF export timed out".
        let detail = e?.data?.detail;
        if (!detail && e?.data instanceof Blob) {
            try { detail = JSON.parse(await e.data.text())?.detail; } catch { /* not JSON */ }
        }
        exportPdfError.value = detail || e?.message || 'PDF export failed';
        // Surface it in the top bar's existing error slot.
        runError.value = exportPdfError.value;
    } finally {
        isExportingPdf.value = false;
    }
}

const isExportingHtml = ref(false);
async function handleExportHtml() {
    if (isExportingHtml.value) return;
    isExportingHtml.value = true;
    try {
        // Pin the export to the artifact actually on screen rather than
        // letting the server pick "latest" — they can differ.
        const { data, error: fetchError } = await useMyFetch(
            api.exportUrl!('html', artifact.value?.id),
            {
                responseType: 'blob' as any,
                // Same reasoning as the PDF export: a silent ofetch retry on
                // 409/504 would repeat server-side work and double the spin.
                retry: 0,
                timeout: 360_000,
            } as any,
        );
        if (fetchError.value || !data.value) throw fetchError.value || new Error('HTML export failed');

        const url = URL.createObjectURL(data.value as Blob);
        const a = document.createElement('a');
        a.href = url;
        // Strip filesystem-reserved characters only, so non-Latin titles
        // (e.g. Hebrew) survive as the download name.
        const base = String(pageTitle.value || 'dashboard').replace(/[\\/:*?"<>|]+/g, ' ').trim();
        a.download = `${base || 'dashboard'}.html`;
        document.body.appendChild(a);
        a.click();
        document.body.removeChild(a);
        URL.revokeObjectURL(url);
    } catch (e: any) {
        console.error('HTML export failed:', e);
        // With responseType 'blob' the error body arrives as a Blob, so the
        // server's JSON {detail} has to be read out of it.
        let detail = e?.data?.detail;
        if (!detail && e?.data instanceof Blob) {
            try { detail = JSON.parse(await e.data.text())?.detail; } catch { /* not JSON */ }
        }
        runError.value = detail || e?.message || 'HTML export failed';
    } finally {
        isExportingHtml.value = false;
    }
}

// Fork state
const forkEligibility = ref<any>(null);
const isForking = ref(false);

const forkReasonLabel = computed(() => {
    const reason = forkEligibility.value?.reason;
    switch (reason) {
        case 'not_logged_in': return 'Sign in to fork this report';
        case 'different_org': return 'You must be in the same organization';
        // No 'user_auth_required' case: a delegated source no longer blocks
        // forking. Access is decided by no_data_source_access, and then per
        // query by whether the forker's own credentials can run it.
        case 'no_data_source_access': return 'You don\'t have access to the data sources';
        case 'forks_disabled': return 'Forking is disabled for this organization';
        default: return '';
    }
});

async function handleFork() {
    if (isForking.value) return;
    isForking.value = true;
    try {
        if (!api.fork) return;
        const { data, error: fetchError } = await useMyFetch(api.fork, {
            method: 'POST',
            body: {},
        });
        if (data.value && !fetchError.value) {
            // A conversation's Fork answers with the new conversation; a
            // dashboard's with the conversation of the new dashboard.
            const forked = data.value as any;
            navigateTo(`/reports/${forked.report_id || forked.id}`);
        }
    } catch (e) {
        console.error('Failed to fork report:', e);
    } finally {
        isForking.value = false;
    }
}

// This page renders unauthenticated, with no i18n context and no org
// settings — useRelativeTime degrades to English and the viewer's timezone,
// which is what this page did on its own.
const { relativeTime: formatTime } = useRelativeTime()

// Transform visualizationsData to toolExecution format for ToolWidgetPreview
const toolExecutions = computed(() => {
    return visualizationsData.value.map(viz => ({
        id: viz.id,
        tool_name: 'query',
        status: 'success',
        created_step: {
            id: viz.id,
            title: viz.title,
            data: { rows: viz.rows, columns: viz.columns },
            data_model: viz.dataModel || { type: 'table' },
            code: viz.code || ''
        },
        created_visualizations: [{
            id: viz.id,
            title: viz.title,
            view: viz.view,
            status: 'success'
        }]
    }));
});

// Check if we have slides mode with preview images (use SlideViewer instead of iframe)
const hasSlidesWithPreviews = computed(() => {
    if (!artifact.value) return false;
    if (artifact.value.mode !== 'slides') return false;
    const previewImages = artifact.value.content?.preview_images;
    return Array.isArray(previewImages) && previewImages.length > 0;
});

// Slides artifact with no previews and python-pptx code — nothing the browser
// can render, so the page shows a fallback instead of injecting the source
// into the iframe (where it would appear as raw text).
const slidesPreviewsMissing = computed(() => {
    if (artifact.value?.mode !== 'slides') return false;
    if (hasSlidesWithPreviews.value) return false;
    return !isHtmlSlidesCode(artifact.value?.content?.code || '');
});

// Shared document (mode='doc') — rendered read-only in DocViewer, never an iframe
const isDocMode = computed(() => artifact.value?.mode === 'doc');

// Browser tab / "Add to Home screen" shortcut name. Without this, the browser
// falls back to the URL's last path segment (the report UUID). Prefer the
// artifact's own title, then the report title, then a sensible default — and
// treat the "Untitled Artifact" placeholder as no title.
const pageTitle = computed(() => {
    const artTitle = artifact.value?.title;
    if (artTitle && artTitle !== 'Untitled Artifact') return artTitle;
    return report.value?.title || 'Report';
});
useHead(() => ({ title: pageTitle.value }));

// Access error state
const accessError = ref<'login' | 'denied' | null>(null);

// Fetch report info
async function loadReport() {
    try {
        const { data, error: fetchError } = await useMyFetch(api.meta);
        if (fetchError.value) {
            const status = (fetchError.value as any)?.statusCode || (fetchError.value as any)?.status;
            // Not published yet: its editors see the draft instead -- unless
            // the draft just sent us here (it would bounce back and forth).
            const fromDraft = route.query.from === 'draft';
            if (props.source === 'dashboard' && !fromDraft
                && (fetchError.value as any)?.data?.error_code === 'dashboard.not_published') {
                navigateTo(`/d/${props.id}/draft`, { replace: true });
                return;
            }
            // The draft is its editors' only (or its conversation is gone):
            // anyone else gets the published page, which applies its own check.
            if (props.source === 'draft' && status === 404) {
                // A version or draft that is gone: the plain draft first.
                if (draftQuery) {
                    navigateTo(`/d/${props.id}/draft`, { replace: true });
                    return;
                }
                navigateTo(`/d/${props.id}?from=draft`, { replace: true });
                return;
            }
            if (status === 401) {
                accessError.value = 'login';
                return;
            }
            if (status === 403) {
                accessError.value = 'denied';
                return;
            }
            navigateTo('/not_found');
            return;
        }
        if (!data.value) {
            navigateTo('/not_found');
            return;
        }
        report.value = asReport(data.value);
        forkEligibility.value = (data.value as any)?.fork_eligibility || null;
    } catch (e) {
        console.error('Failed to load report:', e);
        navigateTo('/not_found');
    }
}

// The page reads the conversation's /r answer; a dashboard's (and its
// draft's) carries the same things under its own names.
function asReport(meta: any) {
    if (props.source === 'report') return meta;
    if (props.source === 'dashboard') dashboardOwner.value = !!meta.is_owner;
    if (props.source === 'draft') dashboardOwner.value = !!meta.can_edit;
    if (props.source === 'draft') {
        draftReportId.value = meta.report_id || null;
        // Whose rows the version shows (collab01): the reader's own version
        // holds theirs; anyone else's they run for themselves ("Run as me"),
        // kept as theirs alone. The run is pinned to the version on screen.
        draftDataOwner.value = typeof meta.version_is_mine === 'boolean' ? meta.version_is_mine : !!meta.is_owner;
        if (meta.report_id && !draftDataOwner.value) {
            const qs = new URLSearchParams();
            if (meta.version_number) qs.set('version', String(meta.version_number));
            if (meta.draft?.id && meta.draft?.slug) qs.set('draft', meta.draft.slug);
            const runUrl = withDraftQuery(`/api/d/${encodeURIComponent(props.id)}/draft/run`, qs.toString());
            api.run = () => runUrl;
        }
    }
    dashboardMeta.value = meta;
    return {
        id: meta.id,
        title: meta.title,
        general: meta.general || {},
        include_data_tab: meta.include_data_tab,
        artifact_chat_enabled: !!meta.chat_enabled,
        refresh_on_view: !!meta.refresh_on_view,
        data_sources: meta.data_sources || [],
        last_run_at: meta.last_run_at,
        theme_name: meta.theme_name,
    };
}
const dashboardMeta = ref<any>(null);

// A dashboard's published version (or its draft): one artifact, whose
// content the page renders as it renders a conversation's newest.
async function loadDashboardContent() {
    try {
        const { data, error } = await useMyFetch(api.content!);
        if (error.value || !data.value) throw error.value || new Error('Missing dashboard content');
        const body = data.value as any;
        const meta = dashboardMeta.value || {};
        artifact.value = {
            // The version on screen: the release, or the draft's version.
            id: meta.release_id || body.version_id || meta.version_id,
            artifact_id: meta.id || props.id,
            mode: body.mode,
            title: meta.title,
            content: body.content || {},
        };
        hasArtifacts.value = true;
        await loadArtifactFiles();
    } catch (e) {
        hasArtifacts.value = false;
        artifactLoadFailed.value = true;
        console.error('[PublicArtifact] Failed to load dashboard content');
    }
}

// Fetch the latest artifact for this report (using public endpoints)
async function loadArtifact() {
    artifactLoadFailed.value = false;
    if (api.content) return loadDashboardContent();
    try {
        // Use public endpoint - no auth required
        const { data, error } = await useMyFetch(api.artifacts!);
        if (error.value) throw error.value;
        if (data.value && Array.isArray(data.value) && data.value.length > 0) {
            hasArtifacts.value = true;
            // Prefer the newest dashboard/deck (the list is created_at desc):
            // a doc saved after the dashboard must not take over the shared
            // page — same rule as the backend's get_latest_by_report.
            const rows = data.value as any[];
            const latestArtifactId = (rows.find(a => a.mode !== 'doc') || rows[0]).id;
            // Use public artifact endpoint
            const { data: fullArtifact, error: detailError } = await useMyFetch(api.artifact!(latestArtifactId));
            if (detailError.value || !fullArtifact.value) throw detailError.value || new Error("Missing artifact response");
            if (fullArtifact.value) {
                artifact.value = fullArtifact.value;
                await loadArtifactFiles();
            }
        } else {
            hasArtifacts.value = false;
        }
    } catch (e) {
        hasArtifacts.value = false;
        artifactLoadFailed.value = true;
        console.error('[PublicArtifact] Failed to load artifact');
    }
}

// Fetch visualization data for the artifact (using public endpoints)
async function loadVisualizationData(artifactId?: string) {
    try {
        // Use public endpoint - no auth required
        // If artifactId provided, filter to only queries used by that artifact
        const { data: queriesRes } = await useMyFetch(api.queries(artifactId));
        const queries = Array.isArray(queriesRes.value) ? queriesRes.value : [];

        // Fetch all steps in parallel — awaiting each one serially made the
        // page's time-to-render scale linearly with the number of queries.
        const stepResults = await Promise.all(
            queries.map((query) => useMyFetch(api.step((query as any).id)))
        );

        const vizData = [];
        const rowsByQuery = new Map<string, any[]>();
        const appliedByQuery = new Map<string, any>();
        const titleToId = new Map<string, string>();
        let newestViewerRun: Date | null = null;
        let anyWithheld = false;
        let anyOwnResult = false;
        let failedReason: string | null = null;
        const nextWithheldCharts: Array<{ title: string; noAccess: boolean }> = [];
        let visibleQueries = 0;
        let anyNoAccess = false;
        let anyNeedsConnect = false;
        let anyOwnSuccess = false;
        // A chart drawn from the version author's snapshot, not the reader's
        // own run (collab01): the version bar says whose results these are.
        let anyAuthorSnapshot = false;
        const nextPending: Array<{ id: string; title: string; vizIds: string[] }> = [];
        for (let qi = 0; qi < queries.length; qi++) {
            const query = queries[qi];
            // Public step endpoint - returns PublicStepSchema directly
            const { data: step } = stepResults[qi];
            // Still running with this editor's credentials (a joined copy,
            // collab01): the chart says so and the page asks again.
            if ((step.value as any)?.pending) {
                nextPending.push({
                    id: String((query as any).id),
                    title: (query as any).title || '',
                    vizIds: ((query as any).visualizations || []).map((v: any) => String(v.id)),
                });
            }

            // Steps the viewer re-ran carry their per-user result row — its
            // presence gates auto-run (first-time viewers only), and a failed
            // row's reason feeds the gate's error state.
            const vr = (step.value as any)?.viewer_result;
            if ((step.value as any)?.snapshot_withheld) {
                anyWithheld = true;
                nextWithheldCharts.push({
                    title: (query as any).title || 'Untitled',
                    noAccess: vr?.error_code === 'no_access',
                });
            } else if (step.value) {
                if (!(step.value as any).pending && vr?.status !== 'success') anyAuthorSnapshot = true;
                // Only a step that actually loaded counts as viewable — see
                // the matching guard in ArtifactFrame. A null step (failed
                // fetch, or no default step) has an undefined
                // `snapshot_withheld`, which used to read as "visible" and
                // lift the gate onto uniformly empty rows.
                visibleQueries += 1;
            }
            if (vr) {
                anyOwnResult = true;
                if (vr.status === 'error' && !failedReason) failedReason = vr.status_reason || null;
                if (vr.status === 'error' && vr.error_code === 'no_access') anyNoAccess = true;
                if (vr.status === 'error' && vr.error_code === 'credentials_required') anyNeedsConnect = true;
                if (vr.status === 'success') anyOwnSuccess = true;
            }
            const viewerRun = vr?.last_run_at;
            if (viewerRun) {
                const ts = new Date(viewerRun.endsWith('Z') ? viewerRun : viewerRun + 'Z');
                if (!newestViewerRun || ts > newestViewerRun) newestViewerRun = ts;
            }

            // Declared query parameters drive artifact controls (useParams()).
            if (Array.isArray((query as any).parameters) && (query as any).parameters.length) {
                queryParamSpecs.value[(query as any).id] = (query as any).parameters;
            }
            rowsByQuery.set(String((query as any).id), (step.value as any)?.data?.rows || []);
            appliedByQuery.set(String((query as any).id), (step.value as any)?.applied_params || {});
            if ((query as any).title) titleToId.set(String((query as any).title).toLowerCase(), String((query as any).id));

            // Process each visualization in the query (matches ArtifactFrame.vue structure)
            const visualizations = (query as any).visualizations || [];
            for (const viz of visualizations) {
                vizData.push({
                    id: viz.id,  // Use visualization ID, not query ID
                    title: viz.title || query.title || 'Untitled',
                    view: viz.view || {},  // Use visualization's view config
                    rows: (step.value as any)?.data?.rows || [],
                    columns: (step.value as any)?.data?.columns || [],
                    dataModel: (step.value as any)?.data_model || {},
                    code: (step.value as any)?.code || '',
                    queryId: (query as any).id
                });
            }

            // Fallback: if no visualizations, create entry from query (legacy support)
            if (visualizations.length === 0 && step.value) {
                vizData.push({
                    id: query.id,
                    title: query.title || 'Untitled',
                    view: (step.value as any).view || {},
                    rows: (step.value as any).data?.rows || [],
                    columns: (step.value as any).data?.columns || [],
                    dataModel: (step.value as any).data_model || {},
                    code: (step.value as any).code || ''
                });
            }
        }
        // Reorder vizData to match artifact's visualization_ids order
        const vizIds = artifact.value?.content?.visualization_ids;
        if (vizIds && vizIds.length > 0) {
            const vizMap = new Map(vizData.map(v => [v.id, v]));
            const ordered = vizIds.map((id: string) => vizMap.get(id)).filter(Boolean);
            const orderedIds = new Set(vizIds);
            for (const v of vizData) {
                if (!orderedIds.has(v.id)) ordered.push(v);
            }
            visualizationsData.value = ordered;
        } else {
            visualizationsData.value = vizData;
        }
        await resolveParamOptions(rowsByQuery, titleToId, appliedByQuery);
        viewerLastRunAt.value = newestViewerRun;
        snapshotWithheld.value = anyWithheld;
        withheldCharts.value = nextWithheldCharts;
        allWithheld.value = queries.length > 0 && visibleQueries === 0;
        viewerRunNoAccess.value = anyNoAccess;
        viewerRunNeedsConnect.value = anyNeedsConnect;
        viewerHasOwnSuccess.value = anyOwnSuccess;
        showingAuthorData.value = anyAuthorSnapshot;
        hasOwnResult.value = anyOwnResult;
        viewerRunFailedReason.value = failedReason;
        pendingQueries.value = nextPending;
    } catch (e) {
        console.error('Failed to load visualization data:', e);
    } finally {
        schedulePendingPoll();
    }
}

// ── Charts still running as this editor (collab01) ──────────────────────────
// Ask for those steps every few seconds; once one has landed, reload the data
// and push it into the page. Stops when none is pending, or on unmount.
const PENDING_POLL_MS = 4000;
const pendingQueries = ref<Array<{ id: string; title: string; vizIds: string[] }>>([]);
let pendingTimer: ReturnType<typeof setTimeout> | null = null;
let unmounted = false;
function schedulePendingPoll() {
    if (pendingTimer) clearTimeout(pendingTimer);
    pendingTimer = null;
    if (unmounted || !pendingQueries.value.length) return;
    pendingTimer = setTimeout(pollPending, PENDING_POLL_MS);
}
async function pollPending() {
    pendingTimer = null;
    if (unmounted) return;
    const results = await Promise.all(pendingQueries.value.map((q) =>
        useMyFetch(api.step(q.id)).catch(() => null)));
    if (unmounted) return;
    const landed = results.some((r: any) => !r || r.error?.value || !(r.data?.value as any)?.pending);
    if (!landed) { schedulePendingPoll(); return; }
    await loadVisualizationData(artifact.value?.id);
    if (unmounted) return;
    if (dataReady.value) deliverArtifactData();
}


// ── Query parameters (authenticated viewers) ────────────────────────────────
// Same contract as ArtifactFrame: the iframe posts ARTIFACT_SET_PARAMS /
// ARTIFACT_REFRESH_PARAMS; this host runs the consuming queries in viewer
// mode and pushes fresh rows back with a new ARTIFACT_DATA message.
// Anonymous viewers can't run (auth required) — their controls no-op.
const artifactIframeRef = ref<HTMLIFrameElement | null>(null);
// The draft page counts no view (collab01): editors at work are not readers.
useArtifactRuntime(() => ({frames: [artifactIframeRef.value], artifactId: artifact.value?.artifact_id,
    recordViews: props.source !== 'draft'}));
// The artifact iframe is built ONCE from a frozen seed (srcdocSeed) and every
// later data change has to be posted into it. A message posted before the
// iframe finishes loading is simply lost, so deliveries wait for `load`.
const artifactIframeLoaded = ref(false);
let artifactPushPending = false;

// App color mode, forwarded into the artifact iframe. Non-reactive snapshot so
// a toggle updates the live iframe via postMessage instead of reloading srcdoc.
const colorMode = useColorMode();
let artifactColorMode: 'light' | 'dark' = colorMode.value === 'dark' ? 'dark' : 'light';
watch(() => colorMode.value, (v) => {
    artifactColorMode = v === 'dark' ? 'dark' : 'light';
    artifactIframeRef.value?.contentWindow?.postMessage(
        { type: 'ARTIFACT_SET_COLOR_MODE', mode: artifactColorMode },
        '*'
    );
});

const queryParamSpecs = ref<Record<string, any[]>>({});
const paramValues = ref<Record<string, any>>({});
// Host-resolved stable choices per param (static options + options-source
// query rows) — mirrors ArtifactFrame; useParamOptions() reads these.
const paramOptions = ref<Record<string, Array<{ value: any; label: string }>>>({});
// Highest ARTIFACT_SET_PARAMS seq seen; echoed as paramsPayload().ack so the
// runtime store knows which in-flight commits a data push reflects.
let paramAckSeq = 0;
// Latest run id per query — a slower, superseded run must not overwrite rows
// a newer run already delivered.
let paramRunCounter = 0;
const latestParamRunForQid: Record<string, number> = {};
// Frozen at dataReady so live updates never recompute srcdoc (iframe reload).
const srcdocSeed = ref<any>(null);

function artifactRuntime() {
    const v = Number(artifact.value?.content?.runtime_version || 0);
    return { version: Number.isFinite(v) ? v : 0 };
}

function paramsPayload() {
    const byName: Record<string, any> = {};
    for (const [qid, specs] of Object.entries(queryParamSpecs.value)) {
        for (const spec of (specs as any[]) || []) {
            if (!byName[spec.name]) byName[spec.name] = { ...spec, query_ids: [] };
            byName[spec.name].query_ids.push(qid);
        }
    }
    return {
        declarations: Object.values(byName),
        values: { ...paramValues.value },
        options: { ...paramOptions.value },
        ack: paramAckSeq,
    };
}

// Resolve stable control choices. `rowsByQueryId` carries the step rows this
// load already fetched; `titleToId` maps query titles (lowercased) to ids so
// loose agent-persisted refs still resolve; a source query outside the
// artifact is fetched from the report-level public list (plus its step) once.
async function resolveParamOptions(
    rowsByQueryId: Map<string, any[]>,
    titleToId: Map<string, string>,
    appliedByQueryId: Map<string, any> = new Map(),
) {
    const out: Record<string, Array<{ value: any; label: string }>> = {};
    let fetchedAll = false;
    for (const specs of Object.values(queryParamSpecs.value)) {
        for (const spec of (specs as any[]) || []) {
            if (!spec?.name) continue;
            if (!out[spec.name] && Array.isArray(spec.options) && spec.options.length) {
                out[spec.name] = spec.options.map((v: any) =>
                    (v && typeof v === 'object' && 'value' in v) ? v : { value: v, label: String(v) });
            }
            const src = spec.options_source;
            if (!src?.query_id) continue;
            const lookup = () => {
                const ref = String(src.query_id);
                return rowsByQueryId.get(ref)
                    ?? rowsByQueryId.get(titleToId.get(ref.toLowerCase()) || '');
            };
            let rows = lookup();
            if (!rows && !fetchedAll) {
                fetchedAll = true;
                try {
                    const { data } = await useMyFetch(api.queries());
                    const all = Array.isArray(data.value) ? (data.value as any[]) : [];
                    const missing = all.filter(q => !rowsByQueryId.has(String(q.id)));
                    const steps = await Promise.all(
                        missing.map(q => useMyFetch(api.step(q.id))));
                    missing.forEach((q, i) => {
                        rowsByQueryId.set(String(q.id), (steps[i].data.value as any)?.data?.rows || []);
                        if (q.title) titleToId.set(String(q.title).toLowerCase(), String(q.id));
                    });
                } catch { /* options degrade to static/none */ }
                rows = lookup();
            }
            const seen = new Set<string>();
            const opts: Array<{ value: any; label: string }> = [];
            for (const r of rows || []) {
                const value = r?.[src.value_column];
                if (value === undefined || value === null) continue;
                const key = String(value);
                if (seen.has(key)) continue;
                seen.add(key);
                const label = src.label_column ? (r?.[src.label_column] ?? value) : value;
                opts.push({ value, label: String(label) });
            }
            if (opts.length) out[spec.name] = opts;
        }
    }
    // Last-resort tier (mirrors ArtifactFrame): derive stable choices once
    // per load from the declaring query's initially loaded rows, matching a
    // column by (singularized) param name. Never recomputed on param-driven
    // pushes, so the list cannot narrow itself to the current selection.
    const norm = (s: any) => String(s || '').toLowerCase().replace(/[^a-z0-9]/g, '');
    for (const [qid, specs] of Object.entries(queryParamSpecs.value)) {
        const rows = rowsByQueryId.get(String(qid)) || [];
        const applied = appliedByQueryId.get(String(qid)) || {};
        const cols = rows.length ? Object.keys(rows[0] || {}) : [];
        if (!rows.length || !cols.length) continue;
        for (const spec of (specs as any[]) || []) {
            if (!spec?.name || out[spec.name] || spec.source === 'identity') continue;
            // Skip data the param already filtered — a degenerate one-value list.
            if (applied[spec.name] !== null && applied[spec.name] !== undefined) continue;
            let n = norm(spec.name);
            if (n.endsWith('s')) n = n.slice(0, -1);
            const valueCol = cols.find(c => norm(c) === norm(spec.name))
                || cols.find(c => norm(c) === n)
                || cols.find(c => norm(c).startsWith(n))
                || cols.find(c => norm(c).includes(n));
            if (!valueCol) continue;
            const stem = norm(valueCol).replace(/id$/, '');
            const labelCol = stem
                ? cols.find(c => [stem + 'name', stem + 'title', stem].includes(norm(c)) && c !== valueCol)
                : null;
            const seen = new Set<string>();
            const opts: Array<{ value: any; label: string }> = [];
            for (const r of rows) {
                const value = r?.[valueCol];
                if (value === undefined || value === null) continue;
                const key = String(value);
                if (seen.has(key)) continue;
                seen.add(key);
                opts.push({ value, label: String(labelCol ? (r?.[labelCol] ?? value) : value) });
                if (opts.length >= 200) break;
            }
            if (opts.length) out[spec.name] = opts;
        }
    }
    paramOptions.value = out;
}

function initParamValues() {
    const next: Record<string, any> = {};
    for (const specs of Object.values(queryParamSpecs.value)) {
        for (const spec of (specs as any[]) || []) {
            if (spec.source === 'identity') continue;
            if (spec.default !== undefined && spec.default !== null) next[spec.name] = spec.default;
        }
    }
    paramValues.value = next;
}

function queriesWithIdentityParams(): string[] {
    return Object.entries(queryParamSpecs.value)
        .filter(([, specs]) => ((specs as any[]) || []).some((s: any) =>
            s.source === 'identity' || s.source === 'input_identity_default'))
        .map(([qid]) => qid);
}

function postToArtifactIframe(msg: any) {
    try {
        artifactIframeRef.value?.contentWindow?.postMessage(msg, '*');
    } catch { /* iframe not ready */ }
}

// Get the rows just loaded into the artifact that is already on screen.
//
// Without this a viewer's run never reached the dashboard: the page loads the
// fresh rows (loadVisualizationData) but the iframe keeps rendering its frozen
// seed — for a first-time viewer, the withheld EMPTY rows. The gate lifted to
// reveal "no data", a second Run changed nothing, and only a full reload
// (which re-seeds the iframe) showed the data. Refresh-on-view had the same
// gap: its fresher rows never left the page.
function deliverArtifactData() {
    if (artifactIframeLoaded.value) {
        pushArtifactData();
    } else {
        // Still loading its first render (the auto-run case) — send on load.
        artifactPushPending = true;
    }
}

function onArtifactIframeLoad() {
    artifactIframeLoaded.value = true;
    if (artifactPushPending) {
        artifactPushPending = false;
        pushArtifactData();
    }
}

function pushArtifactData() {
    postToArtifactIframe({
        type: 'ARTIFACT_DATA',
        payload: JSON.parse(JSON.stringify({
            report: {
                id: report.value.id,
                title: report.value.title,
                theme: report.value.theme_name || report.value.report_theme_name
            },
            visualizations: visualizationsData.value,
            files: filesData.value,
            current_user: viewerContext.value,
            params: paramsPayload(),
            runtime: artifactRuntime()
        }))
    });
}

async function runParamQueries(
    changes: Record<string, any> | null,
    targets: string[] | null = null,
    opts: { force?: boolean; identityOnly?: boolean } = {},
) {
    if (!viewerContext.value) {
        // Anonymous: no authenticated identity to run under.
        postToArtifactIframe({ type: 'ARTIFACT_PARAMS_STATUS', payload: { loading: false, error: 'Sign in to change filters' } });
        return;
    }
    const changedNames = Object.keys(changes || {});
    // Names no query declares would silently no-op — surface them instead
    // (mirrors ArtifactFrame).
    const declaredNames = new Set(
        Object.values(queryParamSpecs.value).flatMap((specs: any) =>
            (specs || []).map((s: any) => s.name)));
    const unknown = changedNames.filter(n => !declaredNames.has(n));
    if (unknown.length) {
        postToArtifactIframe({ type: 'ARTIFACT_PARAMS_STATUS', payload: {
            loading: false,
            error: `Unknown parameter(s): ${unknown.join(', ')} — declared: ${[...declaredNames].join(', ') || 'none'}. ` +
                'Controls must call setParam with a declared param name.',
        }});
        return;
    }
    for (const name of changedNames) paramValues.value[name] = (changes as any)[name];

    let affected = Object.entries(queryParamSpecs.value)
        .filter(([qid, specs]) => {
            if (targets && !targets.includes(qid)) return false;
            const list = (specs as any[]) || [];
            if (opts.identityOnly) {
                return list.some((s: any) => s.source === 'identity' || s.source === 'input_identity_default');
            }
            if (!changedNames.length) return list.length > 0;
            return list.some((s: any) => changedNames.includes(s.name));
        })
        .map(([qid]) => qid);
    affected = [...new Set(affected)];
    if (!affected.length) return;

    const myRun = ++paramRunCounter;
    for (const qid of affected) latestParamRunForQid[qid] = myRun;

    postToArtifactIframe({ type: 'ARTIFACT_PARAMS_STATUS', payload: { loading: true } });
    let firstError: string | null = null;
    await Promise.all(affected.map(async (qid) => {
        try {
            const body: Record<string, any> = { mode: 'viewer', params: {}, force_refresh: !!opts.force };
            for (const spec of (queryParamSpecs.value[qid] as any[]) || []) {
                if (spec.source === 'identity') continue;
                if (Object.prototype.hasOwnProperty.call(paramValues.value, spec.name)) {
                    body.params[spec.name] = paramValues.value[spec.name];
                }
            }
            const { data: runRes, error } = await useMyFetch(api.paramRun(qid), { method: 'POST', body });
            const res: any = runRes.value;
            if (error.value || !res || res.status !== 'success') {
                const msg = (error.value as any)?.data?.detail || res?.error || 'Query run failed';
                if (!firstError) firstError = String(msg);
                return;
            }
            if (latestParamRunForQid[qid] !== myRun) return; // superseded mid-flight
            for (const viz of visualizationsData.value as any[]) {
                if (viz.queryId === qid) {
                    viz.rows = res.data?.rows || [];
                    viz.columns = res.data?.columns || [];
                }
            }
        } catch (e: any) {
            if (!firstError) firstError = e?.message || 'Query run failed';
        }
    }));
    visualizationsData.value = [...visualizationsData.value];
    // A fully superseded run stays quiet: the newer run owns the status line
    // and pushes its own (fresher) data.
    if (myRun === paramRunCounter) {
        postToArtifactIframe({ type: 'ARTIFACT_PARAMS_STATUS', payload: { loading: false, error: firstError } });
        pushArtifactData();
    }
}

function handleArtifactParamsMessage(event: MessageEvent) {
    if (event.source !== artifactIframeRef.value?.contentWindow || !event.data) return;
    if (event.data.type === 'ARTIFACT_SET_PARAMS') {
        paramAckSeq = Math.max(paramAckSeq, Number(event.data.seq) || 0);
        runParamQueries(event.data.changes || {}, event.data.targets || null, {});
    } else if (event.data.type === 'ARTIFACT_REFRESH_PARAMS') {
        runParamQueries(null, event.data.targets || null, { force: true });
    } else if (event.data.type === COLLAB_LOCATED_MESSAGE && Array.isArray(event.data.keys)) {
        locatedKeys.value = new Set(event.data.keys.filter((k: unknown) => typeof k === 'string').slice(0, 500));
    }
}

// Build the iframe srcdoc - only compute once all data is ready
const iframeSrcdoc = computed(() => {
    if (!dataReady.value) return null;

    const artifactCode = artifact.value?.content?.code;
    if (!artifactCode) return null;

    // python-pptx slides source must never be injected into the iframe
    if (slidesPreviewsMissing.value) return null;

    const seed = srcdocSeed.value || {
        report: {
            id: report.value.id,
            title: report.value.title,
            theme: report.value.theme_name || report.value.report_theme_name
        },
        visualizations: visualizationsData.value,
        files: filesData.value,
        current_user: viewerContext.value,
        params: paramsPayload(),
        runtime: artifactRuntime()
    };
    // Editors' pages carry the chart-marks listener (collab01).
    const pageMode = (artifact.value?.mode || 'page') === 'page';
    return buildArtifactIframeHtml({
        data: seed,
        code: overlaysEnabled.value && pageMode ? artifactCode + chartOverlayScriptTag() : artifactCode,
        mode: artifact.value?.mode || 'page',
        resourceApp: artifact.value?.content?.sdk_version === 1,
        colorMode: artifactColorMode,
    });
});

// A new srcdoc is a new document — deliveries wait for its load again.
watch(() => iframeSrcdoc.value, () => { artifactIframeLoaded.value = false; });

// ── Presence, attribution, review changes (collab01) ────────────────────────
// Editors only: viewers and anonymous readers get none of it.
const overlaysEnabled = computed(() => props.source !== 'report' && hasCap('edit'));
const presenceEnabled = computed(() => overlaysEnabled.value && reportLoaded.value);
const presence = useDashboardPresence(
    computed(() => (props.source !== 'report' ? dashboardMeta.value?.id || null : null)),
    ref(null),
    presenceEnabled,
);
const presenceOthers = presence.others;

const changesOpen = ref(false);
const changes = ref<any>(null);
const changesLoading = ref(false);
const changesError = ref<string | null>(null);
const activeChangeKey = ref<string | null>(null);
const locatedKeys = ref<Set<string>>(new Set());

async function loadChanges() {
    if (props.source !== 'draft' || !hasCap('edit') || changesLoading.value) return;
    changesLoading.value = true;
    changesError.value = null;
    try {
        const { data, error } = await useMyFetch(withDraftQuery(`/api/d/${encodeURIComponent(props.id)}/draft/changes`));
        if (unmounted) return;
        if (error.value) { changesError.value = getErrorMessage(error.value, t('dashboardCollab.page.changesFailed')); return; }
        changes.value = data.value || null;
    } finally {
        changesLoading.value = false;
    }
}
function openChanges() {
    changesOpen.value = true;
    loadChanges();
}
function toggleChanges() {
    if (changesOpen.value) changesOpen.value = false;
    else openChanges();
}
// "Review changes" (someone published since) lands here with ?changes=1:
// open the panel once the page knows the reader may edit.
watch(dashboardMeta, (meta) => {
    if (meta && route.query.changes === '1' && !changesOpen.value && hasCap('edit')) openChanges();
}, { immediate: true });

function firstName(name: string | null | undefined) {
    return (name || '').split(/[\s@]/)[0] || name || '';
}

// The marks drawn over the charts in the page (chartOverlay.ts): charts
// still running as you, charts another editor's agent is changing now, and
// "Changed by" on hover (or while the panel points at one).
const chartMarks = computed<ChartMark[]>(() => {
    if (!overlaysEnabled.value) return [];
    const vizList = visualizationsData.value as Array<{ id: string; title?: string }>;
    const titles = new Map<string, string>();
    for (const el of (changes.value?.elements || [])) titles.set(String(el.key), el.title || '');
    const marks: ChartMark[] = [];
    for (const q of pendingQueries.value) {
        marks.push({ key: `pending:${q.id}`, ids: q.vizIds, tone: 'pending', label: t('dashboardCollab.page.runningAsYou') });
    }
    const agentCharts = new Set<string>();
    for (const entry of presence.agentEditors.value) {
        for (const key of entry.element_keys || []) {
            agentCharts.add(String(key));
            marks.push({
                key: `agent:${entry.user.id}:${key}`,
                ids: vizIdsForKey(String(key), titles.get(String(key)), vizList),
                tone: 'agent',
                label: t('dashboardCollab.page.agentEditingChart', { name: firstName(entry.user.name) }),
            });
        }
    }
    for (const el of (changes.value?.elements || [])) {
        if (!el.changed_by || agentCharts.has(String(el.key))) continue;
        marks.push({
            key: String(el.key),
            ids: vizIdsForKey(String(el.key), el.title, vizList),
            tone: 'changed',
            label: t('dashboardCollab.page.changedByShort', {
                name: el.changed_by.name || t('dashboardCollab.page.someone'),
                when: el.changed_at ? relativeTime(el.changed_at) : '',
            }),
            active: activeChangeKey.value === String(el.key),
        });
    }
    return marks.filter((m) => m.ids.length);
});
function postChartMarks() {
    if (!overlaysEnabled.value || !artifactIframeLoaded.value) return;
    const rtl = typeof document !== 'undefined' && document.documentElement.getAttribute('dir') === 'rtl';
    postToArtifactIframe({ type: COLLAB_MARKS_MESSAGE, marks: JSON.parse(JSON.stringify(chartMarks.value)), rtl });
}
watch([chartMarks, artifactIframeLoaded], postChartMarks);

onBeforeUnmount(() => {
    unmounted = true;
    if (pendingTimer) clearTimeout(pendingTimer);
    pendingTimer = null;
    window.removeEventListener('message', handleArtifactParamsMessage);
});

// Resolve embedded files to PUBLIC, report-scoped token URLs so a non-auth
// viewer can render generated images / PDFs in the artifact. The token is
// authorized by (report is public) + (file is embedded in this report's artifact).
async function loadArtifactFiles() {
    const files = (artifact.value as any)?.content?.files;
    if (!Array.isArray(files) || files.length === 0) { filesData.value = []; return; }
    filesData.value = await Promise.all(files.map(async (f: any) => {
        try {
            const { data } = await useMyFetch(api.embedToken(f.id));
            return { id: f.id, content_type: f.content_type, filename: f.filename, url: (data.value as any)?.url || '' };
        } catch (e) {
            return { id: f.id, content_type: f.content_type, filename: f.filename, url: '' };
        }
    }));
}

// Mirror the report's last_run_at into the "Refreshed ..." label. Backend
// stores UTC without timezone info, so append 'Z' when it's missing. A viewer
// who ran the dashboard under their own identity has a newer per-user run
// (viewerLastRunAt) — prefer it over the shared snapshot's timestamp.
function syncLastRefreshed() {
    if (viewerLastRunAt.value) {
        lastRefreshedAt.value = viewerLastRunAt.value;
        return;
    }
    const ts = report.value?.last_run_at;
    lastRefreshedAt.value = ts ? new Date(ts.endsWith('Z') ? ts : ts + 'Z') : null;
}

// Rerun this report's queries because a viewer opened the page, then swap the
// fresh data in underneath the already-painted dashboard.
//
// The server is the authority on whether this actually runs: it enforces the
// opt-in flag, view permission, a 5-minute floor between runs, and a
// single-flight claim, answering 200 with skipped=true when it declines. So the
// page can simply ask on every open — the local flag check below only avoids a
// pointless round trip for the reports that never opted in.
// Withheld dashboard, signed-in viewer, and either no result of their own or a
// last run that stopped on a missing sign-in (only a run names the connection
// to connect, and they may have connected since): run for them, once per page
// load -- a viewer whose run failed otherwise gets the explicit fallback state
// instead of a retry loop.
async function autoRunForViewer() {
    if (!snapshotWithheld.value || !canRun.value || isRunning.value || autoRunTried.value) return;
    // An editor previewing someone else's version (collab01) runs it as
    // themselves once whenever a chart is withheld -- its newer charts have
    // no result of theirs yet, though older ones may.
    const needsRun = !hasOwnResult.value || (viewerRunNeedsConnect.value && dataSourceErrors.value.length === 0)
        || (props.source === 'draft' && withheldCharts.value.length > 0);
    if (!needsRun) return;
    autoRunTried.value = true;
    await handleRun();
}

async function refreshOnView() {
    if (!report.value?.refresh_on_view || !api.refreshOnView) return;

    isRefreshingOnView.value = true;
    try {
        const { data, error: rerunError } = await useMyFetch(api.refreshOnView, { method: 'POST' });
        const run = data.value as any;
        // Declined, failed, or nothing actually reran — leave the rendered data
        // alone. Reloading it would cost a round trip to redraw the same rows.
        // (A dashboard's refresh is all-or-nothing: it says "success".)
        const reran = props.source === 'dashboard' ? run?.status === 'success' : !!run?.steps_succeeded;
        if (rerunError.value || !run || run.skipped || !reran) return;

        await loadVisualizationData(artifact.value?.id);
        deliverArtifactData();
        report.value.last_run_at = run.last_run_at;
        syncLastRefreshed();
        // A refresh drops every reader's own results. A reader looking at a
        // withheld dashboard would be left with nothing of theirs: run again
        // for them, as on a first open (one more run, whatever ran before).
        autoRunTried.value = false;
        await autoRunForViewer();
    } catch (e) {
        // A failed refresh must never take down a dashboard that already
        // rendered — the viewer keeps the data the page loaded with.
        console.error('Refresh on view failed:', e);
    } finally {
        isRefreshingOnView.value = false;
    }
}

// An old /r/{conversation} link: since the data migration (phase 9) the
// dashboard it showed is a dashboard of its own; go there, keeping the link
// working (emails, bookmarks, pages that embed it). A conversation without
// one is served here as before.
async function forwardToDashboard(): Promise<boolean> {
    if (props.source !== 'report') return false;
    const { data, error } = await useMyFetch(`/r/${encodeURIComponent(props.id)}/dashboard`);
    const target = (data.value as any)?.dashboard_id;
    if (error.value || !target) return false;
    await navigateTo(`/d/${encodeURIComponent(target)}`, { replace: true });
    return true;
}

onMounted(async () => {
    if (await forwardToDashboard()) return;
    // Load report and artifact in parallel first (viewer identity alongside —
    // it gates nothing; anonymous viewers just get current_user=null)
    // A dashboard's content is read after its answer (which names the
    // version on screen, or sends an unpublished one to its draft).
    if (props.source === 'report') {
        await Promise.all([
            loadReport(),
            loadArtifact(),
            fetchViewerContext()
        ]);
    } else {
        await Promise.all([loadReport(), fetchViewerContext()]);
        if (accessError.value || !dashboardMeta.value) return;
        await loadArtifact();
    }

    // Load visualization data with artifact filter (if artifact exists)
    // This ensures we only fetch queries used by the artifact
    const artifactId = artifact.value?.id;
    await loadVisualizationData(artifactId);

    // Params: defaults from declarations; identity-scoped queries run as the
    // signed-in viewer BEFORE first paint so the dashboard opens on their
    // slice (per-viewer cached — revisits are cache hits). Anonymous viewers
    // render the shared/withheld state unchanged.
    initParamValues();
    if (viewerContext.value && queriesWithIdentityParams().length) {
        try { await runParamQueries(null, null, { identityOnly: true }); } catch { /* non-fatal */ }
    }
    window.addEventListener('message', handleArtifactParamsMessage);

    // Freeze the srcdoc seed so later param runs update via postMessage
    // without reloading the iframe.
    srcdocSeed.value = JSON.parse(JSON.stringify({
        report: {
            id: report.value.id,
            title: report.value.title,
            theme: report.value.theme_name || report.value.report_theme_name
        },
        visualizations: visualizationsData.value,
        files: filesData.value,
        current_user: viewerContext.value,
        params: paramsPayload(),
        runtime: artifactRuntime()
    }));

    // Mark data as ready - this triggers iframeSrcdoc to compute once with all data
    dataReady.value = true;
    reportLoaded.value = true;
    // Use the report's last_run_at timestamp (when data was actually refreshed)
    // syncLastRefreshed prefers the viewer's own per-user run timestamp when set.
    syncLastRefreshed();

    // Withheld dashboard, signed-in viewer, no result of their own yet: run
    // the queries for them instead of making them find a Run button — the
    // gate shows "Loading your data" while it's in flight. Fires once per
    // page load and only for first-time viewers; a viewer whose earlier run
    // failed gets the explicit fallback state instead of a retry loop.
    // Results are cached per viewer (step_user_results), so later opens
    // render immediately without re-running.
    await autoRunForViewer();

    // "Changed by" on hover needs the changes; the panel reuses them.
    loadChanges();

    // Paint first, refresh second: the viewer sees the dashboard immediately and
    // fresh numbers replace the cached ones when the rerun lands, rather than
    // staring at a spinner for however long the queries take.
    await refreshOnView();
});
</script>
