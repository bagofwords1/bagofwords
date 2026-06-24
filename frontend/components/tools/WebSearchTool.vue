<template>
  <div class="mt-1">
    <!-- Header row: status + primary query. Clickable to expand when there's
         more to show (sources, extra queries, or a no-results note). -->
    <div
      class="flex items-center text-xs"
      :class="expandable ? 'cursor-pointer select-none' : ''"
      @click="expandable && (expanded = !expanded)"
    >
      <Icon
        v-if="expandable"
        :name="expanded ? 'heroicons-chevron-down' : 'heroicons-chevron-right'"
        class="w-3 h-3 me-1 text-gray-400 shrink-0"
      />
      <span v-if="status === 'in_progress' || status === 'running'" class="tool-shimmer flex items-center">
        <Icon name="heroicons-magnifying-glass" class="w-3 h-3 me-1.5 text-gray-400" />
        Searching the web
        <span v-if="displayQuery" class="ms-1 truncate max-w-[320px] text-gray-500 dark:text-gray-400">"{{ displayQuery }}"</span>
      </span>
      <span v-else-if="isSuccess" class="text-gray-600 dark:text-gray-400 flex items-center">
        <Icon name="heroicons-magnifying-glass" class="w-3 h-3 me-1.5 text-green-500" />
        <span>Searched the web</span>
        <span v-if="displayQuery" class="ms-1 truncate max-w-[360px] text-gray-600 dark:text-gray-400">"{{ displayQuery }}"</span>
      </span>
      <span v-else class="text-gray-600 dark:text-gray-400 flex items-center">
        <Icon name="heroicons-magnifying-glass" class="w-3 h-3 me-1.5 text-orange-500" />
        <span>Web search failed</span>
        <span v-if="displayQuery" class="ms-1 truncate max-w-[360px] text-gray-600 dark:text-gray-400">"{{ displayQuery }}"</span>
      </span>
      <span v-if="sources.length" class="ms-1.5 text-[10px] text-gray-400 shrink-0">
        · {{ sources.length }} {{ sources.length === 1 ? 'source' : 'sources' }}
      </span>
    </div>

    <!-- Expanded detail -->
    <div v-if="expanded" class="mt-1.5 ms-4 space-y-1">
      <!-- Additional queries the provider ran -->
      <div v-if="extraQueries.length" class="space-y-0.5">
        <div v-for="(q, i) in extraQueries" :key="`q${i}`" class="text-[11px] text-gray-400 truncate max-w-[380px]">"{{ q }}"</div>
      </div>

      <!-- Sources with favicons -->
      <div v-if="sources.length" class="space-y-1 pt-0.5">
        <a
          v-for="(s, i) in sources"
          :key="`s${i}`"
          :href="s.url"
          target="_blank"
          rel="noopener noreferrer"
          class="flex items-center gap-1.5 text-[11px] text-blue-600 hover:underline"
          @click.stop
        >
          <img :src="faviconFor(s.url)" class="w-3.5 h-3.5 rounded-sm shrink-0" loading="lazy" @error="onFaviconError" />
          <span class="truncate max-w-[360px]">{{ s.title || hostOf(s.url) }}</span>
        </a>
      </div>

      <div v-else-if="isSuccess" class="text-[11px] text-gray-400">No results found</div>
    </div>
  </div>
</template>

<script setup lang="ts">
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
function faviconFor(url: string): string {
  const h = hostOf(url)
  return `https://www.google.com/s2/favicons?domain=${encodeURIComponent(h)}&sz=32`
}
function onFaviconError(e: Event) {
  // Hide broken favicon images so the row stays clean.
  const el = e.target as HTMLImageElement
  if (el) el.style.visibility = 'hidden'
}
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
</style>
