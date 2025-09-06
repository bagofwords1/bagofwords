<template>
  <div class="mt-1">
    <div class="space-y-2">
      <div v-for="(d, i) in drafts" :key="i" class="relative bg-gray-50 hover:bg-gray-50 border border-gray-150 rounded-md p-3 transition-colors">
        <!-- Action buttons - top right -->
        <div class="absolute top-2 right-2 flex gap-1">
          <button 
            class="hover:bg-green-100 p-1 cursor-pointer transition-colors"
            title="Accept instruction"
          >
            <Icon name="heroicons:check" class="w-3 h-3 text-green-600" />
          </button>
          <button 
            class="hover:bg-red-100 p-1 cursor-pointer transition-colors"
            title="Reject instruction"
          >
            <Icon name="heroicons:x-mark" class="w-3 h-3 text-red-600" />
          </button>
        </div>
        
        <!-- Content -->
        <div class="text-xs text-gray-800 leading-relaxed pr-12">{{ d.text }}</div>
        <div v-if="d.category" class="text-xs text-gray-500 mt-1 font-medium">{{ d.category }}</div>
      </div>
    </div>
  </div>
  <div v-if="!hasAnyDrafts" class="mt-1">
    <div class="markdown-wrapper">
      <MDC :value="streamText" class="markdown-content" />
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

interface InstructionDraft {
  text: string
  category?: string | null
}

interface Props {
  toolExecution: ToolExecution
}

const props = defineProps<Props>()

const streamText = computed<string>(() => {
  const rj = props.toolExecution?.result_json || {}
  // During streaming, backend sends tool.partial with payload.delta → we stash into result_json.answer
  return typeof rj.answer === 'string' ? rj.answer : ''
})

const drafts = computed<InstructionDraft[]>(() => {
  const rj = props.toolExecution?.result_json || {}
  const out = rj?.drafts || rj?.instructions
  if (Array.isArray(out)) {
    return out.map((d: any) => ({ text: String(d?.text || ''), category: d?.category || null })).filter(d => d.text)
  }
  return []
})

const hasAnyDrafts = computed<boolean>(() => drafts.value.length > 0)

</script>

<style scoped>
.markdown-wrapper :deep(.markdown-content) {
  font-size: 14px;
  line-height: 2;
}
</style>


