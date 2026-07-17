<template>
  <div class="mt-1">
    <div class="mb-2 flex items-center text-xs text-gray-500 dark:text-gray-400">
      <span v-if="status === 'running'" class="tool-shimmer flex items-center">
        <Icon name="heroicons-queue-list" class="w-3 h-3 me-1 text-gray-400" />
        {{ t('tools.getEvalRuns.listing') }}
      </span>
      <span v-else class="text-gray-700 dark:text-gray-300 flex items-center">
        <Icon name="heroicons-queue-list" class="w-3 h-3 me-1 text-purple-400" />
        <span class="align-middle">{{ t('tools.getEvalRuns.listed') }}</span>
        <span v-if="items.length" class="ms-1.5 text-[10px] text-gray-400">· {{ items.length === 1 ? t('tools.getEvalRuns.runSingular', { count: items.length }) : t('tools.getEvalRuns.runPlural', { count: items.length }) }}</span>
      </span>
    </div>

    <ul v-if="items.length" class="text-xs text-gray-600 dark:text-gray-400 ms-1 space-y-1 leading-snug">
      <li v-for="r in items" :key="r.run_id" class="flex items-center py-0.5 px-1 rounded">
        <Icon name="heroicons-play-circle" class="w-3 h-3 me-1 text-gray-400 flex-shrink-0" />
        <NuxtLink :to="`/evals/runs/${r.run_id}`" class="font-medium text-gray-700 dark:text-gray-300 truncate hover:text-blue-600" :title="r.title || r.run_id">
          {{ r.title || r.run_id }}
        </NuxtLink>
        <span class="ms-1.5 inline-flex px-1.5 py-0.5 rounded-full text-[9px] font-medium flex-shrink-0" :class="runStatusClass(r.status)">{{ r.status }}</span>
        <span v-if="r.total" class="ms-1.5 text-[10px] text-gray-500 dark:text-gray-400 flex-shrink-0">{{ r.passed }}/{{ r.total }}</span>
        <span v-if="r.build_number" class="ms-1.5 text-[9px] px-1 py-0.5 rounded bg-gray-100 dark:bg-gray-800 text-gray-500 dark:text-gray-400 flex-shrink-0">#{{ r.build_number }}</span>
      </li>
    </ul>

    <div v-if="status !== 'running' && !items.length" class="text-xs text-gray-400 ms-1">
      {{ t('tools.getEvalRuns.empty') }}
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import { useI18n } from 'vue-i18n'

const { t } = useI18n()

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
.tool-shimmer {
  animation: shimmer 1.6s linear infinite;
  background: linear-gradient(90deg, rgba(0,0,0,0) 0%, rgba(160,160,160,0.15) 50%, rgba(0,0,0,0) 100%);
  background-size: 300% 100%;
  background-clip: text;
}
@keyframes shimmer {
  0% { background-position: 0% 0; }
  100% { background-position: 100% 0; }
}
</style>
