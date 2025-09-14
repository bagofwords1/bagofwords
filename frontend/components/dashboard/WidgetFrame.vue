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

const noBorder = computed(() => {
  try {
    const cb = (props as any)?.widget?.view?.style?.cardBorder
    return typeof cb === 'string' && cb.trim().toLowerCase() === 'none'
  } catch {
    return false
  }
})

const wrapperClasses = computed(() => [
  'grid-stack-item-content',
  'rounded',
  'overflow-hidden',
  'flex',
  'flex-col',
  'relative',
  'p-0',
  { 'shadow-sm': !noBorder.value, 'border': !props.isText && !noBorder.value, 'text-hover': props.isText && props.edit }
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
  if (noBorder.value) {
    return {
      ...styles,
      border: 'none'
    }
  }
  return styles
})
</script>


