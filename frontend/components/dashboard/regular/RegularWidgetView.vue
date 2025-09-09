<template>
  <div class="flex-grow overflow-auto p-2 min-h-0">
    <div v-if="resolvedComp" class="mt-1 h-full">
      <component
        :key="`${widget.id}:${themeName}`"
        :is="resolvedComp"
        :widget="widget"
        :data="widget.last_step?.data"
        :data_model="widget.last_step?.data_model"
        :step="widget.last_step"
        :view="resolvedView"
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

const resolvedComp = computed(() => getCompForType(props.widget?.last_step?.data_model?.type))

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
  const layoutOverrides = props.widget?.layout_view_overrides || null
  if (!layoutOverrides && !stepView) return null
  return deepMerge(stepView || {}, layoutOverrides || {})
})
</script>


