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

// Sample artifact code when no artifact exists
const sampleArtifactCode = computed(() => {
    const SC = '</' + 'script>';
    return `
<script type="text/babel">
function App() {
  const data = useArtifactData();

  if (!data) {
    return (
      <div className="flex items-center justify-center h-full">
        <div className="animate-pulse text-gray-400">Loading...</div>
      </div>
    );
  }

  const { report, visualizations } = data;

  return (
    <div className="min-h-full bg-gradient-to-br from-slate-50 to-slate-100 p-8">
      <div className="mb-8">
        <h1 className="text-3xl font-bold text-gray-900 tracking-tight">
          {report?.title || 'Dashboard'}
        </h1>
        <p className="text-sm text-gray-500 mt-2">
          {visualizations.length} visualization{visualizations.length !== 1 ? 's' : ''} available
        </p>
      </div>

      {visualizations.length === 0 ? (
        <div className="flex flex-col items-center justify-center py-16 text-center">
          <div className="w-16 h-16 bg-gray-100 rounded-full flex items-center justify-center mb-4">
            <svg className="w-8 h-8 text-gray-400" fill="none" viewBox="0 0 24 24" stroke="currentColor">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={1.5} d="M9 17v-2m3 2v-4m3 4v-6m2 10H7a2 2 0 01-2-2V5a2 2 0 012-2h5.586a1 1 0 01.707.293l5.414 5.414a1 1 0 01.293.707V19a2 2 0 01-2 2z" />
            </svg>
          </div>
          <h3 className="text-lg font-medium text-gray-900 mb-1">No visualizations yet</h3>
          <p className="text-sm text-gray-500 max-w-sm">
            This dashboard doesn't have any visualizations to display.
          </p>
        </div>
      ) : (
        <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
          {visualizations.map((viz) => (
            <VisualizationCard key={viz.id} viz={viz} />
          ))}
        </div>
      )}
    </div>
  );
}

function VisualizationCard({ viz }) {
  const chartRef = React.useRef(null);
  const chartInstance = React.useRef(null);

  React.useEffect(() => {
    if (!chartRef.current || !viz.rows?.length) return;

    if (chartInstance.current) {
      chartInstance.current.dispose();
    }

    const chart = echarts.init(chartRef.current);
    chartInstance.current = chart;

    const options = buildChartOptions(viz);
    if (options) {
      chart.setOption(options);
    }

    const resizeHandler = () => chart.resize();
    window.addEventListener('resize', resizeHandler);

    return () => {
      window.removeEventListener('resize', resizeHandler);
      chart.dispose();
    };
  }, [viz]);

  const viewType = viz.view?.view?.type || viz.view?.type || viz.dataModel?.type || 'table';

  return (
    <div className="bg-white rounded-2xl shadow-sm border border-gray-100 overflow-hidden hover:shadow-md transition-shadow">
      <div className="px-5 py-4 border-b border-gray-50">
        <h3 className="font-semibold text-gray-900">{viz.title}</h3>
        <span className="text-xs text-gray-400 uppercase tracking-wide">{viewType}</span>
      </div>
      <div className="p-5">
        {viz.rows?.length > 0 ? (
          viewType === 'table' ? (
            <TableView data={viz} />
          ) : (
            <div ref={chartRef} className="h-72 w-full" />
          )
        ) : (
          <div className="h-72 flex items-center justify-center text-gray-400">
            No data available
          </div>
        )}
      </div>
      <div className="px-5 py-3 bg-gray-50/50 text-xs text-gray-500">
        {viz.rows?.length || 0} rows
      </div>
    </div>
  );
}

function TableView({ data }) {
  const { rows, columns } = data;
  const cols = columns?.length
    ? columns.map(c => c.field || c.colId || c.headerName)
    : Object.keys(rows[0] || {});

  return (
    <div className="overflow-x-auto max-h-72 rounded-lg border border-gray-100">
      <table className="w-full text-sm">
        <thead className="bg-gray-50 sticky top-0">
          <tr>
            {cols.slice(0, 6).map((col) => (
              <th key={col} className="text-left px-3 py-2 font-medium text-gray-600 border-b border-gray-100">
                {col}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.slice(0, 10).map((row, i) => (
            <tr key={i} className="border-b border-gray-50 hover:bg-gray-50/50">
              {cols.slice(0, 6).map((col) => (
                <td key={col} className="px-3 py-2 text-gray-700">
                  {formatValue(row[col] ?? row[col.toLowerCase()])}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
      {rows.length > 10 && (
        <div className="text-xs text-gray-400 p-2 text-center bg-gray-50">
          Showing 10 of {rows.length} rows
        </div>
      )}
    </div>
  );
}

function formatValue(val) {
  if (val === null || val === undefined) return '-';
  if (typeof val === 'number') return val.toLocaleString();
  return String(val);
}

function buildChartOptions(viz) {
  const { rows, view, dataModel } = viz;
  if (!rows?.length) return null;

  const type = (view?.view?.type || view?.type || dataModel?.type || '').toLowerCase();
  const colors = view?.view?.palette?.colors || ['#3b82f6', '#10b981', '#f59e0b', '#ef4444', '#8b5cf6'];

  const normalizedRows = rows.map(r => {
    const o = {};
    Object.keys(r).forEach(k => o[k.toLowerCase()] = r[k]);
    return o;
  });

  const series = dataModel?.series?.[0] || {};
  const categoryKey = (view?.view?.x || series.key || Object.keys(normalizedRows[0])[0])?.toLowerCase();
  const valueKey = (view?.view?.y || series.value || Object.keys(normalizedRows[0])[1])?.toLowerCase();

  if (!categoryKey) return null;

  const categories = [...new Set(normalizedRows.map(r => String(r[categoryKey] || '')))];
  const values = categories.map(cat => {
    const row = normalizedRows.find(r => String(r[categoryKey]) === cat);
    const v = row ? Number(row[valueKey]) : 0;
    return isNaN(v) ? 0 : v;
  });

  if (type === 'pie_chart' || type === 'pie') {
    return {
      tooltip: { trigger: 'item', formatter: '{b}: {c} ({d}%)' },
      series: [{
        type: 'pie',
        radius: ['45%', '75%'],
        center: ['50%', '50%'],
        data: categories.map((name, i) => ({
          name,
          value: values[i],
          itemStyle: { color: colors[i % colors.length] }
        })),
        label: { show: false },
        emphasis: { label: { show: true, fontSize: 14, fontWeight: 'bold' } }
      }]
    };
  }

  if (type === 'bar_chart' || type === 'bar' || !type || type === 'table') {
    return {
      tooltip: { trigger: 'axis', axisPointer: { type: 'shadow' } },
      grid: { left: 50, right: 20, bottom: 50, top: 20, containLabel: true },
      xAxis: {
        type: 'category',
        data: categories,
        axisLabel: { rotate: categories.length > 6 ? 45 : 0, fontSize: 11, color: '#6b7280' },
        axisLine: { lineStyle: { color: '#e5e7eb' } }
      },
      yAxis: {
        type: 'value',
        axisLine: { show: false },
        splitLine: { lineStyle: { color: '#f3f4f6' } }
      },
      series: [{
        type: 'bar',
        data: values,
        itemStyle: {
          color: new echarts.graphic.LinearGradient(0, 0, 0, 1, [
            { offset: 0, color: colors[0] },
            { offset: 1, color: colors[0] + '80' }
          ]),
          borderRadius: [6, 6, 0, 0]
        },
        barMaxWidth: 50
      }]
    };
  }

  if (type === 'line_chart' || type === 'line' || type === 'area_chart' || type === 'area') {
    const isArea = type.includes('area');
    return {
      tooltip: { trigger: 'axis' },
      grid: { left: 50, right: 20, bottom: 50, top: 20, containLabel: true },
      xAxis: {
        type: 'category',
        data: categories,
        axisLabel: { rotate: categories.length > 6 ? 45 : 0, fontSize: 11, color: '#6b7280' },
        axisLine: { lineStyle: { color: '#e5e7eb' } }
      },
      yAxis: {
        type: 'value',
        axisLine: { show: false },
        splitLine: { lineStyle: { color: '#f3f4f6' } }
      },
      series: [{
        type: 'line',
        data: values,
        smooth: true,
        symbol: 'circle',
        symbolSize: 6,
        itemStyle: { color: colors[0] },
        lineStyle: { width: 3 },
        areaStyle: isArea ? {
          color: new echarts.graphic.LinearGradient(0, 0, 0, 1, [
            { offset: 0, color: colors[0] + '40' },
            { offset: 1, color: colors[0] + '05' }
          ])
        } : undefined
      }]
    };
  }

  return null;
}

const root = ReactDOM.createRoot(document.getElementById('root'));
root.render(<App />);
${SC}
`;
});

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

    // Use saved artifact code or sample code
    const artifactCode = artifact.value?.content?.code || sampleArtifactCode.value;
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
