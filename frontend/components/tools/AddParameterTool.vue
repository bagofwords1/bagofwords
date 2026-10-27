<template>
  <div class="mb-2">
    <!-- Header -->
    <div class="flex items-center text-chat-2 text-gray-500 dark:text-gray-400 cursor-pointer hover:text-gray-700 dark:hover:text-gray-300" @click="isCollapsed = !isCollapsed">
      <Icon :name="isCollapsed ? 'heroicons-chevron-right' : 'heroicons-chevron-down'" class="w-3 h-3 me-1.5 text-ink-muted rtl-flip" />
      <Spinner v-if="status === 'running'" class="w-3 h-3 me-1.5 text-ink-muted" />
      <Icon v-else-if="hasError" name="heroicons-exclamation-circle" class="w-3 h-3 me-1.5 text-amber-500" />
      <Icon v-else-if="status === 'success'" name="heroicons-check" class="w-3 h-3 me-1.5 text-green-500" />
      <Icon v-else name="heroicons-adjustments-horizontal" class="w-3 h-3 me-1.5 text-ink-muted" />

      <span v-if="status === 'running'" class="tool-shimmer">{{ $t('tools.addParameter.adding', { name: paramLabel }) }}</span>
      <span v-else-if="hasError" class="text-gray-700 dark:text-gray-300">{{ $t('tools.addParameter.failed', { name: paramLabel }) }}</span>
      <span v-else-if="queryTitle" class="text-gray-700 dark:text-gray-300">{{ $t('tools.addParameter.added', { name: paramLabel, query: queryTitle }) }}</span>
      <span v-else class="text-gray-700 dark:text-gray-300">{{ $t('tools.addParameter.addedNoQuery', { name: paramLabel }) }}</span>

      <span v-if="formatDuration" class="ms-1.5 text-ink-muted">{{ formatDuration }}</span>
    </div>

    <!-- Expanded content -->
    <template v-if="!isCollapsed">
      <!-- What was declared: type, default, bound column -->
      <div v-if="status === 'success' && !hasError" class="mt-1 ms-[18px] flex flex-wrap gap-x-3 gap-y-0.5 text-xs text-gray-500 dark:text-gray-400">
        <span v-if="param.type">{{ $t('tools.addParameter.type', { type: param.type }) }}</span>
        <span>{{ $t('tools.addParameter.default', { value: defaultLabel }) }}</span>
        <span v-if="column">{{ $t('tools.addParameter.column', { column }) }}</span>
      </div>
      <!-- Error -->
      <div v-if="hasError && errorMessage" class="mt-1 ms-[18px] text-xs text-gray-500 dark:text-gray-400">
        {{ errorMessage }}
      </div>
    </template>
  </div>
</template>

<script setup lang="ts">
import { computed, ref } from 'vue'
import Spinner from '~/components/Spinner.vue'
import { toolErrorText } from '~/utils/toolError'

interface Props {
  toolExecution: {
    id: string
    tool_name: string
    arguments_json?: {
      query_id?: string
      parameter?: { name?: string; label?: string; type?: string; default?: unknown }
      column?: string
    }
    result_json?: {
      success?: boolean
      title?: string
      error?: string
    }
    status: string
    result_summary?: string
    duration_ms?: number
  }
  readonly?: boolean
}

const props = defineProps<Props>()

const { t } = useI18n()

const isCollapsed = ref(true)
const status = computed(() => props.toolExecution.status)
const param = computed(() => props.toolExecution.arguments_json?.parameter || {})
const paramLabel = computed(() => param.value.label || param.value.name || '')
const column = computed(() => props.toolExecution.arguments_json?.column || '')

// The query title is only known once the tool resolved the query.
const queryTitle = computed(() => props.toolExecution.result_json?.title || '')

// The tool reports refusals (not found, already declared) as success: false.
const hasError = computed(() => status.value === 'error' || props.toolExecution.result_json?.success === false)
const errorMessage = computed(() => toolErrorText(props.toolExecution.result_json?.error) || props.toolExecution.result_summary || '')

const defaultLabel = computed(() => {
  const v = param.value.default
  if (v === null || v === undefined || v === '') return t('tools.addParameter.defaultAll')
  if (Array.isArray(v)) return v.join(', ')
  if (typeof v === 'object') return JSON.stringify(v)
  return String(v)
})

const formatDuration = computed(() => {
  const ms = props.toolExecution.duration_ms
  if (!ms) return ''
  return ms >= 1000 ? `${(ms / 1000).toFixed(1)}s` : `${Math.round(ms)}ms`
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
