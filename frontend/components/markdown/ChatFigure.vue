<template>
  <!-- A marker that names no result renders nothing (useCitations already
       drops those; this is the last guard). -->
  <figure
    v-if="source && model.kind !== 'number'"
    ref="root"
    class="bow-figure"
    :class="{ 'is-entering': entering }"
    :data-state="state"
    :data-kind="model.kind"
    data-testid="chat-figure"
  >
    <header class="fig-head">
      <span class="fig-num nums" aria-hidden="true">{{ number }}</span>
      <div class="min-w-0 flex-1">
        <div class="fig-title" :title="title"><bdi>{{ title }}</bdi></div>
        <div class="fig-meta">
          <template v-if="legend.length > 1">
            <span v-for="s in legend" :key="s.name" class="fig-legend-item">
              <span class="fig-swatch" :style="{ background: s.color }" aria-hidden="true" /><bdi>{{ s.name }}</bdi>
            </span>
            <span class="fig-dot" aria-hidden="true">·</span>
          </template>
          <span class="nums" data-testid="chat-figure-rows" :dir="locale === 'he' ? 'rtl' : 'ltr'">{{ rowsLabel }}</span>
        </div>
      </div>
      <div class="fig-actions">
        <div v-if="isChart" class="fig-seg" role="group" :aria-label="t('figure.view')">
          <button type="button" :aria-pressed="view === 'chart'" :title="t('figure.chart')" :aria-label="t('figure.chart')" @click="view = 'chart'">
            <Icon :name="model.kind === 'line' ? 'heroicons:presentation-chart-line' : 'heroicons:chart-bar'" class="h-3.5 w-3.5" />
          </button>
          <button type="button" :aria-pressed="view === 'table'" :title="t('figure.table')" :aria-label="t('figure.table')" data-testid="chat-figure-as-table" @click="view = 'table'">
            <Icon name="heroicons:table-cells" class="h-3.5 w-3.5" />
          </button>
        </div>
        <button v-if="ctx?.open" type="button" class="fig-icon-btn" :title="t('figure.open')" :aria-label="t('figure.open')" data-testid="chat-figure-open" @click="ctx.open(source)">
          <Icon name="heroicons:arrow-up-right" class="h-3.5 w-3.5 rtl-flip" />
        </button>
      </div>
    </header>

    <!-- Chart. The body takes its final height at once — skeleton while the
         full rows load, then the chart — so the answer never jumps. -->
    <div v-if="isChart && view === 'chart'" class="fig-body" dir="ltr" :style="{ height: chartHeight + 'px' }">
      <div v-if="state === 'loading'" class="fig-skeleton" :class="model.kind === 'bars' ? 'is-bars' : 'is-area'" aria-hidden="true">
        <span v-for="i in skeletonBars" :key="i" class="bow-shimmer" :style="skeletonStyle(i)" />
      </div>
      <ClientOnly v-else-if="model.kind !== 'other'">
        <VChart class="h-full w-full" :option="chartOption" autoresize :init-options="{ renderer: 'svg' }" />
      </ClientOnly>
      <Suspense v-else>
        <RenderVisual :widget="widgetShim" :data="dataForRenderer" :data_model="dataModel" :view="rawView" />
        <template #fallback><div class="bow-shimmer h-full w-full rounded-md" /></template>
      </Suspense>
      <span v-if="state === 'loading'" class="sr-only" role="status">{{ t('figure.loading') }}</span>
    </div>

    <!-- Table: the rows as they are, searchable and sortable. -->
    <div v-else class="fig-table-wrap">
      <div v-if="state === 'loading'" class="fig-table-skeleton" aria-hidden="true">
        <span v-for="i in Math.min(model.rows.length || 4, PREVIEW_ROWS)" :key="i" class="bow-shimmer" />
      </div>
      <template v-else>
        <div v-if="tableRows.length > PREVIEW_ROWS" class="fig-search">
          <Icon name="heroicons:magnifying-glass" class="h-3.5 w-3.5 shrink-0" aria-hidden="true" />
          <input v-model="query" type="search" :placeholder="t('figure.search', { count: tableRows.length })" :aria-label="t('figure.searchLabel')" data-testid="chat-figure-search" />
        </div>
        <div class="fig-table-scroll">
          <table class="fig-table">
            <thead>
              <tr>
                <th v-for="col in model.columns" :key="col.field" :class="{ 'is-num': col.numeric }" :aria-sort="sortKey === col.field ? (sortDir === 1 ? 'ascending' : 'descending') : 'none'">
                  <button type="button" @click="toggleSort(col.field)">
                    <bdi>{{ col.label }}</bdi>
                    <Icon v-if="sortKey === col.field" :name="sortDir === 1 ? 'heroicons:arrow-up-20-solid' : 'heroicons:arrow-down-20-solid'" class="h-3 w-3" />
                  </button>
                </th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="(row, i) in visibleRows" :key="i">
                <td v-for="col in model.columns" :key="col.field" :class="{ 'is-num nums': col.numeric }">
                  <bdi>{{ cell(row[col.field], col) }}</bdi>
                </td>
              </tr>
              <tr v-if="!visibleRows.length"><td :colspan="model.columns.length" class="fig-empty">{{ t('figure.noMatch') }}</td></tr>
            </tbody>
          </table>
        </div>
        <button v-if="filteredRows.length > PREVIEW_ROWS && !query" type="button" class="fig-more" data-testid="chat-figure-more" @click="showAll = !showAll">
          {{ showAll ? t('figure.showLess') : t('figure.showAll', { count: filteredRows.length }) }}
        </button>
      </template>
    </div>
  </figure>
</template>

<script setup lang="ts">
/**
 * A result placed in an answer: `{{show:<visualization id>}}`, rewritten by
 * useCitations into `<bow-figure source="<block id>" number="n">`, a
 * markstream custom node. The page provides FIGURE_CONTEXT to resolve the
 * source (composables/figureModel.ts).
 *
 * What it draws comes from create_data's own inference — chart type, columns,
 * breakdown, units (figureModel). Bars, columns and lines are drawn here in a
 * quiet monochrome style; any other chart type uses the existing renderer in
 * the same frame. A single value is never a figure.
 *
 * Motion: the frame fades and rises once, the first time a result is shown in
 * this page; a re-render (streaming text after it) reuses the drawn figure.
 */
import { computed, inject, onMounted, ref, watch } from 'vue'
import { use } from 'echarts/core'
import { TooltipComponent, GridComponent } from 'echarts/components'
import { BarChart, LineChart } from 'echarts/charts'
import { SVGRenderer } from 'echarts/renderers'
import RenderVisual from '~/components/RenderVisual.vue'
import { FIGURE_CONTEXT, figureModel, formatFigureValue, type FigureColumn } from '~/composables/figureModel'

use([TooltipComponent, GridComponent, BarChart, LineChart, SVGRenderer])

defineOptions({ inheritAttrs: false })
const props = defineProps<{ node: any }>()
const { t, locale } = useI18n()
const colorMode = useColorMode()
const ctx = inject(FIGURE_CONTEXT, null)

const PREVIEW_ROWS = 8
const MAX_BARS = 12

const attr = (k: string) => String((props.node?.attrs || []).find((a: [string, string]) => a[0] === k)?.[1] ?? '')
const source = computed(() => ctx?.resolve(attr('source')) || null)
const number = computed(() => Number(attr('number')) || null)
const te = computed<any>(() => source.value?.toolExecution || null)
const title = computed(() => source.value?.output.title || t('figure.untitled'))

// ── Full rows: a step may carry a preview; load it once per page. ───────────
const loaded = ref<any>(null)
const failed = ref(false)
const model = computed(() => figureModel(te.value, loaded.value || undefined))
const stepId = computed(() => String(te.value?.created_step_id || te.value?.created_step?.id || ''))
const needsRows = computed(() => !loaded.value && !failed.value && model.value.partial && !!ctx?.canHydrate && !!stepId.value)
const state = computed(() => (needsRows.value ? 'loading' : 'ready'))
async function hydrate() {
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
watch(needsRows, (v) => { if (v) void hydrate() }, { immediate: true })

// ── Entrance: once per result per page. ─────────────────────────────────────
const entering = ref(false)
const firstShow = !SHOWN.has(attr('source'))
onMounted(() => {
  if (!firstShow) return
  SHOWN.add(attr('source'))
  entering.value = true
  setTimeout(() => { entering.value = false }, 400)
})

// ── View ────────────────────────────────────────────────────────────────────
const isChart = computed(() => ['bars', 'columns', 'line', 'other'].includes(model.value.kind))
const view = ref<'chart' | 'table'>('chart')
const rowsLabel = computed(() => {
  const m = model.value
  if (isChart.value && view.value === 'chart' && m.kind === 'bars' && m.categories.length > MAX_BARS) {
    return t('figure.shownOf', { shown: MAX_BARS, total: m.categories.length })
  }
  return t('figure.rows', { count: (m.totalRows || m.rows.length).toLocaleString() })
})

// ── Palette: monochrome first, from the theme tokens. ───────────────────────
const palette = ref({ ink: '#0a0a0a', muted: '#71717a', grid: '#f4f4f5', bg: '#ffffff', tip: '#0a0a0a', tipInk: '#fafafa', series: ['#0a0a0a', '#2563eb', '#a1a1aa', '#10b981', '#f59e0b', '#8b5cf6'] })
function readPalette() {
  if (typeof window === 'undefined') return
  const cs = getComputedStyle(document.documentElement)
  const rgb = (n: string, f: string) => { const v = cs.getPropertyValue(n).trim(); return v ? `rgb(${v.split(/\s+/).join(',')})` : f }
  const dark = colorMode.value === 'dark'
  const ink = rgb('--bow-ink-rgb', dark ? '#fafafa' : '#0a0a0a')
  palette.value = {
    ink,
    muted: rgb('--bow-ink-muted-rgb', '#71717a'),
    grid: rgb('--bow-line-subtle-rgb', '#f4f4f5'),
    bg: rgb('--bow-bg-rgb', '#ffffff'),
    tip: rgb('--bow-bg-inverted-rgb', '#0a0a0a'),
    tipInk: rgb('--bow-ink-inverted-rgb', '#fafafa'),
    series: [ink, dark ? '#60a5fa' : '#2563eb', dark ? '#71717a' : '#a1a1aa', '#10b981', '#f59e0b', '#8b5cf6'],
  }
}
onMounted(readPalette)
watch(() => colorMode.value, () => requestAnimationFrame(readPalette))
const legend = computed(() => model.value.series.map((s, i) => ({ name: s.name, color: palette.value.series[i % palette.value.series.length] })))

const reducedMotion = typeof window !== 'undefined' && !!window.matchMedia?.('(prefers-reduced-motion: reduce)').matches
const fmt = (v: any, short = false) => formatFigureValue(v, model.value.format, model.value.currency, locale.value, short)
// Mirror with the answer, not the UI: an English answer in a Hebrew UI keeps
// its bars left to right. The figure's own direction is the answer's.
const root = ref<HTMLElement | null>(null)
const answerDir = ref<'ltr' | 'rtl'>('ltr')
function readDir() { if (root.value) answerDir.value = getComputedStyle(root.value).direction === 'rtl' ? 'rtl' : 'ltr' }
// The renderer stamps the answer's dir on its blocks just after they mount.
onMounted(() => { readDir(); requestAnimationFrame(readDir); setTimeout(readDir, 300); setTimeout(readDir, 1200) })
const isRtl = computed(() => answerDir.value === 'rtl')

const barCount = computed(() => Math.min(model.value.categories.length, MAX_BARS))
const chartHeight = computed(() => (model.value.kind === 'bars' ? Math.max(96, barCount.value * 30) : 240))
const skeletonBars = computed(() => (model.value.kind === 'bars' ? Math.max(3, barCount.value) : 12))
function skeletonStyle(i: number) {
  if (model.value.kind === 'bars') return { width: `${Math.max(18, 92 - i * (70 / Math.max(3, barCount.value)))}%` }
  return { height: `${30 + ((i * 37) % 55)}%` }
}

const chartOption = computed(() => {
  const p = palette.value
  const m = model.value
  const font = 'Inter, ui-sans-serif, system-ui, sans-serif'
  const animate = !reducedMotion && firstShow
  const tooltip = {
    trigger: 'axis', confine: true,
    axisPointer: { type: m.kind === 'line' ? 'line' : 'shadow', lineStyle: { color: p.muted, width: 1, type: [3, 3] }, shadowStyle: { color: 'rgba(120,120,130,0.08)' } },
    backgroundColor: p.tip, borderWidth: 0, padding: [6, 10],
    textStyle: { color: p.tipInk, fontSize: 12, fontFamily: font },
    extraCssText: 'border-radius:6px;box-shadow:0 4px 14px rgba(0,0,0,.16);',
    formatter: (items: any[]) => {
      const head = `<div style="opacity:.65;margin-bottom:2px">${escapeHtml(items[0]?.name ?? '')}</div>`
      return head + items.map((it) => `<div style="display:flex;align-items:center;gap:6px"><span style="width:8px;height:8px;border-radius:2px;background:${it.color}"></span>${items.length > 1 ? `<span style="opacity:.75">${escapeHtml(it.seriesName)}</span>` : ''}<b style="margin-inline-start:auto;font-weight:600">${fmt(it.value)}</b></div>`).join('')
    },
  }
  const base = { animation: animate, animationDuration: 450, animationEasing: 'cubicOut', animationDurationUpdate: 240, tooltip, textStyle: { fontFamily: font } }

  if (m.kind === 'bars') {
    const cats = m.categories.slice(0, MAX_BARS)
    const values = m.series[0]?.values.slice(0, MAX_BARS) || []
    return {
      ...base,
      grid: { left: isRtl.value ? 56 : 8, right: isRtl.value ? 8 : 56, top: 0, bottom: 0, containLabel: true },
      xAxis: { type: 'value', show: false, axisLabel: { show: false }, splitLine: { show: false }, inverse: isRtl.value },
      yAxis: {
        type: 'category', inverse: true, position: isRtl.value ? 'right' : 'left', data: cats,
        axisLine: { show: false }, axisTick: { show: false },
        axisLabel: { color: p.muted, fontSize: 12, margin: 12, width: 132, overflow: 'truncate', align: isRtl.value ? 'left' : 'right' },
      },
      series: [{
        type: 'bar', data: values, barWidth: 14, barMinHeight: 2,
        itemStyle: { color: p.ink, borderRadius: isRtl.value ? [3, 0, 0, 3] : [0, 3, 3, 0] },
        label: { show: true, position: isRtl.value ? 'left' : 'right', distance: 8, color: p.ink, fontSize: 12, fontWeight: 500, formatter: (x: any) => fmt(x.value, true) },
        emphasis: { itemStyle: { opacity: 0.85 } },
      }],
    }
  }

  const many = m.categories.length > 8
  const xAxis = {
    type: 'category', data: m.categories, inverse: isRtl.value, boundaryGap: m.kind === 'columns',
    axisLine: { lineStyle: { color: p.grid } }, axisTick: { show: false },
    axisLabel: { color: p.muted, fontSize: 11, margin: 10, hideOverlap: true },
  }
  const yAxis = {
    type: 'value', position: isRtl.value ? 'right' : 'left', splitNumber: 4,
    axisLabel: { color: p.muted, fontSize: 11, formatter: (v: number) => fmt(v, true) },
    splitLine: { lineStyle: { color: p.grid } },
  }
  const grid = { left: 8, right: 12, top: 8, bottom: 0, containLabel: true }
  if (m.kind === 'columns') {
    return {
      ...base, grid, xAxis, yAxis,
      series: m.series.map((s, i) => ({
        type: 'bar', name: s.name, data: s.values, stack: m.stacked ? 'total' : undefined,
        barMaxWidth: m.series.length > 1 ? 14 : 28, barGap: '20%',
        itemStyle: { color: legend.value[i]?.color, borderRadius: m.stacked && i < m.series.length - 1 ? 0 : [3, 3, 0, 0] },
        emphasis: { itemStyle: { opacity: 0.85 } },
      })),
    }
  }
  const single = m.series.length === 1
  return {
    ...base, grid, xAxis, yAxis,
    series: m.series.map((s, i) => {
      const color = legend.value[i]?.color
      return {
        type: 'line', name: s.name, data: s.values, smooth: 0.2, showSymbol: !many, symbol: 'circle', symbolSize: 5, connectNulls: true,
        lineStyle: { color, width: 1.75 }, itemStyle: { color },
        stack: m.stacked ? 'total' : undefined,
        areaStyle: single || m.area ? { color: { type: 'linear', x: 0, y: 0, x2: 0, y2: 1, colorStops: [{ offset: 0, color: alpha(color, 0.14) }, { offset: 1, color: alpha(color, 0) }] } } : undefined,
        emphasis: { focus: 'series' },
      }
    }),
  }
})
function alpha(color: string, a: number) {
  const m = /rgb\((\d+),\s*(\d+),\s*(\d+)\)/.exec(color)
  if (m) return `rgba(${m[1]},${m[2]},${m[3]},${a})`
  const h = /^#([0-9a-f]{6})$/i.exec(color)
  if (h) { const n = parseInt(h[1], 16); return `rgba(${n >> 16},${(n >> 8) & 255},${n & 255},${a})` }
  return color
}
function escapeHtml(s: string) { return String(s).replace(/[&<>"]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]!)) }

// ── Fallback renderer (types not drawn here) ────────────────────────────────
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
const query = ref('')
const showAll = ref(false)
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
    rows = [...rows].sort((a, b) => {
      const x = a[k]; const y = b[k]
      if (x == null) return 1
      if (y == null) return -1
      return (typeof x === 'number' && typeof y === 'number' ? x - y : String(x).localeCompare(String(y))) * sortDir.value
    })
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
</script>

<script lang="ts">
// Per page: results already drawn (no second entrance) and step loads in
// flight (no second request when the transcript re-renders).
const SHOWN = new Set<string>()
const STEP_CACHE = new Map<string, Promise<any>>()
</script>

<style scoped>
.bow-figure {
  margin: 1.25rem 0 1.5rem;
  border-radius: var(--bow-radius-lg);
  border: 1px solid rgb(var(--bow-line-rgb));
  background: rgb(var(--bow-bg-rgb));
  overflow: hidden;
  font-size: var(--bow-chat-font-size-3);
}
.bow-figure.is-entering { animation: fig-in 320ms cubic-bezier(0.2, 0.7, 0.2, 1) both; }
@keyframes fig-in { from { opacity: 0; transform: translateY(6px); } to { opacity: 1; transform: none; } }
@media (prefers-reduced-motion: reduce) { .bow-figure.is-entering { animation: none; } }

.fig-head {
  display: flex;
  align-items: flex-start;
  gap: 0.625rem;
  padding: 0.75rem 0.875rem 0.625rem 1rem;
}
.fig-num {
  flex-shrink: 0;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  min-width: 1.25rem;
  height: 1.25rem;
  margin-top: 0.0625rem;
  padding: 0 0.3rem;
  border-radius: var(--bow-radius-sm);
  border: 1px solid rgb(var(--bow-line-rgb));
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  font-size: 0.6875rem;
  font-weight: 500;
  color: rgb(var(--bow-ink-muted-rgb));
}
.fig-title {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-size: var(--bow-chat-font-size-2);
  font-weight: 600;
  line-height: 1.45;
  letter-spacing: -0.01em;
  color: rgb(var(--bow-ink-rgb));
}
.fig-meta {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 0.25rem 0.625rem;
  margin-top: 0.0625rem;
  color: rgb(var(--bow-ink-muted-rgb));
}
.fig-legend-item { display: inline-flex; align-items: center; gap: 0.375rem; }
.fig-swatch { width: 0.5rem; height: 0.5rem; border-radius: 2px; }
.fig-dot { opacity: 0.6; }
.fig-actions { display: flex; align-items: center; gap: 0.25rem; }
.fig-seg {
  display: inline-flex;
  padding: 2px;
  border-radius: var(--bow-radius-md);
  border: 1px solid rgb(var(--bow-line-rgb));
}
.fig-seg button, .fig-icon-btn {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  height: 1.5rem;
  width: 1.625rem;
  border-radius: var(--bow-radius-sm);
  color: rgb(var(--bow-ink-muted-rgb));
  transition: color 120ms ease, background-color 120ms ease;
}
.fig-seg button:hover, .fig-icon-btn:hover { color: rgb(var(--bow-ink-rgb)); }
.fig-seg button[aria-pressed='true'] { background: rgb(var(--bow-bg-muted-rgb)); color: rgb(var(--bow-ink-rgb)); }
.fig-icon-btn { height: 1.75rem; width: 1.75rem; border-radius: var(--bow-radius-md); border: 1px solid rgb(var(--bow-line-rgb)); }

.fig-body { position: relative; margin: 0.25rem 1rem 1rem; }
.fig-skeleton { position: absolute; inset: 0; display: flex; gap: 6px; }
.fig-skeleton.is-bars { flex-direction: column; justify-content: space-around; padding-inline-start: 22%; }
.fig-skeleton.is-bars span { height: 14px; border-radius: 3px; }
.fig-skeleton.is-area { align-items: flex-end; }
.fig-skeleton.is-area span { flex: 1; border-radius: 3px 3px 0 0; }

/* Table */
.fig-table-wrap { border-top: 1px solid rgb(var(--bow-line-rgb)); }
.fig-search {
  display: flex;
  align-items: center;
  gap: 0.5rem;
  height: 2.25rem;
  padding: 0 1rem;
  border-bottom: 1px solid rgb(var(--bow-line-subtle-rgb));
  color: rgb(var(--bow-ink-subtle-rgb));
}
.fig-search input { flex: 1; min-width: 0; border: 0; outline: none; background: transparent; font-size: inherit; color: rgb(var(--bow-ink-rgb)); }
.fig-search input::placeholder { color: rgb(var(--bow-ink-subtle-rgb)); }
.fig-table-scroll { overflow-x: auto; }
.fig-table { width: 100%; border-collapse: collapse; }
.fig-table th {
  padding: 0.5rem 1rem;
  text-align: start;
  font-weight: 500;
  white-space: nowrap;
  color: rgb(var(--bow-ink-muted-rgb));
  background: rgb(var(--bow-bg-subtle-rgb));
  border-bottom: 1px solid rgb(var(--bow-line-rgb));
}
.fig-table th button { display: inline-flex; align-items: center; gap: 0.25rem; }
.fig-table th button:hover { color: rgb(var(--bow-ink-rgb)); }
.fig-table th.is-num, .fig-table td.is-num { text-align: end; }
.fig-table th.is-num button { flex-direction: row-reverse; }
.fig-table td {
  padding: 0.5rem 1rem;
  white-space: nowrap;
  color: rgb(var(--bow-ink-2-rgb));
  border-bottom: 1px solid rgb(var(--bow-line-subtle-rgb));
}
.fig-table tbody tr:last-child td { border-bottom: 0; }
.fig-table tbody tr:hover td { background: rgb(var(--bow-bg-subtle-rgb)); }
.fig-empty { text-align: center; color: rgb(var(--bow-ink-muted-rgb)); padding: 1.25rem 0; }
.fig-more {
  display: block;
  width: 100%;
  padding: 0.5rem 1rem;
  text-align: center;
  font-weight: 500;
  color: rgb(var(--bow-ink-muted-rgb));
  border-top: 1px solid rgb(var(--bow-line-rgb));
  transition: color 120ms ease, background-color 120ms ease;
}
.fig-more:hover { color: rgb(var(--bow-ink-rgb)); background: rgb(var(--bow-bg-subtle-rgb)); }
.fig-table-skeleton { display: flex; flex-direction: column; gap: 10px; padding: 0.875rem 1rem; }
.fig-table-skeleton span { height: 12px; border-radius: 3px; }
</style>
