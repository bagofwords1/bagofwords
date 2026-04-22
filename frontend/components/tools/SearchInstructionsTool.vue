<template>
  <div class="mt-1">
    <!-- Status header -->
    <Transition name="fade" appear>
      <div class="mb-2 flex items-center text-xs text-gray-500">
        <span v-if="status === 'running'" class="tool-shimmer flex items-center">
          <Icon name="heroicons-magnifying-glass" class="w-3 h-3 me-1 text-gray-400" />
          Searching instructions for {{ queryLabel }}…
        </span>
        <span v-else class="text-gray-700 flex items-center">
          <Icon name="heroicons-magnifying-glass" class="w-3 h-3 me-1 text-gray-400" />
          <span class="align-middle">Searched instructions for {{ queryLabel }}</span>
          <span v-if="total > 0" class="ms-1.5 text-[10px] text-gray-400">· {{ total }} match{{ total === 1 ? '' : 'es' }}</span>
        </span>
      </div>
    </Transition>

    <!-- Results list -->
    <Transition name="fade" appear>
      <div v-if="instructions.length" class="text-xs text-gray-600">
        <ul class="ms-1 space-y-1 leading-snug">
          <li v-for="(item, idx) in instructions" :key="item.id || idx">
            <!-- Header row -->
            <div
              class="flex items-center py-1 px-1 rounded cursor-pointer hover:bg-gray-50"
              @click="toggleItem(idx)"
              :aria-expanded="isExpanded(idx)"
            >
              <Icon :name="isExpanded(idx) ? 'heroicons-chevron-down' : 'heroicons-chevron-right'" class="w-3 h-3 text-gray-400 me-1" />
              <Icon name="heroicons-cube" class="w-3 h-3 me-1 text-indigo-400 flex-shrink-0" />
              <div class="font-medium text-gray-700 truncate">
                {{ displayTitle(item) }}
              </div>
              <span v-if="item.category" class="ms-1.5 text-[9px] px-1 py-0.5 rounded bg-gray-100 text-gray-500 flex-shrink-0">{{ item.category }}</span>
              <span v-if="item.load_mode" class="ms-1 text-[9px] px-1 py-0.5 rounded flex-shrink-0"
                :class="item.load_mode === 'always' ? 'bg-blue-50 text-blue-600' : 'bg-gray-100 text-gray-500'">
                {{ item.load_mode }}
              </span>
            </div>
            <!-- Detail row -->
            <Transition name="fade">
              <div v-if="isExpanded(idx)" class="ps-6 pe-1 pb-2">
                <div class="instruction-content text-[12px] text-gray-700 leading-relaxed mb-1 cursor-pointer hover:text-gray-900"
                     @click="emit('openInstruction', item.id)">
                  <MDC :value="item.text || ''" class="markdown-content" />
                </div>
                <button
                  class="text-[10px] text-blue-600 hover:text-blue-800 inline-flex items-center gap-0.5"
                  @click="emit('openInstruction', item.id)"
                >
                  <Icon name="heroicons:arrow-top-right-on-square" class="w-3 h-3" />
                  <span>{{ $t('tools.searchInstructions.open') }}</span>
                </button>
              </div>
            </Transition>
          </li>
        </ul>
      </div>
    </Transition>

    <!-- Empty state (after search completes with no results) -->
    <div v-if="status !== 'running' && !instructions.length" class="text-xs text-gray-400 ms-1">
      No matching instructions found.
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, ref } from 'vue'

interface ToolExecution {
  id: string
  tool_name: string
  tool_action?: string
  status: string
  result_summary?: string
  result_json?: any
  arguments_json?: any
}

interface Props {
  toolExecution: ToolExecution
}

const props = defineProps<Props>()
const emit = defineEmits<{ (e: 'openInstruction', id: string): void }>()

const status = computed<string>(() => props.toolExecution?.status || '')

const queryLabel = computed<string>(() => {
  const rj = props.toolExecution?.result_json || {}
  let q: any = rj.search_query
  if (q == null) q = (props.toolExecution as any)?.arguments_json?.query
  if (Array.isArray(q)) return q.filter(Boolean).map((s: string) => `"${s}"`).join(', ') || 'instructions'
  if (typeof q === 'string' && q) return `"${q}"`
  return 'instructions'
})

const instructions = computed<any[]>(() => {
  const rj: any = props.toolExecution?.result_json || {}
  return Array.isArray(rj.instructions) ? rj.instructions : []
})

const total = computed<number>(() => {
  const rj: any = props.toolExecution?.result_json || {}
  return typeof rj.total === 'number' ? rj.total : instructions.value.length
})

const expandedItems = ref<Set<number>>(new Set())
function toggleItem(index: number) {
  if (expandedItems.value.has(index)) {
    expandedItems.value.delete(index)
  } else {
    expandedItems.value.add(index)
  }
}
function isExpanded(index: number): boolean {
  return expandedItems.value.has(index)
}

function displayTitle(item: any): string {
  if (item?.title) return item.title
  const text = String(item?.text || '').trim()
  if (!text) return 'Untitled'
  const firstLine = text.split('\n')[0].replace(/^#+\s*/, '').trim()
  return firstLine.length > 80 ? firstLine.slice(0, 77) + '…' : firstLine || 'Untitled'
}
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

.fade-enter-active, .fade-leave-active {
  transition: opacity 0.25s ease, transform 0.25s ease;
}
.fade-enter-from, .fade-leave-to {
  opacity: 0;
  transform: translateY(2px);
}

.instruction-content :deep(.markdown-content) {
  font-size: 12px;
  line-height: 1.5;
}
.instruction-content :deep(.markdown-content p) {
  margin: 0 0 0.4em 0;
}
.instruction-content :deep(.markdown-content p:last-child) {
  margin-bottom: 0;
}
.instruction-content :deep(.markdown-content code) {
  font-size: 10px;
  padding: 0.1em 0.3em;
  background: rgba(0, 0, 0, 0.05);
  border-radius: 3px;
}
</style>
