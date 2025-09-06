<template>
  <div class="mt-1">
    <div class="space-y-2" v-if="drafts.length">
      <div v-for="(d, i) in drafts" :key="i" class="relative bg-gray-50 hover:bg-gray-50 border border-gray-150 rounded-md p-3 transition-colors">
        <div class="absolute top-2 right-2 flex gap-1">
          <button class="hover:bg-green-100 p-1 cursor-pointer transition-colors" title="Accept instruction">
            <Icon name="heroicons:check" class="w-3 h-3 text-green-600" />
          </button>
          <button class="hover:bg-red-100 p-1 cursor-pointer transition-colors" title="Reject instruction">
            <Icon name="heroicons:x-mark" class="w-3 h-3 text-red-600" />
          </button>
        </div>
        <div class="text-xs text-gray-800 leading-relaxed pr-12">{{ d.text }}</div>
        <div v-if="d.category" class="text-xs text-gray-500 mt-1 font-medium">{{ d.category }}</div>
      </div>
    </div>
    <div v-else class="text-xs text-gray-400">No suggestions yet.</div>
  </div>
</template>

<script setup lang="ts">
import { computed } from 'vue'

interface ToolExecution {
  id: string
  tool_name: string
  status: string
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

const drafts = computed<InstructionDraft[]>(() => {
  const rj = props.toolExecution?.result_json || {}
  const out = rj?.drafts || rj?.instructions
  if (Array.isArray(out)) {
    return out.map((d: any) => ({ text: String(d?.text || ''), category: d?.category || null })).filter(d => d.text)
  }
  return []
})
</script>

<style scoped>
.markdown-wrapper :deep(.markdown-content) {
  font-size: 14px;
  line-height: 2;
}
</style>


