<template>
  <div class="mt-1">
    <div class="mb-2 flex items-center text-xs text-gray-500">
      <span v-if="status === 'running'" class="tool-shimmer flex items-center">
        <Icon name="heroicons-magnifying-glass" class="w-3 h-3 me-1 text-gray-400" />
        Searching evals {{ queryLabel ? `for ${queryLabel}` : '' }}
      </span>
      <span v-else class="text-gray-700 flex items-center">
        <Icon name="heroicons-magnifying-glass" class="w-3 h-3 me-1 text-gray-400" />
        <span class="align-middle">Searched evals {{ queryLabel ? `for ${queryLabel}` : '' }}</span>
        <span v-if="total > 0" class="ms-1.5 text-[10px] text-gray-400">· {{ total }} match{{ total === 1 ? '' : 'es' }}</span>
      </span>
    </div>

    <div v-if="items.length" class="text-xs text-gray-600">
      <ul class="ms-1 space-y-1 leading-snug">
        <li v-for="item in items" :key="item.id" class="flex items-center py-1 px-1 rounded">
          <Icon name="heroicons-beaker" class="w-3 h-3 me-1 text-purple-400 flex-shrink-0" />
          <NuxtLink :to="`/evals`" class="font-medium text-gray-700 truncate hover:text-blue-600" :title="item.prompt_content || item.name">
            {{ item.name || '(unnamed)' }}
          </NuxtLink>
          <span v-if="item.suite_name" class="ms-1.5 text-[9px] px-1 py-0.5 rounded bg-gray-100 text-gray-500 flex-shrink-0">{{ item.suite_name }}</span>
          <span v-if="item.status === 'draft'" class="ms-1 text-[9px] px-1 py-0.5 rounded bg-amber-100 text-amber-800 flex-shrink-0">draft</span>
          <span v-else-if="item.status === 'archived'" class="ms-1 text-[9px] px-1 py-0.5 rounded bg-gray-200 text-gray-700 flex-shrink-0">archived</span>
          <span v-if="item.auto_generated" class="ms-1 text-[9px] px-1 py-0.5 rounded bg-purple-100 text-purple-800 flex-shrink-0">auto</span>
        </li>
      </ul>
    </div>

    <div v-if="status !== 'running' && !items.length" class="text-xs text-gray-400 ms-1">
      No matching evals.
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed } from 'vue'

interface ToolExecution {
  id: string
  tool_name: string
  status: string
  result_json?: any
  arguments_json?: any
}

const props = defineProps<{ toolExecution: ToolExecution }>()

const status = computed(() => props.toolExecution?.status || '')

const queryLabel = computed<string>(() => {
  const q = props.toolExecution?.arguments_json?.query
  if (typeof q === 'string' && q.trim()) return `"${q}"`
  return ''
})

const items = computed<any[]>(() => {
  const rj: any = props.toolExecution?.result_json || {}
  return Array.isArray(rj.items) ? rj.items : []
})

const total = computed<number>(() => {
  const rj: any = props.toolExecution?.result_json || {}
  return typeof rj.total === 'number' ? rj.total : items.value.length
})
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
