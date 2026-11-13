<template>
  <div class="mb-2">
    <!-- Main Header -->
    <div class="flex items-center text-chat-2 text-gray-500 dark:text-gray-400 cursor-pointer hover:text-gray-700 dark:hover:text-gray-300" @click="toggleCollapsed">
      <Icon :name="isCollapsed ? 'heroicons-chevron-right' : 'heroicons-chevron-down'" class="w-3 h-3 me-1.5 text-ink-muted rtl-flip" />
      <Spinner v-if="status === 'running'" class="w-3 h-3 me-1.5 text-ink-muted" />
      <Icon v-else-if="status === 'success'" name="heroicons-document-text" class="w-3 h-3 me-1.5 text-blue-500" />
      <Icon v-else-if="status === 'error'" name="heroicons-exclamation-circle" class="w-3 h-3 me-1.5 text-amber-500" />

      <span v-if="status === 'running'" class="tool-shimmer">Reading report…</span>
      <span v-else-if="status === 'success' && found" class="text-gray-700 dark:text-gray-300">{{ successLabel }}</span>
      <span v-else-if="status === 'success' && !found" class="text-gray-700 dark:text-gray-300 italic">Report not found</span>
      <span v-else-if="status === 'error'" class="text-gray-700 dark:text-gray-300">Failed to read report</span>
      <span v-else class="text-gray-700 dark:text-gray-300">Read report</span>

      <span v-if="reportMode && reportMode !== 'chat' && status === 'success' && found" class="ms-2 px-1.5 py-0.5 rounded text-chat-3 font-medium bg-gray-100 dark:bg-gray-800 text-gray-600 dark:text-gray-400">
        {{ reportMode }}
      </span>
      <!-- Paging position, like read_file's window badge. -->
      <span v-if="anyTruncated" class="ms-2 text-chat-3 text-yellow-600">{{ t('tools.readFile.truncated') }}</span>
      <span v-if="windowLabel" class="ms-2 text-chat-3 px-1 py-0.5 rounded bg-blue-50 dark:bg-blue-900/30 text-blue-600 dark:text-blue-400">{{ windowLabel }}</span>
    </div>

    <!-- Collapsible content -->
    <Transition name="fade">
      <div v-if="!isCollapsed && status === 'success' && found" class="mt-2 ms-4 space-y-2 text-chat-2 text-gray-600 dark:text-gray-400">
        <!-- Meta -->
        <div class="flex flex-wrap items-center gap-x-3 gap-y-1 text-chat-3 text-gray-500 dark:text-gray-400">
          <span v-if="reportStatus" class="px-1 py-0.5 rounded" :class="reportStatus === 'published' ? 'bg-green-50 dark:bg-green-950 text-green-600' : 'bg-gray-100 dark:bg-gray-800 text-gray-500 dark:text-gray-400'">{{ reportStatus }}</span>
          <span v-if="dataSources.length">Data: {{ dataSources.join(', ') }}</span>
          <span v-if="artifacts.length">{{ artifacts.length }} artifact{{ artifacts.length === 1 ? '' : 's' }}</span>
          <span v-if="conversation.length">{{ conversation.length }} message{{ conversation.length === 1 ? '' : 's' }}</span>
        </div>

        <!-- Artifacts -->
        <div v-if="artifacts.length">
          <div class="text-chat-3 font-medium text-gray-500 dark:text-gray-400 mb-0.5">Artifacts</div>
          <ul class="space-y-0.5">
            <li v-for="a in artifacts" :key="a.id" class="flex items-center">
              <Icon name="heroicons-chart-bar-square" class="w-3 h-3 me-1 text-ink-muted" />
              <span class="truncate text-gray-700 dark:text-gray-300">{{ a.title || 'Untitled' }}</span>
              <span v-if="a.mode" class="ms-1 text-chat-3 text-ink-muted">{{ a.mode }}<template v-if="a.version"> v{{ a.version }}</template></span>
            </li>
          </ul>
        </div>

        <!-- Conversation -->
        <div v-if="conversation.length">
          <div class="text-chat-3 font-medium text-gray-500 dark:text-gray-400 mb-0.5">Conversation</div>
          <ul class="space-y-1">
            <li v-for="(m, i) in conversation" :key="m.index ?? i" class="leading-snug">
              <span v-if="m.index != null" class="text-chat-3 text-ink-muted me-1 tabular-nums" dir="ltr">#{{ m.index }}</span>
              <span class="text-chat-3 uppercase tracking-wide me-1" :class="m.role === 'user' ? 'text-blue-500' : 'text-ink-muted'">{{ m.role }}</span>
              <span class="text-gray-700 dark:text-gray-300">{{ truncate(m.content) }}</span>
              <span v-if="m.truncated" class="ms-1 text-chat-3 text-yellow-600">{{ t('tools.readReport.messageTruncated', { shown: (contentStart + String(m.content || '').length), total: m.content_chars }) }}</span>
            </li>
          </ul>
        </div>
        <ToolCallParams :params="toolExecution.arguments_json" />
      </div>
    </Transition>

    <!-- Error / not-found message -->
    <div v-if="(status === 'error' || (status === 'success' && !found)) && message" class="mt-1 ms-4 text-chat-2 text-gray-500 dark:text-gray-400">
      {{ message }}
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, ref } from 'vue'
import Spinner from '~/components/Spinner.vue'
import ToolCallParams from '~/components/tools/ToolCallParams.vue'

const { t } = useResponseI18n()

interface Props {
  toolExecution: {
    id: string
    tool_name: string
    tool_action?: string
    arguments_json?: { report_id?: string, offset?: number, limit?: number, content_offset?: number, max_content_chars?: number }
    result_json?: any
    status: string
    result_summary?: string
  }
  readonly?: boolean
}

const props = defineProps<Props>()
const isCollapsed = ref(true)

const status = computed(() => props.toolExecution.status)
const rj = computed<any>(() => props.toolExecution.result_json || {})

const found = computed<boolean>(() => rj.value?.success === true)
const reportTitle = computed<string>(() => rj.value?.title || 'Untitled')
const reportMode = computed<string>(() => rj.value?.mode || '')
const reportStatus = computed<string>(() => rj.value?.status || '')
const dataSources = computed<string[]>(() => Array.isArray(rj.value?.data_sources) ? rj.value.data_sources : [])
const artifacts = computed<any[]>(() => Array.isArray(rj.value?.artifacts) ? rj.value.artifacts : [])
const conversation = computed<any[]>(() => Array.isArray(rj.value?.conversation) ? rj.value.conversation : [])
const message = computed<string>(() => rj.value?.message || rj.value?.error || '')

// Paging state. `page` is absent on executions from before pagination existed;
// those keep the old card with no badge.
const page = computed<any>(() => rj.value?.page || null)
const contentStart = computed<number>(() => Number(props.toolExecution.arguments_json?.content_offset || 0))
const anyTruncated = computed<boolean>(() => conversation.value.some((m: any) => m?.truncated))
const windowLabel = computed<string>(() => {
  const p = page.value
  if (!p || !p.total_messages) return ''
  // A read into one message's text: show which characters of it.
  if (p.returned === 1 && contentStart.value > 0) {
    const m = conversation.value[0] || {}
    const end = contentStart.value + String(m.content || '').length
    return t('tools.readReport.charWindow', { index: m.index ?? p.offset, start: contentStart.value, end, total: m.content_chars ?? end })
  }
  if (!p.returned) return t('tools.readReport.emptyWindow', { offset: p.offset, total: p.total_messages })
  return t('tools.readReport.window', { start: p.offset, end: p.offset + p.returned - 1, total: p.total_messages })
})

const successLabel = computed(() => `Read "${reportTitle.value}"`)

function toggleCollapsed() {
  isCollapsed.value = !isCollapsed.value
}

function truncate(text: string): string {
  const t = String(text || '')
  return t.length > 200 ? t.slice(0, 197) + '…' : t
}
</script>

<style scoped>
.fade-enter-active,
.fade-leave-active { transition: opacity 0.25s ease; }
.fade-enter-from,
.fade-leave-to { opacity: 0; }

@keyframes shimmer { 0% { background-position: -100% 0; } 100% { background-position: 100% 0; } }
.tool-shimmer {
  background: linear-gradient(90deg, #888 0%, #999 25%, #ccc 50%, #999 75%, #888 100%);
  background-size: 200% 100%;
  -webkit-background-clip: text;
  background-clip: text;
  color: transparent;
  animation: shimmer 2s linear infinite;
  font-weight: 400;
  opacity: 1;
}
</style>
