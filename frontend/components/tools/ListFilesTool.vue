<template>
  <div class="min-w-0">
    <ToolCallHeader :tool-execution="toolExecution" icon="heroicons-folder" :expanded="toolHeaderExpanded" @toggle="toolHeaderExpanded = !toolHeaderExpanded">
      <template #label>
      <template v-if="status === 'running'">{{ modelTitle ? modelTitle + '…' : `Listing ${noun.many}…` }}</template>
      <template v-else>{{ modelTitle || `Listed ${noun.many}` }}</template></template>
    </ToolCallHeader>
    <div v-show="toolHeaderExpanded" class="tool-call-details w-full min-w-0 mt-2">
      <div class="mb-2 flex flex-wrap items-center gap-2 text-chat-3 text-ink-muted">
      <template v-if="status === 'running'"></template>
      <template v-else><span v-if="files.length" class="ms-2 text-ink-muted">{{ files.length }}{{ truncated ? '+' : '' }} {{ files.length === 1 ? noun.one : noun.many }}</span></template></div>

    <Transition name="fade" appear>
      <div v-if="toolHeaderExpanded && status !== 'running'" class="text-chat-2 text-gray-600 dark:text-gray-400">
        <ul v-if="files.length" class="ms-1 space-y-1 leading-snug">
          <li v-for="(f, idx) in files.slice(0, 10)" :key="f.id || idx">
            <div
              class="flex items-center py-1 px-1 rounded cursor-pointer hover:bg-gray-50 dark:hover:bg-gray-800"
              @click="toggleItem(idx)"
              :aria-expanded="isExpanded(idx)"
            >
              <Icon :name="isExpanded(idx) ? 'heroicons-chevron-down' : 'heroicons-chevron-right'" class="w-3 h-3 text-ink-muted me-1 rtl-flip" />
              <Icon :name="isChat ? 'heroicons-chat-bubble-bottom-center-text' : 'heroicons-document'" class="w-3 h-3 me-1 text-ink-muted" />
              <div class="font-medium text-gray-700 dark:text-gray-300 truncate">{{ f.name || noun.one }}</div>
              <span v-if="f.path && f.path !== f.name" class="ms-2 text-chat-3 text-ink-muted truncate max-w-[16rem]" :title="f.path" dir="ltr">{{ f.path }}</span>
              <span v-if="f.size" class="ms-2 text-chat-3 text-ink-muted shrink-0">{{ formatBytes(f.size) }}</span>
            </div>
            <Transition name="fade">
              <div v-if="isExpanded(idx)" class="ps-6 pe-1 pb-1 text-gray-500 dark:text-gray-400 space-y-0.5">
                <template v-if="isChat">
                  <div v-if="f.preview" class="text-chat-3 whitespace-pre-line text-gray-600 dark:text-gray-300">{{ f.preview }}</div>
                  <div v-if="f.thread" class="text-chat-3"><span class="text-ink-muted">In reply to:</span> {{ f.thread }}</div>
                  <div v-for="(line, li) in (f.context || [])" :key="li" class="text-chat-3 ps-2 border-s border-gray-200 dark:border-gray-700 text-gray-500 dark:text-gray-400">{{ line }}</div>
                  <div v-if="f.folder" class="text-chat-3"><span class="text-ink-muted">In:</span> {{ f.folder }}</div>
                  <div v-if="f.modified_at" class="text-chat-3"><span class="text-ink-muted">Sent:</span> {{ f.modified_at }}</div>
                </template>
                <template v-else>
                <div v-if="f.path" class="text-chat-3"><span class="text-ink-muted">Path:</span> {{ f.path }}</div>
                <div v-if="f.mime_type" class="text-chat-3"><span class="text-ink-muted">Type:</span> {{ f.mime_type }}</div>
                <div v-if="f.modified_at" class="text-chat-3"><span class="text-ink-muted">Modified:</span> {{ f.modified_at }}</div>
                </template>
                <a v-if="f.web_url" :href="f.web_url" target="_blank" rel="noopener" class="text-chat-2 text-blue-600 hover:underline inline-flex items-center gap-1">
                  Open <Icon name="heroicons-arrow-top-right-on-square" class="w-3 h-3" />
                </a>
              </div>
            </Transition>
          </li>
          <li v-if="files.length > 10" class="ps-1 text-chat-3 text-ink-muted">+{{ files.length - 10 }} more</li>
        </ul>
        <ToolCallParams :params="toolExecution?.arguments_json" />
      </div>
    </Transition>

    <div v-if="status !== 'running' && !files.length && errorMessage" class="text-chat-2 text-amber-600 mt-1">{{ errorMessage }}</div>
  
    </div>
  </div>
</template>

<script setup lang="ts">
import ToolCallHeader from '~/components/tools/ToolCallHeader.vue'
import { ref as activityRef } from 'vue'
const toolHeaderExpanded = activityRef(false)

import { computed, ref } from 'vue'
import ToolCallParams from '~/components/tools/ToolCallParams.vue'
import DataSourceIcon from '~/components/DataSourceIcon.vue'
import { useToolConnectionIcon, FILE_SOURCE_TYPES, fileToolNoun } from '~/composables/useToolConnectionIcon'
import { toolErrorText } from '~/utils/toolError'

interface ToolExecution {
  id: string
  tool_name: string
  status: string
  result_summary?: string
  result_json?: any
}

const props = defineProps<{ toolExecution: ToolExecution; dataSources?: any[] }>()
// files / emails / pages — same card, source-appropriate noun.
const noun = computed(() => fileToolNoun(props.toolExecution?.tool_name))
// Chat messages show their text, thread and conversation instead of file metadata.
const isChat = computed(() => noun.value.one === 'message')

const connIcon = useToolConnectionIcon(
  () => props.toolExecution,
  () => props.dataSources,
  { connectionTypes: FILE_SOURCE_TYPES },
)

const status = computed(() => props.toolExecution?.status || '')

const modelTitle = computed<string>(() => {
  const t = props.toolExecution?.arguments_json?.title
  return typeof t === 'string' && t.trim() ? t.trim() : ''
})
const files = computed<any[]>(() => {
  const rj = props.toolExecution?.result_json || {}
  return Array.isArray(rj.files) ? rj.files : []
})
const truncated = computed(() => !!props.toolExecution?.result_json?.truncated)
const errorMessage = computed(() => toolErrorText(props.toolExecution?.result_json?.error))

const expanded = ref(false)

const expandedItems = ref<Set<number>>(new Set())
function toggleItem(i: number) {
  if (expandedItems.value.has(i)) expandedItems.value.delete(i)
  else expandedItems.value.add(i)
}
function isExpanded(i: number) { return expandedItems.value.has(i) }

function formatBytes(n: number): string {
  if (!n) return ''
  const units = ['B', 'KB', 'MB', 'GB']
  let i = 0, v = n
  while (v >= 1024 && i < units.length - 1) { v /= 1024; i++ }
  return `${v.toFixed(v < 10 && i > 0 ? 1 : 0)} ${units[i]}`
}
</script>

<style scoped>
.fade-enter-active, .fade-leave-active { transition: opacity 0.25s ease, transform 0.25s ease; }
.fade-enter-from, .fade-leave-to { opacity: 0; transform: translateY(2px); }
</style>
