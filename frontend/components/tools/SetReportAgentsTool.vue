<template>
  <div class="mt-1 text-xs">
    <div class="flex items-center text-gray-700 dark:text-gray-300">
      <Icon name="heroicons-view-columns" class="w-3 h-3 me-1 text-gray-400 flex-shrink-0" />
      <span v-if="status === 'running'" class="tool-shimmer">Setting agent focus…</span>
      <span v-else-if="names.length">Focused on <span class="font-medium">{{ names.join(', ') }}</span></span>
      <span v-else>Cleared agent focus — back to Auto</span>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed } from 'vue'

const props = defineProps<{ toolExecution: { status: string; result_json?: any } }>()
const status = computed<string>(() => props.toolExecution?.status || '')
const names = computed<string[]>(() => {
  const rj: any = props.toolExecution?.result_json || {}
  return Array.isArray(rj.focused_agent_names) ? rj.focused_agent_names : []
})
</script>

<style scoped>
.tool-shimmer {
  animation: shimmer 1.6s linear infinite;
  background: linear-gradient(90deg, rgba(0,0,0,0) 0%, rgba(160,160,160,0.15) 50%, rgba(0,0,0,0) 100%);
  background-size: 300% 100%;
  background-clip: text;
}
@keyframes shimmer { 0% { background-position: 0% 0; } 100% { background-position: 100% 0; } }
</style>
