<template>
  <div :class="wrapperClasses" :style="computedStyle">
    <slot />
  </div>
</template>

<script setup lang="ts">
import { computed } from 'vue'

const props = defineProps<{
  widget: any
  edit: boolean
  isText: boolean
  itemStyle: any
  cardBorder?: string
}>()

// Frame is now transparent - individual components handle their own styling
const wrapperClasses = computed(() => [
  'grid-stack-item-content',
  'overflow-hidden',
  'flex',
  'flex-col',
  'relative',
  'p-0',
  { 'text-hover': props.isText && props.edit }
])

const computedStyle = computed(() => {
  // Only apply text edit border, otherwise let components style themselves
  if (props.isText && props.edit) {
    return {
      border: '1px solid transparent',
      '--tw-card-border': props.cardBorder || '#e5e7eb',
      backgroundColor: 'transparent'
    }
  }
    return {
    backgroundColor: 'transparent',
      border: 'none'
  }
})
</script>


