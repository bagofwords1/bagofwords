<template>
  <div class="min-w-0">
    <!-- Header -->
    <ToolCallHeader :tool-execution="toolExecution" icon="heroicons-document-text" :expanded="toolHeaderExpanded" @toggle="toolHeaderExpanded = !toolHeaderExpanded">
      <template #label>
      <template v-if="status === 'running'">{{ t('tools.editDoc.editing') }}</template>
      <template v-else-if="status === 'success'">{{ t('tools.editDoc.edited') }}</template>
      <template v-else-if="status === 'error'">{{ t('tools.editDoc.failed') }}</template>
      <template v-else>{{ t('tools.editDoc.label') }}</template>
      <template v-if="status === 'success' && diffApplied !== null">{{ diffApplied ? t('tools.editDoc.diff') : t('tools.editDoc.rewrite') }}</template></template>
    </ToolCallHeader>
    <div v-show="toolHeaderExpanded" class="tool-call-details w-full min-w-0 mt-2">
      <div class="mb-2 flex flex-wrap items-center gap-2 text-chat-3 text-ink-muted"><span v-if="docVersion" class="ms-1.5 px-1.5 py-0.5 rounded text-chat-3 font-medium bg-gray-100 dark:bg-gray-800 text-gray-600 dark:text-gray-400">
        v{{ docVersion }}</span><span v-if="formatDuration" class="ms-1.5 text-ink-muted">{{ formatDuration }}</span></div>

    <!-- Expanded content -->
    <template v-if="toolHeaderExpanded">
      <div v-if="editsCount > 0" class="mt-1 ms-[18px] text-chat-2 text-ink-muted">
        {{ t('tools.editDoc.editsCount', { n: editsCount }) }}
      </div>
      <div v-if="status === 'error' && errorMessage" class="mt-1 ms-[18px] text-chat-2 text-gray-500 dark:text-gray-400">
        {{ errorMessage }}
      </div>
    </template>

    <!-- Preview Card -->
    <div
      v-if="status === 'success' && docId"
      class="mt-1.5 ms-[18px] cursor-pointer group"
      @click="emit('openArtifact', { artifactId: docId })"
    >
      <div class="flex items-center gap-2.5 px-2 py-1.5 rounded-md border border-gray-200 dark:border-gray-700 hover:border-gray-300 dark:hover:border-gray-600 hover:bg-gray-50 dark:hover:bg-gray-800 transition-all max-w-xs">
        <div class="w-10 h-10 rounded flex-shrink-0 flex items-center justify-center bg-emerald-50 dark:bg-emerald-950">
          <Icon name="heroicons:document-text" class="w-5 h-5 text-emerald-500" />
        </div>
        <div class="flex-1 min-w-0">
          <div class="text-chat-2 font-medium text-gray-700 dark:text-gray-300 truncate">
            {{ docTitle || t('tools.createDoc.untitled') }}
          </div>
          <div class="text-chat-3 text-ink-muted">{{ t('tools.createDoc.openDocument') }}</div>
        </div>
        <Icon name="heroicons:arrow-top-right-on-square" class="w-3.5 h-3.5 text-ink-muted group-hover:text-gray-600 dark:group-hover:text-gray-400 flex-shrink-0" />
      </div>
    </div>
  
    </div>
  </div>
</template>

<script setup lang="ts">
import ToolCallHeader from '~/components/tools/ToolCallHeader.vue'
import { ref as activityRef } from 'vue'
const toolHeaderExpanded = activityRef(false)

const { t } = useResponseI18n()

import { computed, ref } from 'vue'
import { toolErrorText } from '~/utils/toolError'

interface Props {
  toolExecution: {
    id: string
    tool_name: string
    arguments_json?: { doc_id?: string; title?: string; edits?: any[]; markdown?: string }
    result_json?: {
      doc_id?: string
      title?: string
      version?: number
      visualization_ids?: string[]
      diff_applied?: boolean
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
const docVersion = computed(() => props.toolExecution.result_json?.version)
const diffApplied = computed(() => props.toolExecution.result_json?.diff_applied ?? null)
const editsCount = computed(() => (props.toolExecution.arguments_json?.edits || []).length)
const errorMessage = computed(() => toolErrorText(props.toolExecution.result_json?.error) || props.toolExecution.result_summary || '')

const formatDuration = computed(() => {
  const ms = props.toolExecution.duration_ms
  if (!ms) return ''
  return ms >= 1000 ? `${(ms / 1000).toFixed(1)}s` : `${ms}ms`
})
</script>
