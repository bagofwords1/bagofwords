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

const wrapperClasses = computed(() => [
  'grid-stack-item-content',
  'rounded',
  'overflow-hidden',
  'flex',
  'flex-col',
  'relative',
  'p-0',
  'shadow-sm',
  { 'border': !props.isText, 'text-hover': props.isText && props.edit }
])

const computedStyle = computed(() => {
  const styles = Array.isArray(props.itemStyle) ? Object.assign({}, ...props.itemStyle) : (props.itemStyle || {})
  if (props.isText && props.edit) {
    return {
      ...styles,
      border: '1px solid transparent',
      '--tw-card-border': props.cardBorder || '#e5e7eb'
    }
  }
  return styles
})
</script>


