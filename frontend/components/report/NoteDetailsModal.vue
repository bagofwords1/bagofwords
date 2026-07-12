<template>
  <UModal v-model="isOpen" :ui="{ width: 'sm:max-w-2xl' }">
    <div class="p-5">
      <div class="flex items-start justify-between mb-3">
        <div class="flex items-center gap-2 min-w-0">
          <Icon name="heroicons:pencil-square" class="w-4 h-4 text-amber-500 flex-shrink-0" />
          <h3 class="text-sm font-semibold text-gray-900 dark:text-white truncate">
            {{ note?.title || $t('tools.createNote.untitled') }}
          </h3>
          <span class="text-[10px] px-1.5 py-0.5 rounded bg-amber-50 dark:bg-amber-950 text-amber-600 dark:text-amber-400 flex-shrink-0">
            {{ note?.source === 'user' ? $t('notes.byYou') : $t('notes.byAgent') }}
          </span>
        </div>
        <button @click="isOpen = false" class="text-gray-400 hover:text-gray-600 flex-shrink-0">
          <Icon name="heroicons:x-mark" class="w-4 h-4" />
        </button>
      </div>

      <div dir="auto" class="note-markdown text-sm text-gray-700 dark:text-gray-300 max-h-[60vh] overflow-y-auto">
        <MarkdownRender v-if="note?.content" :content="note.content" :final="true" :typewriter="false" :render-code-blocks-as-pre="true" />
      </div>

      <div class="mt-4 pt-3 border-t border-gray-100 dark:border-gray-800 text-[11px] text-gray-400">
        {{ $t('notes.updated') }} {{ formatTime(note?.updated_at) }}
      </div>
    </div>
  </UModal>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import { MarkdownRender } from 'markstream-vue'

const props = defineProps<{ modelValue: boolean; note: any }>()
const emit = defineEmits(['update:modelValue'])

const isOpen = computed({
  get: () => props.modelValue,
  set: (v) => emit('update:modelValue', v),
})

function formatTime(ts?: string): string {
  if (!ts) return ''
  try { return new Date(ts).toLocaleString() } catch { return ts }
}
</script>

<style scoped>
.note-markdown :deep(ul) { list-style: disc; padding-inline-start: 1.25rem; margin: 0.5rem 0; }
.note-markdown :deep(ol) { list-style: decimal; padding-inline-start: 1.25rem; margin: 0.5rem 0; }
.note-markdown :deep(li) { margin: 0.15rem 0; }
.note-markdown :deep(h1), .note-markdown :deep(h2), .note-markdown :deep(h3) { font-weight: 600; margin: 0.75rem 0 0.35rem; }
.note-markdown :deep(code) { font-size: 0.8125rem; background: rgb(243 244 246); border-radius: 0.25rem; padding: 0.05rem 0.3rem; }
:global(.dark) .note-markdown :deep(code) { background: rgb(31 41 55); }
</style>
