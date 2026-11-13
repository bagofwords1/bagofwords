<template>
  <!-- The one empty state for the console: a soft icon tile, a title, one
       line of help and (optionally) the action that fills it. -->
  <div class="flex flex-col items-center justify-center text-center px-6" :class="compact ? 'py-10' : 'py-16'">
    <div class="flex items-center justify-center w-10 h-10 rounded-lg bg-surface-muted text-ink-muted">
      <UIcon :name="icon" class="w-5 h-5" />
    </div>
    <h3 class="mt-4 text-sm font-semibold text-ink">{{ title }}</h3>
    <p v-if="description" class="mt-1 max-w-sm text-sm text-ink-muted leading-relaxed">{{ description }}</p>
    <div v-if="$slots.default" class="mt-5">
      <slot />
    </div>
  </div>
</template>

<script setup lang="ts">
defineProps<{
  icon: string
  title: string
  description?: string
  compact?: boolean
}>()
</script>
