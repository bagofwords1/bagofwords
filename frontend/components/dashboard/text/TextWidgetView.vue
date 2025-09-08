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
      :view="widget.view"
      :reportThemeName="themeName"
      :reportOverrides="reportOverrides"
    />
  </div>
</template>

<script setup lang="ts">
import { defineAsyncComponent } from 'vue'
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
</script>


