<template>
  <div class="p-4">
    <div v-if="showHeader" class="mb-4">
      <div class="text-sm font-medium text-gray-800 dark:text-gray-200">{{ headerTitle }}</div>
      <p class="mt-1 text-xs text-gray-500 dark:text-gray-400">{{ headerSubtitle }}</p>
    </div>

    <div v-for="conn in fileConnections" :key="conn.id"
         class="mb-3 rounded-lg border border-gray-200 dark:border-gray-700 p-4 bg-white dark:bg-gray-900">
      <div class="flex items-center justify-between">
        <div class="flex items-center gap-2">
          <DataSourceIcon :type="conn.type" class="w-4 h-4" />
          <span class="text-sm font-medium text-gray-900 dark:text-gray-100">{{ conn.name }}</span>
          <span class="text-[10px] uppercase tracking-wide text-gray-400 dark:text-gray-500">{{ conn.type }}</span>
        </div>
        <span :class="indexBadgeClass(indexModeOf(conn))"
              class="text-[10px] px-2 py-0.5 rounded-full font-medium">
          {{ indexLabel(indexModeOf(conn)) }}
        </span>
      </div>

      <!-- Base (path / bucket+prefix) -->
      <div class="mt-3 grid grid-cols-[90px_1fr] gap-x-3 gap-y-1.5 text-xs">
        <span class="text-gray-400 dark:text-gray-500">Base</span>
        <span class="font-mono text-gray-700 dark:text-gray-300 break-all">{{ baseOf(conn) || '—' }}</span>

        <span class="text-gray-400 dark:text-gray-500">Scope</span>
        <div>
          <template v-if="globsOf(conn).length">
            <code v-for="g in globsOf(conn)" :key="g"
                  class="inline-block mr-1.5 mb-1 px-1.5 py-0.5 rounded bg-blue-50 dark:bg-blue-900/30 text-blue-700 dark:text-blue-300 border border-blue-100 dark:border-blue-800">{{ g }}</code>
          </template>
          <span v-else class="text-gray-500 dark:text-gray-400 italic">whole path (no patterns)</span>
        </div>
      </div>

      <p class="mt-3 text-[11px] text-gray-400 dark:text-gray-500 leading-relaxed">
        The agent can list, search and read <strong>only</strong> files matching these patterns —
        access to anything else is denied and audited. Edit the path, patterns or indexing on the
        connection itself.
      </p>
    </div>

    <div v-if="!loading && fileConnections.length === 0"
         class="text-sm text-gray-500 dark:text-gray-400 py-4 text-center">
      No file connections attached to this agent.
    </div>

    <div v-if="showSave" class="mt-4 flex justify-end">
      <button type="button" @click="$emit('saved')"
              class="px-4 py-2 text-sm font-medium text-white rounded-lg bg-blue-600 hover:bg-blue-700">
        {{ saveLabel }}
      </button>
    </div>
  </div>
</template>

<script setup lang="ts">
import DataSourceIcon from '@/components/DataSourceIcon.vue'

const props = withDefaults(defineProps<{
  connections: any[]
  registryByType: Record<string, any>
  loading?: boolean
  showHeader?: boolean
  headerTitle?: string
  headerSubtitle?: string
  showSave?: boolean
  saveLabel?: string
}>(), {
  loading: false,
  showHeader: true,
  headerTitle: 'File scope',
  headerSubtitle: 'File connections are scoped by include-patterns, not by picking individual files. Everything matching is available to the agent.',
  showSave: false,
  saveLabel: 'Continue',
})
defineEmits(['saved'])

const fileConnections = computed(() =>
  (props.connections || []).filter((c: any) => props.registryByType[c.type]?.data_shape === 'files')
)

const cfg = (c: any) => c?.config || {}
const baseOf = (c: any) => {
  const g = cfg(c)
  if (g.bucket) return `s3://${g.bucket}/${g.prefix || ''}`
  return g.root_path || ''
}
const globsOf = (c: any) => {
  const raw = cfg(c).include_globs
  if (!raw) return [] as string[]
  return String(raw).split(/[,\n]/).map((s) => s.trim()).filter(Boolean)
}
const indexModeOf = (c: any) => cfg(c).index_mode || (cfg(c).index_content === false ? 'metadata' : 'content')
const indexLabel = (m: string) => ({ none: 'Live (not indexed)', metadata: 'Indexed: file list', content: 'Indexed: contents' } as any)[m] || m
const indexBadgeClass = (m: string) => ({
  none: 'bg-amber-50 text-amber-700 dark:bg-amber-900/30 dark:text-amber-300',
  metadata: 'bg-gray-100 text-gray-600 dark:bg-gray-800 dark:text-gray-300',
  content: 'bg-green-50 text-green-700 dark:bg-green-900/30 dark:text-green-300',
} as any)[m] || 'bg-gray-100 text-gray-600'
</script>
