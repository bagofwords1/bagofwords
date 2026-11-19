<template>
	<Teleport to="body">
		<!-- Stays mounted (v-show) so the result cards below survive between
		     hovers: each card hydrates once, not on every hover of its chip. -->
		<div
			v-show="visible"
			ref="rootEl"
			class="citation-preview fixed z-popover w-[420px] max-w-[calc(100vw-24px)] overflow-hidden rounded-lg border border-line bg-surface-elevated text-start shadow-lg"
			:style="style"
			:role="visible ? 'tooltip' : undefined"
			:data-testid="visible ? 'citation-preview' : undefined"
			@mouseenter="$emit('enter')"
			@mouseleave="$emit('leave')"
		>
			<!-- The result's own card, compact: the same chart, grid, code and tabs
			     the reader gets in the transcript and the side panel. Kept alive
			     per source, so a second hover reuses the hydrated card. -->
			<KeepAlive :max="8">
			<ToolWidgetPreview
				v-if="visible && source && useResultCard"
				:key="source.key"
				:tool-execution="source.toolExecution"
				compact
				readonly
				:can-expand="canOpen"
				data-testid="citation-preview-card"
				@openDataPanel="$emit('open')"
			>
				<template #header-prefix>
					<span v-if="number" class="citation-number me-2 shrink-0" aria-hidden="true">{{ number }}</span>
				</template>
			</ToolWidgetPreview>
			</KeepAlive>

			<!-- Without a stored result to hand that card (a single value, or a
			     shared link, which carries preview rows only): a light stand-in
			     with the same tabs. -->
			<template v-if="visible && source && !useResultCard">
			<div class="flex items-center gap-2 px-3 pt-2.5" :class="tabs.length > 1 ? 'pb-1.5' : 'pb-2'">
				<span v-if="number" class="citation-number shrink-0" aria-hidden="true">{{ number }}</span>
				<span class="min-w-0 flex-1 truncate text-xs font-medium text-ink"><bdi>{{ title }}</bdi></span>
				<span v-if="meta" class="shrink-0 text-2xs text-ink-muted nums">{{ meta }}</span>
				<button
					v-if="canOpen"
					type="button"
					class="shrink-0 rounded text-ink-muted hover:text-ink focus-visible:outline focus-visible:outline-1 focus-visible:outline-blue-500"
					:aria-label="translate('reportView.citations.openSource', { title })"
					:title="translate('reportView.citations.openSource', { title })"
					data-testid="citation-preview-open"
					@click="$emit('open')"
				>
					<Icon name="heroicons:arrows-pointing-out" class="h-3.5 w-3.5" />
				</button>
			</div>

			<!-- Chart / Data / Code: the same views the full result offers. Code
			     is listed only for readers allowed to see it. -->
			<div v-if="tabs.length > 1" class="flex gap-3 border-b border-line px-3" role="tablist">
				<button
					v-for="tab in tabs"
					:key="tab"
					type="button"
					role="tab"
					:aria-selected="activeTab === tab"
					class="-mb-px border-b-2 pb-1.5 text-2xs"
					:class="activeTab === tab ? 'border-[rgb(var(--bow-accent-rgb))] text-ink' : 'border-transparent text-ink-muted hover:text-ink'"
					:data-testid="`citation-preview-tab-${tab}`"
					@click="activeTab = tab"
				>{{ translate(`reportView.citations.tabs.${tab}`) }}</button>
			</div>
			<div v-else class="border-b border-line"></div>

			<div v-if="activeTab === 'chart'" class="h-[190px] px-1 pt-1" data-testid="citation-preview-chart">
				<Suspense>
					<RenderVisual :widget="widgetShim" :data="data" :data_model="dataModel" :view="view" />
					<template #fallback>
						<div class="flex h-full w-full items-center justify-center"><Spinner class="h-4 w-4 text-ink-muted" /></div>
					</template>
				</Suspense>
			</div>

			<div v-else-if="activeTab === 'code'" class="max-h-[220px] overflow-auto px-3 py-2" data-testid="citation-preview-code">
				<pre class="m-0 whitespace-pre-wrap break-words font-mono text-2xs leading-relaxed text-ink-2" dir="ltr">{{ code }}</pre>
			</div>

			<!-- Data. A single value is its own preview; a table shows its first rows. -->
			<div v-else-if="source.output.kind === 'number'" class="px-3 py-3" data-testid="citation-preview-value">
				<div class="text-2xl font-semibold text-ink nums"><bdi>{{ source.output.value || '—' }}</bdi></div>
				<div v-if="columns[0]" class="mt-0.5 text-2xs text-ink-muted"><bdi>{{ columns[0].label }}</bdi></div>
			</div>
			<div v-else-if="previewRows.length" class="px-3 py-2" data-testid="citation-preview-table">
				<table class="w-full table-fixed border-collapse text-2xs">
					<thead>
						<tr>
							<th v-for="col in columns" :key="col.field" class="truncate border-b border-line pb-1 pe-2 text-start font-medium text-ink-muted" :class="{ 'text-end': col.numeric }"><bdi>{{ col.label }}</bdi></th>
						</tr>
					</thead>
					<tbody>
						<tr v-for="(row, ri) in previewRows" :key="ri">
							<td v-for="col in columns" :key="col.field" class="truncate border-b border-line-subtle py-1 pe-2 text-ink-2" :class="{ 'text-end nums': col.numeric }"><bdi>{{ formatCell(row[col.field]) }}</bdi></td>
						</tr>
					</tbody>
				</table>
				<div v-if="moreRows > 0 || moreColumns > 0" class="pt-1.5 text-2xs text-ink-muted">
					<span v-if="moreRows > 0">{{ translate('reportView.citations.moreRows', { count: moreRows.toLocaleString() }) }}</span>
					<span v-if="moreRows > 0 && moreColumns > 0"> · </span>
					<span v-if="moreColumns > 0">{{ translate('reportView.citations.moreColumns', { count: moreColumns }) }}</span>
				</div>
			</div>
			<div v-else class="px-3 py-3 text-2xs text-ink-muted">{{ translate('reportView.citations.noPreview') }}</div>
			</template>
		</div>
	</Teleport>
</template>

<script setup lang="ts">
/**
 * Hover/focus preview of a cited result (see composables/useCitations.ts).
 *
 * A table or chart is shown with the result's own card — ToolWidgetPreview in
 * its `compact` mode — so the preview has the same Chart / Data / Code tabs,
 * the same renderers and the same code-permission rule as the transcript and
 * the side panel. A single value, and any result the host has no stored step
 * for (the shared link), gets a light stand-in with the same tabs. It is a
 * glance, not the working view — the header button opens the full result
 * where the host has somewhere to open it.
 */
import { computed, nextTick, onErrorCaptured, ref, watch } from 'vue'
import type { CitationSource } from '~/composables/useCitations'
import RenderVisual from '~/components/RenderVisual.vue'
import ToolWidgetPreview from '~/components/tools/ToolWidgetPreview.vue'
import Spinner from '~/components/Spinner.vue'

const props = defineProps<{
	source: CitationSource | null
	/** Viewport rect of the chip being previewed. */
	anchor: { top: number; bottom: number; left: number; right: number } | null
	number?: number | null
	translate: (key: string, values?: any) => string
	/** Host opens the full result on click. */
	canOpen?: boolean
}>()
defineEmits<{ (e: 'enter'): void; (e: 'leave'): void; (e: 'open'): void }>()

const MAX_ROWS = 5
const MAX_COLUMNS = 4
const CHART_TYPES = new Set(['pie_chart', 'line_chart', 'bar_chart', 'area_chart', 'heatmap', 'scatter_plot', 'map', 'candlestick', 'treemap', 'radar_chart'])

const visible = computed(() => !!(props.source && props.anchor))
const te = computed<any>(() => props.source?.toolExecution || null)
// A single value reads better as the value itself than as a one-cell grid.
const useResultCard = computed(() => props.source?.output.kind !== 'number'
	&& !!(te.value?.created_step || te.value?.created_widget))
const data = computed<any>(() => te.value?.created_step?.data || te.value?.result_json?.data || te.value?.result_json?.data_preview || null)
const rows = computed<any[]>(() => (Array.isArray(data.value?.rows) ? data.value.rows : []))
const title = computed(() => props.source?.output.title || props.translate('reportView.citations.untitled'))
const meta = computed(() => {
	const out = props.source?.output
	if (!out || out.kind === 'number' || out.rowCount == null) return ''
	return props.translate('tools.common.rows', { n: out.rowCount.toLocaleString() })
})

const allColumns = computed(() => {
	const declared = Array.isArray(data.value?.columns) ? data.value.columns : []
	const fields: { field: string; label: string }[] = declared
		.map((c: any) => ({ field: String(c?.field ?? c?.name ?? ''), label: String(c?.headerName ?? c?.field ?? c?.name ?? '') }))
		.filter((c: any) => c.field)
	if (fields.length) return fields
	const first = rows.value[0]
	return first && typeof first === 'object' && !Array.isArray(first)
		? Object.keys(first).map((k) => ({ field: k, label: k }))
		: []
})
const columns = computed(() => allColumns.value.slice(0, MAX_COLUMNS).map((c) => ({
	...c,
	numeric: rows.value.slice(0, MAX_ROWS).every((r) => r?.[c.field] == null || typeof r[c.field] === 'number'),
})))
const previewRows = computed(() => (columns.value.length ? rows.value.slice(0, MAX_ROWS) : []))
const moreRows = computed(() => Math.max(0, (props.source?.output.rowCount ?? rows.value.length) - previewRows.value.length))
const moreColumns = computed(() => Math.max(0, allColumns.value.length - columns.value.length))

function formatCell(v: any): string {
	if (v === null || v === undefined) return ''
	if (typeof v === 'number' && Number.isFinite(v)) return v.toLocaleString(undefined, { maximumFractionDigits: 2 })
	return String(v)
}

// --- chart -------------------------------------------------------------------
const view = computed<any>(() => {
	const v = te.value?.created_visualizations?.[0]?.view || te.value?.result_json?.view || te.value?.created_step?.view
	if (!v) return null
	if (v.view?.type) return v
	return v.type ? { view: v, version: 'v2' } : v
})
const dataModel = computed<any>(() => te.value?.created_step?.data_model || te.value?.result_json?.data_model || null)
const widgetShim = computed<any>(() => ({ id: `cite-${props.source?.key || 'preview'}`, title: title.value }))
const chartFailed = ref(false)
onErrorCaptured(() => { chartFailed.value = true; return false })
watch(() => props.source?.key, () => { chartFailed.value = false })
const showChart = computed(() => {
	if (props.source?.output.kind !== 'chart' || chartFailed.value || !rows.value.length) return false
	const type = String(view.value?.view?.type || dataModel.value?.type || '').toLowerCase()
	return CHART_TYPES.has(type)
})

// --- tabs --------------------------------------------------------------------
// Same rule as the result card: the server already withholds code from readers
// who may not see it, and the permission is checked here as well so a payload
// cached from before a role change cannot resurrect the tab.
const canViewCode = useCanViewCode()
const code = computed<string>(() => (canViewCode.value
	? String(te.value?.created_step?.code || te.value?.result_json?.code || '')
	: ''))
type Tab = 'chart' | 'data' | 'code'
const tabs = computed<Tab[]>(() => {
	const out: Tab[] = []
	if (showChart.value) out.push('chart')
	out.push('data')
	if (code.value) out.push('code')
	return out
})
// A newly previewed source opens on its first tab; a tab that stops being
// available (code withheld) falls back to it.
const activeTab = ref<Tab>('data')
watch(() => props.source?.key, () => { activeTab.value = tabs.value[0] }, { immediate: true })
watch(tabs, (list) => { if (!list.includes(activeTab.value)) activeTab.value = list[0] })

// --- placement ---------------------------------------------------------------
// Below the chip when there is room, otherwise above; clamped to the viewport.
const rootEl = ref<HTMLElement | null>(null)
const height = ref(0)
watch(() => [props.source?.key, props.anchor?.top, props.anchor?.left, activeTab.value], async () => {
	await nextTick()
	height.value = rootEl.value?.offsetHeight || 0
}, { immediate: true, flush: 'post' })

const style = computed(() => {
	const a = props.anchor
	if (!a || typeof window === 'undefined') return {}
	const width = Math.min(420, window.innerWidth - 24)
	const left = Math.max(12, Math.min(a.left - 12, window.innerWidth - width - 12))
	const estimated = height.value || 240
	const below = a.bottom + 6
	const fitsBelow = below + estimated <= window.innerHeight - 8
	const top = fitsBelow ? below : Math.max(8, a.top - 6 - estimated)
	return { left: `${left}px`, top: `${top}px` }
})
</script>

<style scoped>
.citation-number {
	display: inline-flex;
	align-items: center;
	justify-content: center;
	min-width: 1.25rem;
	height: 1.25rem;
	padding: 0 0.3rem;
	border-radius: 9999px;
	font-size: 0.6875rem;
	font-weight: 600;
	line-height: 1;
	color: rgb(var(--bow-accent-rgb));
	background: rgb(var(--bow-accent-subtle-rgb));
}
</style>
