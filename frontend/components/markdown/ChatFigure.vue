<template>
  <figure
    v-if="source && model.kind !== 'number'"
    ref="root"
    class="pf"
    :class="{ 'is-entering': entering }"
    :data-state="state"
    :data-kind="model.kind"
    :data-highlight="[...highlight].join(',') || undefined"
    data-testid="chat-figure"
  >
    <!-- Chart. The body takes its final height at once: a faint outline while
         the full rows load, crossfading into the chart. -->
    <div v-if="isChart && view === 'chart'" class="pf-body" dir="ltr" :style="{ height: chartHeight + 'px' }">
      <Transition name="pf-ghost">
        <div v-if="state === 'loading'" class="pf-ghost" :class="model.kind === 'bars' ? 'is-bars' : 'is-area'" aria-hidden="true">
          <span v-for="i in ghostCount" :key="i" :style="ghostStyle(i)" />
        </div>
      </Transition>
      <ClientOnly v-if="state === 'ready' && model.kind !== 'other'">
        <VChart ref="chartRef" class="h-full w-full" :option="chartOption" autoresize :init-options="{ renderer: 'svg' }" />
      </ClientOnly>
      <Suspense v-else-if="state === 'ready'">
        <RenderVisual :widget="widgetShim" :data="dataForRenderer" :data_model="dataModel" :view="rawView" />
        <template #fallback><div class="h-full w-full" /></template>
      </Suspense>
      <span v-if="state === 'loading'" class="sr-only" role="status">{{ t('figure.loading') }}</span>
    </div>

    <!-- Table: three hairlines, nothing else. -->
    <div v-else class="pf-table-wrap">
      <div v-if="state === 'loading'" class="pf-ghost-table" aria-hidden="true"><span v-for="i in 5" :key="i" /></div>
      <template v-else>
        <input v-if="showAll && tableRows.length > PREVIEW_ROWS" v-model="query" type="search" class="pf-search" :placeholder="t('figure.search', { count: tableRows.length })" :aria-label="t('figure.searchLabel')" data-testid="chat-figure-search" />
        <div ref="scroller" class="pf-table-scroll" :class="{ 'is-scrollable': scrollable, 'at-end': atEnd }" @scroll="onScroll">
          <table class="pf-table">
            <thead>
              <tr>
                <th v-for="col in model.columns" :key="col.field" :class="{ 'is-num': col.numeric }" :aria-sort="sortKey === col.field ? (sortDir === 1 ? 'ascending' : 'descending') : 'none'">
                  <button type="button" @click="toggleSort(col.field)"><bdi>{{ col.label }}</bdi><span v-if="sortKey === col.field" aria-hidden="true">{{ sortDir === 1 ? ' ↑' : ' ↓' }}</span></button>
                </th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="(row, i) in visibleRows" :key="i" :class="{ 'is-hl': isHighlightedRow(row) }" :style="entering ? { animationDelay: `${120 + i * 22}ms` } : undefined">
                <td v-for="col in model.columns" :key="col.field" :class="{ 'is-num nums': col.numeric }"><bdi>{{ cell(row[col.field], col) }}</bdi></td>
              </tr>
              <tr v-if="!visibleRows.length"><td :colspan="model.columns.length" class="pf-empty">{{ t('figure.noMatch') }}</td></tr>
            </tbody>
          </table>
        </div>
        <button v-if="filteredRows.length > PREVIEW_ROWS && !query" type="button" class="pf-link pf-more" data-testid="chat-figure-more" @click="showAll = !showAll">
          {{ showAll ? t('figure.showLess') : t('figure.showAll', { count: filteredRows.length }) }}
        </button>
      </template>
    </div>

    <figcaption class="pf-cap">
      <span class="pf-cap-text">
        <span class="pf-label nums">{{ t('figure.caption', { n: number }) }}</span>
        <bdi class="pf-title">{{ title }}</bdi><span v-if="captionDetail" class="pf-detail">, <bdi>{{ captionDetail }}</bdi></span>.
        <span v-if="legend.length > 1" class="pf-legend">
          <span v-for="s in legend" :key="s.name" class="pf-key" :class="{ 'is-dim': anyHl && !highlight.has(s.name) }"><i :style="{ background: s.color }" aria-hidden="true" /><bdi>{{ s.name }}</bdi></span>
        </span>
      </span>
      <span class="pf-actions">
        <button v-if="isChart" type="button" class="pf-link" data-testid="chat-figure-as-table" @click="view = view === 'chart' ? 'table' : 'chart'">{{ view === 'chart' ? t('figure.table') : t('figure.chart') }}</button>
        <template v-if="ctx?.open">
          <span v-if="isChart" class="pf-sep" aria-hidden="true">·</span>
          <button type="button" class="pf-link" data-testid="chat-figure-open" @click="ctx.open(source)">{{ t('figure.open') }} <span class="inline-block rtl-flip" aria-hidden="true">↗</span></button>
        </template>
        <span class="pf-sep" aria-hidden="true">·</span>
        <UDropdown :items="menuItems" :popper="{ placement: 'bottom-end' }" :ui="{ width: 'w-52' }">
          <button type="button" class="pf-link pf-more-btn" :aria-label="t('figure.more')" :title="t('figure.more')" data-testid="chat-figure-menu">•••</button>
        </UDropdown>
      </span>
    </figcaption>

    <!-- Save as query: the same dialog the result card uses. -->
    <EntityCreateModal
      v-if="canSave && saveOpen"
      :visible="saveOpen"
      :initial-title="title"
      :initial-code="te?.created_step?.code || ''"
      :initial-view="rawView?.view || rawView || null"
      :initial-data="fullData"
      :data-model="dataModel"
      :step-id="stepId || null"
      :initial-data-source-ids="reportDataSourceIds"
      :parameters="[]"
      @close="saveOpen = false"
      @saved="onSaved"
    />
    <UModal v-model="codeOpen" :ui="{ width: 'sm:max-w-3xl' }">
      <div class="p-4">
        <div class="mb-2 flex items-center justify-between">
          <span class="text-sm font-medium text-ink">{{ title }}</span>
          <UButton color="gray" variant="ghost" size="xs" icon="i-heroicons-x-mark" :aria-label="t('common.close')" @click="codeOpen = false" />
        </div>
        <pre class="max-h-[60vh] overflow-auto rounded-md bg-surface-subtle p-3 text-xs text-ink-2" dir="ltr">{{ code }}</pre>
      </div>
    </UModal>
  </figure>
</template>

<script setup lang="ts">
/**
 * A result placed in an answer: `{{show:<visualization id> highlight="…"}}`,
 * rewritten by useCitations into `<bow-figure source="<block id>" number="n">`,
 * a markstream custom node. The page provides FIGURE_CONTEXT to resolve the
 * source (composables/figureModel.ts).
 *
 * Set like a figure in a paper: no frame, the caption below ("Figure 1. …"),
 * text controls on hover. What it draws comes from create_data's own
 * inference (figureModel); bars, columns and lines are drawn here in one muted
 * accent (a small editorial palette for several series), any other type by
 * the existing renderer in the same frame. `highlight` puts the categories the
 * sentence is about in full colour and tints the rest. A single value is never
 * a figure.
 *
 * Motion, the first time a result appears in the page only (never on a
 * re-render, reload or scroll back; none with reduced motion): the figure
 * fades and rises, bars grow one after another, a line draws left to right
 * and then shows its last value, table rows follow each other, the caption
 * arrives last. A loading outline crossfades into the chart.
 */
import { computed, inject, onBeforeUnmount, onMounted, ref, unref, watch } from 'vue'
import { use } from 'echarts/core'
import { TooltipComponent, GridComponent, MarkPointComponent } from 'echarts/components'
import { BarChart, LineChart } from 'echarts/charts'
import { SVGRenderer } from 'echarts/renderers'
import RenderVisual from '~/components/RenderVisual.vue'
import EntityCreateModal from '~/components/entity/EntityCreateModal.vue'
import { FIGURE_CONTEXT, dataOf, figureModel, formatFigureValue, type FigureColumn } from '~/composables/figureModel'

use([TooltipComponent, GridComponent, MarkPointComponent, BarChart, LineChart, SVGRenderer])

defineOptions({ inheritAttrs: false })
const props = defineProps<{ node: any }>()
const { t, locale } = useI18n()
const colorMode = useColorMode()
const toast = useToast()
const canViewCode = useCanViewCode()
const ctx = inject(FIGURE_CONTEXT, null)
const reportSnapshot = inject<any>('reportSnapshot', null)
const artifactVizIds = inject<any>('artifactVizIds', null)

const PREVIEW_ROWS = 6
const MAX_BARS = 8

const attr = (k: string) => String((props.node?.attrs || []).find((a: [string, string]) => a[0] === k)?.[1] ?? '')
const source = computed(() => ctx?.resolve(attr('source')) || null)
const number = computed(() => Number(attr('number')) || 1)
const te = computed<any>(() => source.value?.toolExecution || null)
const title = computed(() => source.value?.output.title || t('figure.untitled'))

// ── Full rows: a step may carry a preview; load it once per page. ───────────
const loaded = ref<any>(null)
const failed = ref(false)
const model = computed(() => figureModel(te.value, loaded.value || undefined))
const stepId = computed(() => String(te.value?.created_step_id || te.value?.created_step?.id || ''))
const needsRows = computed(() => !loaded.value && !failed.value && model.value.partial && !!ctx?.canHydrate && !!stepId.value)
// create_data ends with a deterministic chart and refines it in the
// background (`viz_pending`, cleared by the `visualization.refined` event).
// Draw once, the refined chart: hold the outline while it is pending, but
// never longer than VIZ_WAIT_MS (a run that died leaves the flag set).
const vizPendingFlag = computed(() => te.value?.result_json?.viz_pending === true)
const vizWaitOver = ref(false)
let vizTimer: ReturnType<typeof setTimeout> | null = null
watch(vizPendingFlag, (pending) => {
  if (vizTimer) { clearTimeout(vizTimer); vizTimer = null }
  if (!pending) return
  const key = attr('source')
  const since = VIZ_WAIT_SINCE.get(key) ?? Date.now()
  VIZ_WAIT_SINCE.set(key, since)
  const left = VIZ_WAIT_MS - (Date.now() - since)
  if (left <= 0) { vizWaitOver.value = true; return }
  vizTimer = setTimeout(() => { vizWaitOver.value = true }, left)
}, { immediate: true })
onBeforeUnmount(() => { if (vizTimer) clearTimeout(vizTimer) })
const vizPending = computed(() => vizPendingFlag.value && !vizWaitOver.value)
const state = computed(() => (needsRows.value || vizPending.value ? 'loading' : 'ready'))
async function loadRows(): Promise<void> {
  if (!needsRows.value) return
  try {
    let pending = STEP_CACHE.get(stepId.value)
    if (!pending) {
      pending = useMyFetch(`/api/steps/${stepId.value}`, { method: 'GET' }).then(({ data, error }: any) => {
        if (error?.value) throw error.value
        return (data.value as any)?.data || null
      })
      STEP_CACHE.set(stepId.value, pending)
    }
    const data = await pending
    if (data?.rows) loaded.value = data
    else failed.value = true
  } catch {
    STEP_CACHE.delete(stepId.value)
    failed.value = true
  }
}
watch(needsRows, (v) => { if (v) void loadRows() }, { immediate: true })
const fullData = computed(() => loaded.value || dataOf(te.value))

// ── Entrance: once per result per page. ─────────────────────────────────────
const reducedMotion = typeof window !== 'undefined' && !!window.matchMedia?.('(prefers-reduced-motion: reduce)').matches
const firstShow = !SHOWN.has(attr('source')) && !reducedMotion
const entering = ref(false)
onMounted(() => {
  SHOWN.add(attr('source'))
  if (!firstShow) return
  entering.value = true
  setTimeout(() => { entering.value = false }, 1600)
})

// ── View & caption ──────────────────────────────────────────────────────────
const isChart = computed(() => ['bars', 'columns', 'line', 'other'].includes(model.value.kind))
const view = ref<'chart' | 'table'>('chart')
const query = ref('')
const showAll = ref(false)
const captionDetail = computed(() => {
  const m = model.value
  if (view.value === 'chart' && m.kind === 'bars' && m.categories.length > MAX_BARS) return t('figure.topOf', { shown: MAX_BARS, total: m.categories.length })
  if ((view.value === 'table' || m.kind === 'table') && m.rows.length > PREVIEW_ROWS && !showAll.value) return t('figure.firstOf', { shown: PREVIEW_ROWS, total: m.totalRows || m.rows.length })
  return ''
})

// ── Direction follows the answer, not the UI ────────────────────────────────
const root = ref<HTMLElement | null>(null)
const answerDir = ref<'ltr' | 'rtl'>('ltr')
function readDir() { if (root.value) answerDir.value = getComputedStyle(root.value).direction === 'rtl' ? 'rtl' : 'ltr' }
// The renderer stamps the answer's dir on its blocks just after they mount.
onMounted(() => { readDir(); requestAnimationFrame(readDir); setTimeout(readDir, 300); setTimeout(readDir, 1200) })
const isRtl = computed(() => answerDir.value === 'rtl')

// ── Palette: one muted accent; a small editorial set for several series ────
const dark = computed(() => colorMode.value === 'dark')
const SERIES_LIGHT = ['#5F6AD9', '#D99A3B', '#3F8F86', '#C9677A', '#868EA3']
const SERIES_DARK = ['#8B96F5', '#E8B25E', '#5BB3A8', '#E38A9C', '#A3AABD']
const seriesColors = computed(() => (dark.value ? SERIES_DARK : SERIES_LIGHT))
const accent = computed(() => seriesColors.value[0])
const ink = ref({ text: '#374151', muted: '#667085', grid: 'rgba(17,24,39,0.09)', bg: '#ffffff', tip: '#111827', tipInk: '#f9fafb' })
function readInk() {
  const cs = getComputedStyle(document.documentElement)
  const rgb = (n: string, f: string) => { const v = cs.getPropertyValue(n).trim(); return v ? `rgb(${v.split(/\s+/).join(',')})` : f }
  ink.value = {
    text: rgb('--bow-ink-2-rgb', '#374151'),
    muted: rgb('--bow-ink-muted-rgb', '#667085'),
    grid: dark.value ? 'rgba(255,255,255,0.10)' : 'rgba(17,24,39,0.09)',
    bg: rgb('--bow-bg-rgb', '#ffffff'),
    tip: rgb('--bow-bg-inverted-rgb', '#111827'),
    tipInk: rgb('--bow-ink-inverted-rgb', '#f9fafb'),
  }
}
onMounted(readInk)
watch(dark, () => requestAnimationFrame(readInk))
const legend = computed(() => model.value.series.map((s, i) => ({ name: s.name, color: seriesColors.value[i % seriesColors.value.length] })))
function alpha(hex: string, a: number) {
  const n = parseInt(hex.slice(1), 16)
  return `rgba(${n >> 16},${(n >> 8) & 255},${n & 255},${a})`
}

// ── Highlight: only values that exist in the result count ───────────────────
const highlight = computed(() => {
  const wanted = attr('highlight').split(',').map((s) => s.trim()).filter(Boolean).slice(0, 3)
  const m = model.value
  const present = new Set([...m.categories, ...m.series.map((s) => s.name), ...m.rows.flatMap((r) => Object.values(r).map(String))])
  return new Set(wanted.filter((w) => present.has(w)))
})
const anyHl = computed(() => highlight.value.size > 0)
const isHighlightedRow = (row: Record<string, any>) => anyHl.value && Object.values(row).some((v) => highlight.value.has(String(v)))

// ── Chart ───────────────────────────────────────────────────────────────────
const fmt = (v: any, short = false) => formatFigureValue(v, model.value.format, model.value.currency, locale.value, short)
const barCount = computed(() => Math.min(model.value.categories.length, MAX_BARS))
const chartHeight = computed(() => (model.value.kind === 'bars' ? Math.max(84, barCount.value * 28) : 220))
const ghostCount = computed(() => (model.value.kind === 'bars' ? Math.max(3, barCount.value) : 16))
function ghostStyle(i: number) {
  if (model.value.kind === 'bars') return { width: `${Math.max(14, 86 - i * (64 / Math.max(3, barCount.value)))}%` }
  return { height: `${34 + ((i * 29) % 46)}%` }
}

const chartOption = computed(() => {
  const m = model.value
  const p = ink.value
  const font = 'Inter, ui-sans-serif, system-ui, sans-serif'
  const tooltip = {
    trigger: 'axis', confine: true,
    axisPointer: { type: m.kind === 'line' ? 'line' : 'shadow', lineStyle: { color: p.muted, width: 1, type: [2, 3] }, shadowStyle: { color: alpha(accent.value, 0.06) } },
    backgroundColor: p.tip, borderWidth: 0, padding: [6, 10],
    textStyle: { color: p.tipInk, fontSize: 12, fontFamily: font },
    extraCssText: 'border-radius:6px;box-shadow:0 6px 18px rgba(0,0,0,.14);',
    formatter: (items: any[]) => `<div style="opacity:.65;margin-bottom:2px">${esc(items[0]?.name ?? '')}</div>` + items.map((it) => `<div style="display:flex;gap:8px;align-items:center"><span style="width:8px;height:8px;border-radius:50%;background:${it.color}"></span>${items.length > 1 ? `<span style="opacity:.75">${esc(it.seriesName)}</span>` : ''}<b style="margin-inline-start:auto;font-weight:600">${fmt(it.value)}</b></div>`).join(''),
  }
  const motion = firstShow
    ? { animation: true, animationDuration: 460, animationEasing: 'cubicOut', animationDelay: (idx: number) => idx * 32, animationDurationUpdate: 240 }
    : { animation: false }
  const base = { ...motion, tooltip, textStyle: { fontFamily: font } }

  if (m.kind === 'bars') {
    const cats = m.categories.slice(0, MAX_BARS)
    const values = m.series[0]?.values.slice(0, MAX_BARS) || []
    return {
      ...base,
      grid: { left: isRtl.value ? 52 : 8, right: isRtl.value ? 8 : 52, top: 0, bottom: 0, containLabel: true },
      xAxis: { type: 'value', show: false, axisLabel: { show: false }, splitLine: { show: false }, inverse: isRtl.value },
      yAxis: {
        type: 'category', inverse: true, position: isRtl.value ? 'right' : 'left', data: cats,
        axisLine: { show: false }, axisTick: { show: false },
        axisLabel: {
          color: p.text, fontSize: 12, margin: 14, width: 128, overflow: 'truncate', align: isRtl.value ? 'left' : 'right',
          formatter: (v: string) => (highlight.value.has(v) ? `{hl|${v}}` : v),
          rich: { hl: { color: p.text, fontWeight: 600, fontSize: 12, fontFamily: font } },
        },
      },
      series: [{
        type: 'bar', barWidth: 10, barMinHeight: 2,
        data: cats.map((c, i) => ({
          value: values[i],
          itemStyle: { color: !anyHl.value || highlight.value.has(c) ? accent.value : alpha(accent.value, 0.3), borderRadius: isRtl.value ? [2, 0, 0, 2] : [0, 2, 2, 0] },
          label: { color: highlight.value.has(c) ? p.text : p.muted, fontWeight: highlight.value.has(c) ? 600 : 400 },
        })),
        label: { show: true, position: isRtl.value ? 'left' : 'right', distance: 8, fontSize: 12, formatter: (x: any) => fmt(x.value, true) },
        emphasis: { disabled: true },
      }],
    }
  }

  const xAxis = {
    type: 'category', data: m.categories, inverse: isRtl.value, boundaryGap: m.kind === 'columns',
    axisLine: { show: true, lineStyle: { color: p.grid } }, axisTick: { show: false },
    axisLabel: { color: p.muted, fontSize: 11, margin: 10, hideOverlap: true },
  }
  const yAxis = {
    type: 'value', position: isRtl.value ? 'right' : 'left', splitNumber: 3,
    axisLabel: { color: p.muted, fontSize: 11, formatter: (v: number) => fmt(v, true) },
    splitLine: { lineStyle: { color: p.grid, type: [2, 4] } },
  }
  const grid = { left: 8, right: m.kind === 'line' ? 56 : 12, top: 12, bottom: 0, containLabel: true }
  const tone = (name: string, color: string) => (!anyHl.value || highlight.value.has(name) ? color : alpha(color, 0.28))
  if (m.kind === 'columns') {
    return {
      ...base, grid, xAxis, yAxis,
      series: m.series.map((s, i) => ({
        type: 'bar', name: s.name, stack: m.stacked ? 'total' : undefined,
        barMaxWidth: m.series.length > 1 ? 12 : 18, barGap: '30%',
        data: s.values.map((v, ci) => ({
          value: v,
          itemStyle: { color: m.series.length > 1 ? tone(s.name, legend.value[i].color) : tone(m.categories[ci], accent.value), borderRadius: [2, 2, 0, 0] },
        })),
        emphasis: { disabled: true },
      })),
    }
  }
  const single = m.series.length === 1
  const lineMs = 700
  return {
    ...base,
    ...(firstShow ? { animationDuration: lineMs, animationDelay: 80 } : {}),
    grid, xAxis, yAxis,
    series: m.series.map((s, i) => {
      const color = tone(s.name, legend.value[i]?.color || accent.value)
      const last = s.values.length - 1
      return {
        type: 'line', name: s.name, data: s.values, smooth: 0.25, showSymbol: false, connectNulls: true,
        lineStyle: { color, width: anyHl.value && highlight.value.has(s.name) ? 2.25 : 1.75 }, itemStyle: { color },
        areaStyle: single ? { color: { type: 'linear', x: 0, y: 0, x2: 0, y2: 1, colorStops: [{ offset: 0, color: alpha(legend.value[0]?.color || accent.value, 0.12) }, { offset: 1, color: alpha(legend.value[0]?.color || accent.value, 0) }] } } : undefined,
        // The latest value, labelled where its line ends — after the line arrives.
        markPoint: last >= 0 && s.values[last] != null ? {
          symbol: 'circle', symbolSize: 6, itemStyle: { color, borderColor: p.bg, borderWidth: 2 },
          label: { show: true, position: 'right', distance: 6, color, fontSize: 11, fontWeight: 600, formatter: () => fmt(s.values[last], true) },
          data: [{ coord: [last, s.values[last]] }],
          animation: firstShow, animationDelay: firstShow ? lineMs + 60 : 0, animationDuration: 260,
        } : undefined,
        emphasis: { disabled: true },
      }
    }),
  }
})
function esc(s: string) { return String(s).replace(/[&<>"]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]!)) }

// ── Fallback renderer ───────────────────────────────────────────────────────
const rawView = computed<any>(() => {
  const v = te.value?.created_visualizations?.[0]?.view || te.value?.result_json?.view || te.value?.created_step?.view
  if (!v) return null
  if (v.view?.type) return v
  return v.type ? { view: v, version: 'v2' } : v
})
const dataModel = computed<any>(() => te.value?.created_step?.data_model || te.value?.result_json?.data_model || null)
const dataForRenderer = computed<any>(() => ({ rows: model.value.rows, columns: model.value.columns.map((c) => ({ field: c.field, headerName: c.label })) }))
const widgetShim = computed<any>(() => ({ id: `figure-${attr('source')}`, title: title.value }))

// ── Table ───────────────────────────────────────────────────────────────────
const sortKey = ref<string | null>(null)
const sortDir = ref<1 | -1>(-1)
const tableRows = computed(() => model.value.rows)
function toggleSort(field: string) {
  if (sortKey.value === field) sortDir.value = sortDir.value === 1 ? -1 : 1
  else { sortKey.value = field; sortDir.value = model.value.columns.find((c) => c.field === field)?.numeric ? -1 : 1 }
}
const filteredRows = computed(() => {
  let rows = tableRows.value
  const q = query.value.trim().toLowerCase()
  if (q) rows = rows.filter((r) => Object.values(r).some((v) => String(v ?? '').toLowerCase().includes(q)))
  if (sortKey.value) {
    const k = sortKey.value
    rows = [...rows].sort((a, b) => { const x = a[k]; const y = b[k]; if (x == null) return 1; if (y == null) return -1; return (typeof x === 'number' && typeof y === 'number' ? x - y : String(x).localeCompare(String(y))) * sortDir.value })
  }
  return rows
})
const visibleRows = computed(() => (showAll.value || query.value ? filteredRows.value : filteredRows.value.slice(0, PREVIEW_ROWS)))
// The view's measure carries its unit (currency, percent); other numbers are plain.
const firstMeasure = computed(() => {
  const v = te.value?.created_visualizations?.[0]?.view || te.value?.result_json?.view
  const y = (v?.view || v)?.y
  return String(Array.isArray(y) ? y[0] : y || '')
})
function cell(v: any, col: FigureColumn) {
  if (v == null || v === '') return '—'
  if (!col.numeric) return String(v)
  return col.field === firstMeasure.value ? fmt(v) : formatFigureValue(v, 'number', 'USD', locale.value)
}

// Wide tables: fade the far edge until scrolled to it.
const scroller = ref<HTMLElement | null>(null)
const scrollable = ref(false)
const atEnd = ref(false)
function measureScroll() {
  const el = scroller.value
  if (!el) return
  scrollable.value = el.scrollWidth > el.clientWidth + 2
  atEnd.value = Math.abs(el.scrollLeft) + el.clientWidth >= el.scrollWidth - 2
}
function onScroll() { measureScroll() }
// The table appears after loading, and the column can change width: observe it.
let resizeObs: ResizeObserver | null = null
watch(scroller, (el, prev) => {
  if (prev) resizeObs?.unobserve(prev)
  if (!el) return
  resizeObs ??= new ResizeObserver(() => measureScroll())
  resizeObs.observe(el)
  requestAnimationFrame(measureScroll)
})
onBeforeUnmount(() => resizeObs?.disconnect())
watch([view, showAll, () => model.value.rows.length], () => requestAnimationFrame(measureScroll))

// ── Actions (the "•••" menu): the result card's own endpoints and dialogs ───
const vizId = computed(() => String(source.value?.ids?.[0] || ''))
const reportId = computed(() => String(ctx?.reportId || ''))
const canEdit = computed(() => !!ctx?.canEdit && !!reportId.value)
const onDashboard = ref(false)
const inDashboard = computed(() => onDashboard.value || (unref(artifactVizIds) || []).map(String).includes(vizId.value))
const adding = ref(false)
const saveOpen = ref(false)
const savedLocal = ref(false)
const saved = computed(() => savedLocal.value || !!te.value?.created_step?.created_entity_id)
const canSave = computed(() => canEdit.value && !!stepId.value)
const code = computed(() => (canViewCode.value ? String(te.value?.created_step?.code || te.value?.result_json?.code || '') : ''))
const codeOpen = ref(false)
const reportDataSourceIds = computed(() => (unref(reportSnapshot)?.data_sources || []).map((d: any) => String(d.id)))
const chartRef = ref<any>(null)
const fileName = computed(() => (title.value || 'result').replace(/[\\/:*?"<>|]+/g, ' ').trim() || 'result')

async function rowsForExport(): Promise<{ rows: any[]; columns: FigureColumn[] }> {
  if (model.value.partial && ctx?.canHydrate) await loadRows()
  return { rows: model.value.rows, columns: model.value.columns }
}
function saveBlob(blob: Blob, name: string) {
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url; a.download = name; a.style.display = 'none'
  document.body.appendChild(a); a.click(); a.remove()
  setTimeout(() => URL.revokeObjectURL(url), 1000)
}
const csvCell = (v: any) => { const s = v == null ? '' : String(v); return /[",\n\r]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s }
async function downloadCsv() {
  const { rows, columns } = await rowsForExport()
  const lines = [columns.map((c) => csvCell(c.label)).join(','), ...rows.map((r) => columns.map((c) => csvCell(r[c.field])).join(','))]
  // BOM: Excel reads non-ASCII (e.g. Hebrew) headers as UTF-8.
  saveBlob(new Blob(['﻿' + lines.join('\n')], { type: 'text/csv;charset=utf-8' }), `${fileName.value}.csv`)
}
async function downloadExcel() {
  if (!stepId.value || !ctx?.canHydrate) return downloadCsv()
  const { data, error }: any = await useMyFetch(`/api/steps/${stepId.value}/export`, { query: { format: 'xlsx' }, responseType: 'blob' })
  if (error?.value || !data?.value) { toast.add({ title: t('figure.exportFailed'), color: 'red' }); return }
  saveBlob(data.value as Blob, `${fileName.value}.xlsx`)
}
async function downloadImage() {
  const chart = chartRef.value
  if (!chart?.getDataURL) return
  // The chart is SVG: draw it onto a canvas at 2x for a crisp PNG.
  const svgUrl = chart.getDataURL({ type: 'svg' })
  const img = new Image()
  await new Promise<void>((resolve, reject) => { img.onload = () => resolve(); img.onerror = reject; img.src = svgUrl })
  const scale = 2
  const canvas = document.createElement('canvas')
  canvas.width = img.width * scale; canvas.height = img.height * scale
  const g = canvas.getContext('2d')!
  g.fillStyle = ink.value.bg; g.fillRect(0, 0, canvas.width, canvas.height)
  g.drawImage(img, 0, 0, canvas.width, canvas.height)
  canvas.toBlob((b) => b && saveBlob(b, `${fileName.value}.png`), 'image/png')
}
async function copyData() {
  const { rows, columns } = await rowsForExport()
  const tsv = [columns.map((c) => c.label).join('\t'), ...rows.map((r) => columns.map((c) => String(r[c.field] ?? '')).join('\t'))].join('\n')
  try { await navigator.clipboard.writeText(tsv); toast.add({ title: t('figure.copied') }) } catch { toast.add({ title: t('figure.copyFailed'), color: 'red' }) }
}
async function addToDashboard() {
  if (!canEdit.value || !vizId.value || adding.value || inDashboard.value) return
  adding.value = true
  try {
    const { data, error }: any = await useMyFetch(`/api/artifacts/report/${reportId.value}/add-visualization`, { method: 'POST', body: { visualization_id: vizId.value } })
    if (error?.value) { toast.add({ title: error.value?.data?.detail || t('figure.addFailed'), color: 'red' }); return }
    onDashboard.value = true
    const id = data?.value?.id
    if (id) {
      window.dispatchEvent(new CustomEvent('artifact:created', { detail: { report_id: reportId.value, artifact_id: id } }))
      window.dispatchEvent(new CustomEvent('artifact:open', { detail: { artifact_id: id } }))
    }
  } finally { adding.value = false }
}
function onSaved() { saveOpen.value = false; savedLocal.value = true }

const menuItems = computed(() => {
  const groups: any[][] = []
  const write: any[] = []
  if (canEdit.value && vizId.value) {
    write.push(inDashboard.value
      ? { label: t('figure.addedToDashboard'), icon: 'i-heroicons-check-circle', disabled: true }
      : { label: t('figure.addToDashboard'), icon: 'i-heroicons-squares-plus', disabled: adding.value, click: addToDashboard })
  }
  if (canSave.value) {
    write.push(saved.value
      ? { label: t('figure.savedQuery'), icon: 'i-heroicons-check-badge', disabled: true }
      : { label: t('figure.saveQuery'), icon: 'i-heroicons-bookmark', click: () => { saveOpen.value = true } })
  }
  if (write.length) groups.push(write)
  const files: any[] = [{ label: t('figure.downloadCsv'), icon: 'i-heroicons-document-text', click: downloadCsv }]
  if (stepId.value && ctx?.canHydrate) files.push({ label: t('figure.downloadExcel'), icon: 'i-heroicons-table-cells', click: downloadExcel })
  if (isChart.value && view.value === 'chart' && model.value.kind !== 'other') files.push({ label: t('figure.downloadImage'), icon: 'i-heroicons-photo', click: downloadImage })
  files.push({ label: t('figure.copyData'), icon: 'i-heroicons-clipboard-document', click: copyData })
  groups.push(files)
  if (code.value) groups.push([{ label: t('figure.viewCode'), icon: 'i-heroicons-code-bracket', click: () => { codeOpen.value = true } }])
  return groups
})
</script>

<script lang="ts">
// Per page: results already drawn (no second entrance) and step loads in
// flight (no second request when the transcript re-renders).
const SHOWN = new Set<string>()
// When each figure started waiting for its refined chart: the renderer
// re-creates the block while text streams, and the wait must not restart.
const VIZ_WAIT_SINCE = new Map<string, number>()
const VIZ_WAIT_MS = 12000
const STEP_CACHE = new Map<string, Promise<any>>()
</script>

<style scoped>
.pf { position: relative; margin: 2.25em 0; }

/* Entrance (first appearance only): the figure, then its caption. */
.pf.is-entering { animation: pf-in 300ms cubic-bezier(0.2, 0.7, 0.2, 1) both; }
.pf.is-entering .pf-cap { animation: pf-fade 320ms ease-out 420ms both; }
.pf.is-entering .pf-table tbody tr { animation: pf-fade 260ms ease-out both; }
@keyframes pf-in { from { opacity: 0; transform: translateY(4px); } to { opacity: 1; transform: none; } }
@keyframes pf-fade { from { opacity: 0; } to { opacity: 1; } }
@media (prefers-reduced-motion: reduce) { .pf.is-entering, .pf.is-entering * { animation: none !important; } }

.pf-body { position: relative; }

/* Loading: a still, faint outline of the chart to come; it crossfades out. */
.pf-ghost { position: absolute; inset: 0; display: flex; gap: 7px; opacity: 0.07; }
.pf-ghost span { background: rgb(var(--bow-ink-rgb)); border-radius: 2px; }
.pf-ghost.is-bars { flex-direction: column; justify-content: space-around; padding-inline-start: 24%; }
.pf-ghost.is-bars span { height: 10px; }
.pf-ghost.is-area { align-items: flex-end; border-bottom: 1px solid rgb(var(--bow-ink-rgb)); }
.pf-ghost.is-area span { flex: 1; }
.pf-ghost-leave-active { transition: opacity 260ms ease; }
.pf-ghost-leave-to { opacity: 0; }
.pf-ghost-table { display: flex; flex-direction: column; gap: 12px; padding: 0.75rem 0; opacity: 0.07; }
.pf-ghost-table span { height: 10px; border-radius: 2px; background: rgb(var(--bow-ink-rgb)); }

/* Caption: below the figure, like a paper. */
.pf-cap {
  display: flex;
  flex-wrap: wrap;
  align-items: baseline;
  gap: 0.25rem 1rem;
  margin-top: 0.875rem;
  font-size: var(--bow-chat-font-size-3);
  line-height: 1.5;
  color: rgb(var(--bow-ink-muted-rgb));
}
.pf-cap-text { flex: 1 1 22rem; min-width: 0; }
.pf-actions { margin-inline-start: auto; }
.pf-label { font-weight: 600; color: rgb(var(--bow-ink-2-rgb)); margin-inline-end: 0.3em; }
.pf-title { color: rgb(var(--bow-ink-2-rgb)); }
.pf-legend { display: inline-flex; flex-wrap: wrap; gap: 0.25rem 0.875rem; margin-inline-start: 0.875rem; }
.pf-key { display: inline-flex; align-items: center; gap: 0.375rem; white-space: nowrap; }
.pf-key i { width: 0.5rem; height: 0.5rem; border-radius: 50%; display: inline-block; }
.pf-key.is-dim { opacity: 0.55; }

/* Controls: text, on hover or focus (always on touch). */
.pf-actions { display: inline-flex; align-items: baseline; gap: 0.5rem; white-space: nowrap; opacity: 0; transition: opacity 180ms ease; }
.pf:hover .pf-actions, .pf:focus-within .pf-actions { opacity: 1; }
@media (hover: none) { .pf-actions { opacity: 1; } }
.pf-link { color: rgb(var(--bow-ink-muted-rgb)); font-size: var(--bow-chat-font-size-3); text-decoration: underline; text-decoration-color: transparent; text-underline-offset: 3px; transition: color 120ms ease, text-decoration-color 120ms ease; }
.pf-link:hover { color: rgb(var(--bow-ink-rgb)); text-decoration-color: currentColor; }
.pf-more-btn { letter-spacing: 0.08em; text-decoration: none; }
.pf-sep { opacity: 0.5; }

/* Table: booktabs. */
.pf-table-scroll { overflow-x: auto; }
/* A table wider than the column scrolls; the far edge fades to say so. */
.pf-table-scroll.is-scrollable { mask-image: linear-gradient(to var(--pf-end, right), #000 calc(100% - 2.5rem), transparent); }
.pf-table-scroll.is-scrollable.at-end { mask-image: none; }
[dir='rtl'] .pf-table-scroll { --pf-end: left; }
.pf-table { width: 100%; border-collapse: collapse; font-size: var(--bow-chat-font-size-3); border-top: 1.5px solid rgb(var(--bow-ink-2-rgb) / 0.55); border-bottom: 1.5px solid rgb(var(--bow-ink-2-rgb) / 0.55); }
.pf-table th { padding: 0.55rem 0.75rem 0.45rem; text-align: start; font-weight: 600; color: rgb(var(--bow-ink-2-rgb)); border-bottom: 1px solid rgb(var(--bow-ink-2-rgb) / 0.25); white-space: nowrap; }
.pf-table th:first-child, .pf-table td:first-child { padding-inline-start: 0; }
.pf-table th:last-child, .pf-table td:last-child { padding-inline-end: 0; }
.pf-table th button:hover { color: rgb(var(--bow-ink-rgb)); }
.pf-table th.is-num, .pf-table td.is-num { text-align: end; }
.pf-table td { padding: 0.4rem 0.75rem; color: rgb(var(--bow-ink-2-rgb)); white-space: nowrap; }
.pf-table tbody tr:first-child td { padding-top: 0.6rem; }
.pf-table tbody tr:last-child td { padding-bottom: 0.6rem; }
.pf-table tr.is-hl td { color: rgb(var(--bow-ink-rgb)); font-weight: 600; }
.pf-empty { text-align: center; color: rgb(var(--bow-ink-muted-rgb)); padding: 1rem 0; }
.pf-more { display: inline-block; margin-top: 0.5rem; }
.pf-search { display: block; width: 100%; margin-bottom: 0.5rem; padding: 0.3rem 0; border: 0; border-bottom: 1px solid rgb(var(--bow-line-rgb)); background: transparent; outline: none; font-size: var(--bow-chat-font-size-3); color: rgb(var(--bow-ink-rgb)); }
.pf-search:focus { border-bottom-color: rgb(var(--bow-ink-muted-rgb)); }
</style>
