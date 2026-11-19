<template>
  <span v-if="live && !hasSteps" role="status" :aria-label="translate('prompt.thinking')" class="inline-flex items-center py-2" data-testid="run-thinking">
    <span class="h-3 w-3 rounded-full bg-ink" aria-hidden="true" />
  </span>
  <button v-else-if="hasSteps" type="button" :aria-expanded="expanded"
    class="run-fold-toggle my-1 inline-flex w-fit max-w-full self-start items-center gap-1.5 rounded py-0.5 text-start text-chat text-ink-muted hover:text-ink focus:outline-none focus-visible:ring-1 focus-visible:ring-blue-500"
    data-testid="run-fold-toggle" :data-live="live ? '' : undefined" @click="$emit('toggle')">
    <span v-if="live">{{ translate('reportView.runFold.working') }}</span>
    <span v-else-if="duration" class="nums">{{ translate('reportView.runFold.workedFor', { duration }) }}</span>
    <span v-else>{{ translate('reportView.runFold.worked') }}</span>
    <Icon :name="expanded ? 'heroicons-chevron-down' : 'heroicons-chevron-right'" class="h-3 w-3 shrink-0 rtl-flip" aria-hidden="true" />
  </button>
</template>

<script setup lang="ts">
/** A run only offers activity disclosure once a tool step exists. */
defineProps<{
  expanded: boolean
  duration: string
  translate: (key: string, values?: any) => string
  live?: boolean
  hasSteps: boolean
}>()
defineEmits<{ (e: 'toggle'): void }>()
</script>

<style scoped>
.run-fold-toggle {
  flex-shrink: 0;
}
</style>
