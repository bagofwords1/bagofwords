<template>
  <div class="mt-1">
    <div class="mb-2 flex items-center text-xs text-gray-700 dark:text-gray-300">
      <Icon
        :name="status === 'running' ? 'heroicons-arrow-path' : 'heroicons-arrows-right-left'"
        class="w-3 h-3 me-1 text-gray-400 dark:text-gray-500 flex-shrink-0"
        :class="{ 'animate-spin': status === 'running' }"
      />
      <template v-if="status !== 'running' && routed">
        <span class="align-middle me-1">Routed to</span>
        <LLMProviderIcon v-if="providerType" :provider="providerType" :icon="true" class="w-3.5 h-3.5 me-1 flex-shrink-0" />
        <span class="font-medium align-middle">{{ modelName }}</span>
      </template>
      <span v-else class="align-middle">{{ label }}</span>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import LLMProviderIcon from '~/components/LLMProviderIcon.vue'

interface ToolExecution {
  id: string
  tool_name: string
  status: string
  result_summary?: string
  result_json?: any
}

const props = defineProps<{ toolExecution: ToolExecution }>()

const status = computed<string>(() => props.toolExecution?.status || '')
const rj = computed<any>(() => props.toolExecution?.result_json || {})

const routed = computed<boolean>(() => !!rj.value.routed)
// Prefer the friendly model name; fall back to the provider model_id.
const modelName = computed<string>(() => rj.value.model_name || rj.value.model || 'model')
const providerType = computed<string | null>(() => rj.value.provider_type || null)

const label = computed<string>(() => {
  if (status.value === 'running') return 'Choosing model…'
  // Not routed (kept current model, or invalid target) — fall back to the summary.
  return props.toolExecution?.result_summary || 'Model routing'
})
</script>
