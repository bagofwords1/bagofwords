<template>
  <figure class="doc-viz my-6" :class="{ 'doc-viz--tall': isChart }">
    <!-- Render error / missing data are quiet cards, never a broken page -->
    <div
      v-if="renderFailed || !viz"
      class="flex flex-col items-center justify-center gap-1 rounded-lg border border-dashed border-gray-200 dark:border-gray-700 bg-gray-50/50 dark:bg-gray-800/40 py-8 px-4"
    >
      <Icon name="heroicons:chart-bar" class="w-5 h-5 text-gray-300 dark:text-gray-600" />
      <span class="text-xs text-gray-400 dark:text-gray-500">
        {{ !viz ? $t('docViewer.vizUnavailable') : $t('docViewer.vizRenderFailed') }}
      </span>
      <span v-if="viz?.title" class="text-[11px] text-gray-300 dark:text-gray-600">{{ viz.title }}</span>
    </div>

    <template v-else>
      <div
        class="rounded-lg border border-gray-100 dark:border-gray-800 bg-white dark:bg-gray-900 overflow-hidden"
      >
        <div v-if="viz.title" class="px-4 pt-3 pb-1 text-[13px] font-medium text-gray-700 dark:text-gray-300">
          {{ viz.title }}
        </div>
        <!-- Chart types -->
        <div v-if="isChart" class="h-[320px] px-2 pb-2">
          <Suspense>
            <RenderVisual
              :widget="widgetShim"
              :data="dataShim"
              :data_model="viz.dataModel"
              :view="viz.view"
            />
            <template #fallback>
              <div class="flex items-center justify-center w-full h-full">
                <Spinner class="w-5 h-5 text-gray-300" />
              </div>
            </template>
          </Suspense>
        </div>
        <!-- Count / metric card -->
        <div v-else-if="isCount" class="px-4 pb-4">
          <RenderCount
            :widget="widgetShim"
            :data="dataShim"
            :data_model="viz.dataModel"
            :view="viz.view"
          />
        </div>
        <!-- Table (default). `v-else`: a chart and a count card ARE the
             rendering of their data — without it every chart in the document
             also carried a redundant grid of its own rows underneath. -->
        <!-- On paper the grid is the wrong renderer: AG Grid virtualizes rows
             inside a fixed-height scroller, so a print would carry only the
             handful of rows that happened to be mounted. Lay every row out as
             a real table and let it break across pages. -->
        <table v-else-if="paper" class="doc-viz-paper-table">
          <thead>
            <tr>
              <th v-for="col in paperColumns" :key="col.field" :class="{ 'is-num': col.numeric }">
                {{ col.label }}
              </th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="(row, ri) in paperRows" :key="ri">
              <td v-for="col in paperColumns" :key="col.field" :class="{ 'is-num': col.numeric }">
                {{ formatCell(row[col.field]) }}
              </td>
            </tr>
          </tbody>
        </table>
        <!-- RenderTable/AgGrid is h-full, so the container MUST have an
             explicit height or the grid collapses to 0px. -->
        <div v-else :style="{ height: tableHeight }">
          <RenderTable :widget="widgetShim" :step="tableStepShim" />
        </div>
      </div>
      <figcaption v-if="caption" class="mt-1.5 text-center text-[11px] text-gray-400 dark:text-gray-500">
        {{ caption }}
      </figcaption>
    </template>
  </figure>
</template>

<script setup lang="ts">
import { computed, ref, onErrorCaptured } from 'vue'
import RenderVisual from '~/components/RenderVisual.vue'
import RenderTable from '~/components/RenderTable.vue'
import RenderCount from '~/components/RenderCount.vue'
import Spinner from '~/components/Spinner.vue'

// Shape produced by ArtifactFrame.fetchData / public share hydration
interface DocViz {
  id: string
  title?: string
  view?: any
  rows?: any[]
  columns?: any[]
  dataModel?: any
  stepStatus?: string
}

const props = defineProps<{
  viz: DocViz | null
  caption?: string
  /** Rendering onto paper (PDF export): no scrollers, no virtualization. */
  paper?: boolean
}>()

const renderFailed = ref(false)
// A single broken chart must never take down the document.
onErrorCaptured(() => {
  renderFailed.value = true
  return false
})

const CHART_TYPES = new Set([
  'pie_chart', 'line_chart', 'bar_chart', 'area_chart', 'heatmap',
  'scatter_plot', 'map', 'candlestick', 'treemap', 'radar_chart',
])

const vizType = computed(() => {
  const view = props.viz?.view as any
  const t = view?.view?.type || view?.type || props.viz?.dataModel?.type
  return String(t || 'table').toLowerCase()
})

const isChart = computed(() => CHART_TYPES.has(vizType.value))
const isCount = computed(() => vizType.value === 'count' || vizType.value === 'metric_card')

const widgetShim = computed(() => ({ id: props.viz?.id, title: props.viz?.title || '' }))
const dataShim = computed(() => ({
  rows: props.viz?.rows || [],
  columns: props.viz?.columns || [],
}))
const tableStepShim = computed(() => ({
  status: props.viz?.stepStatus || 'success',
  data: { rows: props.viz?.rows || [], columns: props.viz?.columns || [] },
  data_model: { ...(props.viz?.dataModel || {}), type: 'table' },
}))

// ---- Paper table -----------------------------------------------------------
// Columns come from the step's own column list; a column whose values are
// numeric is right-aligned, the one bit of formatting a data table cannot do
// without and still read as one.
const paperRows = computed(() => props.viz?.rows || [])

const paperColumns = computed(() => {
  const columns = props.viz?.columns || []
  const fields = columns.length
    ? columns.map((c: any) => ({ field: c.field, label: c.headerName || c.field }))
    : Object.keys(paperRows.value[0] || {}).map(f => ({ field: f, label: f }))
  return fields.map(f => ({
    ...f,
    numeric: paperRows.value.some(r => typeof r?.[f.field] === 'number'),
  }))
})

function formatCell(value: any): string {
  if (value === null || value === undefined) return ''
  if (typeof value === 'number') return value.toLocaleString()
  if (typeof value === 'object') return JSON.stringify(value)
  return String(value)
}

// Explicit height for the table container (AgGrid needs a sized parent).
// header (~44px) + rows * ~34px, clamped so small tables stay compact and
// large ones scroll internally instead of dominating the document.
const tableHeight = computed(() => {
  const n = props.viz?.rows?.length || 0
  const px = Math.min(Math.max(44 + n * 34, 140), 440)
  return `${px}px`
})
</script>

<style scoped>
/* Paper rendering of a table visualization. Mirrors the document's own table
   styling (DocViewer) so an embedded query result and a markdown table read as
   one document, and repeats the header on every page it spills onto. */
.doc-viz-paper-table {
  width: 100%;
  border-collapse: collapse;
  font-size: 0.9em;
}
.doc-viz-paper-table thead { display: table-header-group; }
.doc-viz-paper-table tr { break-inside: avoid; }
.doc-viz-paper-table th {
  text-align: start;
  font-weight: 600;
  color: rgb(55 65 81);
  padding: 0.4em 0.6em;
  border-bottom: 1.5px solid rgb(209 213 219);
  white-space: nowrap;
}
.doc-viz-paper-table td {
  padding: 0.35em 0.6em;
  border-bottom: 1px solid rgb(243 244 246);
  vertical-align: top;
}
.doc-viz-paper-table th.is-num,
.doc-viz-paper-table td.is-num {
  text-align: end;
  font-variant-numeric: tabular-nums;
}
</style>
