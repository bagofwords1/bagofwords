<template>
  <div class="mb-2">
    <!-- Main Header -->
    <div class="flex items-center text-xs text-gray-500 dark:text-gray-400 cursor-pointer hover:text-gray-700 dark:hover:text-gray-300" @click="toggleCollapsed">
      <Icon :name="isCollapsed ? 'heroicons-chevron-right' : 'heroicons-chevron-down'" class="w-3 h-3 me-1.5 text-gray-400 rtl-flip" />
      <Spinner v-if="status === 'running'" class="w-3 h-3 me-1.5 text-gray-400" />
      <Icon v-else-if="status === 'success'" name="heroicons-document-text" class="w-3 h-3 me-1.5 text-blue-500" />
      <Icon v-else-if="status === 'error'" name="heroicons-exclamation-circle" class="w-3 h-3 me-1.5 text-amber-500" />

      <span v-if="status === 'running'" class="tool-shimmer">Reading report…</span>
      <span v-else-if="status === 'success' && found" class="text-gray-700 dark:text-gray-300">{{ successLabel }}</span>
      <span v-else-if="status === 'success' && !found" class="text-gray-700 dark:text-gray-300 italic">Report not found</span>
      <span v-else-if="status === 'error'" class="text-gray-700 dark:text-gray-300">Failed to read report</span>
      <span v-else class="text-gray-700 dark:text-gray-300">Read report</span>

      <span v-if="reportMode && reportMode !== 'chat' && status === 'success' && found" class="ms-2 px-1.5 py-0.5 rounded text-[10px] font-medium bg-gray-100 dark:bg-gray-800 text-gray-600 dark:text-gray-400">
        {{ reportMode }}
      </span>
    </div>

    <!-- Collapsible content -->
    <Transition name="fade">
      <div v-if="!isCollapsed && status === 'success' && found" class="mt-2 ms-4 space-y-2 text-xs text-gray-600 dark:text-gray-400">
        <!-- Meta -->
        <div class="flex flex-wrap items-center gap-x-3 gap-y-1 text-[11px] text-gray-500 dark:text-gray-400">
          <span v-if="reportStatus" class="px-1 py-0.5 rounded" :class="reportStatus === 'published' ? 'bg-green-50 dark:bg-green-950 text-green-600' : 'bg-gray-100 dark:bg-gray-800 text-gray-500 dark:text-gray-400'">{{ reportStatus }}</span>
          <span v-if="dataSources.length">Data: {{ dataSources.join(', ') }}</span>
          <span v-if="artifacts.length">{{ artifacts.length }} artifact{{ artifacts.length === 1 ? '' : 's' }}</span>
          <span v-if="conversation.length">{{ conversation.length }} message{{ conversation.length === 1 ? '' : 's' }}</span>
        </div>

        <!-- Artifacts -->
        <div v-if="artifacts.length">
          <div class="text-[11px] font-medium text-gray-500 dark:text-gray-400 mb-0.5">Artifacts</div>
          <ul class="space-y-0.5">
            <li v-for="a in artifacts" :key="a.id" class="flex items-center">
              <Icon name="heroicons-chart-bar-square" class="w-3 h-3 me-1 text-gray-400" />
              <span class="truncate text-gray-700 dark:text-gray-300">{{ a.title || 'Untitled' }}</span>
              <span v-if="a.mode" class="ms-1 text-[9px] text-gray-400">{{ a.mode }}<template v-if="a.version"> v{{ a.version }}</template></span>
            </li>
          </ul>
        </div>

        <!-- Conversation -->
        <div v-if="conversation.length">
          <div class="text-[11px] font-medium text-gray-500 dark:text-gray-400 mb-0.5">Conversation</div>
          <ul class="space-y-1">
            <li v-for="(m, i) in conversation" :key="i" class="leading-snug">
              <span class="text-[9px] uppercase tracking-wide me-1" :class="m.role === 'user' ? 'text-blue-500' : 'text-gray-400'">{{ m.role }}</span>
              <span class="text-gray-700 dark:text-gray-300">{{ truncate(m.content) }}</span>
            </li>
          </ul>
        </div>
      </div>
    </Transition>

    <!-- Error / not-found message -->
    <div v-if="(status === 'error' || (status === 'success' && !found)) && message" class="mt-1 ms-4 text-xs text-gray-500 dark:text-gray-400">
      {{ message }}
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, ref } from 'vue'
import Spinner from '~/components/Spinner.vue'

interface Props {
  toolExecution: {
    id: string
    tool_name: string
    tool_action?: string
    arguments_json?: { report_id?: string }
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
