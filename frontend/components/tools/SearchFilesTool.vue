<template>
  <div class="mt-1">
    <Transition name="fade" appear>
      <div class="mb-2 flex items-center text-chat-2 text-gray-500 dark:text-gray-400 cursor-pointer hover:text-gray-700 dark:hover:text-gray-300">
        <span v-if="status === 'running'" class="flex items-center">
          <Spinner class="w-3 h-3 me-1.5 shrink-0 text-ink-muted" />
          <span class="tool-shimmer">
            <template v-if="modelTitle">{{ modelTitle }}…</template>
            <template v-else>Searching {{ noun.many }}<template v-if="queryLabel"> for {{ queryLabel }}</template>…</template>
          </span>
        </span>
        <span
          v-else
          class="text-gray-700 dark:text-gray-300 flex items-center cursor-pointer"
          @click="expanded = !expanded"
          :aria-expanded="expanded"
        >
          <Icon :name="expanded ? 'heroicons-chevron-down' : 'heroicons-chevron-right'" class="w-3 h-3 me-1 text-ink-muted rtl-flip" />
          <DataSourceIcon v-if="connIcon" :type="connIcon.type" :connector-key="connIcon.connectorKey" class="w-3 h-3 me-1 shrink-0" />
          <Icon v-else name="heroicons-magnifying-glass" class="w-3 h-3 me-1 text-ink-muted" />
          <template v-if="modelTitle">
            <span class="align-middle">{{ modelTitle }}</span>
            <span v-if="files.length" class="ms-2 text-ink-muted">{{ files.length }} {{ files.length === 1 ? noun.one : noun.many }}</span>
          </template>
          <template v-else>
            <span class="align-middle">Searched {{ noun.many }}<template v-if="queryLabel"> for&nbsp;</template></span>
            <Transition v-if="queryLabel" name="fade-in" mode="out-in">
              <span :key="queryLabel || ''" class="align-middle">{{ queryLabel }}</span>
            </Transition>
            <span v-if="files.length" class="ms-2 text-ink-muted">{{ files.length }} {{ files.length === 1 ? noun.one : noun.many }}</span>
          </template>
        </span>
      </div>
    </Transition>

    <Transition name="fade" appear>
      <div v-if="expanded && status !== 'running'" class="text-chat-2 text-gray-600 dark:text-gray-400">
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
              <span
                v-if="matchLabels(f).length"
                data-testid="search-file-match"
                class="ms-2 shrink-0 text-chat-3 px-1.5 py-0.5 rounded bg-emerald-50 text-emerald-700 dark:bg-emerald-900/30 dark:text-emerald-300"
              >{{ matchLabels(f)[0] }}<template v-if="matchLabels(f).length > 1"> +{{ matchLabels(f).length - 1 }}</template></span>
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
                <div v-if="matchLabels(f).length" class="text-chat-3"><span class="text-ink-muted">{{ t('tools.searchFiles.foundIn') }}</span> {{ matchLabels(f).join(', ') }}</div>
                <div v-else-if="Array.isArray(f.matches)" class="text-chat-3"><span class="text-ink-muted">{{ t('tools.searchFiles.foundIn') }}</span> {{ t('tools.searchFiles.fileName') }}</div>
                <div v-if="indexStatusNote(f)" class="text-chat-3 text-amber-600 dark:text-amber-400">{{ indexStatusNote(f) }}</div>
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
        <div v-if="indexNote" data-testid="search-index-note" class="ms-1 mt-1 text-chat-3 text-ink-muted">{{ indexNote }}</div>
        <ToolCallParams :params="toolExecution?.arguments_json" />
      </div>
    </Transition>

    <div v-if="status !== 'running' && !files.length && errorMessage" class="text-chat-2 text-amber-600 mt-1">{{ errorMessage }}</div>
  </div>
</template>

<script setup lang="ts">
import { computed, ref } from 'vue'
import Spinner from '~/components/Spinner.vue'
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
  arguments_json?: any
}

const props = defineProps<{ toolExecution: ToolExecution; dataSources?: any[] }>()
const { t } = useResponseI18n()
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

const queryLabel = computed(() => {
  const q = props.toolExecution?.arguments_json?.query
    ?? props.toolExecution?.result_json?.query
  if (typeof q === 'string') return `"${q}"`
  return ''
})

const files = computed<any[]>(() => {
  const rj = props.toolExecution?.result_json || {}
  return Array.isArray(rj.files) ? rj.files : []
})

const errorMessage = computed(() => toolErrorText(props.toolExecution?.result_json?.error))

// Full-text index hits say where in the file the words were found.
function matchLabels(f: any): string[] {
  const ms = Array.isArray(f?.matches) ? f.matches : []
  return ms.map((m: any) => {
    if (m.kind === 'page') return t('tools.searchFiles.page', { n: m.page })
    if (m.kind === 'sheet') return t('tools.searchFiles.sheet', { name: m.sheet })
    if (m.kind === 'chars' || m.kind === 'bytes') return t('tools.searchFiles.offset', { n: Number(m.offset || 0).toLocaleString() })
    return ''
  }).filter(Boolean)
}

function indexStatusNote(f: any): string {
  if (f?.index_status === 'partial') return t('tools.searchFiles.partlyIndexed')
  if (f?.index_status === 'no_text') return t('tools.searchFiles.noText')
  if (f?.index_status === 'error') return t('tools.searchFiles.notIndexed')
  return ''
}

// Which index answered, so a reader can tell a ranked full-text answer from a
// keyword match or a live scan.
const indexNote = computed(() => {
  const rj = props.toolExecution?.result_json || {}
  const key = ({ fulltext: 'indexFulltext', keywords: 'indexKeywords', names: 'indexNames' } as any)[rj.index]
    || (rj.source === 'live' ? 'liveScan' : '')
  if (!key) return ''
  const how = t(`tools.searchFiles.${key}`)
  const parts = [rj.source === 'catalog+live' ? t('tools.searchFiles.plusLiveScan', { index: how }) : how]
  if (rj.indexed_at) {
    parts.push(t('tools.searchFiles.indexedAt', { when: String(rj.indexed_at).replace('T', ' ').replace('Z', ' UTC') }))
  }
  return parts.join(' · ')
})

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
@keyframes shimmer { 0% { background-position: -100% 0; } 100% { background-position: 100% 0; } }
.tool-shimmer {
  background: linear-gradient(90deg, #888 0%, #999 25%, #ccc 50%, #999 75%, #888 100%);
  background-size: 200% 100%;
  -webkit-background-clip: text;
  background-clip: text;
  color: transparent;
  animation: shimmer 2s linear infinite;
}
.fade-enter-active, .fade-leave-active { transition: opacity 0.25s ease, transform 0.25s ease; }
.fade-enter-from, .fade-leave-to { opacity: 0; transform: translateY(2px); }
</style>
