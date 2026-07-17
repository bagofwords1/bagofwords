<template>
  <div class="mt-1">
    <!-- Status header -->
    <Transition name="fade" appear>
      <div class="mb-2 flex items-center text-xs text-gray-500 dark:text-gray-400">
        <span v-if="status === 'running'" class="tool-shimmer flex items-center">
          <Icon name="heroicons-magnifying-glass-circle" class="w-3 h-3 me-1 text-gray-400" />
          <span>{{ $t('tools.getConnection.inspecting') }}</span>
        </span>
        <span v-else class="text-gray-700 dark:text-gray-300 flex items-center flex-wrap">
          <Icon name="heroicons-magnifying-glass-circle" class="w-3 h-3 me-1 text-gray-400" />
          <span class="align-middle">{{ $t('tools.getConnection.inspected', { name: connName }) }}</span>
          <span v-if="args.pattern" class="ms-1.5 text-[10px] px-1 py-0.5 rounded bg-gray-100 dark:bg-gray-800 text-gray-500 font-mono flex-shrink-0">{{ args.pattern }}</span>
          <span class="ms-1.5 text-[10px] text-gray-400">· {{ $t('tools.getConnection.matchCount', { count: total }) }}</span>
          <span v-if="hasMore || page > 1" class="ms-1 text-[10px] text-gray-400">· {{ $t('tools.getConnection.page', { page }) }}</span>
        </span>
      </div>
    </Transition>

    <Transition name="fade" appear>
      <div v-if="status !== 'running'" class="text-xs text-gray-600 dark:text-gray-400 ms-1">
        <!-- File scope (file-shaped connections) — always visible, it's tiny. -->
        <div v-if="fileScope" class="mb-1.5 rounded border border-gray-100 dark:border-gray-800 px-2 py-1 text-[11px] space-y-0.5">
          <div v-if="fileScope.token_scoped" class="text-gray-500">{{ $t('tools.getConnection.tokenScoped') }}</div>
          <template v-else>
            <div v-if="fileScope.base"><span class="text-gray-400">{{ $t('tools.getConnection.scopeBase') }}:</span> <span class="font-mono">{{ fileScope.base }}</span></div>
            <div><span class="text-gray-400">{{ $t('tools.getConnection.scopeGlobs') }}:</span> <span class="font-mono">{{ (fileScope.include_globs && fileScope.include_globs.length) ? fileScope.include_globs.join(', ') : '*' }}</span></div>
            <div v-if="fileScope.index_mode"><span class="text-gray-400">{{ $t('tools.getConnection.indexMode') }}:</span> {{ fileScope.index_mode }}</div>
          </template>
        </div>

        <!-- Collapsed by default: a result page can be hundreds of rows. -->
        <button
          v-if="tables.length || toolItems.length || files.length"
          class="inline-flex items-center gap-1 text-[11px] text-gray-500 hover:text-gray-700 dark:hover:text-gray-300 py-0.5"
          @click="expanded = !expanded"
        >
          <Icon :name="expanded ? 'heroicons-chevron-down' : 'heroicons-chevron-right'" class="w-3 h-3 rtl-flip" />
          {{ expanded ? $t('tools.getConnection.hideResults') : $t('tools.getConnection.showResults', { count: shownCount }) }}
        </button>

        <div v-if="expanded" class="max-h-64 overflow-y-auto mt-1">
        <!-- Tables grouped by schema -->
        <div v-if="tables.length" class="space-y-1">
          <div v-for="group in tablesBySchema" :key="group.schema || '__none__'">
            <div v-if="group.schema" class="text-[10px] uppercase tracking-wide text-gray-400 mt-1">{{ group.schema }}</div>
            <ul class="ms-1 leading-snug">
              <li v-for="tbl in group.tables" :key="tbl.name" class="flex items-center py-0.5">
                <Icon name="heroicons-table-cells" class="w-3 h-3 me-1 text-blue-400 flex-shrink-0" />
                <span class="text-gray-700 dark:text-gray-300 truncate">{{ tbl.name }}</span>
                <span class="ms-1.5 text-[9px] text-gray-400 flex-shrink-0">{{ $t('tools.getConnection.columns', { count: tbl.column_count }) }}</span>
              </li>
            </ul>
          </div>
        </div>

        <!-- Tools -->
        <ul v-if="toolItems.length" class="ms-1 space-y-0.5 leading-snug">
          <li v-for="tool in toolItems" :key="tool.name" class="flex items-center py-0.5">
            <Icon name="heroicons-wrench-screwdriver" class="w-3 h-3 me-1 text-violet-400 flex-shrink-0" />
            <span class="text-gray-700 dark:text-gray-300 font-mono text-[11px]">{{ tool.name }}</span>
            <span v-if="tool.default_policy && tool.default_policy !== 'allow'" class="ms-1.5 text-[9px] px-1 py-0.5 rounded bg-amber-50 dark:bg-amber-950 text-amber-700 dark:text-amber-400 flex-shrink-0">{{ tool.default_policy }}</span>
            <span v-if="tool.description" class="ms-1.5 text-[10px] text-gray-400 truncate">{{ tool.description }}</span>
          </li>
        </ul>

        <!-- Files -->
        <ul v-if="files.length" class="ms-1 space-y-0.5 leading-snug">
          <li v-for="f in files" :key="f" class="flex items-center py-0.5">
            <Icon name="heroicons-document" class="w-3 h-3 me-1 text-gray-400 flex-shrink-0" />
            <span class="text-gray-700 dark:text-gray-300 font-mono text-[11px] truncate">{{ f }}</span>
          </li>
        </ul>
        </div>

        <!-- Empty -->
        <div v-if="!tables.length && !toolItems.length && !files.length" class="text-gray-400">
          {{ $t('tools.getConnection.empty') }}
        </div>
      </div>
    </Transition>
  </div>
</template>

<script setup lang="ts">
import { computed, ref } from 'vue'

interface ToolExecution {
  id: string
  tool_name: string
  status: string
  result_summary?: string
  result_json?: any
  arguments_json?: any
}
const props = defineProps<{ toolExecution: ToolExecution }>()

const status = computed<string>(() => props.toolExecution?.status || '')
const result = computed(() => props.toolExecution?.result_json || {})
const args = computed(() => props.toolExecution?.arguments_json || {})

const connName = computed<string>(() => result.value.name || args.value.connection_id || '')
const tables = computed<any[]>(() => Array.isArray(result.value.tables) ? result.value.tables : [])
const toolItems = computed<any[]>(() => Array.isArray(result.value.tools) ? result.value.tools : [])
const files = computed<string[]>(() => Array.isArray(result.value.files) ? result.value.files : [])
const fileScope = computed<any | null>(() => result.value.file_scope || null)
const total = computed<number>(() => typeof result.value.total === 'number' ? result.value.total : 0)
const page = computed<number>(() => result.value.page || 1)
const hasMore = computed<boolean>(() => !!result.value.has_more)

const expanded = ref(false)
const shownCount = computed<number>(() => tables.value.length + toolItems.value.length + files.value.length)

const tablesBySchema = computed(() => {
  const groups: Record<string, any[]> = {}
  for (const tbl of tables.value) {
    const key = tbl.schema_name || ''
    if (!groups[key]) groups[key] = []
    groups[key].push(tbl)
  }
  return Object.keys(groups).sort().map((schema) => ({ schema, tables: groups[schema] }))
})
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
