<template>
  <div class="widget-container">
    <!-- Widget header with title and toggle -->
    <div class="widget-header" @click="toggleCollapsed">
      <div class="flex items-center justify-between w-full">
        <div class="flex items-center">
          <Icon :name="isCollapsed ? 'heroicons-chevron-right' : 'heroicons-chevron-down'" class="w-3.5 h-3.5 mr-1.5 text-gray-500" />
          <h3 class="widget-title">{{ widgetTitle }}</h3>
        </div>
        <div v-if="rowCount" class="text-[11px] text-gray-400">
          {{ rowCount }} rows
        </div>
      </div>
    </div>

    <!-- Collapsible content -->
    <Transition name="slide-fade">
      <div v-if="!isCollapsed" class="widget-content">
        <!-- Tab Navigation -->
        <div v-if="showTabs" class="flex border-b border-gray-100 mb-2">
          <button 
            v-if="showVisual"
            @click="activeTab = 'chart'"
            :class="[
              'px-3 py-1.5 text-xs font-medium border-b-2 transition-colors',
              activeTab === 'chart' 
                ? 'border-blue-500 text-blue-600' 
                : 'border-transparent text-gray-400 hover:text-gray-600'
            ]"
          >
            Chart
          </button>
          <button 
            v-if="hasData"
            @click="activeTab = 'table'"
            :class="[
              'px-3 py-1.5 text-xs font-medium border-b-2 transition-colors',
              activeTab === 'table' 
                ? 'border-blue-500 text-blue-600' 
                : 'border-transparent text-gray-400 hover:text-gray-600'
            ]"
          >
            Data
          </button>
        </div>

        <!-- Tab Content -->
        <div class="tab-content">
          <!-- Chart Content -->
          <Transition name="fade" mode="out-in">
            <div v-if="(showTabs && activeTab === 'chart') || (!showTabs && showVisual)" class="bg-gray-50 rounded-sm p-2">
              <div v-if="chartVisualTypes.has(step?.data_model?.type)" class="h-[340px]">
                <RenderVisual :widget="widget" :data="step?.data" :data_model="step?.data_model" />
              </div>
              <div v-else-if="step?.data_model?.type === 'count'">
                <RenderCount :show_title="true" :widget="widget" :data="step?.data" :data_model="step?.data_model" />
              </div>
            </div>
          </Transition>

          <!-- Table Content -->
          <Transition name="fade" mode="out-in">
            <div v-if="(showTabs && activeTab === 'table') || (!showTabs && hasData && !showVisual)" class="h-[400px]">
              <RenderTable :widget="widget" :step="step" />
            </div>
          </Transition>
        </div>

        <!-- Bottom Action Button Placeholder -->
        <div class="mt-2 pt-2 border-t border-gray-100">
          <button class="text-[11px] text-gray-400 px-2 py-0.5 rounded hover:bg-gray-50 transition-colors" disabled>
            Action placeholder
          </button>
        </div>
      </div>
    </Transition>
  </div>
</template>

<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import RenderVisual from '../RenderVisual.vue'
import RenderCount from '../RenderCount.vue'
import RenderTable from '../RenderTable.vue'

interface ToolExecution {
  id: string
  tool_name: string
  status: string
  result_json?: any
  created_widget_id?: string
  created_step_id?: string
  created_widget?: any
  created_step?: any
}

const props = defineProps<{ toolExecution: ToolExecution }>()

// Reactive state for collapsible behavior
const isCollapsed = ref(false) // Start expanded

// Tab state - default to chart if available, otherwise table
const activeTab = ref<'chart' | 'table'>('chart')

const widget = computed(() => props.toolExecution?.created_widget || null)
const step = computed(() => props.toolExecution?.created_step || null)

// Widget title from various sources
const widgetTitle = computed(() => {
  return widget.value?.title || 
         step.value?.title || 
         props.toolExecution?.result_json?.widget_title ||
         'Results'
})

// Row count for display
const rowCount = computed(() => {
  const rows = step.value?.data?.rows
  if (Array.isArray(rows)) {
    return `${rows.length.toLocaleString()}`
  }
  return null
})

const chartVisualTypes = new Set<string>([
  'pie_chart',
  'line_chart',
  'bar_chart',
  'area_chart',
  'heatmap',
  'scatter_plot',
  'map',
  'candlestick',
  'treemap',
  'radar_chart'
])

const showVisual = computed(() => {
  const t = step.value?.data_model?.type
  return !!t && (chartVisualTypes.has(t) || t === 'count')
})

const hasData = computed(() => {
  const rows = step.value?.data?.rows
  if (Array.isArray(rows)) return rows.length >= 0
  // If structure differs, still attempt to show table; RenderTable guards internal nulls
  return !!step.value
})

// Show tabs only when both chart and table are available
const showTabs = computed(() => showVisual.value && hasData.value)

// Watch for data changes to update active tab
watch([showVisual, hasData], () => {
  if (showVisual.value) {
    activeTab.value = 'chart'
  } else if (hasData.value) {
    activeTab.value = 'table'
  }
}, { immediate: true })

function toggleCollapsed() {
  isCollapsed.value = !isCollapsed.value
}
</script>

<style scoped>
.widget-container {
  @apply mt-2 mb-2 border border-gray-100 rounded-lg bg-white shadow-sm;
}

.widget-header {
  @apply px-3 py-2 cursor-pointer hover:bg-gray-50 border-b border-gray-100 transition-colors duration-150;
}

.widget-title {
  @apply text-xs font-medium text-gray-700 select-none;
}

.widget-content {
  @apply p-3;
}

.fade-enter-active,
.fade-leave-active {
  transition: opacity 0.2s ease;
}

.fade-enter-from,
.fade-leave-to {
  opacity: 0;
}

.slide-fade-enter-active {
  transition: all 0.2s ease-out;
}

.slide-fade-leave-active {
  transition: all 0.2s ease-in;
}

.slide-fade-enter-from,
.slide-fade-leave-to {
  transform: translateY(-10px);
  opacity: 0;
}
</style>


