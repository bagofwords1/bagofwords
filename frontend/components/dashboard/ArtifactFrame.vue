<template>
  <div class="h-full w-full flex flex-col bg-gray-50">
    <!-- Header / Toolbar -->
    <div class="flex-shrink-0 flex items-center justify-between px-4 py-2 bg-white border-b">
      <div class="flex items-center gap-3">
        <UTooltip text="Back to chat">
          <button @click="$emit('close')" class="hover:bg-gray-100 p-1 rounded">
            <Icon name="heroicons:x-mark" class="w-4 h-4 text-gray-500" />
          </button>
        </UTooltip>

        <!-- Artifact Selector Dropdown -->
        <div class="flex items-center gap-2">
          <USelectMenu
            v-if="artifactsList.length > 0"
            v-model="selectedArtifactId"
            :options="artifactOptions"
            value-attribute="value"
            option-attribute="label"
            size="xs"
            class="min-w-[200px]"
            placeholder="Select artifact..."
          >
            <template #label>
              <span class="truncate text-xs">{{ selectedArtifactLabel }}</span>
            </template>
          </USelectMenu>
          <span v-else class="text-xs text-gray-400 italic">No artifacts yet</span>

          <!-- Use this version button (shown when non-latest is selected) -->
          <button
            v-if="!isLatestSelected && artifactsList.length > 1"
            @click="useThisVersion"
            :disabled="isDuplicating"
            class="text-xs px-2 py-1 bg-blue-50 text-blue-600 hover:bg-blue-100 rounded border border-blue-200 transition-colors disabled:opacity-50 flex items-center gap-1"
          >
            <Spinner v-if="isDuplicating" class="w-3 h-3" />
            <Icon v-else name="heroicons:arrow-uturn-up" class="w-3 h-3" />
            Use this version
          </button>
        </div>
      </div>

      <div class="flex items-center gap-2">
        <span v-if="isLoading" class="text-xs text-gray-400">Loading...</span>
        <span v-else-if="dataReady" class="text-xs text-green-600 flex items-center gap-1">
          <Icon name="heroicons:check-circle" class="w-3 h-3" />
          Ready
        </span>

        <!-- Refresh Dashboard (rerun + refresh) -->
        <UTooltip text="Refresh Dashboard">
          <button
            @click="refreshDashboard"
            :disabled="isRefreshing"
            class="p-1.5 hover:bg-gray-100 rounded transition-colors disabled:opacity-50"
          >
            <Spinner v-if="isRefreshing" class="w-4 h-4 text-gray-500" />
            <Icon v-else name="heroicons:arrow-path" class="w-4 h-4 text-gray-500" />
          </button>
        </UTooltip>

        <!-- Schedule -->
        <CronModal v-if="report" :report="report" />

        <!-- Export PPTX (slides mode only) -->
        <UTooltip v-if="selectedArtifact?.mode === 'slides'" text="Export as PowerPoint">
          <button
            @click="exportPptx"
            :disabled="isExporting"
            class="text-lg items-center flex gap-1 hover:bg-gray-100 px-2 py-1 rounded disabled:opacity-50"
          >
            <Icon v-if="isExporting" name="heroicons:arrow-path" class="w-4 h-4 text-gray-500 animate-spin" />
            <Icon v-else name="heroicons:arrow-down-tray" class="w-4 h-4 text-purple-600" />
            <span class="text-xs text-purple-600 font-medium">PPTX</span>
          </button>
        </UTooltip>

        <!-- Fullscreen -->
        <UTooltip text="Full screen">
          <button @click="openFullscreen" class="text-lg items-center flex gap-1 hover:bg-gray-100 px-2 py-1 rounded">
            <Icon name="heroicons:arrows-pointing-out" class="w-4 h-4 text-gray-500" />
          </button>
        </UTooltip>

        <!-- Open in new tab (if published) -->
        <UTooltip text="Open in new tab" v-if="report?.status === 'published'">
          <a :href="`/r/${report.id}`" target="_blank" class="text-lg items-center flex gap-1 hover:bg-gray-100 px-2 py-1 rounded">
            <Icon name="heroicons:arrow-top-right-on-square" class="w-4 h-4 text-gray-500" />
          </a>
        </UTooltip>

        <!-- Publish -->
        <PublishModal v-if="report" :report="report" />
      </div>
    </div>

    <!-- Iframe Container -->
    <div class="flex-1 min-h-0 relative">
      <!-- Loading State -->
      <div v-if="isLoading || !selectedArtifact" class="absolute inset-0 flex items-center justify-center bg-white">
        <div class="flex flex-col items-center gap-3">
          <Spinner class="w-6 h-6 text-gray-400" />
          <span class="text-sm text-gray-400">Loading artifact...</span>
        </div>
      </div>

      <!-- Pending Artifact State (generating) -->
      <div v-else-if="isPendingArtifact" class="absolute inset-0 flex items-center justify-center bg-gradient-to-br from-slate-50 to-slate-100">
        <div class="flex flex-col items-center gap-4 text-center px-8">
          <div class="w-16 h-16 bg-blue-100 rounded-2xl flex items-center justify-center">
            <Spinner class="w-8 h-8 text-blue-500" />
          </div>
          <div>
            <h3 class="text-lg font-semibold text-gray-900">{{ selectedArtifact?.title || 'Generating Dashboard' }}</h3>
            <p class="text-sm text-gray-500 mt-1">Creating your visualization...</p>
          </div>
        </div>
      </div>

      <!-- Iframe (hidden while loading or pending) -->
      <iframe
        v-show="!isLoading && selectedArtifact && !isPendingArtifact"
        ref="iframeRef"
        :srcdoc="iframeSrcdoc"
        sandbox="allow-scripts allow-same-origin"
        class="absolute inset-0 w-full h-full border-0 bg-white"
        @load="onIframeLoad"
      />
    </div>

    <!-- Fullscreen Modal -->
    <Teleport to="body">
      <UModal v-model="isFullscreenOpen" :ui="{ width: 'sm:max-w-[98vw]', height: 'h-[98vh]' }">
        <div class="h-full flex flex-col">
          <!-- Modal Header -->
          <div class="p-3 flex justify-between items-center border-b bg-white">
            <div class="flex items-center gap-3">
              <span class="text-sm font-medium text-gray-700">{{ selectedArtifact?.title || reportData?.title || 'Artifact' }}</span>
              <span v-if="selectedArtifact" class="text-xs text-gray-400">v{{ selectedArtifact.version }}</span>
            </div>
            <UButton color="gray" variant="ghost" icon="i-heroicons-x-mark-20-solid" @click="closeFullscreen" />
          </div>

          <!-- Modal Content - Full artifact iframe -->
          <div class="flex-1 min-h-0 relative bg-white">
            <iframe
              v-if="isFullscreenOpen"
              :srcdoc="iframeSrcdoc"
              sandbox="allow-scripts allow-same-origin"
              class="absolute inset-0 w-full h-full border-0"
            />
          </div>
        </div>
      </UModal>
    </Teleport>
  </div>
</template>

<script setup lang="ts">
import { ref, computed, onMounted, onUnmounted, watch } from 'vue';
import { useMyFetch } from '~/composables/useMyFetch';
import CronModal from '../CronModal.vue';
import PublishModal from '../PublishModal.vue';
import Spinner from '../Spinner.vue';

const toast = useToast();
const config = useRuntimeConfig();
const { token } = useAuth();
const { organization } = useOrganization();

interface ArtifactItem {
  id: string;
  title: string;
  version: number;
  created_at: string;
  mode: string;
  status?: string;
}

const props = defineProps<{
  reportId: string;
  report?: any;
  artifactCode?: string;
}>();

defineEmits<{
  (e: 'close'): void;
}>();

// Fullscreen modal state
const isFullscreenOpen = ref(false);

// Export state
const isExporting = ref(false);

// Refresh state
const isRefreshing = ref(false);

// Refresh Dashboard - reruns report queries and refreshes data
async function refreshDashboard() {
  if (isRefreshing.value) return;

  isRefreshing.value = true;
  isLoading.value = true;

  try {
    // Rerun the report (re-execute queries)
    const { error } = await useMyFetch(`/api/reports/${props.reportId}/rerun`, { method: 'POST' });
    if (error.value) throw error.value;

    // Refresh artifact data
    await refreshAll();

    toast.add({ title: 'Dashboard refreshed', color: 'green' });
  } catch (error: any) {
    console.error('Failed to refresh dashboard:', error);
    toast.add({ title: 'Error', description: `Failed to refresh dashboard. ${error.message || ''}`, color: 'red' });
  } finally {
    isRefreshing.value = false;
  }
}

// Open fullscreen modal
function openFullscreen() {
  isFullscreenOpen.value = true;
}

// Close fullscreen modal
function closeFullscreen() {
  isFullscreenOpen.value = false;
}

// Export artifact as PPTX
async function exportPptx() {
  if (!selectedArtifactId.value || isExporting.value) return;

  isExporting.value = true;
  try {
    // Use native fetch for blob download with same auth pattern as useMyFetch
    const headers: Record<string, string> = {
      Authorization: `${token.value}`,
    };
    if (organization.value?.id) {
      headers['X-Organization-Id'] = organization.value.id;
    }

    const response = await fetch(`${config.public.baseURL}/artifacts/${selectedArtifactId.value}/export/pptx`, {
      method: 'GET',
      headers
    });

    if (!response.ok) {
      throw new Error(`HTTP error! status: ${response.status}`);
    }

    const blob = await response.blob();

    // Create download link
    const url = window.URL.createObjectURL(blob);
    const link = document.createElement('a');
    link.href = url;
    link.download = `${selectedArtifact.value?.title || 'presentation'}.pptx`;
    document.body.appendChild(link);
    link.click();
    document.body.removeChild(link);
    window.URL.revokeObjectURL(url);

    toast.add({ title: 'Export complete', description: 'PowerPoint file downloaded successfully.' });
  } catch (error: any) {
    console.error('Failed to export PPTX:', error);
    toast.add({ title: 'Export failed', description: error.message || 'Failed to export PowerPoint file.', color: 'red' });
  } finally {
    isExporting.value = false;
  }
}

const iframeRef = ref<HTMLIFrameElement | null>(null);
const isLoading = ref(true);
const dataReady = ref(false);
const iframeReady = ref(false);
const visualizationsData = ref<any[]>([]);
const reportData = ref<any>(null);

// Artifact selection state
const artifactsList = ref<ArtifactItem[]>([]);
const selectedArtifactId = ref<string | undefined>(undefined);
const selectedArtifact = ref<any>(null);

// Computed options for dropdown
const artifactOptions = computed(() => {
  return artifactsList.value.map(a => ({
    value: a.id,
    label: `${a.title || 'Untitled'} (v${a.version})`,
    artifact: a
  }));
});

const selectedArtifactLabel = computed(() => {
  const selected = artifactsList.value.find(a => a.id === selectedArtifactId.value);
  if (selected) {
    return `${selected.title || 'Untitled'} (v${selected.version})`;
  }
  return 'Select artifact...';
});

// Check if selected artifact is the latest (first in list, sorted by created_at desc)
const isLatestSelected = computed(() => {
  if (!selectedArtifactId.value || artifactsList.value.length === 0) return true;
  return artifactsList.value[0].id === selectedArtifactId.value;
});

// Check if selected artifact is pending (still generating)
const isPendingArtifact = computed(() => {
  return selectedArtifact.value?.status === 'pending';
});

// State for "Use this version" action
const isDuplicating = ref(false);

// Duplicate the selected artifact to make it the latest/default
async function useThisVersion() {
  if (!selectedArtifactId.value || isDuplicating.value) return;

  isDuplicating.value = true;
  try {
    const { data, error } = await useMyFetch(`/api/artifacts/${selectedArtifactId.value}/duplicate`, {
      method: 'POST'
    });

    if (error.value) throw error.value;

    // Refresh the list and select the new artifact
    await fetchArtifactsList();
    if (data.value && (data.value as any).id) {
      selectedArtifactId.value = (data.value as any).id;
    }

    toast.add({ title: 'Version set as default', color: 'green' });
  } catch (error: any) {
    console.error('Failed to set version as default:', error);
    toast.add({ title: 'Error', description: 'Failed to set version as default.', color: 'red' });
  } finally {
    isDuplicating.value = false;
  }
}

// Handle artifact:select event (select a specific artifact by ID)
function handleArtifactSelect(event: Event) {
  const artifactId = (event as CustomEvent).detail?.artifact_id;
  if (artifactId && artifactsList.value.some(a => a.id === artifactId)) {
    selectedArtifactId.value = artifactId;
  }
}

// Handle artifact:created event (refresh list and select new artifact)
async function handleArtifactCreated(event: Event) {
  const artifactId = (event as CustomEvent).detail?.artifact_id;
  await fetchArtifactsList();
  if (artifactId) {
    selectedArtifactId.value = artifactId;
    // Force refetch in case same artifact transitioned from pending to completed
    await fetchSelectedArtifact();
  }
}

// Load artifacts and data on mount
onMounted(async () => {
  window.addEventListener('message', handleIframeMessage);
  window.addEventListener('artifact:select', handleArtifactSelect);
  window.addEventListener('artifact:created', handleArtifactCreated);

  await Promise.all([
    fetchArtifactsList(),
    fetchData()
  ]);
});

// Fetch list of all artifacts for the report
async function fetchArtifactsList() {
  try {
    const { data } = await useMyFetch(`/artifacts/report/${props.reportId}`);
    if (data.value && Array.isArray(data.value)) {
      artifactsList.value = data.value as ArtifactItem[];

      // Auto-select the most recent artifact
      if (artifactsList.value.length > 0) {
        selectedArtifactId.value = artifactsList.value[0].id;
        await fetchSelectedArtifact();
      }
    }
  } catch (e) {
    console.log('[ArtifactFrame] No artifacts found');
  }
}

// Fetch the full artifact content when selection changes
async function fetchSelectedArtifact() {
  if (!selectedArtifactId.value) {
    selectedArtifact.value = null;
    return;
  }

  try {
    const { data } = await useMyFetch(`/api/artifacts/${selectedArtifactId.value}`);
    if (data.value) {
      selectedArtifact.value = data.value;
      console.log('[ArtifactFrame] Loaded artifact:', (data.value as any).title);
    }
  } catch (e) {
    console.error('[ArtifactFrame] Failed to fetch artifact:', e);
  }
}

// Watch for artifact selection changes
watch(selectedArtifactId, async () => {
  await fetchSelectedArtifact();
});

onUnmounted(() => {
  window.removeEventListener('message', handleIframeMessage);
  window.removeEventListener('artifact:select', handleArtifactSelect);
  window.removeEventListener('artifact:created', handleArtifactCreated);
});

// Handle messages from iframe
function handleIframeMessage(event: MessageEvent) {
  if (event.data?.type === 'ARTIFACT_READY') {
    console.log('[ArtifactFrame] Iframe ready');
    iframeReady.value = true;
    sendDataToIframe();
  }
}

// Send data to iframe via postMessage
function sendDataToIframe() {
  if (!iframeRef.value?.contentWindow || !iframeReady.value) return;

  const payload = {
    report: reportData.value,
    visualizations: visualizationsData.value
  };

  iframeRef.value.contentWindow.postMessage({
    type: 'ARTIFACT_DATA',
    payload
  }, '*');

  dataReady.value = true;
  console.log('[ArtifactFrame] Data sent to iframe:', visualizationsData.value.length, 'visualizations');
}

// Fetch visualization data for the report
async function fetchData() {
  isLoading.value = true;
  dataReady.value = false;

  try {
    // Fetch report info
    const { data: reportRes } = await useMyFetch(`/api/reports/${props.reportId}`);
    if (reportRes.value) {
      reportData.value = {
        id: (reportRes.value as any).id,
        title: (reportRes.value as any).title,
        theme: (reportRes.value as any).theme_name || (reportRes.value as any).report_theme_name
      };
    }

    // Fetch queries with visualizations
    const { data: queriesRes } = await useMyFetch(`/api/queries?report_id=${props.reportId}`);
    const queries = Array.isArray(queriesRes.value) ? queriesRes.value : [];

    // Build visualization data array
    const vizData: any[] = [];

    for (const query of queries) {
      // Fetch default step for this query
      const { data: stepRes } = await useMyFetch(`/api/queries/${query.id}/default_step`);
      const step = (stepRes.value as any)?.step;

      // Process each visualization in the query
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
    console.log('[ArtifactFrame] Fetched', vizData.length, 'visualizations');

  } catch (e) {
    console.error('[ArtifactFrame] Failed to fetch data:', e);
  } finally {
    isLoading.value = false;
    if (iframeReady.value) {
      sendDataToIframe();
    }
  }
}

// Refresh everything
async function refreshAll() {
  await Promise.all([
    fetchArtifactsList(),
    fetchData()
  ]);
}

// Called when iframe loads
function onIframeLoad() {
  // Iframe loaded, but we wait for ARTIFACT_READY message
}

// Sample React code for when no artifact exists
const sampleArtifactCode = computed(() => {
  const SC = '</' + 'script>';
  return `
<script type="text/babel">
// Default Artifact - Create one with the agent!
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
      {/* Header */}
      <div className="mb-8">
        <h1 className="text-3xl font-bold text-gray-900 tracking-tight">
          {report?.title || 'Dashboard'}
        </h1>
        <p className="text-sm text-gray-500 mt-2">
          {visualizations.length} visualization{visualizations.length !== 1 ? 's' : ''} available
        </p>
      </div>

      {/* Empty state */}
      {visualizations.length === 0 ? (
        <div className="flex flex-col items-center justify-center py-16 text-center">
          <div className="w-16 h-16 bg-gray-100 rounded-full flex items-center justify-center mb-4">
            <svg className="w-8 h-8 text-gray-400" fill="none" viewBox="0 0 24 24" stroke="currentColor">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={1.5} d="M9 17v-2m3 2v-4m3 4v-6m2 10H7a2 2 0 01-2-2V5a2 2 0 012-2h5.586a1 1 0 01.707.293l5.414 5.414a1 1 0 01.293.707V19a2 2 0 01-2 2z" />
            </svg>
          </div>
          <h3 className="text-lg font-medium text-gray-900 mb-1">No visualizations yet</h3>
          <p className="text-sm text-gray-500 max-w-sm">
            Ask the agent to create visualizations, then generate an artifact to see them here.
          </p>
        </div>
      ) : (
        /* Grid of visualizations */
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

// Build the full iframe srcdoc with embedded data
const iframeSrcdoc = computed(() => {
  const embeddedData = JSON.stringify({
    report: reportData.value,
    visualizations: visualizationsData.value
  });

  // Priority: props > selected artifact from DB > sample code
  const artifactCode = props.artifactCode
    || selectedArtifact.value?.content?.code
    || sampleArtifactCode.value;

  const artifactMode = selectedArtifact.value?.mode || 'page';
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
    console.log('[Slides] Data loaded:', window.ARTIFACT_DATA?.visualizations?.length || 0, 'visualizations');
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
  <div id="root"><div style="display:flex;align-items:center;justify-content:center;height:100%;color:#9ca3af;">Loading artifact...</div></div>

  <script>
    window.ARTIFACT_DATA = ${embeddedData};
    window.useArtifactData = function() {
      return window.ARTIFACT_DATA;
    };
    console.log('[Artifact] Data loaded:', window.ARTIFACT_DATA?.visualizations?.length || 0, 'visualizations');
  ${SC}

  ${artifactCode}
</body>
</html>`;
});

// Re-send data when it changes
watch([visualizationsData, iframeReady], () => {
  if (iframeReady.value && visualizationsData.value.length > 0) {
    sendDataToIframe();
  }
});
</script>
