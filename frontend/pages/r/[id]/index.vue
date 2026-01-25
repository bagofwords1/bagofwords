<template>
    <div class="h-screen w-screen relative bg-gray-50 flex flex-col">
        <!-- Top Bar -->
        <div v-if="showTopBar && reportLoaded" class="flex-shrink-0 h-10 bg-white border-b border-gray-200 relative">
            <!-- Left: Back to app (absolute) -->
            <a
                href="/"
                class="absolute left-4 top-1/2 -translate-y-1/2 flex items-center gap-1 text-xs text-gray-400 hover:text-gray-600 transition-colors"
            >
                <Icon name="heroicons:arrow-left" class="w-3.5 h-3.5" />
                <span>Back to app</span>
            </a>

            <!-- Center: Tab Menu + Refreshed (matching dashboard content padding) -->
            <div class="h-full flex-1 flex items-center">
                <div class="w-full flex items-center justify-between px-[200px]">
                    <!-- Tab Menu -->
                    <div class="flex items-center gap-1">
                        <button
                            @click="activeTab = 'report'"
                            :class="[
                                'px-3 py-1.5 text-xs font-medium rounded transition-colors',
                                activeTab === 'report'
                                    ? 'bg-gray-100 text-gray-900'
                                    : 'text-gray-500 hover:text-gray-700 hover:bg-gray-50'
                            ]"
                        >
                            Report
                        </button>
                        <button
                            @click="activeTab = 'data'"
                            :class="[
                                'px-3 py-1.5 text-xs font-medium rounded transition-colors',
                                activeTab === 'data'
                                    ? 'bg-gray-100 text-gray-900'
                                    : 'text-gray-500 hover:text-gray-700 hover:bg-gray-50'
                            ]"
                        >
                            Data ({{ visualizationsData.length }})
                        </button>
                    </div>

                    <!-- Refreshed text -->
                    <span v-if="lastRefreshedAt" class="text-[11px] text-gray-400">
                        Refreshed {{ formatTime(lastRefreshedAt) }}
                    </span>
                </div>
            </div>

            <!-- Right: Close (absolute) -->
            <button
                @click="showTopBar = false"
                class="absolute right-4 top-1/2 -translate-y-1/2 p-1 text-gray-400 hover:text-gray-600 hover:bg-gray-100 rounded transition-colors"
            >
                <Icon name="heroicons:x-mark" class="w-4 h-4" />
            </button>
        </div>

        <!-- Made with Bag of words badge -->
        <a v-if="report.general?.bow_credit !== false"
           href="https://bagofwords.com"
           target="_blank"
           class="fixed z-[1000] bottom-5 right-5 block bg-black text-gray-200 font-light px-2 py-1 rounded-md text-xs hover:bg-gray-800 transition-colors">
            Made with <span class="font-bold text-white">Bag of words</span>
        </a>

        <!-- Main Content Area -->
        <div class="flex-1 min-h-0 relative">
            <!-- Report Tab: Artifact/Dashboard Content -->
            <template v-if="activeTab === 'report'">
                <!-- Artifact Content - Full screen (modern reports with artifacts) -->
                <iframe
                    v-if="hasArtifacts && iframeSrcdoc"
                    :srcdoc="iframeSrcdoc"
                    sandbox="allow-scripts allow-same-origin"
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
            <div v-else-if="activeTab === 'data'" class="absolute inset-0 overflow-y-auto bg-gray-50 p-4">
                <div v-if="visualizationsData.length === 0" class="flex items-center justify-center h-full text-gray-400">
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

const route = useRoute();
const report_id = route.params.id;

const report = ref<any>({
    title: '',
    id: '',
    user: { name: '' },
    general: {}
});

const artifact = ref<any>(null);
const visualizationsData = ref<any[]>([]);
const hasArtifacts = ref(false);
const hasLegacyLayout = ref(false);
const reportLoaded = ref(false);
const dataReady = ref(false);

// Top bar state
const showTopBar = ref(true);
const activeTab = ref<'report' | 'data'>('report');
const lastRefreshedAt = ref<Date | null>(null);

// Format time for display
function formatTime(date: Date | null) {
    if (!date) return '';
    const now = new Date();
    const diff = now.getTime() - date.getTime();
    const minutes = Math.floor(diff / 60000);
    if (minutes < 1) return 'just now';
    if (minutes < 60) return `${minutes}m ago`;
    const hours = Math.floor(minutes / 60);
    if (hours < 24) return `${hours}h ago`;
    return date.toLocaleDateString();
}

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

definePageMeta({
    layout: false,
    auth: false
});

// Fetch report info
async function loadReport() {
    try {
        const { data } = await useMyFetch(`/api/r/${report_id}`);
        if (!data.value) {
            navigateTo('/not_found');
            return;
        }
        report.value = data.value;
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

        const vizData = [];
        for (const query of queries) {
            // Use public step endpoint - returns PublicStepSchema directly
            const { data: step } = await useMyFetch(`/api/r/${report_id}/queries/${query.id}/step`);

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
                    code: (step.value as any)?.code || ''
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
        visualizationsData.value = vizData;
    } catch (e) {
        console.error('Failed to load visualization data:', e);
    }
}


// Build the iframe srcdoc - only compute once all data is ready
const iframeSrcdoc = computed(() => {
    // Wait until all data is loaded to prevent multiple iframe reloads
    if (!dataReady.value) return null;

    const artifactCode = artifact.value?.content?.code;
    if (!artifactCode) return null;

    const embeddedData = JSON.stringify({
        report: {
            id: report.value.id,
            title: report.value.title,
            theme: report.value.theme_name || report.value.report_theme_name
        },
        visualizations: visualizationsData.value
    });
    const artifactMode = artifact.value?.mode || 'page';

    const SC = '</' + 'script>';

    // Slides mode: Pure HTML + Tailwind (no React/Babel)
    if (artifactMode === 'slides') {
        return `<!DOCTYPE html>
<html>
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <script src="https://cdn.tailwindcss.com">${SC}
  <style>
    html, body { height: 100%; margin: 0; padding: 0; }
    body { font-family: system-ui, -apple-system, sans-serif; }
    .slide { transition: opacity 0.3s ease-in-out; }
  </style>
</head>
<body class="bg-slate-900">
  <script>
    window.ARTIFACT_DATA = ${embeddedData};
  ${SC}

  ${artifactCode}
</body>
</html>`;
    }

    // Dashboard mode: React + Babel + ECharts
    return `<!DOCTYPE html>
<html>
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <script src="https://cdn.tailwindcss.com">${SC}
  <script crossorigin src="https://unpkg.com/react@18/umd/react.development.js">${SC}
  <script crossorigin src="https://unpkg.com/react-dom@18/umd/react-dom.development.js">${SC}
  <script src="https://unpkg.com/@babel/standalone/babel.min.js">${SC}
  <script src="https://cdn.jsdelivr.net/npm/echarts@5/dist/echarts.min.js">${SC}
  <style>
    html, body, #root { height: 100%; margin: 0; padding: 0; }
    body { font-family: system-ui, -apple-system, sans-serif; }
  </style>
</head>
<body>
  <div id="root"><div style="display:flex;align-items:center;justify-content:center;height:100%;color:#9ca3af;">Loading...</div></div>

  <script>
    window.ARTIFACT_DATA = ${embeddedData};
    window.useArtifactData = function() {
      return window.ARTIFACT_DATA;
    };
    // Global LoadingSpinner component for artifact code
    window.LoadingSpinner = function(props) {
      var size = props && props.size ? props.size : 24;
      return React.createElement('svg', {
        xmlns: 'http://www.w3.org/2000/svg',
        width: size,
        height: size,
        viewBox: '0 0 24 24',
        className: props && props.className ? props.className : ''
      },
        React.createElement('path', {
          fill: 'currentColor',
          d: 'M12 2A10 10 0 1 0 22 12A10 10 0 0 0 12 2Zm0 18a8 8 0 1 1 8-8A8 8 0 0 1 12 20Z',
          opacity: '0.5'
        }),
        React.createElement('path', {
          fill: 'currentColor',
          d: 'M20 12h2A10 10 0 0 0 12 2V4A8 8 0 0 1 20 12Z'
        },
          React.createElement('animateTransform', {
            attributeName: 'transform',
            dur: '1s',
            from: '0 12 12',
            repeatCount: 'indefinite',
            to: '360 12 12',
            type: 'rotate'
          })
        )
      );
    };
    // Fix ECharts 0-height issue: resize all charts after render
    window.resizeAllCharts = function() {
      if (typeof echarts !== 'undefined') {
        var charts = document.querySelectorAll('[_echarts_instance_]');
        charts.forEach(function(el) {
          var chart = echarts.getInstanceByDom(el);
          if (chart) chart.resize();
        });
      }
    };
    // Auto-resize after React renders
    setTimeout(window.resizeAllCharts, 100);
    setTimeout(window.resizeAllCharts, 500);
    window.addEventListener('resize', window.resizeAllCharts);
  ${SC}

  ${artifactCode}
</body>
</html>`;
});

onMounted(async () => {
    // Load report and artifact in parallel first
    await Promise.all([
        loadReport(),
        loadArtifact()
    ]);

    // Load visualization data with artifact filter (if artifact exists)
    // This ensures we only fetch queries used by the artifact
    const artifactId = artifact.value?.id;
    await loadVisualizationData(artifactId);

    // If no artifacts, check for legacy layout
    if (!hasArtifacts.value) {
        await checkLegacyLayout();
    }

    // Mark data as ready - this triggers iframeSrcdoc to compute once with all data
    dataReady.value = true;
    reportLoaded.value = true;
    // Use the report's last_run_at timestamp (when data was actually refreshed)
    // Append 'Z' to treat as UTC since backend stores UTC without timezone info
    if (report.value.last_run_at) {
        const ts = report.value.last_run_at;
        lastRefreshedAt.value = new Date(ts.endsWith('Z') ? ts : ts + 'Z');
    } else {
        lastRefreshedAt.value = null;
    }
});
</script>
