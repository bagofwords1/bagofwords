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
        <!-- Table (default) — RenderTable/AgGrid is h-full, so the container
             MUST have an explicit height or the grid collapses to 0px. -->
        <div :style="{ height: tableHeight }">
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

const props = defineProps<{ viz: DocViz | null; caption?: string }>()

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

// Explicit height for the table container (AgGrid needs a sized parent).
// header (~44px) + rows * ~34px, clamped so small tables stay compact and
// large ones scroll internally instead of dominating the document.
const tableHeight = computed(() => {
  const n = props.viz?.rows?.length || 0
  const px = Math.min(Math.max(44 + n * 34, 140), 440)
  return `${px}px`
})
</script>
