<template>
  <div class="min-w-0">
    <!-- Header row: status + primary query. Clickable to expand when there's
         more to show (sources, extra queries, or a no-results note). -->
    <ToolCallHeader :tool-execution="toolExecution" icon="heroicons-magnifying-glass" :expanded="toolHeaderExpanded" @toggle="toolHeaderExpanded = !toolHeaderExpanded">
      <template #label>
      <template v-if="status === 'in_progress' || status === 'running'">
        Searching the web
        </template>
      <template v-else-if="isSuccess">Searched the web</template>
      <template v-else>Web search failed</template></template>
    </ToolCallHeader>
    <div v-show="toolHeaderExpanded" class="tool-call-details w-full min-w-0 mt-2">
      <div class="mb-2 flex flex-wrap items-center gap-2 text-chat-3 text-ink-muted">
      <template v-if="status === 'in_progress' || status === 'running'"><span v-if="displayQuery" class="ms-1 truncate max-w-[320px] text-gray-500 dark:text-gray-400">"{{ displayQuery }}"</span></template>
      <template v-else-if="isSuccess"><span v-if="displayQuery" class="ms-1 truncate max-w-[360px] text-gray-600 dark:text-gray-400">"{{ displayQuery }}"</span></template>
      <template v-else><span v-if="displayQuery" class="ms-1 truncate max-w-[360px] text-gray-600 dark:text-gray-400">"{{ displayQuery }}"</span></template><span v-if="sources.length" class="ms-1.5 text-chat-3 text-ink-muted shrink-0">
        · {{ sources.length }} {{ sources.length === 1 ? 'source' : 'sources' }}</span></div>

    <!-- Expanded detail -->
    <div v-if="toolHeaderExpanded" class="mt-1.5 ms-4 space-y-1">
      <!-- Additional queries the provider ran -->
      <div v-if="extraQueries.length" class="space-y-0.5">
        <div v-for="(q, i) in extraQueries" :key="`q${i}`" class="text-chat-2 text-ink-muted truncate max-w-[380px]">"{{ q }}"</div>
      </div>

      <!-- Sources -->
      <div v-if="sources.length" class="space-y-1 pt-0.5">
        <a
          v-for="(s, i) in sources"
          :key="`s${i}`"
          :href="s.url"
          target="_blank"
          rel="noopener noreferrer"
          class="flex items-center gap-1.5 text-chat-2 text-blue-600 hover:underline"
          @click.stop
        >
          <Icon name="heroicons-globe-alt" class="w-3.5 h-3.5 text-ink-muted shrink-0" />
          <span class="truncate max-w-[360px]">{{ s.title || hostOf(s.url) }}</span>
        </a>
      </div>

      <div v-else-if="isSuccess" class="text-chat-2 text-ink-muted">No results found</div>
    </div>
  
    </div>
  </div>
</template>

<script setup lang="ts">
import ToolCallHeader from '~/components/tools/ToolCallHeader.vue'
import { ref as activityRef } from 'vue'
const toolHeaderExpanded = activityRef(false)

import { computed, ref } from 'vue'

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

const hasSourcesField = computed<boolean>(() => Array.isArray(result.value?.sources))
const sources = computed<Array<{ title?: string; url: string }>>(() => {
  const s = result.value?.sources
  return Array.isArray(s) ? s.filter((x: any) => x && x.url) : []
})

// Expandable when there's detail worth revealing.
const expandable = computed<boolean>(() =>
  sources.value.length > 0 || extraQueries.value.length > 0 || (isSuccess.value && hasSourcesField.value)
)
const expanded = ref(false)

function hostOf(url: string): string {
  try { return new URL(url).hostname.replace(/^www\./, '') } catch { return url }
}
</script>

<style scoped>
.tool-call-details { overflow-wrap: anywhere; }
</style>
