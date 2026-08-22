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
            <a v-if="accessError === 'login'" :href="`/users/sign-in?redirect=/r/${$route.params.id}`"
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
                <!-- Run button: signed-in non-owner viewers re-run the dashboard's
                     queries into their own per-user results (whose credentials run
                     is the owner's share setting; never touches the shared data) -->
                <button
                    v-if="canRun"
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
                <button
                    v-if="canExportPdf"
                    @click="handleExportPdf"
                    :disabled="isExportingPdf"
                    class="flex items-center gap-1 text-xs text-gray-400 hover:text-gray-600 transition-colors disabled:opacity-50"
                    title="Download this dashboard as a PDF"
                >
                    <Icon :name="isExportingPdf ? 'heroicons:arrow-path' : 'heroicons:arrow-down-tray'"
                        :class="['w-3.5 h-3.5', isExportingPdf ? 'animate-spin' : '']" />
                    <span class="hidden sm:inline">{{ isExportingPdf ? 'Exporting...' : 'Export PDF' }}</span>
                </button>
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
                <NuxtLink
                    v-if="isOwner"
                    :to="`/reports/${report_id}`"
                    class="flex items-center gap-1 text-xs text-gray-400 hover:text-gray-600 transition-colors"
                >
                    <Icon name="heroicons:pencil-square" class="w-3.5 h-3.5" />
                    <span class="hidden sm:inline">Edit Report</span>
                </NuxtLink>
                <button
                    @click="showTopBar = false"
                    class="p-1 text-gray-400 hover:text-gray-600 hover:bg-gray-100 dark:hover:bg-gray-700 rounded transition-colors"
                >
                    <Icon name="heroicons:x-mark" class="w-4 h-4" />
                </button>
            </div>
        </div>

        <!-- Made with Bag of words badge -->
        <a v-if="report.general?.bow_credit !== false"
           href="https://bagofwords.com"
           target="_blank"
           class="fixed z-[1000] bottom-5 end-5 block bg-black text-gray-200 font-light px-2 py-1 rounded-md text-xs hover:bg-gray-800 transition-colors">
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
                <div v-if="snapshotWithheld" class="absolute inset-0 z-20 flex items-center justify-center bg-white/85 dark:bg-gray-900/85">
                    <ViewerRunGate :state="gateState" :report-id="String($route.params.id)"
                        :is-running="isRunning" :source-errors="dataSourceErrors"
                        :error-message="gateErrorMessage" :source-type="gateSourceType" @run="handleRun" />
                </div>

                <!-- Slides with Preview Images - Use SlideViewer -->
                <SlideViewer
                    v-if="hasSlidesWithPreviews && artifact"
                    :artifact-id="artifact.id"
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
                    sandbox="allow-scripts allow-same-origin allow-downloads"
                    class="absolute inset-0 w-full h-full border-0 bg-white"
                />

                <!-- Legacy Dashboard View (reports with dashboard_layout_versions but no artifacts) -->
                <DashboardComponent
                    v-else-if="hasLegacyLayout && !hasArtifacts && reportLoaded"
                    :report="report"
                    :edit="false"
                    class="absolute inset-0 w-full h-full"
                />

                <!-- Loading state -->
                <div v-else-if="!reportLoaded" class="absolute inset-0 flex items-center justify-center text-gray-400">
                    <div class="text-center">
                        <Icon name="heroicons:document-chart-bar" class="w-12 h-12 mx-auto mb-3 opacity-50" />
                        <p>Loading...</p>
                    </div>
                </div>

                <!-- Empty state (no artifacts, no legacy layout) -->
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
                <div v-if="snapshotWithheld" class="flex items-center justify-center h-full">
                    <ViewerRunGate :state="gateState" :report-id="String($route.params.id)"
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
        </div>
    </div>
</template>

<script setup lang="ts">
import DashboardComponent from '~/components/DashboardComponent.vue';
import ToolWidgetPreview from '~/components/tools/ToolWidgetPreview.vue';
import SlideViewer from '~/components/dashboard/SlideViewer.vue';
import DocViewer from '~/components/dashboard/DocViewer.vue';
import ViewerRunGate from '~/components/dashboard/ViewerRunGate.vue';
import { buildArtifactIframeHtml, isHtmlSlidesCode } from '~/utils/artifactIframe';

const route = useRoute();
const report_id = route.params.id;
const { data: currentUser } = useAuth();

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
const hasLegacyLayout = ref(false);
const reportLoaded = ref(false);
const dataReady = ref(false);

// Check if current user is the report owner
const isOwner = computed(() => {
    const userId = (currentUser.value as any)?.user?.id || (currentUser.value as any)?.id;
    return userId && report.value?.user?.id === userId;
});

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
const runError = ref<string | null>(null);
// Newest per-viewer result timestamp seen while loading step data
const viewerLastRunAt = ref<Date | null>(null);
// True when the backend hid the owner's snapshot from this viewer
// (viewer-identity sharing on user-scoped data sources, or RLS)
const snapshotWithheld = ref(false);
// True when any step already carries a per-viewer result row for this user —
// auto-run only fires for a first-time viewer (no row at all); a failed
// earlier run becomes an explicit fallback state instead of a retry loop.
const hasOwnResult = ref(false);
// status_reason of the viewer's failed result row, if their last run errored
const viewerRunFailedReason = ref<string | null>(null);
// Data sources whose viewer client couldn't be built on the last run
// (e.g. a user_required source the viewer hasn't connected yet)
const dataSourceErrors = ref<Array<{ data_source: string; data_source_id?: string; code?: string; error: string }>>([]);
// What the gate's error state shows: the viewer's failed run reason, or the
// first data-source failure when the run itself never produced results.
const gateErrorMessage = computed(() =>
    viewerRunFailedReason.value || dataSourceErrors.value[0]?.error || null);

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
    return !!userId && !isOwner.value && hasArtifacts.value && reportLoaded.value;
});

// Which gate the withheld overlay shows. Auto-run makes 'loading' the state a
// signed-in viewer normally sees; the rest are fallbacks for when a run can't
// succeed on its own. Data-source errors carry a machine-readable code so the
// gate offers the right action: connect their credential ('connect'), ask an
// admin ('no_access'), or retry ('error').
const gateState = computed<'loading' | 'signin' | 'connect' | 'no_access' | 'error' | 'ready'>(() => {
    if (isRunning.value) return 'loading';
    if (!canRun.value) return 'signin';
    const errs = dataSourceErrors.value;
    if (errs.some((e) => e.code === 'credentials_required')) return 'connect';
    if (errs.some((e) => e.code === 'no_access')) return 'no_access';
    if (errs.length > 0 || viewerRunFailedReason.value) return 'error';
    return 'ready';
});

async function handleRun() {
    if (isRunning.value) return;
    isRunning.value = true;
    runError.value = null;
    try {
        const artifactParam = artifact.value?.id ? `?artifact_id=${artifact.value.id}` : '';
        const { data, error: fetchError } = await useMyFetch(`/api/r/${report_id}/run${artifactParam}`, { method: 'POST' });
        if (fetchError.value) throw fetchError.value;
        const run = (data.value || {}) as any;
        dataSourceErrors.value = run.data_source_errors || [];
        await loadVisualizationData(artifact.value?.id);
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
const canExportPdf = computed(() =>
    reportLoaded.value && hasArtifacts.value && !!artifact.value &&
    artifact.value.mode !== 'doc' && !snapshotWithheld.value);

async function handleExportPdf() {
    if (isExportingPdf.value) return;
    isExportingPdf.value = true;
    exportPdfError.value = null;
    try {
        const { data, error: fetchError } = await useMyFetch(`/api/r/${report_id}/export_pdf`, {
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

// Fork state
const forkEligibility = ref<any>(null);
const isForking = ref(false);

const forkReasonLabel = computed(() => {
    const reason = forkEligibility.value?.reason;
    switch (reason) {
        case 'not_logged_in': return 'Sign in to fork this report';
        case 'different_org': return 'You must be in the same organization';
        case 'user_auth_required': return 'Data source requires user credentials';
        case 'no_data_source_access': return 'You don\'t have access to the data sources';
        case 'forks_disabled': return 'Forking is disabled for this organization';
        default: return '';
    }
});

async function handleFork() {
    if (isForking.value) return;
    isForking.value = true;
    try {
        const { data, error: fetchError } = await useMyFetch(`/api/reports/${report_id}/fork`, {
            method: 'POST',
            body: {},
        });
        if (data.value && !fetchError.value) {
            navigateTo(`/reports/${(data.value as any).id}`);
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

definePageMeta({
    layout: false,
    auth: false
});

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
        const { data, error: fetchError } = await useMyFetch(`/api/r/${report_id}`);
        if (fetchError.value) {
            const status = (fetchError.value as any)?.statusCode || (fetchError.value as any)?.status;
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
        report.value = data.value;
        forkEligibility.value = (data.value as any)?.fork_eligibility || null;
    } catch (e) {
        console.error('Failed to load report:', e);
        navigateTo('/not_found');
    }
}

// Fetch the latest artifact for this report (using public endpoints)
async function loadArtifact() {
    try {
        // Use public endpoint - no auth required
        const { data } = await useMyFetch(`/api/r/${report_id}/artifacts`);
        if (data.value && Array.isArray(data.value) && data.value.length > 0) {
            hasArtifacts.value = true;
            // Get the most recent artifact (first in list)
            const latestArtifactId = data.value[0].id;
            // Use public artifact endpoint
            const { data: fullArtifact } = await useMyFetch(`/api/r/${report_id}/artifacts/${latestArtifactId}`);
            if (fullArtifact.value) {
                artifact.value = fullArtifact.value;
                await loadArtifactFiles();
            }
        } else {
            hasArtifacts.value = false;
        }
    } catch (e) {
        hasArtifacts.value = false;
        console.log('[PublicArtifact] No artifact found, will check for legacy layout');
    }
}

// Check if report has legacy dashboard layout
async function checkLegacyLayout() {
    try {
        const { data } = await useMyFetch(`/api/r/${report_id}/layouts?hydrate=true`);
        const layouts = Array.isArray(data.value) ? data.value : [];
        const activeLayout = layouts.find((l) => l.is_active);
        if (activeLayout?.blocks && Array.isArray(activeLayout.blocks) && activeLayout.blocks.length > 0) {
            hasLegacyLayout.value = true;
        }
    } catch (e) {
        hasLegacyLayout.value = false;
    }
}

// Fetch visualization data for the artifact (using public endpoints)
async function loadVisualizationData(artifactId?: string) {
    try {
        // Use public endpoint - no auth required
        // If artifactId provided, filter to only queries used by that artifact
        const queryParams = artifactId ? `?artifact_id=${artifactId}` : '';
        const { data: queriesRes } = await useMyFetch(`/api/r/${report_id}/queries${queryParams}`);
        const queries = Array.isArray(queriesRes.value) ? queriesRes.value : [];

        // Fetch all steps in parallel — awaiting each one serially made the
        // page's time-to-render scale linearly with the number of queries.
        const stepResults = await Promise.all(
            queries.map((query) => useMyFetch(`/api/r/${report_id}/queries/${(query as any).id}/step`))
        );

        const vizData = [];
        const rowsByQuery = new Map<string, any[]>();
        let newestViewerRun: Date | null = null;
        let anyWithheld = false;
        let anyOwnResult = false;
        let failedReason: string | null = null;
        for (let qi = 0; qi < queries.length; qi++) {
            const query = queries[qi];
            // Public step endpoint - returns PublicStepSchema directly
            const { data: step } = stepResults[qi];

            if ((step.value as any)?.snapshot_withheld) anyWithheld = true;

            // Steps the viewer re-ran carry their per-user result row — its
            // presence gates auto-run (first-time viewers only), and a failed
            // row's reason feeds the gate's error state.
            const vr = (step.value as any)?.viewer_result;
            if (vr) {
                anyOwnResult = true;
                if (vr.status === 'error' && !failedReason) failedReason = vr.status_reason || null;
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
        await resolveParamOptions(rowsByQuery);
        viewerLastRunAt.value = newestViewerRun;
        snapshotWithheld.value = anyWithheld;
        hasOwnResult.value = anyOwnResult;
        viewerRunFailedReason.value = failedReason;
    } catch (e) {
        console.error('Failed to load visualization data:', e);
    }
}


// ── Query parameters (authenticated viewers) ────────────────────────────────
// Same contract as ArtifactFrame: the iframe posts ARTIFACT_SET_PARAMS /
// ARTIFACT_REFRESH_PARAMS; this host runs the consuming queries in viewer
// mode and pushes fresh rows back with a new ARTIFACT_DATA message.
// Anonymous viewers can't run (auth required) — their controls no-op.
const artifactIframeRef = ref<HTMLIFrameElement | null>(null);
const queryParamSpecs = ref<Record<string, any[]>>({});
const paramValues = ref<Record<string, any>>({});
// Host-resolved stable choices per param (static options + options-source
// query rows) — mirrors ArtifactFrame; useParamOptions() reads these.
const paramOptions = ref<Record<string, Array<{ value: any; label: string }>>>({});
// Frozen at dataReady so live updates never recompute srcdoc (iframe reload).
const srcdocSeed = ref<any>(null);

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
    };
}

// Resolve stable control choices. `rowsByQueryId` carries the step rows this
// load already fetched; a source query outside the artifact is fetched from
// the report-level public list (plus its step) once.
async function resolveParamOptions(rowsByQueryId: Map<string, any[]>) {
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
            let rows = rowsByQueryId.get(String(src.query_id));
            if (!rows && !fetchedAll) {
                fetchedAll = true;
                try {
                    const { data } = await useMyFetch(`/api/r/${report_id}/queries`);
                    const all = Array.isArray(data.value) ? (data.value as any[]) : [];
                    const missing = all.filter(q => !rowsByQueryId.has(String(q.id)));
                    const steps = await Promise.all(
                        missing.map(q => useMyFetch(`/api/r/${report_id}/queries/${q.id}/step`)));
                    missing.forEach((q, i) => {
                        rowsByQueryId.set(String(q.id), (steps[i].data.value as any)?.data?.rows || []);
                    });
                } catch { /* options degrade to static/none */ }
                rows = rowsByQueryId.get(String(src.query_id));
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
        artifactIframeRef.value?.contentWindow?.postMessage(msg, window.location.origin);
    } catch { /* iframe not ready */ }
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
            params: paramsPayload()
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
            const { data: runRes, error } = await useMyFetch(`/api/queries/${qid}/run`, { method: 'POST', body });
            const res: any = runRes.value;
            if (error.value || !res || res.status !== 'success') {
                const msg = (error.value as any)?.data?.detail || res?.error || 'Query run failed';
                if (!firstError) firstError = String(msg);
                return;
            }
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
    postToArtifactIframe({ type: 'ARTIFACT_PARAMS_STATUS', payload: { loading: false, error: firstError } });
    pushArtifactData();
}

function handleArtifactParamsMessage(event: MessageEvent) {
    if (event.source !== artifactIframeRef.value?.contentWindow || !event.data) return;
    if (event.data.type === 'ARTIFACT_SET_PARAMS') {
        runParamQueries(event.data.changes || {}, event.data.targets || null, {});
    } else if (event.data.type === 'ARTIFACT_REFRESH_PARAMS') {
        runParamQueries(null, event.data.targets || null, { force: true });
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
        params: paramsPayload()
    };
    return buildArtifactIframeHtml({
        data: seed,
        code: artifactCode,
        mode: artifact.value?.mode || 'page',
    });
});

// Resolve embedded files to PUBLIC, report-scoped token URLs so a non-auth
// viewer can render generated images / PDFs in the artifact. The token is
// authorized by (report is public) + (file is embedded in this report's artifact).
async function loadArtifactFiles() {
    const files = (artifact.value as any)?.content?.files;
    if (!Array.isArray(files) || files.length === 0) { filesData.value = []; return; }
    filesData.value = await Promise.all(files.map(async (f: any) => {
        try {
            const { data } = await useMyFetch(`/api/r/${report_id}/files/${f.id}/embed_token`);
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
async function refreshOnView() {
    if (!report.value?.refresh_on_view) return;

    isRefreshingOnView.value = true;
    try {
        const { data, error: rerunError } = await useMyFetch(`/api/r/${report_id}/rerun`, { method: 'POST' });
        const run = data.value as any;
        // Declined, failed, or nothing actually reran — leave the rendered data
        // alone. Reloading it would cost a round trip to redraw the same rows.
        if (rerunError.value || !run || run.skipped || !run.steps_succeeded) return;

        await loadVisualizationData(artifact.value?.id);
        report.value.last_run_at = run.last_run_at;
        syncLastRefreshed();
    } catch (e) {
        // A failed refresh must never take down a dashboard that already
        // rendered — the viewer keeps the data the page loaded with.
        console.error('Refresh on view failed:', e);
    } finally {
        isRefreshingOnView.value = false;
    }
}

onMounted(async () => {
    // Load report and artifact in parallel first (viewer identity alongside —
    // it gates nothing; anonymous viewers just get current_user=null)
    await Promise.all([
        loadReport(),
        loadArtifact(),
        fetchViewerContext()
    ]);

    // Load visualization data with artifact filter (if artifact exists)
    // This ensures we only fetch queries used by the artifact
    const artifactId = artifact.value?.id;
    await loadVisualizationData(artifactId);

    // If no artifacts, check for legacy layout
    if (!hasArtifacts.value) {
        await checkLegacyLayout();
    }

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
        params: paramsPayload()
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
    if (snapshotWithheld.value && canRun.value && !hasOwnResult.value && !autoRunTried.value) {
        autoRunTried.value = true;
        await handleRun();
    }

    // Paint first, refresh second: the viewer sees the dashboard immediately and
    // fresh numbers replace the cached ones when the rerun lands, rather than
    // staring at a spinner for however long the queries take.
    await refreshOnView();
});
</script>
