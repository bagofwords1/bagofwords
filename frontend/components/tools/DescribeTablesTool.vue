<template>
  <div class="mt-1">
    <!-- Status header -->
    <div class="mb-2 flex items-center text-xs text-gray-500 cursor-pointer hover:text-gray-700">
      <span v-if="status === 'running'" class="tool-shimmer flex items-center">
        <Spinner class="w-3 h-3 mr-1.5 text-gray-400" />
        Searching {{ queryLabel }}…
      </span>
      <span v-else class="text-gray-700">Searched {{ queryLabel }}</span>
    </div>
  </div>
  
</template>

<script setup lang="ts">
import { computed } from 'vue'
import Spinner from '~/components/Spinner.vue'

interface ToolExecution {
  id: string
  tool_name: string
  tool_action?: string
  status: string
  result_summary?: string
  result_json?: any
}

interface Props {
  toolExecution: ToolExecution
}

const props = defineProps<Props>()

const status = computed<string>(() => props.toolExecution?.status || '')

const queryLabel = computed<string>(() => {
  const rj = props.toolExecution?.result_json || {}
  // Prefer explicit search_query from result
  let q: any = rj.search_query
  // Fallback to original arguments sent to tool
  if (q == null) q = (props.toolExecution as any)?.arguments_json?.query
  if (Array.isArray(q)) return q.join(', ')
  if (typeof q === 'string') return q
  if (q && typeof q === 'object') return JSON.stringify(q)
  // Fallback to summary parsing if present
  const sum = props.toolExecution?.result_summary || ''
  const m = sum.match(/^Searching\s+(.+?)…?$/)
  return m ? m[1] : 'tables'
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


