<template>
  <div class="mt-1">
    <!-- Status header -->
    <Transition name="fade" appear>
      <div class="mb-2 flex items-center text-xs text-gray-500 dark:text-gray-400">
        <span v-if="status === 'running'" class="tool-shimmer flex items-center">
          <Icon name="heroicons-magnifying-glass" class="w-3 h-3 me-1 text-gray-400" />
          <span>Searching prompts for {{ queryLabel }}…</span>
        </span>
        <span v-else class="text-gray-700 dark:text-gray-300 flex items-center">
          <Icon name="heroicons-magnifying-glass" class="w-3 h-3 me-1 text-gray-400" />
          <span class="align-middle">Searched prompts for {{ queryLabel }}</span>
          <span v-if="total > 0" class="ms-1.5 text-[10px] text-gray-400">· {{ total === 1 ? '1 match' : total + ' matches' }}</span>
        </span>
      </div>
    </Transition>

    <!-- Results list -->
    <Transition name="fade" appear>
      <div v-if="prompts.length" class="text-xs text-gray-600 dark:text-gray-400">
        <ul class="ms-1 space-y-1 leading-snug">
          <li v-for="(item, idx) in prompts" :key="item.id || idx">
            <div
              class="flex items-center py-1 px-1 rounded cursor-pointer hover:bg-gray-50 dark:hover:bg-gray-800"
              @click="toggleItem(idx)"
              :aria-expanded="isExpanded(idx)"
            >
              <Icon :name="isExpanded(idx) ? 'heroicons-chevron-down' : 'heroicons-chevron-right'" class="w-3 h-3 text-gray-400 me-1 rtl-flip" />
              <Icon name="heroicons-bookmark-square" class="w-3 h-3 me-1 text-indigo-400 flex-shrink-0" />
              <div class="font-medium text-gray-700 dark:text-gray-300 truncate">{{ displayTitle(item) }}</div>
              <span v-if="item.scope" class="ms-1.5 text-[9px] px-1 py-0.5 rounded bg-gray-100 dark:bg-gray-800 text-gray-500 dark:text-gray-400 flex-shrink-0">{{ item.scope }}</span>
              <span v-if="item.is_starter" class="ms-1 text-[9px] px-1 py-0.5 rounded bg-amber-50 dark:bg-amber-950 text-amber-700 dark:text-amber-400 flex-shrink-0">starter</span>
            </div>
            <Transition name="fade">
              <div v-if="isExpanded(idx)" class="ps-6 pe-1 pb-2">
                <div class="prompt-content text-[12px] text-gray-700 dark:text-gray-300 leading-relaxed mb-1">
                  <MDC :value="item.text || ''" class="markdown-content" />
                </div>
                <button class="text-[10px] text-blue-600 hover:text-blue-800 inline-flex items-center gap-0.5" @click="openPrompts">
                  <Icon name="heroicons:arrow-top-right-on-square" class="w-3 h-3" />
                  <span>Open in Prompts</span>
                </button>
              </div>
            </Transition>
          </li>
        </ul>
      </div>
    </Transition>

    <!-- Empty state -->
    <div v-if="status !== 'running' && !prompts.length" class="text-xs text-gray-400 ms-1">
      No matching prompts.
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, ref } from 'vue'

interface ToolExecution {
  id: string
  tool_name: string
  status: string
  result_summary?: string
  result_json?: any
  arguments_json?: any
}
const props = defineProps<{ toolExecution: ToolExecution }>()

const status = computed<string>(() => props.toolExecution?.status || '')
const result = computed(() => props.toolExecution?.result_json || {})
const args = computed(() => props.toolExecution?.arguments_json || {})

const queryLabel = computed<string>(() => {
  let q: any = args.value.query
  if (Array.isArray(q)) {
    const s = q.filter(Boolean).map((x: string) => `"${x}"`).join(', ')
    if (s) return s
  } else if (typeof q === 'string' && q) {
    return `"${q}"`
  }
  if (args.value.starters_only) return 'conversation starters'
  if (args.value.scope) return `${args.value.scope} prompts`
  if (args.value.data_source_id) return 'this agent'
  return 'all prompts'
})

const prompts = computed<any[]>(() => Array.isArray(result.value.prompts) ? result.value.prompts : [])
const total = computed<number>(() => typeof result.value.total === 'number' ? result.value.total : prompts.value.length)

const expandedItems = ref<Set<number>>(new Set())
function toggleItem(index: number) {
  if (expandedItems.value.has(index)) expandedItems.value.delete(index)
  else expandedItems.value.add(index)
}
function isExpanded(index: number): boolean { return expandedItems.value.has(index) }

function displayTitle(item: any): string {
  if (item?.title) return item.title
  const text = String(item?.text || '').trim()
  const firstLine = text.split('\n')[0].replace(/^#+\s*/, '').trim()
  return firstLine.length > 80 ? firstLine.slice(0, 77) + '…' : (firstLine || 'Untitled')
}
function openPrompts() { navigateTo('/prompts') }
</script>

<style scoped>
.tool-shimmer {
  animation: shimmer 1.6s linear infinite;
  background: linear-gradient(90deg, rgba(0,0,0,0) 0%, rgba(160,160,160,0.15) 50%, rgba(0,0,0,0) 100%);
  background-size: 300% 100%;
  background-clip: text;
}
@keyframes shimmer { 0% { background-position: 0% 0; } 100% { background-position: 100% 0; } }
.fade-enter-active, .fade-leave-active { transition: opacity 0.25s ease, transform 0.25s ease; }
.fade-enter-from, .fade-leave-to { opacity: 0; transform: translateY(2px); }
.prompt-content :deep(.markdown-content) { font-size: 12px; line-height: 1.5; }
.prompt-content :deep(.markdown-content p) { margin: 0 0 0.4em 0; }
.prompt-content :deep(.markdown-content p:last-child) { margin-bottom: 0; }
.prompt-content :deep(.markdown-content code) { font-size: 10px; padding: 0.1em 0.3em; background: rgba(0,0,0,0.05); border-radius: 3px; }
</style>
