<template>
  <div v-if="entries.length" class="text-xs text-gray-600 dark:text-gray-400">
    <div
      class="flex items-center py-1 px-1 rounded cursor-pointer hover:bg-gray-50 dark:hover:bg-gray-800"
      @click="open = !open"
      :aria-expanded="open"
    >
      <Icon :name="open ? 'heroicons-chevron-down' : 'heroicons-chevron-right'" class="w-3 h-3 text-gray-400 me-1 rtl-flip" />
      <span class="text-gray-500 dark:text-gray-400">Parameters</span>
      <span class="ms-2 text-[10px] text-gray-400 dark:text-gray-500">({{ entries.length }})</span>
    </div>
    <Transition name="fade">
      <div v-if="open" class="ps-6 pe-1 pb-1 space-y-0.5">
        <div v-for="[k, v] in entries" :key="k" class="text-[11px] leading-snug">
          <span class="text-gray-400 dark:text-gray-500">{{ k }}:</span>
          <code class="ms-1 px-1 py-0.5 rounded bg-gray-100 dark:bg-gray-800 text-gray-600 dark:text-gray-400 break-all">{{ fmt(v) }}</code>
        </div>
      </div>
    </Transition>
  </div>
</template>

<script setup lang="ts">
// Shared "show me what the agent actually asked for" block: renders a tool
// call's arguments_json behind a collapsed toggle. Used by the file tools
// (list_files / search_files / read_file / grep_files) so paging state
// (offset, cursor), scopes (folder, name_pattern) and flags are inspectable
// from the UI instead of invisible.
import { computed, ref } from 'vue'

const props = defineProps<{
  params?: Record<string, any> | null
  /** Keys to hide. `title` is UI chrome (already shown as the row label). */
  exclude?: string[]
}>()

const open = ref(false)

const entries = computed<[string, any][]>(() => {
  const src = props.params
  if (!src || typeof src !== 'object') return []
  const hidden = new Set(props.exclude ?? ['title'])
  return Object.entries(src).filter(([k, v]) =>
    !hidden.has(k) && v !== null && v !== undefined && v !== ''
  )
})

function fmt(v: any): string {
  if (typeof v === 'string') return v
  try { return JSON.stringify(v) } catch { return String(v) }
}
</script>

<style scoped>
.fade-enter-active, .fade-leave-active { transition: opacity 0.25s ease, transform 0.25s ease; }
.fade-enter-from, .fade-leave-to { opacity: 0; transform: translateY(2px); }
</style>
