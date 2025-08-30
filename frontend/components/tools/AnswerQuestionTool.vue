<template>
  <div class="mt-1">
    <div class="markdown-wrapper">
      <MDC :value="answerText" class="markdown-content" />
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed } from 'vue'

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

const answerText = computed<string>(() => {
  const rj = props.toolExecution?.result_json || {}
  // Prefer streamed 'answer' field; fallback to summary; finally empty
  const txt = (typeof rj.answer === 'string' ? rj.answer : '')
    || (typeof props.toolExecution?.result_summary === 'string' ? props.toolExecution.result_summary : '')
    || ''
  return txt
})
</script>

<style scoped>
.markdown-wrapper :deep(.markdown-content) {
  font-size: 14px;
  line-height: 2;
}
</style>


