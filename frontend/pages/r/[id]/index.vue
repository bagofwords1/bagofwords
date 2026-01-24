<template>
    <div class="h-screen w-screen relative bg-gray-50">
        <!-- Made with Bag of words badge -->
        <a v-if="report.general?.bow_credit !== false"
           href="https://bagofwords.com"
           target="_blank"
           class="fixed z-[1000] bottom-5 right-5 block bg-black text-gray-200 font-light px-2 py-1 rounded-md text-xs hover:bg-gray-800 transition-colors">
            Made with <span class="font-bold text-white">Bag of words</span>
        </a>

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
    </div>
</template>

<script setup>
import DashboardComponent from '~/components/DashboardComponent.vue';

const route = useRoute();
const report_id = route.params.id;

const report = ref({
    title: '',
    id: '',
    user: { name: '' },
    general: {}
});

const artifact = ref(null);
const visualizationsData = ref([]);
const hasArtifacts = ref(false);
const hasLegacyLayout = ref(false);
const reportLoaded = ref(false);

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

// Fetch the latest artifact for this report
async function loadArtifact() {
    try {
        const { data } = await useMyFetch(`/api/artifacts/report/${report_id}`);
        if (data.value && Array.isArray(data.value) && data.value.length > 0) {
            hasArtifacts.value = true;
            // Get the most recent artifact (first in list)
            const latestArtifactId = data.value[0].id;
            const { data: fullArtifact } = await useMyFetch(`/api/artifacts/${latestArtifactId}`);
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

// Fetch visualization data for the artifact
async function loadVisualizationData() {
    try {
        const { data: queriesRes } = await useMyFetch(`/api/queries?report_id=${report_id}`);
        const queries = Array.isArray(queriesRes.value) ? queriesRes.value : [];

        const vizData = [];
        for (const query of queries) {
            const { data: stepRes } = await useMyFetch(`/api/queries/${query.id}/default_step`);
            const step = stepRes.value?.step;

            for (const viz of query.visualizations || []) {
                vizData.push({
                    id: viz.id,
                    title: viz.title || query.title || 'Untitled',
                    view: viz.view || {},
                    rows: step?.data?.rows || [],
                    columns: step?.data?.columns || [],
                    dataModel: step?.data_model || {}
                });
            }
        }
        visualizationsData.value = vizData;
    } catch (e) {
        console.error('Failed to load visualization data:', e);
    }
}


// Build the iframe srcdoc
const iframeSrcdoc = computed(() => {
    const embeddedData = JSON.stringify({
        report: {
            id: report.value.id,
            title: report.value.title,
            theme: report.value.theme_name || report.value.report_theme_name
        },
        visualizations: visualizationsData.value
    });

    const artifactCode = artifact.value?.content?.code;
    if (!artifactCode) return null;
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
  ${SC}

  ${artifactCode}
</body>
</html>`;
});

onMounted(async () => {
    await Promise.all([
        loadReport(),
        loadArtifact(),
        loadVisualizationData()
    ]);

    // If no artifacts, check for legacy layout
    if (!hasArtifacts.value) {
        await checkLegacyLayout();
    }

    reportLoaded.value = true;
});
</script>
