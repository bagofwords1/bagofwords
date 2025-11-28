<template>
  <div 
    class="card-block h-full w-full flex flex-col overflow-hidden"
    :class="[borderClass, backgroundClass]"
    :style="cardStyle"
  >
    <!-- Card Header -->
    <div 
      v-if="showHeader" 
      class="card-header flex-shrink-0 px-6 pt-5 pb-1"
    >
      <div class="flex items-center justify-between">
        <div class="flex-1 min-w-0">
          <h3 v-if="chrome?.title" class="text-base font-semibold text-gray-900 truncate">
            {{ chrome.title }}
          </h3>
          <p v-if="chrome?.subtitle" class="text-sm text-gray-500 mt-1">
            {{ chrome.subtitle }}
          </p>
        </div>
        <!-- Optional: Action slot for buttons -->
        <slot name="actions" />
      </div>
    </div>

    <!-- Card Content -->
    <div class="card-content flex-1 min-h-0 overflow-auto" :class="contentPaddingClass">
      <slot />
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed } from 'vue'

interface ContainerChrome {
  title?: string
  subtitle?: string
  showHeader?: boolean
  border?: 'none' | 'soft' | 'strong'
  padding?: number
  background?: string
}

const props = defineProps<{
  block: {
    chrome?: ContainerChrome
    view_overrides?: Record<string, any>
    children?: Array<{ visualization_id?: string; type?: string }>
  }
  themeName?: string | null
  reportOverrides?: Record<string, any> | null
  // When true, hides header if content is metric_card (to avoid duplicate titles)
  contentIsMetricCard?: boolean
}>()

const chrome = computed(() => props.block.chrome || {})

const showHeader = computed(() => {
  // Explicitly disabled
  if (chrome.value.showHeader === false) return false
  // Hide for metric cards to avoid duplicate titles
  if (props.contentIsMetricCard) return false
  return !!(chrome.value.title || chrome.value.subtitle)
})

const borderClass = computed(() => {
  const border = chrome.value.border || 'none'
  switch (border) {
    case 'strong':
      return 'rounded-xl border-2 border-gray-300 shadow-md'
    case 'soft':
      return 'rounded-xl border border-gray-200/60 shadow-sm'
    case 'none':
    default:
      return 'rounded-xl'
  }
})

const backgroundClass = computed(() => {
  if (chrome.value.background) return ''
  return 'bg-white'
})

const contentPaddingClass = computed(() => {
  // Metric cards have their own padding, don't double up
  if (props.contentIsMetricCard) return ''
  // If no header, add padding all around
  if (!showHeader.value) return 'p-6'
  return 'px-6 pb-5'
})

const cardStyle = computed(() => {
  const style: Record<string, string> = {}
  if (chrome.value.background) {
    style.backgroundColor = chrome.value.background
  }
  return style
})
</script>

<style scoped>
.card-block {
  transition: box-shadow 0.2s ease, border-color 0.2s ease;
}

.card-block:hover {
  box-shadow: 0 4px 12px rgba(0, 0, 0, 0.05);
}

.card-content {
  display: flex;
  flex-direction: column;
}
</style>

