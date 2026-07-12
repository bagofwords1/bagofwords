<template>
  <div class="mb-2">
    <!-- Header -->
    <div class="flex items-center text-xs text-gray-500 dark:text-gray-400 cursor-pointer hover:text-gray-700 dark:hover:text-gray-300" @click="isCollapsed = !isCollapsed">
      <Icon :name="isCollapsed ? 'heroicons-chevron-right' : 'heroicons-chevron-down'" class="w-3 h-3 me-1.5 text-gray-400 dark:text-gray-500 rtl-flip" />
      <Spinner v-if="status === 'running'" class="w-3 h-3 me-1.5 text-gray-400 dark:text-gray-500" />
      <Icon v-else-if="status === 'success'" name="heroicons-check" class="w-3 h-3 me-1.5 text-green-500" />
      <Icon v-else-if="status === 'error'" name="heroicons-exclamation-circle" class="w-3 h-3 me-1.5 text-amber-500" />
      <Icon v-else name="heroicons-document-text" class="w-3 h-3 me-1.5 text-gray-400" />

      <span v-if="status === 'running'" class="tool-shimmer">{{ $t('tools.createDoc.writing') }}</span>
      <span v-else-if="status === 'success'" class="text-gray-700 dark:text-gray-300">{{ $t('tools.createDoc.created') }}</span>
      <span v-else-if="status === 'error'" class="text-gray-700 dark:text-gray-300">{{ $t('tools.createDoc.failed') }}</span>
      <span v-else class="text-gray-700 dark:text-gray-300">{{ $t('tools.createDoc.label') }}</span>

      <span v-if="vizCount > 0" class="ms-2 px-1.5 py-0.5 rounded text-[10px] font-medium bg-gray-100 dark:bg-gray-800 text-gray-600 dark:text-gray-400">
        {{ $t('tools.createDoc.chartsCount', { n: vizCount }) }}
      </span>
      <span v-if="formatDuration" class="ms-1.5 text-gray-400 dark:text-gray-500">{{ formatDuration }}</span>
    </div>

    <!-- Expanded content -->
    <template v-if="!isCollapsed">
      <!-- Outline -->
      <div v-if="outline.length" class="mt-1 ms-[18px] text-xs text-gray-400 dark:text-gray-500 space-y-0.5">
        <div v-for="(h, i) in outline" :key="i" class="truncate font-mono text-[11px]">{{ h }}</div>
      </div>
      <!-- Error -->
      <div v-if="status === 'error' && errorMessage" class="mt-1 ms-[18px] text-xs text-gray-500 dark:text-gray-400">
        {{ errorMessage }}
      </div>
    </template>

    <!-- Preview Card -->
    <div
      v-if="(status === 'success' && docId) || status === 'running'"
      class="mt-1.5 ms-[18px] cursor-pointer group"
      @click="openDoc"
    >
      <div class="flex items-center gap-2.5 px-2 py-1.5 rounded-md border border-gray-200 dark:border-gray-700 hover:border-gray-300 dark:hover:border-gray-600 hover:bg-gray-50 dark:hover:bg-gray-800 transition-all max-w-xs">
        <div class="w-10 h-10 rounded flex-shrink-0 flex items-center justify-center bg-emerald-50 dark:bg-emerald-950">
          <Icon name="heroicons:document-text" class="w-5 h-5 text-emerald-500" />
        </div>
        <div class="flex-1 min-w-0">
          <div class="text-xs font-medium text-gray-700 dark:text-gray-300 truncate">
            {{ docTitle || $t('tools.createDoc.untitled') }}
          </div>
          <div class="text-[10px] text-gray-400 dark:text-gray-500">
            {{ status === 'running' ? $t('tools.createDoc.writing') : $t('tools.createDoc.openDocument') }}
          </div>
        </div>
        <Icon name="heroicons:arrow-top-right-on-square" class="w-3.5 h-3.5 text-gray-400 dark:text-gray-500 group-hover:text-gray-600 dark:group-hover:text-gray-400 flex-shrink-0" />
      </div>
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
    arguments_json?: { title?: string; markdown?: string }
    result_json?: {
      doc_id?: string
      title?: string
      version?: number
      visualization_ids?: string[]
      outline?: string[]
      error?: string
    }
    status: string
    result_summary?: string
    duration_ms?: number
  }
  readonly?: boolean
}

const props = defineProps<Props>()
const emit = defineEmits(['openArtifact'])

const isCollapsed = ref(true)
const status = computed(() => props.toolExecution.status)
const docId = computed(() => props.toolExecution.result_json?.doc_id)
const docTitle = computed(() => props.toolExecution.result_json?.title || props.toolExecution.arguments_json?.title || '')
const outline = computed(() => props.toolExecution.result_json?.outline || [])
const vizCount = computed(() => (props.toolExecution.result_json?.visualization_ids || []).length)
const errorMessage = computed(() => props.toolExecution.result_json?.error || props.toolExecution.result_summary || '')

const formatDuration = computed(() => {
  const ms = props.toolExecution.duration_ms
  if (!ms) return ''
  return ms >= 1000 ? `${(ms / 1000).toFixed(1)}s` : `${ms}ms`
})

function openDoc() {
  if (docId.value) emit('openArtifact', { artifactId: docId.value })
  else emit('openArtifact', { loading: true })
}
</script>
