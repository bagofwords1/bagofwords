<template>
    <!-- Inspect: a DevTools-style pane docked under the app. What the app did
         while its editors (and the agent's own preview) used it, from the
         activity log (services/artifact_activity.py), plus this page's own
         events as they happen. Editors only; the same log the agent reads. -->
    <!-- Collapsed: one thin strip. A name, the counts, a pulse. -->
    <button v-if="!expanded" type="button" @click="setExpanded(true)" data-testid="artifact-inspect-strip"
            :aria-label="$t('artifactActivity.expand')"
            class="flex-shrink-0 flex items-center gap-3 w-full h-6 px-3 border-t border-line bg-surface text-2xs text-ink-muted hover:bg-surface-subtle transition-colors">
        <span class="flex items-center gap-1.5">
            <Icon name="heroicons:chevron-up" class="w-3 h-3" />
            <span class="font-medium text-ink-2">{{ $t('artifactActivity.inspect') }}</span>
        </span>
        <span class="nums">{{ $t('artifactActivity.requestsCount', { n: counts.requests }) }}</span>
        <span v-if="counts.errors" class="nums font-medium text-red-700 dark:text-red-400" data-testid="artifact-inspect-strip-errors">
            {{ $t('artifactActivity.errorsCount', { n: counts.errors }) }}
        </span>
        <span v-if="last" class="min-w-0 truncate font-mono text-ink-subtle ms-1" dir="auto">{{ label(last) }}</span>
        <span class="ms-auto w-1.5 h-1.5 rounded-full shrink-0" :class="live ? 'bg-emerald-500' : 'bg-gray-300 dark:bg-gray-600'" />
    </button>

    <div v-else class="flex-shrink-0 flex flex-col border-t border-line bg-surface select-none" :style="{ height: `${height}px` }"
         data-testid="artifact-inspect">
        <!-- Drag handle -->
        <div class="h-1 cursor-row-resize hover:bg-indigo-300 dark:hover:bg-indigo-700 transition-colors" @mousedown.prevent="startResize" />

        <!-- Toolbar -->
        <div class="flex items-center gap-2 px-3 h-9 border-b border-line text-xs">
            <span class="font-semibold text-ink">{{ $t('artifactActivity.title') }}</span>
            <span class="flex items-center gap-1 text-2xs text-ink-muted" :title="$t('artifactActivity.liveHint')">
                <span class="w-1.5 h-1.5 rounded-full" :class="live ? 'bg-emerald-500' : 'bg-gray-300 dark:bg-gray-600'" />
                {{ $t('artifactActivity.live') }}
            </span>
            <span class="w-px h-4 bg-line mx-1" />
            <div class="flex items-center gap-0.5" role="tablist">
                <UButton v-for="f in filters" :key="f.value" size="2xs" :variant="filter === f.value ? 'soft' : 'ghost'"
                         :color="filter === f.value ? (f.value === 'errors' ? 'red' : 'indigo') : 'gray'"
                         :data-testid="`artifact-inspect-filter-${f.value}`" @click="filter = f.value">
                    {{ f.label }}<span v-if="f.count" class="nums ms-1 text-2xs opacity-70">{{ f.count }}</span>
                </UButton>
            </div>
            <span class="w-px h-4 bg-line mx-1" />
            <USelectMenu v-model="source" :options="sourceOptions" value-attribute="value" option-attribute="label"
                         size="2xs" class="min-w-[6.5rem]" data-testid="artifact-inspect-source" />
            <UInput v-model="query" size="2xs" :placeholder="$t('artifactActivity.filterPlaceholder')" class="w-40"
                    icon="i-heroicons-magnifying-glass" data-testid="artifact-inspect-query" />
            <span class="ms-auto text-2xs text-ink-muted nums whitespace-nowrap">{{ $t('artifactActivity.shown', { shown: shown.length, total: rows.length }) }}</span>
            <UButton size="2xs" color="gray" variant="ghost" icon="i-heroicons-arrow-path" :loading="loading"
                     :aria-label="$t('artifactActivity.refresh')" data-testid="artifact-inspect-refresh" @click="load" />
            <UButton size="2xs" color="gray" variant="ghost" icon="i-heroicons-chevron-down" :aria-label="$t('artifactActivity.collapse')"
                     data-testid="artifact-inspect-close" @click="setExpanded(false)" />
        </div>

        <!-- Rows -->
        <div ref="listRef" class="flex-1 min-h-0 overflow-auto select-text">
            <div v-if="loadError" class="px-4 py-6 text-center text-xs text-red-600 dark:text-red-400" data-testid="artifact-inspect-error">{{ loadError }}</div>
            <div v-else-if="!loading && shown.length === 0" class="px-4 py-8 text-center" data-testid="artifact-inspect-empty">
                <p class="text-xs text-ink-2">{{ rows.length ? $t('artifactActivity.emptyFiltered') : $t('artifactActivity.empty') }}</p>
                <p v-if="!rows.length" class="text-2xs text-ink-muted mt-1">{{ $t('artifactActivity.emptyHint') }}</p>
            </div>
            <table v-else class="w-full text-2xs border-collapse">
                <thead class="sticky top-0 z-10 bg-surface-subtle text-ink-muted">
                    <tr class="text-start">
                        <th class="th w-24">{{ $t('artifactActivity.columns.time') }}</th>
                        <th class="th w-16">{{ $t('artifactActivity.columns.source') }}</th>
                        <th class="th w-16">{{ $t('artifactActivity.columns.kind') }}</th>
                        <th class="th">{{ $t('artifactActivity.columns.event') }}</th>
                        <th class="th w-24">{{ $t('artifactActivity.columns.status') }}</th>
                        <th class="th w-32">{{ $t('artifactActivity.columns.duration') }}</th>
                        <th class="th w-10">{{ $t('artifactActivity.columns.version') }}</th>
                    </tr>
                </thead>
                <tbody>
                    <template v-for="e in shown" :key="e.id">
                        <tr class="border-t border-line/70 cursor-pointer hover:bg-surface-subtle" :class="rowClass(e)"
                            data-testid="artifact-inspect-row" :data-failed="e.failed ? 'true' : 'false'" :data-kind="e.kind"
                            @click="toggle(e.id)">
                            <td class="td nums text-ink-muted whitespace-nowrap" :title="e.created_at || ''">{{ when(e.created_at) }}</td>
                            <td class="td">
                                <span class="font-medium" :class="e.source === 'preview' ? 'text-violet-700 dark:text-violet-300' : 'text-ink-muted'">
                                    {{ e.source === 'preview' ? $t('artifactActivity.sources.preview') : $t('artifactActivity.sources.editor') }}
                                </span>
                            </td>
                            <td class="td"><span class="inline-block px-1.5 py-px rounded-sm font-medium" :class="kindClass(e.kind)">{{ e.kind }}</span></td>
                            <td class="td min-w-0">
                                <span class="font-mono text-ink" dir="auto">{{ label(e) }}</span>
                                <span v-if="sub(e)" class="text-ink-muted ms-2" dir="auto">{{ sub(e) }}</span>
                            </td>
                            <td class="td whitespace-nowrap">
                                <span class="inline-flex items-center gap-1 font-medium" :class="statusClass(e)">
                                    <span class="w-1.5 h-1.5 rounded-full" :class="statusDot(e)" />
                                    {{ statusText(e) }}
                                </span>
                            </td>
                            <td class="td">
                                <div v-if="e.duration_ms != null" class="flex items-center gap-1.5">
                                    <span class="nums w-12 text-end" :class="e.duration_ms >= 2000 ? 'text-amber-700 dark:text-amber-400' : 'text-ink-2'">{{ fmtMs(e.duration_ms) }}</span>
                                    <span class="flex-1 h-1 rounded-full bg-surface-muted overflow-hidden">
                                        <span class="block h-full rounded-full" :class="e.failed ? 'bg-red-400' : (e.duration_ms >= 2000 ? 'bg-amber-400' : 'bg-sky-400')"
                                              :style="{ width: `${Math.max(3, Math.round(100 * e.duration_ms / maxMs))}%` }" />
                                    </span>
                                </div>
                                <span v-else class="text-ink-subtle">—</span>
                            </td>
                            <td class="td nums text-ink-muted">{{ e.version != null ? `v${e.version}` : '' }}</td>
                        </tr>
                        <tr v-if="expandedRow === e.id" class="bg-surface-subtle/60" data-testid="artifact-inspect-detail">
                            <td colspan="7" class="px-4 py-2">
                                <div class="flex flex-wrap gap-x-6 gap-y-1 text-2xs text-ink-muted mb-1.5">
                                    <span v-if="e.op"><span class="text-ink-subtle">op</span> <span class="font-mono text-ink">{{ e.op }}</span></span>
                                    <span v-if="e.error_code"><span class="text-ink-subtle">error_code</span> <span class="font-mono text-red-700 dark:text-red-400">{{ e.error_code }}</span></span>
                                    <span v-if="e.session_id"><span class="text-ink-subtle">session</span> <span class="font-mono">{{ e.session_id.slice(0, 8) }}</span></span>
                                    <span v-if="e.created_at"><span class="text-ink-subtle">at</span> <span class="font-mono">{{ e.created_at }}</span></span>
                                </div>
                                <pre v-if="e.detail && Object.keys(e.detail).length" class="font-mono text-2xs text-ink-2 whitespace-pre-wrap break-words max-h-40 overflow-auto" dir="ltr">{{ JSON.stringify(e.detail, null, 2) }}</pre>
                                <p v-else class="text-2xs text-ink-subtle">{{ $t('artifactActivity.noDetail') }}</p>
                            </td>
                        </tr>
                    </template>
                </tbody>
            </table>
        </div>
    </div>
</template>

<script setup lang="ts">
import type { ActivityEvent } from '~/composables/useArtifactRuntime';

const props = defineProps<{ artifactId: string, reportId: string, version?: number | null }>();

// Collapsed by default: a strip under the app. Expanded sticks per browser.
const EXPANDED_KEY = 'bow.inspect.expanded';
const expanded = ref(false);
try { expanded.value = localStorage.getItem(EXPANDED_KEY) === '1'; } catch { /* no storage: start collapsed */ }
function setExpanded(open: boolean) {
    expanded.value = open;
    try { localStorage.setItem(EXPANDED_KEY, open ? '1' : '0'); } catch { /* per-browser convenience only */ }
    if (open) load();
}

const { t } = useI18n();
const { getErrorMessage } = useErrorMessage();

type Row = {
    id: string, created_at: string | null, kind: string, op: string | null, source: string, version: number | null,
    status: string | null, error_code: string | null, duration_ms: number | null, failed: boolean,
    detail: Record<string, any> | null, session_id?: string | null,
};

const loading = ref(false);
const loadError = ref<string | null>(null);
const fetched = ref<Row[]>([]);
const liveRows = ref<Row[]>([]);
const live = ref(false);
const filter = ref<'all' | 'request' | 'stream' | 'filter' | 'click' | 'errors'>('all');
const source = ref<'all' | 'editor' | 'preview'>('all');
const query = ref('');
const expandedRow = ref<string | null>(null);
const listRef = ref<HTMLElement | null>(null);
const height = ref(260);
let liveSeq = 0;
let poll: ReturnType<typeof setInterval> | null = null;

const sourceOptions = computed(() => [
    { value: 'all', label: t('artifactActivity.sources.all') },
    { value: 'editor', label: t('artifactActivity.sources.editor') },
    { value: 'preview', label: t('artifactActivity.sources.preview') },
]);

// Newest first; what this page did since the last fetch sits on top (the
// server has it too once the broker flushed, so a refresh replaces it).
const rows = computed<Row[]>(() => [...liveRows.value].reverse().concat(fetched.value));

const shown = computed(() => {
    const q = query.value.trim().toLowerCase();
    return rows.value.filter((e) => {
        if (filter.value === 'errors' ? !e.failed : (filter.value !== 'all' && e.kind !== filter.value)) return false;
        if (source.value !== 'all' && e.source !== source.value) return false;
        if (q && !`${e.op || ''} ${label(e)} ${sub(e)} ${e.error_code || ''} ${e.status || ''}`.toLowerCase().includes(q)) return false;
        return true;
    });
});

const filters = computed(() => {
    const count = (p: (e: Row) => boolean) => rows.value.filter(p).length;
    return [
        { value: 'all' as const, label: t('artifactActivity.filters.all'), count: 0 },
        { value: 'request' as const, label: t('artifactActivity.filters.requests'), count: count(e => e.kind === 'request') },
        { value: 'stream' as const, label: t('artifactActivity.filters.streams'), count: count(e => e.kind === 'stream') },
        { value: 'filter' as const, label: t('artifactActivity.filters.filters'), count: count(e => e.kind === 'filter') },
        { value: 'click' as const, label: t('artifactActivity.filters.clicks'), count: count(e => e.kind === 'click') },
        { value: 'errors' as const, label: t('artifactActivity.filters.errors'), count: count(e => e.failed) },
    ];
});

const maxMs = computed(() => Math.max(1, ...shown.value.map(e => e.duration_ms || 0)));
const counts = computed(() => ({
    requests: rows.value.filter(e => e.kind === 'request' || e.kind === 'stream').length,
    errors: rows.value.filter(e => e.failed).length,
}));
const last = computed<Row | null>(() => rows.value[0] || null);

function isFailed(e: { kind: string, status?: string | null }) {
    return e.kind === 'error' || e.kind === 'console' || e.status === 'error' || e.status === 'interrupted';
}

function label(e: Row): string {
    const d = e.detail || {};
    if (e.kind === 'click') return `${d.target || 'element'}`;
    if (e.kind === 'filter') return JSON.stringify(d.params || {}).slice(0, 160);
    if (e.kind === 'error' || e.kind === 'console') return String(d.message || e.error_code || 'unknown').slice(0, 300);
    return e.op || e.kind;
}

function sub(e: Row): string {
    const d = e.detail || {};
    if (e.kind === 'request') {
        const parts = [d.resource, d.action].filter(Boolean);
        if (Array.isArray(d.agents) && d.agents.length) parts.push(d.agents.join(', '));
        if (d.rows != null) parts.push(t('artifactActivity.rows', { n: d.rows }));
        if (e.status === 'error' && d.message) parts.push(String(d.message));
        return parts.join(' · ');
    }
    if (e.kind === 'stream') return d.message ? String(d.message) : '';
    if (e.kind === 'filter') {
        const parts = [];
        if (d.queries != null) parts.push(t('artifactActivity.queries', { n: d.queries }));
        if (d.rows != null) parts.push(t('artifactActivity.rows', { n: d.rows }));
        if (e.status === 'error' && d.message) parts.push(String(d.message));
        return parts.join(', ');
    }
    if (e.kind === 'error' && d.where) return String(d.where);
    return '';
}

function statusText(e: Row) {
    if (e.kind === 'click') return '';
    if (e.kind === 'error' || e.kind === 'console') return e.kind;
    return e.error_code && e.status === 'error' ? e.error_code : (e.status || '');
}
function statusClass(e: Row) {
    if (e.failed) return 'text-red-700 dark:text-red-400';
    if (['aborted', 'cancelled'].includes(e.status || '')) return 'text-amber-700 dark:text-amber-400';
    if (['ok', 'completed'].includes(e.status || '')) return 'text-emerald-700 dark:text-emerald-400';
    return 'text-ink-muted';
}
function statusDot(e: Row) {
    if (e.kind === 'click') return 'bg-transparent';
    if (e.failed) return 'bg-red-500';
    if (['aborted', 'cancelled'].includes(e.status || '')) return 'bg-amber-500';
    if (['ok', 'completed'].includes(e.status || '')) return 'bg-emerald-500';
    return 'bg-gray-300 dark:bg-gray-600';
}
function rowClass(e: Row) {
    if (e.failed) return 'bg-red-50/60 dark:bg-red-950/20';
    if (['aborted', 'cancelled'].includes(e.status || '')) return 'bg-amber-50/50 dark:bg-amber-950/10';
    return '';
}
function kindClass(kind: string) {
    return {
        request: 'bg-sky-100 text-sky-800 dark:bg-sky-900/40 dark:text-sky-200',
        stream: 'bg-violet-100 text-violet-800 dark:bg-violet-900/40 dark:text-violet-200',
        filter: 'bg-teal-100 text-teal-800 dark:bg-teal-900/40 dark:text-teal-200',
        click: 'bg-surface-muted text-ink-2',
        error: 'bg-red-100 text-red-800 dark:bg-red-900/40 dark:text-red-200',
        console: 'bg-amber-100 text-amber-800 dark:bg-amber-900/40 dark:text-amber-200',
    }[kind] || 'bg-surface-muted text-ink-2';
}

function fmtMs(ms: number) {
    return ms >= 1000 ? `${(ms / 1000).toFixed(ms >= 10000 ? 0 : 1)} s` : `${Math.round(ms)} ms`;
}

// The time of day; with its date when it was not today, so a log spanning
// days still reads newest-first at a glance.
function when(iso: string | null) {
    if (!iso) return '';
    const d = new Date(iso);
    if (Number.isNaN(d.getTime())) return '';
    const time = d.toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false });
    const today = new Date();
    const sameDay = d.getFullYear() === today.getFullYear() && d.getMonth() === today.getMonth() && d.getDate() === today.getDate();
    return sameDay ? time : `${d.toLocaleDateString(undefined, { month: 'short', day: 'numeric' })} ${time}`;
}

function toggle(id: string) { expandedRow.value = expandedRow.value === id ? null : id; }

async function load() {
    loading.value = true;
    loadError.value = null;
    try {
        const { data, error } = await useMyFetch(`/api/artifacts/${encodeURIComponent(props.artifactId)}/runtime/activity`, {
            query: { report_id: props.reportId, limit: 200 },
        });
        if (error.value) throw error.value;
        const body: any = data.value || {};
        fetched.value = Array.isArray(body.events) ? body.events : [];
        liveRows.value = [];
    } catch (e: any) {
        loadError.value = getErrorMessage(e);
    } finally {
        loading.value = false;
    }
}

/** An event this page just produced (the broker's onActivity): shown at once,
 *  before the batch reaches the server. */
function push(event: ActivityEvent) {
    live.value = true;
    liveRows.value.push({
        id: `live-${++liveSeq}`, created_at: new Date().toISOString(), kind: event.kind, op: event.op || null,
        source: 'editor', version: props.version ?? null, status: event.status || null, error_code: event.error_code || null,
        duration_ms: event.duration_ms ?? null, failed: isFailed(event), detail: event.detail || null,
    });
    if (liveRows.value.length > 200) liveRows.value.splice(0, liveRows.value.length - 200);
}

function startResize(down: MouseEvent) {
    const startY = down.clientY, startH = height.value;
    const move = (e: MouseEvent) => { height.value = Math.min(Math.max(startH + (startY - e.clientY), 120), window.innerHeight * 0.8); };
    const up = () => { window.removeEventListener('mousemove', move); window.removeEventListener('mouseup', up); };
    window.addEventListener('mousemove', move);
    window.addEventListener('mouseup', up);
}

onMounted(() => {
    load();  // the strip's counts need the history too
    // The agent's preview writes to the same log from the server side: pick it up while open.
    poll = setInterval(() => { if (expanded.value && !loading.value && document.visibilityState === 'visible') load(); }, 10000);
});
onBeforeUnmount(() => { if (poll) clearInterval(poll); });

defineExpose({ push, load, toggle: () => setExpanded(!expanded.value), expanded });
</script>

<style scoped>
.th { @apply px-2 py-1 text-start font-medium whitespace-nowrap; }
.td { @apply px-2 py-1 align-top; }
</style>
