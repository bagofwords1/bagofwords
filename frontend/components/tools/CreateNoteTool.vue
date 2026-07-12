<template>
  <div class="mt-1">
    <!-- Status header -->
    <div
      class="flex items-center text-xs text-gray-500 dark:text-gray-400 cursor-pointer hover:text-gray-700 dark:hover:text-gray-300"
      @click="isExpanded = !isExpanded"
    >
      <span v-if="status === 'running'" class="tool-shimmer flex items-center">
        <Icon name="heroicons-pencil-square" class="w-3 h-3 me-1.5 text-gray-400 dark:text-gray-500" />
        {{ $t('tools.createNote.writing') }}
      </span>
      <span v-else-if="isSuccess" class="text-gray-600 dark:text-gray-400 flex items-center">
        <Icon name="heroicons-pencil-square" class="w-3 h-3 me-1.5 text-amber-500" />
        <span dir="auto" class="truncate max-w-[300px]">{{ $t('tools.createNote.wrotePrefix', { text: title || $t('tools.createNote.untitled') }) }}</span>
        <Icon :name="isExpanded ? 'heroicons-chevron-down' : 'heroicons-chevron-right'" class="w-3 h-3 ms-1 text-gray-400 dark:text-gray-500 shrink-0 rtl-flip" />
      </span>
      <span v-else class="text-gray-600 dark:text-gray-400 flex items-center">
        <Icon name="heroicons-x-circle" class="w-3 h-3 me-1.5 text-red-500" />
        <span>{{ $t('tools.createNote.failed') }}</span>
      </span>
    </div>

    <!-- Expandable note content -->
    <Transition name="slide">
      <div v-if="isExpanded && content" class="mt-2 ms-[18px]">
        <div class="rounded-md border border-amber-100 dark:border-amber-950/60 bg-amber-50/40 dark:bg-amber-950/20 px-3 py-2">
          <div v-if="title" class="text-[11px] font-medium text-amber-700 dark:text-amber-400 mb-1">{{ title }}</div>
          <div dir="auto" class="text-xs text-gray-700 dark:text-gray-300 note-markdown">
            <MarkdownRender :content="content" :final="true" :typewriter="false" :render-code-blocks-as-pre="true" />
          </div>
        </div>
      </div>
    </Transition>
  </div>
</template>

<script setup lang="ts">
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
.note-markdown :deep(h1) { font-size: 0.8125rem; font-weight: 600; margin: 0.5rem 0 0.2rem; }
.note-markdown :deep(h2) { font-size: 0.8125rem; font-weight: 600; margin: 0.45rem 0 0.15rem; }
.note-markdown :deep(h3) { font-size: 0.75rem; font-weight: 600; margin: 0.4rem 0 0.15rem; color: rgb(107 114 128); }
.note-markdown :deep(h1:first-child), .note-markdown :deep(h2:first-child) { margin-top: 0; }
</style>
