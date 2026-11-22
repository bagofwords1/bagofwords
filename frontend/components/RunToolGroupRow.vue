<template>
  <ToolCallHeader class="run-step" :icon="group.icon" :running="group.running" :error="!!group.failed"
    :expanded="expanded" data-testid="run-tool-group" @toggle="$emit('toggle')">
    <!-- One line on purpose: a line break between nodes here would be eaten by Vue's whitespace condensing. -->
    <template #label>{{ label }}<span v-if="failedNote" class="run-group-failed" data-testid="run-tool-group-failed">{{ ' · ' + failedNote }}</span></template>
  </ToolCallHeader>
</template>
<script setup lang="ts">
import ToolCallHeader from '~/components/tools/ToolCallHeader.vue'
import { computed } from 'vue'
import type { RunToolGroup } from '~/composables/useRunActivity'
const props = defineProps<{ group: RunToolGroup; expanded: boolean; translate: (key: string, values?: any) => string }>()
defineEmits<{ (e: 'toggle'): void }>()
// Several calls with at least one failure: count them, even when the run
// recovered (the latest call succeeded and the row shows no warning icon).
// A single failed call already reads as failed from its own label + icon.
const failedNote = computed(() => {
  const { group, translate } = props
  return group.blockIds.length > 1 && group.failedCount > 0
    ? translate('reportView.runFold.failedCount', { count: group.failedCount })
    : ''
})
const label = computed(() => {
  const { group, translate } = props
  if (!group.settled || group.running || group.blockIds.length === 1) {
    const { name, title, status } = group.current
    if (name === 'create_data' && title) {
      if (status === 'success') return translate('tools.createData.createdTitled', { title })
      if (status === 'error' || status === 'failed') return translate('reportView.runFold.steps.dataFailed', { title })
      return translate('tools.createData.creatingTitled', { title })
    }
    if (title) return title
  }
  return group.categories.map(category => translate(`reportView.runFold.groups.${category}`, { count: group.counts[category] })).join(', ')
})
</script>

<style scoped>
/* Amber like the row's warning icon; keeps its colour through the running shimmer. */
.run-group-failed { color: rgb(217 119 6); -webkit-text-fill-color: rgb(217 119 6); }
</style>
