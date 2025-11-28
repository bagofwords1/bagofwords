<template>
  <!-- Edit mode - simple container without variant styling -->
  <div v-if="isEditing" class="text-block-edit h-full w-full flex flex-col">
    <TextWidgetEditor
      :textWidget="{ content: block.content, isEditing: true }"
      @save="(content) => $emit('save', content)"
      @cancel="$emit('cancel')"
      class="flex-1 min-h-0"
    />
  </div>
  <!-- Display mode - with variant styling -->
  <div 
    v-else
    class="text-block h-full w-full overflow-auto"
    :class="[variantClass, containerClass]"
  >
    <div class="prose prose-sm max-w-none" :class="proseClass" v-html="sanitizedContent" />
  </div>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import TextWidgetEditor from '@/components/TextWidgetEditor.vue'

const props = defineProps<{
  block: {
    content?: string
    variant?: 'title' | 'subtitle' | 'paragraph' | 'insight' | 'summary' | 'callout'
    view_overrides?: Record<string, any>
    isEditing?: boolean
  }
  themeName?: string | null
  reportOverrides?: Record<string, any> | null
}>()

defineEmits<{
  (e: 'save', content: string): void
  (e: 'cancel'): void
}>()

const isEditing = computed(() => props.block.isEditing ?? false)

// Content is AI-generated from our backend, so we trust it
const sanitizedContent = computed(() => props.block.content || '')

const variantClass = computed(() => {
  const v = props.block.variant
  switch (v) {
    case 'title':
      return 'text-block--title'
    case 'subtitle':
      return 'text-block--subtitle'
    case 'insight':
      return 'text-block--insight'
    case 'summary':
      return 'text-block--summary'
    case 'callout':
      return 'text-block--callout'
    default:
      return 'text-block--paragraph'
  }
})

const containerClass = computed(() => {
  const v = props.block.variant
  if (v === 'insight' || v === 'callout') {
    return 'p-4 rounded-lg border-l-4 border-blue-500 bg-blue-50/50'
  }
  if (v === 'summary') {
    return 'p-4 rounded-lg bg-gray-50/50 border border-gray-200/50'
  }
  return 'p-2'
})

const proseClass = computed(() => {
  const v = props.block.variant
  if (v === 'title') {
    return 'prose-headings:text-xl prose-headings:font-semibold prose-headings:tracking-tight prose-headings:m-0'
  }
  if (v === 'subtitle') {
    return 'prose-headings:text-sm prose-headings:font-normal prose-headings:text-gray-500 prose-headings:m-0 prose-p:text-sm prose-p:text-gray-500'
  }
  if (v === 'insight' || v === 'callout') {
    return 'prose-p:text-blue-800 prose-headings:text-blue-900'
  }
  return ''
})
</script>

<style scoped>
.text-block {
  display: flex;
  flex-direction: column;
  justify-content: center;
}

.text-block--title {
  justify-content: flex-end; /* Push content to bottom of cell */
}

.text-block--subtitle {
  justify-content: center; /* Center vertically */
}

:deep(h1) {
  font-size: 1.25rem; /* 20px */
  font-weight: 600;
  line-height: 1.3;
  margin: 0;
}

:deep(h2) {
  font-size: 1.125rem; /* 18px */
  font-weight: 600;
  line-height: 1.4;
  margin: 0;
}

:deep(h3) {
  font-size: 1rem; /* 16px */
  font-weight: 600;
  line-height: 1.4;
  margin: 0;
}

:deep(p) {
  margin: 0.5rem 0;
  line-height: 1.6;
}

:deep(p:first-child) {
  margin-top: 0;
}

:deep(p:last-child) {
  margin-bottom: 0;
}

:deep(ul), :deep(ol) {
  margin: 0.5rem 0;
  padding-left: 1.5rem;
}

:deep(li) {
  margin: 0.25rem 0;
}
</style>

