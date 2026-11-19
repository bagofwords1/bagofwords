<template>
  <ToolCallHeader class="run-step" :label="label" :icon="group.icon" :running="group.running" :error="!!group.failed"
    :expanded="expanded" data-testid="run-tool-group" @toggle="$emit('toggle')" />
</template>
<script setup lang="ts">
import ToolCallHeader from '~/components/tools/ToolCallHeader.vue'
import { computed } from 'vue'
import type { RunToolGroup } from '~/composables/useRunActivity'
const props = defineProps<{ group: RunToolGroup; expanded: boolean; translate: (key: string, values?: any) => string }>()
defineEmits<{ (e: 'toggle'): void }>()
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

