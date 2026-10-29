<template>
  <div class="mt-1" dir="auto">
    <div class="mb-1 flex items-center text-chat-2 text-gray-500 dark:text-gray-400 min-w-0">
      <span v-if="status === 'in_progress'" class="tool-shimmer flex items-center">
        <Spinner class="w-3 h-3 me-1 text-ink-muted" />
        {{ t('tools.readSubAgent.reading') }}
      </span>
      <template v-else>
        <Icon name="heroicons-eye" class="w-3 h-3 me-1 text-ink-muted flex-shrink-0" />
        <span class="text-gray-700 dark:text-gray-300 truncate">
          {{ t('tools.readSubAgent.checked', { title: title }) }}
          <span class="text-ink-muted">· {{ t(`tools.delegate.status.${runStatus}`, runStatus) }}</span>
          <span v-if="outcome" class="text-ink-muted">· {{ t(`tools.delegate.outcomes.${outcome}`, outcome) }}<template v-if="confidence !== null"> {{ Math.round(confidence * 100) }}%</template></span>
          <span v-if="stepsUsed !== null" class="text-ink-muted" dir="ltr">· {{ stepsUsed }} {{ t('tools.delegate.steps') }}</span>
        </span>
      </template>
    </div>
    <p v-if="latest" class="ms-4 text-2xs text-ink-muted truncate" :title="latest">{{ t('tools.readSubAgent.latest') }}: {{ latest }}</p>
  </div>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import { useI18n } from 'vue-i18n'
import Spinner from '~/components/Spinner.vue'

const { t } = useI18n()
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
