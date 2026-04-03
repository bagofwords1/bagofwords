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

            <!-- Right: Fork + Edit Report + Close (absolute) -->
            <div class="absolute right-4 top-1/2 -translate-y-1/2 flex items-center gap-2">
                <!-- Fork button -->
                <button
                    v-if="forkEligibility?.can_fork"
                    @click="handleFork"
                    :disabled="isForking"
                    class="flex items-center gap-1 text-xs text-gray-400 hover:text-gray-600 transition-colors disabled:opacity-50"
                >
                    <Icon name="heroicons:arrow-path-rounded-square" class="w-3.5 h-3.5" />
                    <span>{{ isForking ? 'Forking...' : 'Fork' }}</span>
                </button>
                <span
                    v-else-if="forkEligibility && !forkEligibility.can_fork"
                    class="text-[10px] text-gray-300 cursor-default"
                    :title="forkReasonLabel"
                >
                    <Icon name="heroicons:arrow-path-rounded-square" class="w-3.5 h-3.5 inline" />
                </span>
                <a
                    v-if="isOwner"
                    :href="`/reports/${report_id}`"
                    class="flex items-center gap-1 text-xs text-gray-400 hover:text-gray-600 transition-colors"
                >
                    <Icon name="heroicons:pencil-square" class="w-3.5 h-3.5" />
                    <span>Edit Report</span>
                </a>
                <button
                    @click="showTopBar = false"
                    class="p-1 text-gray-400 hover:text-gray-600 hover:bg-gray-100 rounded transition-colors"
                >
                    <Icon name="heroicons:x-mark" class="w-4 h-4" />
                </button>
            </div>
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
                <!-- Slides with Preview Images - Use SlideViewer -->
                <SlideViewer
                    v-if="hasSlidesWithPreviews && artifact"
                    :artifact-id="artifact.id"
                    class="absolute inset-0"
                />

                <!-- Artifact Content - Full screen (modern reports with artifacts) -->
                <iframe
                    v-else-if="hasArtifacts && iframeSrcdoc && !hasSlidesWithPreviews"
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
import SlideViewer from '~/components/dashboard/SlideViewer.vue';

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
const visualizationsData = ref<any[]>([]);
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
const lastRefreshedAt = ref<Date | null>(null);

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

// Check if we have slides mode with preview images (use SlideViewer instead of iframe)
const hasSlidesWithPreviews = computed(() => {
    if (!artifact.value) return false;
    if (artifact.value.mode !== 'slides') return false;
    const previewImages = artifact.value.content?.preview_images;
    return Array.isArray(previewImages) && previewImages.length > 0;
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
  <script src="/libs/tailwindcss-3.4.16.js">${SC}
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
  <script src="/libs/tailwindcss-3.4.16.js">${SC}
  <script crossorigin src="/libs/react-18.production.min.js">${SC}
  <script crossorigin src="/libs/react-dom-18.production.min.js">${SC}
  <script src="/libs/babel-standalone.min.js">${SC}
  <script src="/libs/echarts-5.min.js">${SC}
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
    // ── Global filter store (shared state for useFilters hook) ──
    window.__filterStore = (function() {
      var filters = {};
      var listeners = [];
      return {
        get: function() { return filters; },
        set: function(field, value) {
          var next = {};
          for (var k in filters) next[k] = filters[k];
          if (value == null || value === '') delete next[field];
          else next[field] = value;
          filters = next;
          for (var i = 0; i < listeners.length; i++) listeners[i]();
        },
        reset: function() {
          filters = {};
          for (var i = 0; i < listeners.length; i++) listeners[i]();
        },
        sub: function(fn) {
          listeners.push(fn);
          return function() {
            var idx = listeners.indexOf(fn);
            if (idx >= 0) listeners.splice(idx, 1);
          };
        }
      };
    })();
    // ── useFilters() hook — cross-visualization filtering ──
    window.useFilters = function() {
      var _s = React.useState(0);
      var forceUpdate = _s[1];
      React.useEffect(function() {
        return window.__filterStore.sub(function() {
          forceUpdate(function(c) { return c + 1; });
        });
      }, []);
      var filters = window.__filterStore.get();
      var filterRows = React.useCallback(function(rows, fieldMap) {
        var entries = Object.entries(filters);
        if (!entries.length) return rows;
        return rows.filter(function(row) {
          for (var i = 0; i < entries.length; i++) {
            var key = entries[i][0], val = entries[i][1];
            var col = (fieldMap && fieldMap[key]) ? fieldMap[key] : key;
            if (!Object.prototype.hasOwnProperty.call(row, col)) continue;
            var rv = row[col];
            if (val && typeof val === 'object' && !Array.isArray(val) && (val.from || val.to)) {
              var s = String(rv);
              if (val.from && s < val.from) return false;
              if (val.to && s > val.to) return false;
            } else if (Array.isArray(val)) {
              if (val.length > 0 && val.indexOf(String(rv)) === -1) return false;
            } else {
              if (val && String(rv).toLowerCase().indexOf(String(val).toLowerCase()) === -1) return false;
            }
          }
          return true;
        });
      }, [filters]);
      return {
        filters: filters,
        setFilter: window.__filterStore.set,
        resetFilters: window.__filterStore.reset,
        filterRows: filterRows
      };
    };
    // Global fmt() number formatter
    window.fmt = function(n, opts) {
      if (n == null) return '\\u2014';
      if (typeof n !== 'number') return String(n);
      opts = opts || {};
      if (opts.currency) return new Intl.NumberFormat('en-US', { style: 'currency', currency: opts.currency === true ? 'USD' : opts.currency, maximumFractionDigits: opts.decimals != null ? opts.decimals : 0 }).format(n);
      if (opts.pct) return n.toFixed(1) + '%';
      if (Math.abs(n) >= 1e9) return (n/1e9).toFixed(1) + 'B';
      if (Math.abs(n) >= 1e6) return (n/1e6).toFixed(1) + 'M';
      if (Math.abs(n) >= 1e3) return (n/1e3).toFixed(1) + 'K';
      return n.toLocaleString(undefined, { maximumFractionDigits: 2 });
    };
    // Global CustomTooltip component
    window.CustomTooltip = function(props) {
      if (!props.active || !props.payload || !props.payload.length) return null;
      var h = React.createElement;
      return h('div', { className: 'bg-slate-900 text-white px-4 py-3 rounded-xl shadow-xl border border-slate-700/50 text-sm' }, [
        h('p', { key: 'l', className: 'font-medium text-slate-300 mb-1' }, props.label),
      ].concat(props.payload.map(function(p, i) {
        return h('p', { key: i, className: 'flex items-center gap-2' }, [
          h('span', { key: 'd', className: 'w-2 h-2 rounded-full inline-block', style: { backgroundColor: p.color } }),
          h('span', { key: 'n', className: 'text-slate-400' }, p.name + ': '),
          h('span', { key: 'v', className: 'font-semibold' }, typeof p.value === 'number' ? p.value.toLocaleString() : p.value),
        ]);
      })));
    };
    // Global KPICard component
    window.KPICard = function(props) {
      var h = React.createElement;
      var color = props.color || '#3B82F6';
      var theme = props.className || 'bg-white border-slate-200 text-slate-900';
      var titleCls = props.titleClassName || 'text-slate-500';
      var subtitleCls = props.subtitleClassName || 'text-slate-500';
      return h('div', { className: 'relative rounded-2xl border p-5 shadow-sm overflow-hidden ' + theme }, [
        h('div', { key: 'bar', className: 'absolute inset-x-0 top-0 h-1', style: { background: 'linear-gradient(90deg, ' + color + ', ' + color + '99)' } }),
        h('p', { key: 't', className: 'text-xs font-medium uppercase tracking-wider mb-1 ' + titleCls }, props.title),
        h('p', { key: 'v', className: 'text-2xl font-semibold' }, props.value),
        props.subtitle ? h('p', { key: 's', className: 'text-sm mt-1 ' + subtitleCls }, props.subtitle) : null,
      ]);
    };
    // Global SectionCard wrapper
    window.SectionCard = function(props) {
      var h = React.createElement;
      var theme = props.className || 'bg-white border-slate-200';
      var titleCls = props.titleClassName || 'text-slate-800';
      var subtitleCls = props.subtitleClassName || 'text-slate-500';
      return h('div', { className: 'rounded-2xl border shadow-sm p-6 ' + theme }, [
        props.title ? h('div', { key: 'hdr', className: 'mb-4' }, [
          h('h2', { key: 't', className: 'text-lg font-semibold ' + titleCls }, props.title),
          props.subtitle ? h('p', { key: 's', className: 'text-sm mt-1 ' + subtitleCls }, props.subtitle) : null,
        ]) : null,
        h('div', { key: 'body' }, props.children),
      ]);
    };
    // Global FilterSelect — multi-select dropdown with checkboxes
    window.FilterSelect = function(props) {
      var h = React.createElement;
      var label = props.label || '';
      var rawOpts = props.options || [];
      var opts = rawOpts.map(function(o) { return typeof o === 'object' && o !== null ? { val: o.value, lbl: o.label || String(o.value) } : { val: o, lbl: String(o) }; });
      var selected = props.selected || [];
      var onChange = props.onChange || function(){};
      var theme = props.className || 'bg-white border-slate-200 text-slate-900';
      var searchable = props.searchable !== undefined ? props.searchable : opts.length >= 8;
      var _s = React.useState(false), open = _s[0], setOpen = _s[1];
      var _q = React.useState(''), query = _q[0], setQuery = _q[1];
      var ref = React.useRef(null);
      var searchRef = React.useRef(null);
      React.useEffect(function() {
        function handleClick(e) { if (ref.current && !ref.current.contains(e.target)) setOpen(false); }
        document.addEventListener('mousedown', handleClick);
        return function() { document.removeEventListener('mousedown', handleClick); };
      }, []);
      React.useEffect(function() {
        if (open && searchable && searchRef.current) searchRef.current.focus();
        if (!open) setQuery('');
      }, [open]);
      function toggle(val) {
        var idx = selected.indexOf(val);
        onChange(idx >= 0 ? selected.filter(function(v){ return v !== val; }) : selected.concat([val]));
      }
      var filtered = searchable && query
        ? opts.filter(function(o) { return o.lbl.toLowerCase().indexOf(query.toLowerCase()) !== -1; })
        : opts;
      var selLabels = opts.filter(function(o) { return selected.indexOf(o.val) >= 0; }).map(function(o) { return o.lbl; });
      var display = selected.length === 0 ? 'All' : selLabels.length <= 2 ? selLabels.join(', ') : selected.length + ' selected';
      return h('div', { ref: ref, className: 'relative inline-block min-w-[140px]' }, [
        label ? h('label', { key: 'l', className: 'block text-xs font-medium opacity-60 mb-1' }, label) : null,
        h('button', {
          key: 'btn', type: 'button',
          className: 'w-full flex items-center justify-between gap-2 rounded-lg border px-3 py-1.5 text-sm cursor-pointer ' + theme,
          onClick: function() { setOpen(!open); }
        }, [
          h('span', { key: 't', className: 'truncate' }, display),
          h('svg', { key: 'i', width: 12, height: 12, viewBox: '0 0 12 12', className: 'opacity-50 shrink-0' },
            h('path', { d: 'M3 5l3 3 3-3', stroke: 'currentColor', strokeWidth: 1.5, fill: 'none' }))
        ]),
        open ? h('div', {
          key: 'dd',
          className: 'absolute z-50 mt-1 left-0 right-0 rounded-lg border shadow-lg max-h-72 overflow-auto py-1 ' + theme,
          style: { backgroundColor: '#fff' }
        }, [
          searchable ? h('div', { key: 'search', className: 'px-2 pt-1 pb-1 sticky top-0', style: { backgroundColor: '#fff' } }, [
            h('input', {
              ref: searchRef, type: 'text', value: query,
              placeholder: 'Search...',
              onChange: function(e) { setQuery(e.target.value); },
              className: 'w-full rounded border px-2 py-1 text-sm outline-none focus:border-blue-400 ' + theme,
              onClick: function(e) { e.stopPropagation(); }
            })
          ]) : null,
          selected.length > 0 ? h('button', {
            key: 'clr', type: 'button',
            className: 'w-full text-left px-3 py-1.5 text-xs font-medium opacity-50 hover:opacity-100',
            onClick: function() { onChange([]); }
          }, 'Clear all') : null
        ].concat(filtered.map(function(o) {
          var isSelected = selected.indexOf(o.val) >= 0;
          return h('label', {
            key: o.val,
            className: 'flex items-center gap-2 px-3 py-1.5 text-sm cursor-pointer hover:bg-black/5'
          }, [
            h('input', {
              key: 'cb', type: 'checkbox', checked: isSelected,
              onChange: function() { toggle(o.val); },
              className: 'rounded border-slate-300 accent-blue-500'
            }),
            h('span', { key: 'v', className: 'truncate' }, o.lbl)
          ]);
        }))) : null
      ]);
    };
    // Global FilterSearch — text search input
    window.FilterSearch = function(props) {
      var h = React.createElement;
      var label = props.label || '';
      var value = props.value || '';
      var onChange = props.onChange || function(){};
      var placeholder = props.placeholder || 'Search...';
      var theme = props.className || 'bg-white border-slate-200 text-slate-900';
      return h('div', { className: 'inline-block min-w-[140px]' }, [
        label ? h('label', { key: 'l', className: 'block text-xs font-medium opacity-60 mb-1' }, label) : null,
        h('input', {
          key: 'inp',
          type: 'text',
          value: value,
          placeholder: placeholder,
          onChange: onChange,
          className: 'w-full rounded-lg border px-3 py-1.5 text-sm ' + theme
        })
      ]);
    };
    // Global FilterDateRange — two date inputs for date/time column filtering
    window.FilterDateRange = function(props) {
      var h = React.createElement;
      var label = props.label || '';
      var value = props.value || {};
      var onChange = props.onChange || function(){};
      var theme = props.className || 'bg-white border-slate-200 text-slate-900';
      var inputType = props.type || 'date';
      return h('div', { className: 'inline-block min-w-[200px]' }, [
        label ? h('label', { key: 'l', className: 'block text-xs font-medium opacity-60 mb-1' }, label) : null,
        h('div', { key: 'row', className: 'flex items-center gap-2' }, [
          h('input', {
            key: 'from', type: inputType, value: value.from || '',
            onChange: function(e) { onChange({ from: e.target.value || null, to: value.to || null }); },
            className: 'w-full rounded-lg border px-2 py-1.5 text-sm ' + theme
          }),
          h('span', { key: 'sep', className: 'text-xs opacity-50' }, '\\u2013'),
          h('input', {
            key: 'to', type: inputType, value: value.to || '',
            onChange: function(e) { onChange({ from: value.from || null, to: e.target.value || null }); },
            className: 'w-full rounded-lg border px-2 py-1.5 text-sm ' + theme
          })
        ])
      ]);
    };
    // Expose React hooks as globals
    window.useState = React.useState;
    window.useEffect = React.useEffect;
    window.useRef = React.useRef;
    window.useMemo = React.useMemo;
    window.useCallback = React.useCallback;

    // Register 'bow' ECharts theme
    echarts.registerTheme('bow', {
      color: ['#3B82F6', '#10B981', '#8B5CF6', '#F59E0B', '#EF4444', '#06B6D4', '#EC4899', '#14B8A6', '#60A5FA', '#34D399'],
      backgroundColor: 'transparent',
      categoryAxis: {
        axisLine: { show: false },
        axisTick: { show: false },
        axisLabel: { color: '#64748b', fontSize: 12 },
        splitLine: { show: false }
      },
      valueAxis: {
        axisLine: { show: false },
        axisTick: { show: false },
        axisLabel: { color: '#64748b', fontSize: 12 },
        splitLine: { lineStyle: { color: '#f1f5f9' } }
      },
      line: { smooth: true, symbol: 'none', lineStyle: { width: 2 } },
      bar: { itemStyle: { borderRadius: [6, 6, 0, 0] } },
      pie: { itemStyle: { borderRadius: 6 } },
      grid: { left: 40, right: 20, top: 20, bottom: 40, containLabel: true },
      tooltip: {
        backgroundColor: 'rgba(15, 23, 42, 0.95)',
        borderColor: 'rgba(51, 65, 85, 0.5)',
        borderWidth: 1,
        borderRadius: 12,
        padding: [12, 16],
        textStyle: { color: '#fff', fontSize: 13 },
        trigger: 'axis'
      }
    });

    // Wrap tooltip formatters with try/catch
    function safeOption(opt) {
      if (opt && opt.tooltip && typeof opt.tooltip.formatter === 'function') {
        var orig = opt.tooltip.formatter;
        opt.tooltip.formatter = function() { try { return orig.apply(this, arguments); } catch(e) { return ''; } };
      }
      return opt;
    }

    // Global EChart React wrapper
    window.EChart = function(props) {
      var ref = React.useRef(null);
      var chartRef = React.useRef(null);
      var h = props.height || 400;
      React.useEffect(function() {
        if (!ref.current) return;
        var chart = echarts.init(ref.current, 'bow');
        chartRef.current = chart;
        if (props.option) chart.setOption(safeOption(props.option));
        var ro = new ResizeObserver(function() { chart.resize(); });
        ro.observe(ref.current);
        return function() { ro.disconnect(); chart.dispose(); };
      }, []);
      React.useEffect(function() {
        if (chartRef.current && props.option) {
          chartRef.current.setOption(safeOption(props.option), true);
        }
      }, [props.option]);
      return React.createElement('div', {
        ref: ref,
        style: { width: '100%', height: h },
        className: props.className || ''
      });
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
