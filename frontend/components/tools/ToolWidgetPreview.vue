<template>
  <div class="widget-container">
    <!-- Widget header with title and toggle -->
    <div class="widget-header" @click="toggleCollapsed">
      <div class="flex items-center justify-between w-full">
        <div class="flex items-center">
          <Icon :name="isCollapsed ? 'heroicons-chevron-right' : 'heroicons-chevron-down'" class="w-3.5 h-3.5 mr-1.5 text-gray-500" />
          <h3 class="widget-title">{{ widgetTitle }}</h3>
        </div>
        <div class="flex items-center gap-2">
          <div v-if="rowCount" class="text-[11px] text-gray-400">
            {{ rowCount }} rows
          </div>
          <button 
            v-if="hasDataForDownload"
            @click.stop="downloadCSV"
            class="text-gray-400 hover:text-gray-600 transition-colors"
            title="Download CSV"
          >
            <Icon name="heroicons:arrow-down-tray" class="w-3.5 h-3.5" />
          </button>
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
              <div v-if="resolvedCompEl" class="h-[340px]">
                <component
                  :is="resolvedCompEl"
                  :widget="widget"
                  :data="step?.data"
                  :data_model="step?.data_model"
                  :step="step"
                  :view="step?.view"
                />
              </div>
              <div v-else-if="chartVisualTypes.has(step?.data_model?.type)" class="h-[340px]">
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

        <!-- Bottom Action Button -->
        <div class="mt-2 pt-2 border-t border-gray-100 flex justify-start">
          <button
            v-if="!isPublished"
            :disabled="!canAdd || isAdding"
            @click="onAddClick"
            class="text-xs px-2 py-0.5 rounded transition-colors"
            :class="[
              canAdd && !isAdding ? 'hover:bg-gray-50' : 'text-gray-400 cursor-not-allowed'
            ]"
          >
            <Icon v-if="isAdding" name="heroicons-arrow-path" class="w-3.5 h-3.5 inline-block mr-1 animate-spin" />
            <span v-if="!canAdd">Generating…</span>
            <span v-else class="flex items-center">
                <Icon name="heroicons-chart-pie" class="w-3.5 h-3.5 text-blue-500 inline-block mr-1" />
                Add to dashboard</span>
          </button>
          <span v-else class="text-xs flex items-center">
            <Icon name="heroicons-check" class="w-3.5 h-3.5 mr-1 text-green-500" />
            Added to dashboard</span>
        </div>
      </div>
    </Transition>
  </div>
</template>

<script setup lang="ts">
import { computed, ref, watch, defineAsyncComponent, onMounted } from 'vue'
import { useRoute } from 'vue-router'
import { useMyFetch } from '~/composables/useMyFetch'
import RenderVisual from '../RenderVisual.vue'
import RenderCount from '../RenderCount.vue'
import RenderTable from '../RenderTable.vue'
import { resolveEntryByType } from '@/components/dashboard/registry'

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
const emit = defineEmits(['addWidget'])

// Reactive state for collapsible behavior
const isCollapsed = ref(false) // Start expanded
const isAdding = ref(false)
const layoutBlocks = ref<any[]>([])
const route = useRoute()
const reportId = computed(() => String(route.params.id || ''))

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
  if (!t) return false
  const entry = resolveEntryByType(String(t).toLowerCase())
  if (entry) {
    // treat table as data-only; everything else is a visual
    return entry.componentKey !== 'table.aggrid'
  }
  return chartVisualTypes.has(String(t)) || String(t) === 'count'
})

// Dashboard registry-driven dynamic component
const compCache = new Map<string, any>()
function getCompForType(type?: string | null) {
  const t = (type || '').toLowerCase()
  if (!t) return null
  if (compCache.has(t)) return compCache.get(t)
  const entry = resolveEntryByType(t)
  if (!entry) return null
  const comp = defineAsyncComponent(entry.load)
  compCache.set(t, comp)
  return comp
}
const resolvedCompEl = computed(() => getCompForType(step.value?.data_model?.type))

// Determine if table/data is present
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

// CSV download functionality
const hasDataForDownload = computed(() => {
  const rows = step.value?.data?.rows
  return Array.isArray(rows) && rows.length > 0
})

function downloadCSV() {
  const rows = step.value?.data?.rows
  const columns = step.value?.data?.columns
  
  if (!Array.isArray(rows) || !Array.isArray(columns) || rows.length === 0) {
    return
  }

  // Create CSV content
  const headers = columns.map(col => col.field || col.headerName || col.colId || '').join(',')
  const csvRows = rows.map(row => 
    columns.map(col => {
      const field = col.field || col.colId
      const value = row[field] || ''
      // Escape quotes and wrap in quotes if contains comma or quote
      const stringValue = String(value)
      if (stringValue.includes(',') || stringValue.includes('"') || stringValue.includes('\n')) {
        return `"${stringValue.replace(/"/g, '""')}"`
      }
      return stringValue
    }).join(',')
  )
  
  const csvContent = [headers, ...csvRows].join('\n')
  
  // Create and trigger download
  const blob = new Blob([csvContent], { type: 'text/csv;charset=utf-8;' })
  const link = document.createElement('a')
  const url = URL.createObjectURL(blob)
  link.setAttribute('href', url)
  link.setAttribute('download', `${widgetTitle.value || 'data'}.csv`)
  link.style.visibility = 'hidden'
  document.body.appendChild(link)
  link.click()
  document.body.removeChild(link)
}

// Add-to-dashboard gating and action
const isPublished = computed(() => {
  const id = widget.value?.id
  if (!id) return false
  return layoutBlocks.value.some(b => b?.type === 'widget' && b?.widget_id === id)
})
const canAdd = computed(() => !!(widget.value?.id && step.value?.status === 'success'))

async function onAddClick() {
  if (!canAdd.value || isAdding.value) return
  isAdding.value = true
  try {
    emit('addWidget', { widget: widget.value, step: step.value })
    // Best-effort: refresh membership shortly after parent patches layout
    setTimeout(refreshMembership, 600)
  } finally {
    // Let parent control final state; keep short throttle to avoid double clicks
    setTimeout(() => { isAdding.value = false }, 800)
  }
}

async function refreshMembership() {
  try {
    if (!reportId.value) return
    const { data, error } = await useMyFetch(`/api/reports/${reportId.value}/layouts`, { method: 'GET' })
    if (error.value) throw error.value
    const layouts = Array.isArray(data.value) ? data.value : []
    const active = layouts.find((l: any) => l.is_active)
    layoutBlocks.value = active?.blocks || []
  } catch (e) {
    // noop
  }
}

onMounted(() => {
  refreshMembership()
})
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


