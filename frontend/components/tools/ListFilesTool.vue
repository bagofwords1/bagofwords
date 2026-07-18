<template>
  <div class="mt-1">
    <Transition name="fade" appear>
      <div class="mb-2 flex items-center text-xs text-gray-500 dark:text-gray-400">
        <span v-if="status === 'running'" class="flex items-center">
          <Spinner class="w-3 h-3 me-1.5 shrink-0 text-gray-400" />
          <span class="tool-shimmer">{{ modelTitle ? modelTitle + '…' : 'Listing files…' }}</span>
        </span>
        <span
          v-else
          class="text-gray-700 dark:text-gray-300 flex items-center"
          :class="files.length ? 'cursor-pointer' : ''"
          @click="files.length && (expanded = !expanded)"
          :aria-expanded="files.length ? expanded : undefined"
        >
          <Icon v-if="files.length" :name="expanded ? 'heroicons-chevron-down' : 'heroicons-chevron-right'" class="w-3 h-3 me-1 text-gray-400 dark:text-gray-500 rtl-flip" />
          <Icon name="heroicons-folder" class="w-3 h-3 me-1 text-gray-400 dark:text-gray-500" />
          <span>{{ modelTitle || 'Listed files' }}</span>
          <span v-if="files.length" class="ms-2 text-gray-400 dark:text-gray-500">{{ files.length }}{{ truncated ? '+' : '' }} {{ files.length === 1 ? 'file' : 'files' }}</span>
        </span>
      </div>
    </Transition>

    <Transition name="fade" appear>
      <div v-if="files.length && expanded" class="text-xs text-gray-600 dark:text-gray-400">
        <ul class="ms-1 space-y-1 leading-snug">
          <li v-for="(f, idx) in files.slice(0, 10)" :key="f.id || idx">
            <div
              class="flex items-center py-1 px-1 rounded cursor-pointer hover:bg-gray-50 dark:hover:bg-gray-800"
              @click="toggleItem(idx)"
              :aria-expanded="isExpanded(idx)"
            >
              <Icon :name="isExpanded(idx) ? 'heroicons-chevron-down' : 'heroicons-chevron-right'" class="w-3 h-3 text-gray-400 dark:text-gray-500 me-1 rtl-flip" />
              <Icon name="heroicons-document" class="w-3 h-3 me-1 text-gray-400 dark:text-gray-500" />
              <div class="font-medium text-gray-700 dark:text-gray-300 truncate">{{ f.name || 'file' }}</div>
              <span v-if="f.path && f.path !== f.name" class="ms-2 text-[10px] text-gray-400 dark:text-gray-500 truncate max-w-[16rem]" :title="f.path" dir="ltr">{{ f.path }}</span>
              <span v-if="f.size" class="ms-2 text-[10px] text-gray-400 dark:text-gray-500 shrink-0">{{ formatBytes(f.size) }}</span>
            </div>
            <Transition name="fade">
              <div v-if="isExpanded(idx)" class="ps-6 pe-1 pb-1 text-gray-500 dark:text-gray-400 space-y-0.5">
                <div v-if="f.path" class="text-[11px]"><span class="text-gray-400 dark:text-gray-500">Path:</span> {{ f.path }}</div>
                <div v-if="f.mime_type" class="text-[11px]"><span class="text-gray-400 dark:text-gray-500">Type:</span> {{ f.mime_type }}</div>
                <div v-if="f.modified_at" class="text-[11px]"><span class="text-gray-400 dark:text-gray-500">Modified:</span> {{ f.modified_at }}</div>
                <a v-if="f.web_url" :href="f.web_url" target="_blank" rel="noopener" class="text-[11px] text-blue-600 hover:underline inline-flex items-center gap-1">
                  Open <Icon name="heroicons-arrow-top-right-on-square" class="w-3 h-3" />
                </a>
              </div>
            </Transition>
          </li>
          <li v-if="files.length > 10" class="ps-1 text-[11px] text-gray-400 dark:text-gray-500">+{{ files.length - 10 }} more</li>
        </ul>
      </div>
    </Transition>

    <ToolCallParams v-if="status !== 'running'" :params="toolExecution?.arguments_json" />

    <div v-if="status !== 'running' && !files.length && errorMessage" class="text-xs text-red-600 mt-1">{{ errorMessage }}</div>
  </div>
</template>

<script setup lang="ts">
import { computed, ref } from 'vue'
import Spinner from '~/components/Spinner.vue'
import ToolCallParams from '~/components/tools/ToolCallParams.vue'

interface ToolExecution {
  id: string
  tool_name: string
  status: string
  result_summary?: string
  result_json?: any
}

const props = defineProps<{ toolExecution: ToolExecution }>()

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
const errorMessage = computed(() => props.toolExecution?.result_json?.error || '')

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
.tool-shimmer {
  background: linear-gradient(90deg, #888 0%, #999 25%, #ccc 50%, #999 75%, #888 100%);
  background-size: 200% 100%;
  -webkit-background-clip: text;
  background-clip: text;
  color: transparent;
  animation: shimmer 2s linear infinite;
}
@keyframes shimmer { 0% { background-position: -100% 0; } 100% { background-position: 100% 0; } }
.fade-enter-active, .fade-leave-active { transition: opacity 0.25s ease, transform 0.25s ease; }
.fade-enter-from, .fade-leave-to { opacity: 0; transform: translateY(2px); }
</style>
