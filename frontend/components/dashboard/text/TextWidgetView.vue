<template>
  <div class="flex-grow min-h-0" :class="{ 'p-1': widget.isEditing, 'p-2 overflow-auto': !widget.isEditing }">
    <TextWidgetEditor
      v-if="widget.isEditing"
      :textWidget="widget"
      @save="(content) => $emit('save', content, widget)"
      @cancel="$emit('cancel', widget)"
      class="flex-grow min-h-0"
    />
    <component
      v-else
      :is="getCompForType('text_widget')"
      :key="`${widget.id}:${themeName}`"
      :widget="widget"
      :step="widget"
      :view="resolvedView"
      :reportThemeName="themeName"
      :reportOverrides="reportOverrides"
    />
  </div>
</template>

<script setup lang="ts">
import { defineAsyncComponent, computed } from 'vue'
import TextWidgetEditor from '@/components/TextWidgetEditor.vue'
import { resolveEntryByType } from '@/components/dashboard/registry'

const props = defineProps<{
  widget: any
  themeName: string
  reportOverrides: any
}>()

defineEmits<{
  (e: 'save', content: string, widget: any): void
  (e: 'cancel', widget: any): void
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
  const stepView = (props.widget as any)?.view || null
  const layoutOverrides = (props.widget as any)?.layout_view_overrides || null
  if (!layoutOverrides && !stepView) return null
  return deepMerge(stepView || {}, layoutOverrides || {})
})
</script>


