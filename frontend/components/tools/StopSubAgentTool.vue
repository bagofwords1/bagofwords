<template>
  <div class="min-w-0" dir="auto">
    <ToolCallHeader :expandable="false" :tool-execution="toolExecution" icon="heroicons-user-group" :expanded="toolHeaderExpanded" @toggle="toolHeaderExpanded = !toolHeaderExpanded">
      <template #label>
      <template v-if="status === 'in_progress'">{{ t('tools.stopSubAgent.stopping') }}</template>
      <template v-else>{{ t('tools.stopSubAgent.stopped', { title: title }) }}</template></template>
    </ToolCallHeader>
    
  </div>
</template>

<script setup lang="ts">
import ToolCallHeader from '~/components/tools/ToolCallHeader.vue'
import { ref as activityRef } from 'vue'
const toolHeaderExpanded = activityRef(false)

import { computed } from 'vue'
import { useResponseI18n } from '~/composables/useResponseI18n'

const { t } = useResponseI18n()
const props = defineProps<{ toolExecution: { status: string; result_json?: any; arguments_json?: any } }>()
const status = computed(() => props.toolExecution?.status || '')
const title = computed(() => props.toolExecution?.result_json?.title || props.toolExecution?.arguments_json?.sub_agent_id || '')
</script>
