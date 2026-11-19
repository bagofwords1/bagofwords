<template>
  <div class="min-w-0" dir="auto">
    <ToolCallHeader :tool-execution="toolExecution" icon="heroicons-user-group" :expanded="toolHeaderExpanded" @toggle="toolHeaderExpanded = !toolHeaderExpanded">
      <template #label>
      <template v-if="status === 'in_progress'">{{ t('tools.readSubAgent.reading') }}</template>
      <template v-else>{{ t('tools.readSubAgent.checked', { title: title }) }}
          · {{ t(`tools.delegate.status.${runStatus}`, runStatus) }}<template v-if="outcome">· {{ t(`tools.delegate.outcomes.${outcome}`, outcome) }}<template v-if="confidence !== null">{{ Math.round(confidence * 100) }}%</template></template>
      <template v-if="stepsUsed !== null">· {{ stepsUsed }} {{ t('tools.delegate.steps') }}</template></template></template>
    </ToolCallHeader>
    <div v-show="toolHeaderExpanded" class="tool-call-details w-full min-w-0 mt-2">
    <p v-if="latest" class="ms-4 text-chat-2 text-ink-muted truncate" :title="latest">{{ t('tools.readSubAgent.latest') }}: {{ latest }}</p>
  
    </div>
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
const rj = computed<any>(() => props.toolExecution?.result_json || {})
const title = computed(() => rj.value?.title || props.toolExecution?.arguments_json?.sub_agent_id || '')
const runStatus = computed(() => rj.value?.status === 'waiting' && rj.value?.wait_reason === 'slot' ? 'pending' : (rj.value?.status || 'running'))
const outcome = computed(() => rj.value?.record?.outcome || null)
const confidence = computed(() => (typeof rj.value?.record?.confidence === 'number' ? rj.value.record.confidence : null))
const stepsUsed = computed(() => (typeof rj.value?.steps_used === 'number' ? rj.value.steps_used : null))
const latest = computed(() => {
  const calls = rj.value?.latest_calls || []
  const last = calls[calls.length - 1]
  return last ? `${last.title || last.tool}` : ''
})
</script>
