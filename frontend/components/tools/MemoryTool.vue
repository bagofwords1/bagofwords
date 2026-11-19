<template>
  <!-- A refused or failed memory call is the agent's business (it handles the
       error itself, e.g. a rule it applies instead of saving) — the report
       shows nothing for it; the trace keeps it. -->
  <div v-if="status === 'running' || isSuccess" class="min-w-0">
    <!-- Single-line, non-expandable status for create_memory / edit_memory /
         search_memory. Memory is private: we
         show only that it happened plus the agent's short title — never entry
         text, handles, tags or search results. Details live in the profile. -->
    <ToolCallHeader :expandable="false" :tool-execution="toolExecution" icon="heroicons-book-open" :expanded="toolHeaderExpanded" @toggle="toolHeaderExpanded = !toolHeaderExpanded">
      <template #label>
      <template v-if="status === 'running'">{{ label || runningFallback }}</template>
      <template v-else-if="isSuccess">{{ label || doneFallback }}</template></template>
    </ToolCallHeader>
    
  </div>
</template>

<script setup lang="ts">
import ToolCallHeader from '~/components/tools/ToolCallHeader.vue'
import { ref as activityRef } from 'vue'
const toolHeaderExpanded = activityRef(false)

import { computed } from 'vue'

interface Props {
  toolExecution: {
    tool_name: string
    arguments_json?: { title?: string }
    result_json?: { success?: boolean }
    status: string
  }
  readonly?: boolean
}
const props = defineProps<Props>()
const { t } = useResponseI18n()

const status = computed(() => props.toolExecution.status)
const isSearch = computed(() => props.toolExecution.tool_name === 'search_memory')
// A tool that ran but refused the write (validation, dedupe refusal, …)
// ends with success=false in its result — that is a failure for this line.
const isSuccess = computed(() =>
  status.value === 'success' && props.toolExecution.result_json?.success !== false
)
const label = computed(() => props.toolExecution.arguments_json?.title || '')
const runningFallback = computed(() => isSearch.value ? t('tools.memory.checking') : t('tools.memory.updating'))
const doneFallback = computed(() => isSearch.value ? t('tools.memory.checked') : t('tools.memory.updated'))
</script>
