<template>
  <div class="flex-grow overflow-auto p-2 min-h-0">
    <div v-if="isTable" class="mt-1 h-full">
      <component
        :is="tableComp"
        :widget="widget"
        :step="{ ...(widget.last_step || {}), data_model: { ...(widget.last_step?.data_model || {}), type: 'table' } }"
        :view="finalView"
        :reportThemeName="themeName"
        :reportOverrides="reportOverrides"
      />
    </div>
    <div v-else-if="resolvedComp" class="mt-1 h-full">
      <component
        :key="`${widget.id}:${themeName}`"
        :is="resolvedComp"
        :widget="widget"
        :data="widget.last_step?.data"
        :data_model="widget.last_step?.data_model"
        :step="widget.last_step"
        :view="finalView"
        :reportThemeName="themeName"
        :reportOverrides="reportOverrides"
      />
    </div>
    <div v-else-if="widget.last_step?.type == 'init'" class="text-center items-center flex flex-col justify-center h-full text-gray-500">
      <SpinnerComponent />
      <span class="mt-2 text-sm">Loading...</span>
    </div>
    <div v-else class="text-center items-center flex flex-col justify-center h-full text-gray-400 italic text-sm">
      No data or visualization available.
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, defineAsyncComponent } from 'vue'
import SpinnerComponent from '@/components/SpinnerComponent.vue'
import { resolveEntryByType } from '@/components/dashboard/registry'
import TableAgGrid from '@/components/dashboard/table/TableAgGrid.vue'

const props = defineProps<{
  widget: any
  themeName: string
  reportOverrides: any
}>()

const compCache = new Map<string, any>()
function getCompForType(type?: string | null) {
  const t = (type || '').toLowerCase()
  if (!t) return null as any
  if (compCache.has(t)) return compCache.get(t)
  const entry = resolveEntryByType(t)
  if (!entry) return null as any
  const comp = defineAsyncComponent(entry.load)
  compCache.set(t, comp)
  return comp
}

const resolvedComp = computed(() => {
  const vType = props.widget?.view?.type
  const dmType = props.widget?.last_step?.data_model?.type
  return getCompForType(String(vType || dmType || ''))
})
const isTable = computed(() => String(props.widget?.view?.type || props.widget?.last_step?.data_model?.type || '').toLowerCase() === 'table')
const tableComp = TableAgGrid

function deepMerge(target: any, source: any) {
  const out: any = Array.isArray(target) ? [...target] : { ...target }
  if (!source || typeof source !== 'object') return out
  Object.keys(source).forEach((key) => {
    const sv: any = (source as any)[key]
    if (sv && typeof sv === 'object' && !Array.isArray(sv)) {
      out[key] = deepMerge(out[key] || {}, sv)
    } else {
      out[key] = sv
    }
  })
  return out
}

const resolvedView = computed(() => {
  const stepView = props.widget?.last_step?.view || null
  const vizView = props.widget?.view || null
  const layoutOverrides = props.widget?.layout_view_overrides || null
  if (!layoutOverrides && !vizView && !stepView) return null
  // Merge order: step.view -> viz.view -> layout overrides (each overrides previous)
  const mergedStepViz = deepMerge(stepView || {}, vizView || {})
  return deepMerge(mergedStepViz, layoutOverrides || {})
})

// Prefer explicit widget.view (already merged in DashboardComponent) when available
const finalView = computed(() => {
  return (props.widget?.view && Object.keys(props.widget.view || {}).length > 0)
    ? props.widget.view
    : (resolvedView.value || null)
})
</script>


