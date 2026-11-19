<template>
  <div class="min-w-0">
    <!-- Status header -->
    <ToolCallHeader :tool-execution="toolExecution" icon="heroicons-document-text" :expanded="toolHeaderExpanded" @toggle="toolHeaderExpanded = !toolHeaderExpanded">
      <template #label>
      <template v-if="status === 'running'">{{ t('tools.createNote.writing') }}</template>
      <template v-else-if="isSuccess">{{ t('tools.createNote.wrotePrefix', { text: title || t('tools.createNote.untitled') }) }}</template>
      <template v-else>{{ t('tools.createNote.failed') }}</template></template>
    </ToolCallHeader>
    <div v-show="toolHeaderExpanded" class="tool-call-details w-full min-w-0 mt-2">

    <!-- Expandable note content -->
    <Transition name="slide">
      <div v-if="toolHeaderExpanded && content" class="mt-2 ms-[18px]">
        <div class="rounded-md border border-amber-100 dark:border-amber-950/60 bg-amber-50/40 dark:bg-amber-950/20 px-3 py-2">
          <div v-if="title" dir="auto" class="text-chat-2 font-medium text-amber-700 dark:text-amber-400 mb-1">{{ title }}</div>
          <div class="text-chat-2 text-gray-700 dark:text-gray-300 note-markdown">
            <MarkdownRender :content="content" :final="true" :typewriter="false" :render-code-blocks-as-pre="true" />
          </div>
        </div>
      </div>
    </Transition>
  
    </div>
  </div>
</template>

<script setup lang="ts">
import ToolCallHeader from '~/components/tools/ToolCallHeader.vue'
import { ref as activityRef } from 'vue'
const toolHeaderExpanded = activityRef(false)

const { t } = useResponseI18n()

import { computed, ref } from 'vue'
import { MarkdownRender } from 'markstream-vue'

interface Props {
  toolExecution: {
    tool_name: string
    arguments_json?: { content?: string; title?: string }
    result_json?: { note_id?: string; title?: string; error?: string }
    status: string
  }
  readonly?: boolean
}
const props = defineProps<Props>()

const isExpanded = ref(false)
const status = computed(() => props.toolExecution.status)
const isSuccess = computed(() => status.value === 'success')
const title = computed(() => props.toolExecution.result_json?.title || props.toolExecution.arguments_json?.title || '')
const content = computed(() => props.toolExecution.arguments_json?.content || '')
</script>

<style scoped>
.note-markdown :deep(ul) { list-style: disc; padding-inline-start: 1.1rem; margin: 0.25rem 0; }
.note-markdown :deep(li) { margin: 0.1rem 0; }
/* Note-scale headings — MarkdownRender's defaults are far too large in a card */
.note-markdown :deep(h1) { font-size: var(--bow-chat-font-size-2); font-weight: 600; margin: 0.5rem 0 0.2rem; }
.note-markdown :deep(h2) { font-size: var(--bow-chat-font-size-2); font-weight: 600; margin: 0.45rem 0 0.15rem; }
.note-markdown :deep(h3) { font-size: var(--bow-chat-font-size-2); font-weight: 600; margin: 0.4rem 0 0.15rem; color: rgb(107 114 128); }
.note-markdown :deep(h1:first-child), .note-markdown :deep(h2:first-child) { margin-top: 0; }
</style>
