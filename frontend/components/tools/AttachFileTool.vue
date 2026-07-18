<template>
  <div class="mt-1">
    <Transition name="fade" appear>
      <div class="mb-2 flex items-center text-xs text-gray-500 dark:text-gray-400">
        <span v-if="status === 'running'" class="tool-shimmer flex items-center">
          <Icon name="heroicons-paper-clip" class="w-3 h-3 me-1 text-gray-400" />
          <span>Attaching {{ requestedCount }} file{{ requestedCount === 1 ? '' : 's' }}…</span>
        </span>
        <span v-else class="text-gray-700 dark:text-gray-300 flex items-center">
          <DataSourceIcon v-if="connIcon" :type="connIcon.type" :connector-key="connIcon.connectorKey" class="w-3 h-3 me-1 shrink-0" />
          <Icon v-else name="heroicons-paper-clip" class="w-3 h-3 me-1 text-gray-400" />
          <span>Attached {{ attachedCount }} file{{ attachedCount === 1 ? '' : 's' }} to the report</span>
          <span v-if="failedCount" class="ms-2 text-[10px] px-1 py-0.5 rounded bg-yellow-50 dark:bg-yellow-900/30 text-yellow-700 dark:text-yellow-500">{{ failedCount }} failed</span>
        </span>
      </div>
    </Transition>

    <Transition name="fade" appear>
      <div v-if="files.length" class="text-xs text-gray-600 dark:text-gray-400">
        <div
          class="flex items-center py-1 px-1 rounded cursor-pointer hover:bg-gray-50 dark:hover:bg-gray-800"
          @click="expanded = !expanded"
        >
          <Icon :name="expanded ? 'heroicons-chevron-down' : 'heroicons-chevron-right'" class="w-3 h-3 text-gray-400 me-1 rtl-flip" />
          <span class="text-gray-500 dark:text-gray-400">{{ files.length }} file{{ files.length === 1 ? '' : 's' }}</span>
        </div>
        <Transition name="fade">
          <div v-if="expanded" class="ps-6 pe-1 pb-1 space-y-1">
            <div
              v-for="(f, i) in files"
              :key="i"
              class="flex items-center py-0.5"
            >
              <Icon
                :name="f.session_file_id ? 'heroicons-document-check' : 'heroicons-exclamation-triangle'"
                :class="['w-3 h-3 me-1.5', f.session_file_id ? 'text-green-600 dark:text-green-500' : 'text-yellow-600']"
              />
              <span class="truncate text-gray-700 dark:text-gray-300">{{ f.name || f.file_id }}</span>
              <span v-if="f.size != null" class="ms-2 text-gray-400 text-[10px]">{{ humanSize(f.size) }}</span>
              <span v-if="f.error" class="ms-2 text-red-600 text-[10px]">{{ f.error }}</span>
            </div>
          </div>
        </Transition>
      </div>
    </Transition>

    <div v-if="status !== 'running' && !files.length && errorMessage" class="text-xs text-red-600 mt-1">{{ errorMessage }}</div>
  </div>
</template>

<script setup lang="ts">
import { computed, ref } from 'vue'
import DataSourceIcon from '~/components/DataSourceIcon.vue'
import { useToolConnectionIcon, FILE_SOURCE_TYPES } from '~/composables/useToolConnectionIcon'

interface ToolExecution {
  id: string
  tool_name: string
  status: string
  result_summary?: string
  result_json?: any
  arguments_json?: any
}

const props = defineProps<{ toolExecution: ToolExecution; dataSources?: any[] }>()

const connIcon = useToolConnectionIcon(
  () => props.toolExecution,
  () => props.dataSources,
  { connectionTypes: FILE_SOURCE_TYPES },
)

const status = computed(() => props.toolExecution?.status || '')
const rj = computed<any>(() => props.toolExecution?.result_json || {})

const requestedCount = computed(() => (props.toolExecution?.arguments_json?.file_ids || []).length || 0)
const files = computed<any[]>(() => rj.value.files || [])
const attachedCount = computed(() => rj.value.attached_count ?? files.value.filter(f => f.session_file_id).length)
const failedCount = computed(() => files.value.filter(f => !f.session_file_id).length)
const errorMessage = computed(() => rj.value.error || '')

const expanded = ref(false)

function humanSize(n: number): string {
  if (n == null) return ''
  if (n < 1024) return `${n} B`
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`
  return `${(n / 1024 / 1024).toFixed(1)} MB`
}
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
</style>
