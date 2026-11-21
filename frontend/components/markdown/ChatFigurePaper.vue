<template>
  <figure
    v-if="source && model.kind !== 'number'"
    ref="root"
    class="pf"
    :class="[`pf--${variant}`, { 'is-entering': entering }]"
    :data-state="state"
    :data-kind="model.kind"
    data-testid="chat-figure"
  >
    <!-- Medium: controls float top-end on hover; paper keeps them in the caption. -->
    <div v-if="variant === 'medium'" class="pf-actions pf-actions--float">
      <button v-if="isChart" type="button" class="pf-link" data-testid="chat-figure-as-table" @click="view = view === 'chart' ? 'table' : 'chart'">{{ view === 'chart' ? t('figure.table') : t('figure.chart') }}</button>
      <button v-if="ctx?.open" type="button" class="pf-link" data-testid="chat-figure-open" @click="ctx.open(source)">{{ t('figure.open') }} <span class="rtl-flip inline-block">↗</span></button>
    </div>

    <!-- Chart -->
    <div v-if="isChart && view === 'chart'" class="pf-body" dir="ltr" :style="{ height: chartHeight + 'px' }">
      <div v-if="state === 'loading'" class="pf-ghost" :class="model.kind === 'bars' ? 'is-bars' : 'is-area'" aria-hidden="true">
        <span v-for="i in ghostCount" :key="i" :style="ghostStyle(i)" />
      </div>
      <ClientOnly v-else-if="model.kind !== 'other'">
        <VChart class="h-full w-full" :option="chartOption" autoresize :init-options="{ renderer: 'svg' }" />
      </ClientOnly>
      <Suspense v-else>
        <RenderVisual :widget="widgetShim" :data="dataForRenderer" :data_model="dataModel" :view="rawView" />
        <template #fallback><div class="h-full w-full" /></template>
      </Suspense>
      <span v-if="state === 'loading'" class="sr-only" role="status">{{ t('figure.loading') }}</span>
    </div>

    <!-- Table: booktabs — three hairlines, nothing else. -->
    <div v-else class="pf-table-wrap">
      <div v-if="state === 'loading'" class="pf-ghost-table" aria-hidden="true"><span v-for="i in 5" :key="i" /></div>
      <template v-else>
        <input v-if="showAll && tableRows.length > PREVIEW_ROWS" v-model="query" type="search" class="pf-search" :placeholder="t('figure.search', { count: tableRows.length })" :aria-label="t('figure.searchLabel')" data-testid="chat-figure-search" />
        <div class="pf-table-scroll">
          <table class="pf-table">
            <thead>
              <tr>
                <th v-for="col in model.columns" :key="col.field" :class="{ 'is-num': col.numeric }" :aria-sort="sortKey === col.field ? (sortDir === 1 ? 'ascending' : 'descending') : 'none'">
                  <button type="button" @click="toggleSort(col.field)"><bdi>{{ col.label }}</bdi><span v-if="sortKey === col.field" aria-hidden="true">{{ sortDir === 1 ? ' ↑' : ' ↓' }}</span></button>
                </th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="(row, i) in visibleRows" :key="i" :class="{ 'is-hl': isHighlighted(row) }">
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
        <span class="pf-label nums">{{ variant === 'medium' ? `${number} ·` : t('figure.caption', { n: number }) }}</span>
        <bdi class="pf-title">{{ title }}</bdi><template v-if="captionDetail"><span class="pf-detail">, <bdi>{{ captionDetail }}</bdi></span></template><span v-if="variant !== 'medium'">.</span>
        <span v-if="legend.length > 1" class="pf-legend">
          <span v-for="s in legend" :key="s.name" class="pf-key"><i :style="{ background: s.color }" aria-hidden="true" /><bdi>{{ s.name }}</bdi></span>
        </span>
      </span>
      <span v-if="variant !== 'medium'" class="pf-actions">
        <button v-if="isChart" type="button" class="pf-link" data-testid="chat-figure-as-table" @click="view = view === 'chart' ? 'table' : 'chart'">{{ view === 'chart' ? t('figure.table') : t('figure.chart') }}</button>
        <span v-if="isChart && ctx?.open" class="pf-sep" aria-hidden="true">·</span>
        <button v-if="ctx?.open" type="button" class="pf-link" data-testid="chat-figure-open" @click="ctx.open(source)">{{ t('figure.open') }} <span class="rtl-flip inline-block">↗</span></button>
      </span>
    </figcaption>
  </figure>
</template>

<script setup lang="ts">
/**
 * DESIGN MOCKUP — three "figure in an essay" variants of ChatFigure, chosen
 * with localStorage['bow.figureStyle'] = 'medium' | 'paper' | 'serif'
 * (plugins/markdown-mermaid-png.ts). No frame: whitespace is the frame, the
 * caption sits below, controls are text that appears on hover. One muted
 * accent hue for a single series, a small editorial palette for several, and
 * an emphasised category in full colour with the rest tinted.
 *
 * Mockup-only: localStorage['bow.figureHighlight'] emphasises a category
 * (production would take the marker's `highlight` attribute).
 */
import { computed, inject, onMounted, ref, watch } from 'vue'
import { use } from 'echarts/core'
import { TooltipComponent, GridComponent, MarkPointComponent } from 'echarts/components'
import { BarChart, LineChart } from 'echarts/charts'
import { SVGRenderer } from 'echarts/renderers'
import RenderVisual from '~/components/RenderVisual.vue'
import { FIGURE_CONTEXT, figureModel, formatFigureValue, type FigureColumn } from '~/composables/figureModel'

use([TooltipComponent, GridComponent, MarkPointComponent, BarChart, LineChart, SVGRenderer])

defineOptions({ inheritAttrs: false })
const props = defineProps<{ node: any }>()
const { t, locale } = useI18n()
const colorMode = useColorMode()
const ctx = inject(FIGURE_CONTEXT, null)

const PREVIEW_ROWS = 6
const MAX_BARS = 8
const ls = (k: string) => { try { return localStorage.getItem(k) || '' } catch { return '' } }
const variant = (['medium', 'paper', 'serif'].includes(ls('bow.figureStyle')) ? ls('bow.figureStyle') : 'paper') as 'medium' | 'paper' | 'serif'

const attr = (k: string) => String((props.node?.attrs || []).find((a: [string, string]) => a[0] === k)?.[1] ?? '')
const source = computed(() => ctx?.resolve(attr('source')) || null)
const number = computed(() => Number(attr('number')) || 1)
const te = computed<any>(() => source.value?.toolExecution || null)
const title = computed(() => source.value?.output.title || t('figure.untitled'))

// ── Rows (same as ChatFigure) ───────────────────────────────────────────────
const loaded = ref<any>(null)
const failed = ref(false)
const model = computed(() => figureModel(te.value, loaded.value || undefined))
const stepId = computed(() => String(te.value?.created_step_id || te.value?.created_step?.id || ''))
const needsRows = computed(() => !loaded.value && !failed.value && model.value.partial && !!ctx?.canHydrate && !!stepId.value)
const state = computed(() => (needsRows.value ? 'loading' : 'ready'))
watch(needsRows, async (v) => {
  if (!v) return
  try {
    const { data, error }: any = await useMyFetch(`/api/steps/${stepId.value}`, { method: 'GET' })
    if (error?.value) throw error.value
    const d = (data.value as any)?.data
    if (d?.rows) loaded.value = d
    else failed.value = true
  } catch { failed.value = true }
}, { immediate: true })

const entering = ref(false)
onMounted(() => { entering.value = true; setTimeout(() => { entering.value = false }, 360) })

// ── View & caption ──────────────────────────────────────────────────────────
const isChart = computed(() => ['bars', 'columns', 'line', 'other'].includes(model.value.kind))
const view = ref<'chart' | 'table'>('chart')
const captionDetail = computed(() => {
  const m = model.value
  if (view.value === 'chart' && m.kind === 'bars' && m.categories.length > MAX_BARS) return t('figure.topOf', { shown: MAX_BARS, total: m.categories.length })
  if ((view.value === 'table' || m.kind === 'table') && m.rows.length > PREVIEW_ROWS && !showAll.value) return t('figure.firstOf', { shown: PREVIEW_ROWS, total: m.totalRows || m.rows.length })
  return ''
})

// ── Direction follows the answer ────────────────────────────────────────────
const root = ref<HTMLElement | null>(null)
const answerDir = ref<'ltr' | 'rtl'>('ltr')
function readDir() { if (root.value) answerDir.value = getComputedStyle(root.value).direction === 'rtl' ? 'rtl' : 'ltr' }
onMounted(() => { readDir(); requestAnimationFrame(readDir); setTimeout(readDir, 300); setTimeout(readDir, 1200) })
const isRtl = computed(() => answerDir.value === 'rtl')

// ── Palette: one muted accent; an editorial set for several series ──────────
const dark = computed(() => colorMode.value === 'dark')
const SERIES_LIGHT = ['#5B6CF0', '#E0A43B', '#3A9188', '#D66A7E', '#8B93A7']
const SERIES_DARK = ['#8391FF', '#F0B95A', '#55B8AC', '#EC8DA0', '#A6ADC2']
const series = computed(() => (dark.value ? SERIES_DARK : SERIES_LIGHT))
const accent = computed(() => series.value[0])
const ink = ref({ text: '#374151', muted: '#667085', grid: 'rgba(102,112,133,0.18)', bg: '#ffffff', tip: '#111827', tipInk: '#f9fafb' })
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
const legend = computed(() => model.value.series.map((s, i) => ({ name: s.name, color: series.value[i % series.value.length] })))
const highlight = computed(() => new Set((attr('highlight') || ls('bow.figureHighlight')).split(',').map((s) => s.trim()).filter(Boolean)))
const anyHl = computed(() => model.value.categories.some((c) => highlight.value.has(c)))

function alpha(hex: string, a: number) {
  const n = parseInt(hex.slice(1), 16)
  return `rgba(${n >> 16},${(n >> 8) & 255},${n & 255},${a})`
}
const reducedMotion = typeof window !== 'undefined' && !!window.matchMedia?.('(prefers-reduced-motion: reduce)').matches
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
    formatter: (items: any[]) => `<div style="opacity:.65;margin-bottom:2px">${items[0]?.name ?? ''}</div>` + items.map((it) => `<div style="display:flex;gap:8px;align-items:center"><span style="width:8px;height:8px;border-radius:50%;background:${it.color}"></span>${items.length > 1 ? `<span style="opacity:.75">${it.seriesName}</span>` : ''}<b style="margin-inline-start:auto;font-weight:600">${fmt(it.value)}</b></div>`).join(''),
  }
  const base = { animation: !reducedMotion, animationDuration: 520, animationEasing: 'cubicOut', tooltip, textStyle: { fontFamily: font } }

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
  if (m.kind === 'columns') {
    return {
      ...base, grid, xAxis, yAxis,
      series: m.series.map((s, i) => ({
        type: 'bar', name: s.name, data: s.values, stack: m.stacked ? 'total' : undefined,
        barMaxWidth: m.series.length > 1 ? 12 : 18, barGap: '30%',
        itemStyle: { color: legend.value[i]?.color, borderRadius: [2, 2, 0, 0] },
        emphasis: { disabled: true },
      })),
    }
  }
  const single = m.series.length === 1
  return {
    ...base, grid, xAxis, yAxis,
    series: m.series.map((s, i) => {
      const color = legend.value[i]?.color || accent.value
      const last = s.values.length - 1
      return {
        type: 'line', name: s.name, data: s.values, smooth: 0.25, showSymbol: false, connectNulls: true,
        lineStyle: { color, width: 1.75 }, itemStyle: { color },
        areaStyle: single ? { color: { type: 'linear', x: 0, y: 0, x2: 0, y2: 1, colorStops: [{ offset: 0, color: alpha(color, 0.12) }, { offset: 1, color: alpha(color, 0) }] } } : undefined,
        // The latest value, labelled at the end of its line.
        markPoint: last >= 0 && s.values[last] != null ? {
          symbol: 'circle', symbolSize: 6, itemStyle: { color, borderColor: p.bg, borderWidth: 2 },
          label: { show: true, position: 'right', distance: 6, color, fontSize: 11, fontWeight: 600, formatter: () => fmt(s.values[last], true) },
          data: [{ coord: [last, s.values[last]] }],
        } : undefined,
        emphasis: { disabled: true },
      }
    }),
  }
})

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
    rows = [...rows].sort((a, b) => { const x = a[k]; const y = b[k]; if (x == null) return 1; if (y == null) return -1; return (typeof x === 'number' && typeof y === 'number' ? x - y : String(x).localeCompare(String(y))) * sortDir.value })
  }
  return rows
})
const visibleRows = computed(() => (showAll.value || query.value ? filteredRows.value : filteredRows.value.slice(0, PREVIEW_ROWS)))
const isHighlighted = (row: Record<string, any>) => !!highlight.value.size && Object.values(row).some((v) => highlight.value.has(String(v)))
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

<style scoped>
.pf {
  position: relative;
  margin: 2.25em 0 2.25em;
}
.pf.is-entering { animation: pf-in 340ms ease-out both; }
@keyframes pf-in { from { opacity: 0; } to { opacity: 1; } }
@media (prefers-reduced-motion: reduce) { .pf.is-entering { animation: none; } }

.pf-body { position: relative; }

/* Loading: a still, faint outline of the chart to come. */
.pf-ghost { position: absolute; inset: 0; display: flex; gap: 7px; opacity: 0.07; }
.pf-ghost span { background: rgb(var(--bow-ink-rgb)); border-radius: 2px; }
.pf-ghost.is-bars { flex-direction: column; justify-content: space-around; padding-inline-start: 24%; }
.pf-ghost.is-bars span { height: 10px; }
.pf-ghost.is-area { align-items: flex-end; border-bottom: 1px solid rgb(var(--bow-ink-rgb)); }
.pf-ghost.is-area span { flex: 1; }
.pf-ghost-table { display: flex; flex-direction: column; gap: 12px; padding: 0.75rem 0; opacity: 0.07; }
.pf-ghost-table span { height: 10px; border-radius: 2px; background: rgb(var(--bow-ink-rgb)); }

/* Caption: below the figure. */
.pf-cap {
  display: flex;
  align-items: baseline;
  gap: 1rem;
  margin-top: 0.875rem;
  font-size: var(--bow-chat-font-size-3);
  line-height: 1.5;
  color: rgb(var(--bow-ink-muted-rgb));
}
.pf-cap-text { flex: 1; min-width: 0; }
.pf-label { font-weight: 600; color: rgb(var(--bow-ink-2-rgb)); margin-inline-end: 0.3em; }
.pf-title { color: rgb(var(--bow-ink-2-rgb)); }
.pf-legend { display: inline-flex; flex-wrap: wrap; gap: 0.25rem 0.875rem; margin-inline-start: 0.875rem; }
.pf-key { display: inline-flex; align-items: center; gap: 0.375rem; white-space: nowrap; }
.pf-key i { width: 0.5rem; height: 0.5rem; border-radius: 50%; display: inline-block; }

.pf-actions { display: inline-flex; align-items: baseline; gap: 0.5rem; white-space: nowrap; opacity: 0; transition: opacity 180ms ease; }
.pf:hover .pf-actions, .pf:focus-within .pf-actions { opacity: 1; }
@media (hover: none) { .pf-actions { opacity: 1; } }
.pf-link { color: rgb(var(--bow-ink-muted-rgb)); font-size: var(--bow-chat-font-size-3); text-decoration: underline; text-decoration-color: transparent; text-underline-offset: 3px; transition: color 120ms ease, text-decoration-color 120ms ease; }
.pf-link:hover { color: rgb(var(--bow-ink-rgb)); text-decoration-color: currentColor; }
.pf-sep { opacity: 0.5; }

/* Medium: centred caption, no "Figure" word, controls float top-end. */
.pf--medium .pf-cap { justify-content: center; text-align: center; }
.pf--medium .pf-cap-text { flex: none; }
.pf--medium .pf-label { font-weight: 500; color: rgb(var(--bow-ink-muted-rgb)); }
.pf--medium .pf-title { color: rgb(var(--bow-ink-muted-rgb)); }
.pf--medium .pf-legend { display: flex; justify-content: center; margin: 0.375rem 0 0; }
.pf-actions--float { position: absolute; top: -1.75em; inset-inline-end: 0; z-index: 1; }

/* Serif: the caption set like a journal. */
.pf--serif .pf-cap { font-family: 'Source Serif 4', 'Source Serif Pro', ui-serif, Georgia, 'Times New Roman', serif; font-size: calc(var(--bow-chat-font-size) * 0.93); }
.pf--serif .pf-label { font-variant: small-caps; letter-spacing: 0.04em; font-weight: 600; }
.pf--serif .pf-title { font-style: italic; }
.pf--serif .pf-actions, .pf--serif .pf-link { font-family: Inter, ui-sans-serif, system-ui, sans-serif; }

/* Table: booktabs. */
.pf-table-scroll { overflow-x: auto; }
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
.pf--serif .pf-table { font-family: 'Source Serif 4', ui-serif, Georgia, serif; font-size: calc(var(--bow-chat-font-size) * 0.93); }
.pf-empty { text-align: center; color: rgb(var(--bow-ink-muted-rgb)); padding: 1rem 0; }
.pf-more { display: inline-block; margin-top: 0.5rem; }
.pf-search { display: block; width: 100%; margin-bottom: 0.5rem; padding: 0.3rem 0; border: 0; border-bottom: 1px solid rgb(var(--bow-line-rgb)); background: transparent; outline: none; font-size: var(--bow-chat-font-size-3); color: rgb(var(--bow-ink-rgb)); }
.pf-search:focus { border-bottom-color: rgb(var(--bow-ink-muted-rgb)); }
</style>
