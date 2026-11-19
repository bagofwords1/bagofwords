<template>
  <div class="min-w-0">
    <ToolCallHeader :tool-execution="toolExecution" icon="heroicons-clipboard-document-check" :expanded="toolHeaderExpanded" @toggle="toolHeaderExpanded = !toolHeaderExpanded">
      <template #label>
      <template v-if="status === 'running'">{{ t('tools.getEvalRuns.listing') }}</template>
      <template v-else>{{ t('tools.getEvalRuns.listed') }}</template></template>
    </ToolCallHeader>
    <div v-show="toolHeaderExpanded" class="tool-call-details w-full min-w-0 mt-2">
      <div class="mb-2 flex flex-wrap items-center gap-2 text-chat-3 text-ink-muted">
      <template v-if="status === 'running'"></template>
      <template v-else><span v-if="items.length" class="ms-1.5 text-chat-3 text-ink-muted">· {{ items.length === 1 ? t('tools.getEvalRuns.runSingular', { count: items.length }) : t('tools.getEvalRuns.runPlural', { count: items.length }) }}</span></template></div>

    <ul v-if="items.length" class="text-chat-2 text-gray-600 dark:text-gray-400 ms-1 space-y-1 leading-snug">
      <li v-for="r in items" :key="r.run_id" class="flex items-center py-0.5 px-1 rounded">
        <Icon name="heroicons-play-circle" class="w-3 h-3 me-1 text-ink-muted flex-shrink-0" />
        <NuxtLink :to="`/evals/runs/${r.run_id}`" class="font-medium text-gray-700 dark:text-gray-300 truncate hover:text-blue-600" :title="r.title || r.run_id">
          {{ r.title || r.run_id }}
        </NuxtLink>
        <span class="ms-1.5 inline-flex px-1.5 py-0.5 rounded-full text-chat-3 font-medium flex-shrink-0" :class="runStatusClass(r.status)">{{ r.status }}</span>
        <span v-if="r.total" dir="ltr" class="ms-1.5 text-chat-3 text-gray-500 dark:text-gray-400 flex-shrink-0">{{ r.passed }}/{{ r.total }}</span>
        <span v-if="r.build_number" class="ms-1.5 text-chat-3 px-1 py-0.5 rounded bg-gray-100 dark:bg-gray-800 text-gray-500 dark:text-gray-400 flex-shrink-0">#{{ r.build_number }}</span>
      </li>
    </ul>

    <div v-if="status !== 'running' && !items.length" class="text-chat-2 text-ink-muted ms-1">
      {{ t('tools.getEvalRuns.empty') }}
    </div>
  
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

interface ToolExecution {
  id: string
  tool_name: string
  status: string
  result_json?: any
  arguments_json?: any
}

const props = defineProps<{ toolExecution: ToolExecution }>()

const status = computed(() => props.toolExecution?.status || '')
const items = computed<any[]>(() => {
  const rj: any = props.toolExecution?.result_json || {}
  return Array.isArray(rj.items) ? rj.items : []
})

function runStatusClass(s: string): string {
  if (s === 'success') return 'bg-green-100 text-green-800'
  if (s === 'error') return 'bg-red-100 text-red-800'
  if (s === 'in_progress') return 'bg-blue-50 text-blue-700'
  return 'bg-gray-100 text-gray-700'
}
</script>

<style scoped>
.tool-call-details { overflow-wrap: anywhere; }
</style>
