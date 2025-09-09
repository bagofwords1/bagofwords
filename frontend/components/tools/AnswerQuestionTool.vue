<template>
  <div class="mt-1">
    <!-- Status header -->
    <div class="mb-2 flex items-center text-xs text-gray-500 cursor-pointer hover:text-gray-700">
      <span v-if="status === 'running'" class="tool-shimmer">
        <Icon name="eos-icons:loading" class="w-3 h-3 mr-1.5 animate-spin text-gray-400" />
        Searching...
      </span>
      <span v-else class="text-gray-700">Searched for answer</span>
    </div>

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

const status = computed<string>(() => props.toolExecution?.status || '')

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

.markdown-wrapper :deep(.markdown-content) {
	@apply leading-relaxed;
	font-size: 14px;

	:where(h1, h2, h3, h4, h5, h6) {
		@apply font-bold mb-4 mt-6;
	}

	h1 { @apply text-xl; }
	h2 { @apply text-lg; }
	h3 { @apply text-lg; }

	ul, ol { @apply pl-6 mb-4; }
	ul { @apply list-disc; }
	ol { @apply list-decimal; }
	li { @apply mb-1.5; }

	pre { @apply bg-gray-50 p-4 rounded-lg mb-4 overflow-x-auto; }
	code { @apply bg-gray-50 px-1 py-0.5 rounded text-sm font-mono; }
	blockquote { @apply border-l-4 border-gray-200 pl-4 italic my-4; }
	table { @apply w-full border-collapse mb-4; }
	table th, table td { @apply border border-gray-200 p-2 text-xs bg-white; }
}

</style>


