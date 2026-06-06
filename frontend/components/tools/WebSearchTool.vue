<template>
  <div class="mt-1">
    <Transition name="fade" appear>
      <div class="flex items-center text-xs text-gray-500">
        <span v-if="status === 'in_progress' || status === 'running'" class="tool-shimmer flex items-center">
          <Icon name="heroicons-magnifying-glass" class="w-3 h-3 me-1.5 text-gray-400" />
          Searching the web
          <span v-if="displayQuery" class="ms-1 truncate max-w-[320px] text-gray-500">“{{ displayQuery }}”</span>
        </span>
        <span v-else-if="isSuccess" class="text-gray-600 flex items-center">
          <Icon name="heroicons-magnifying-glass" class="w-3 h-3 me-1.5 text-green-500" />
          <span>Searched the web</span>
          <span v-if="displayQuery" class="ms-1 truncate max-w-[360px] text-gray-600">“{{ displayQuery }}”</span>
        </span>
        <span v-else class="text-gray-600 flex items-center">
          <Icon name="heroicons-magnifying-glass" class="w-3 h-3 me-1.5 text-orange-500" />
          <span>Web search failed</span>
          <span v-if="displayQuery" class="ms-1 truncate max-w-[360px] text-gray-600">“{{ displayQuery }}”</span>
        </span>
      </div>
    </Transition>

    <!-- Additional queries beyond the first, when the provider ran several -->
    <div v-if="extraQueries.length" class="mt-1 ms-4 text-[11px] text-gray-400 space-y-0.5">
      <div v-for="(q, i) in extraQueries" :key="i" class="truncate max-w-[360px]">“{{ q }}”</div>
    </div>

    <!-- Sources found this turn (turn-level citations from the provider) -->
    <div v-if="sources.length" class="mt-1.5 ms-4 space-y-0.5">
      <a
        v-for="(s, i) in sources"
        :key="i"
        :href="s.url"
        target="_blank"
        rel="noopener noreferrer"
        class="block text-[11px] text-blue-600 hover:underline truncate max-w-[360px]"
      >{{ s.title || s.url }}</a>
    </div>
    <div v-else-if="isSuccess && hasSourcesField" class="mt-1 ms-4 text-[11px] text-gray-400">
      No results found
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed } from 'vue'

interface ToolExecution {
  id: string
  tool_name: string
  status: string
  result_json?: any
  arguments_json?: any
}

interface Props {
  toolExecution: ToolExecution
}

const props = defineProps<Props>()

const status = computed<string>(() => props.toolExecution?.status || '')
const result = computed<any>(() => props.toolExecution?.result_json || {})
const args = computed<any>(() => props.toolExecution?.arguments_json || {})

const isSuccess = computed(() => status.value === 'success' || status.value === 'completed')

const queries = computed<string[]>(() => {
  const qs = result.value?.queries || args.value?.queries
  if (Array.isArray(qs) && qs.length) return qs.filter(Boolean)
  const single = result.value?.query || args.value?.query
  return single ? [single] : []
})

const displayQuery = computed<string>(() => queries.value[0] || '')
const extraQueries = computed<string[]>(() => queries.value.slice(1))

// Sources are attached (turn-level) only to the last search of a turn.
const hasSourcesField = computed<boolean>(() => Array.isArray(result.value?.sources))
const sources = computed<Array<{ title?: string; url: string }>>(() => {
  const s = result.value?.sources
  return Array.isArray(s) ? s.filter((x: any) => x && x.url) : []
})
</script>

<style scoped>
.tool-shimmer {
  animation: shimmer 1.6s linear infinite;
  background: linear-gradient(90deg, rgba(0,0,0,0) 0%, rgba(160,160,160,0.15) 50%, rgba(0,0,0,0) 100%);
  background-size: 300% 100%;
  background-clip: text;
}

@keyframes shimmer {
  0% { background-position: 0% 0; }
  100% { background-position: 100% 0; }
}

.fade-enter-active, .fade-leave-active {
  transition: opacity 0.2s ease;
}
.fade-enter-from, .fade-leave-to {
  opacity: 0;
}
</style>
