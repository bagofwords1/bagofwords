<template>
  <div class="min-w-0">
    <!-- Status header -->
    <ToolCallHeader :tool-execution="toolExecution" icon="heroicons-magnifying-glass" :expanded="toolHeaderExpanded" @toggle="toolHeaderExpanded = !toolHeaderExpanded">
      <template #label>
      <template v-if="status === 'running'">{{ t('tools.searchPrompts.searching', { query: queryLabel }) }}</template>
      <template v-else>{{ t('tools.searchPrompts.searched', { query: queryLabel }) }}</template></template>
    </ToolCallHeader>
    <div v-show="toolHeaderExpanded" class="tool-call-details w-full min-w-0 mt-2">
      <div class="mb-2 flex flex-wrap items-center gap-2 text-chat-3 text-ink-muted">
      <template v-if="status === 'running'"></template>
      <template v-else><span v-if="total > 0" class="ms-1.5 text-chat-3 text-ink-muted">· {{ total === 1 ? t('tools.searchPrompts.matchSingular', { count: total }) : t('tools.searchPrompts.matchPlural', { count: total }) }}</span></template></div>

    <!-- Results list -->
    <Transition name="fade" appear>
      <div v-if="prompts.length" class="text-chat-2 text-gray-600 dark:text-gray-400">
        <ul class="ms-1 space-y-1 leading-snug">
          <li v-for="(item, idx) in prompts" :key="item.id || idx">
            <div
              class="flex items-center py-1 px-1 rounded cursor-pointer hover:bg-gray-50 dark:hover:bg-gray-800"
              @click="toggleItem(idx)"
              :aria-expanded="isExpanded(idx)"
            >
              <Icon :name="isExpanded(idx) ? 'heroicons-chevron-down' : 'heroicons-chevron-right'" class="w-3 h-3 text-ink-muted me-1 rtl-flip" />
              <Icon name="heroicons-bookmark-square" class="w-3 h-3 me-1 text-indigo-400 flex-shrink-0" />
              <div class="font-medium text-gray-700 dark:text-gray-300 truncate">{{ displayTitle(item) }}</div>
              <span v-if="item.scope" class="ms-1.5 text-chat-3 px-1 py-0.5 rounded bg-gray-100 dark:bg-gray-800 text-gray-500 dark:text-gray-400 flex-shrink-0">{{ item.scope }}</span>
              <span v-if="item.is_starter" class="ms-1 text-chat-3 px-1 py-0.5 rounded bg-amber-50 dark:bg-amber-950 text-amber-700 dark:text-amber-400 flex-shrink-0">{{ t('tools.searchPrompts.starter') }}</span>
            </div>
            <Transition name="fade">
              <div v-if="isExpanded(idx)" class="ps-6 pe-1 pb-2">
                <div class="prompt-content text-chat-2 text-gray-700 dark:text-gray-300 leading-relaxed mb-1">
                  <MDC :value="item.text || ''" class="markdown-content" />
                </div>
                <button class="text-chat-2 text-blue-600 hover:text-blue-800 inline-flex items-center gap-0.5" @click="openPrompts">
                  <Icon name="heroicons:arrow-top-right-on-square" class="w-3 h-3" />
                  <span>{{ t('tools.searchPrompts.open') }}</span>
                </button>
              </div>
            </Transition>
          </li>
        </ul>
      </div>
    </Transition>

    <!-- Empty state -->
    <div v-if="status !== 'running' && !prompts.length" class="text-chat-2 text-ink-muted ms-1">
      {{ t('tools.searchPrompts.empty') }}
    </div>
  
    </div>
  </div>
</template>

<script setup lang="ts">
import ToolCallHeader from '~/components/tools/ToolCallHeader.vue'
import { ref as activityRef } from 'vue'
const toolHeaderExpanded = activityRef(false)

import { computed, ref } from 'vue'
import { useResponseI18n } from '~/composables/useResponseI18n'
const { t } = useResponseI18n()

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
  if (args.value.starters_only) return t('tools.searchPrompts.queryStarters')
  if (args.value.scope) return t('tools.searchPrompts.queryScope', { scope: args.value.scope })
  if (args.value.data_source_id) return t('tools.searchPrompts.queryThisAgent')
  return t('tools.searchPrompts.queryAll')
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
  return firstLine.length > 80 ? firstLine.slice(0, 77) + '…' : (firstLine || t('tools.searchPrompts.untitled'))
}
function openPrompts() { navigateTo('/prompts') }
</script>

<style scoped>
.fade-enter-active, .fade-leave-active { transition: opacity 0.25s ease, transform 0.25s ease; }
.fade-enter-from, .fade-leave-to { opacity: 0; transform: translateY(2px); }
.prompt-content :deep(.markdown-content) { font-size: var(--bow-chat-font-size-2); line-height: 1.5; }
.prompt-content :deep(.markdown-content p) { margin: 0 0 0.4em 0; }
.prompt-content :deep(.markdown-content p:last-child) { margin-bottom: 0; }
.prompt-content :deep(.markdown-content code) { font-size: 0.9em; padding: 0.1em 0.3em; background: rgba(0,0,0,0.05); border-radius: 3px; }
</style>
