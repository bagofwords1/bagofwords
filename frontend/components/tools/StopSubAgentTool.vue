<template>
  <div class="mt-1" dir="auto">
    <div class="mb-1 flex items-center text-xs text-gray-500 dark:text-gray-400 min-w-0">
      <span v-if="status === 'in_progress'" class="tool-shimmer flex items-center">
        <Spinner class="w-3 h-3 me-1 text-ink-muted" />
        {{ t('tools.stopSubAgent.stopping') }}
      </span>
      <template v-else>
        <Icon name="heroicons-stop-circle" class="w-3 h-3 me-1 text-ink-muted flex-shrink-0" />
        <span class="text-gray-700 dark:text-gray-300 truncate">{{ t('tools.stopSubAgent.stopped', { title: title }) }}</span>
      </template>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import { useI18n } from 'vue-i18n'
import Spinner from '~/components/Spinner.vue'

const { t } = useI18n()
const props = defineProps<{ toolExecution: { status: string; result_json?: any; arguments_json?: any } }>()
const status = computed(() => props.toolExecution?.status || '')
const title = computed(() => props.toolExecution?.result_json?.title || props.toolExecution?.arguments_json?.sub_agent_id || '')
</script>
