<template>
  <div class="min-w-0">
    <!-- Header -->
    <ToolCallHeader :tool-execution="toolExecution" icon="heroicons-document-text" :expanded="toolHeaderExpanded" @toggle="toolHeaderExpanded = !toolHeaderExpanded">
      <template #label>
      <template v-if="status === 'running'">{{ t('tools.addParameter.adding', { name: paramLabel }) }}</template>
      <template v-else-if="hasError">{{ t('tools.addParameter.failed', { name: paramLabel }) }}</template>
      <template v-else-if="queryTitle">{{ t('tools.addParameter.added', { name: paramLabel, query: queryTitle }) }}</template>
      <template v-else>{{ t('tools.addParameter.addedNoQuery', { name: paramLabel }) }}</template></template>
    </ToolCallHeader>
    <div v-show="toolHeaderExpanded" class="tool-call-details w-full min-w-0 mt-2">
      <div class="mb-2 flex flex-wrap items-center gap-2 text-chat-3 text-ink-muted"><span v-if="formatDuration" class="ms-1.5 text-ink-muted">{{ formatDuration }}</span></div>

    <!-- Expanded content -->
    <template v-if="toolHeaderExpanded">
      <!-- What was declared: type, default, bound column -->
      <div v-if="status === 'success' && !hasError" class="mt-1 ms-[18px] flex flex-wrap gap-x-3 gap-y-0.5 text-chat-2 text-gray-500 dark:text-gray-400">
        <span v-if="param.type">{{ t('tools.addParameter.type', { type: param.type }) }}</span>
        <span>{{ t('tools.addParameter.default', { value: defaultLabel }) }}</span>
        <span v-if="column">{{ t('tools.addParameter.column', { column }) }}</span>
      </div>
      <!-- Error -->
      <div v-if="hasError && errorMessage" class="mt-1 ms-[18px] text-chat-2 text-gray-500 dark:text-gray-400">
        {{ errorMessage }}
      </div>
    </template>
  
    </div>
  </div>
</template>

<script setup lang="ts">
import ToolCallHeader from '~/components/tools/ToolCallHeader.vue'
import { ref as activityRef } from 'vue'
const toolHeaderExpanded = activityRef(false)

import { computed, ref } from 'vue'
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

const { t } = useResponseI18n()

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
.tool-call-details { overflow-wrap: anywhere; }
</style>
