<template>
  <div class="p-4">
    <div v-if="showHeader" class="mb-4">
      <div class="text-sm font-medium text-gray-800 dark:text-gray-200">{{ headerTitle }}</div>
      <p class="mt-1 text-xs text-gray-500 dark:text-gray-400">{{ subtitle }}</p>
    </div>

    <div v-for="conn in fileConnections" :key="conn.id"
         class="mb-3 rounded-lg border border-gray-200 dark:border-gray-700 p-4 bg-white dark:bg-gray-900">
      <div class="flex items-center justify-between">
        <div class="flex items-center gap-2">
          <DataSourceIcon :type="conn.type" class="w-4 h-4" />
          <span class="text-sm font-medium text-gray-900 dark:text-gray-100">{{ conn.name }}</span>
          <span class="text-2xs uppercase tracking-wide text-ink-muted">{{ conn.type }}</span>
        </div>
        <span :class="indexBadgeClass(indexModeOf(conn))"
              class="text-2xs px-2 py-0.5 rounded-full font-medium">
          {{ indexLabel(indexModeOf(conn)) }}
        </span>
      </div>

      <!-- Base (path / bucket+prefix) -->
      <div class="mt-3 grid grid-cols-[90px_1fr] gap-x-3 gap-y-1.5 text-xs">
        <span class="text-ink-muted">Base</span>
        <span class="font-mono text-gray-700 dark:text-gray-300 break-all">{{ baseOf(conn) || '—' }}</span>

        <span class="text-ink-muted">Scope</span>
        <div>
          <span v-if="isKms(conn)" class="text-gray-500 dark:text-gray-400 italic">{{ t('kmsFileScope.browsingOnly') }}</span>
          <template v-else-if="globsOf(conn).length">
            <code v-for="g in globsOf(conn)" :key="g"
                  class="inline-block mr-1.5 mb-1 px-1.5 py-0.5 rounded bg-blue-50 dark:bg-blue-900/30 text-blue-700 dark:text-blue-300 border border-blue-100 dark:border-blue-800">{{ g }}</code>
          </template>
          <span v-else class="text-gray-500 dark:text-gray-400 italic">whole path (no patterns)</span>
        </div>
      </div>

      <p v-if="isKms(conn)" class="mt-3 text-2xs text-ink-muted leading-relaxed">{{ t('kmsFileScope.accessNote') }}</p>
      <p v-else class="mt-3 text-2xs text-ink-muted leading-relaxed">
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

const { t } = useI18n()

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
  showSave: false,
  saveLabel: 'Continue',
})
defineEmits(['saved'])

const fileConnections = computed(() =>
  (props.connections || []).filter((c: any) => props.registryByType[c.type]?.data_shape === 'files')
)
const subtitle = computed(() => {
  if (props.headerSubtitle !== undefined) return props.headerSubtitle
  const onlyKms = fileConnections.value.length > 0 && fileConnections.value.every((c: any) => c.type === 'kms_lighthouse')
  return onlyKms
    ? t('kmsFileScope.headerSubtitle')
    : 'File connections are scoped by include-patterns, not by picking individual files. Everything matching is available to the agent.'
})

const cfg = (c: any) => c?.config || {}
const isKms = (c: any) => c.type === 'kms_lighthouse'
const baseOf = (c: any) => {
  const g = cfg(c)
  if (c.type === 'sharepoint_onprem') return [g.site_url, g.drive_name, g.folder_path].filter(Boolean).join(' / ')
  if (c.type === 'documentum') return [g.rest_url, g.repository, g.root_path].filter(Boolean).join(' / ')
  // Root folder 0 is a real folder id: test for blank, not truthiness (as the backend builder does).
  if (c.type === 'kms_lighthouse') {
    const root = g.root_folder_id != null && g.root_folder_id !== '' ? 'folder ' + g.root_folder_id : ''
    return [g.base_url, root].filter(Boolean).join(' / ')
  }
  if (c.type === 'azure_blob' && g.container) return `az://${g.account_name ? g.account_name + '/' : ''}${g.container}/${g.prefix || ''}`
  if (g.bucket) return `${c.type === 'gcs' ? 'gs' : 's3'}://${g.bucket}/${g.prefix || ''}`
  return g.root_path || ''
}
const globsOf = (c: any) => {
  const raw = cfg(c).include_globs
  if (!raw) return [] as string[]
  return String(raw).split(/[,\n]/).map((s) => s.trim()).filter(Boolean)
}
const indexModeOf = (c: any) => cfg(c).index_mode || (cfg(c).index_content === false ? 'metadata' : 'content')
const indexLabel = (m: string) => ({ none: 'Live (not indexed)', metadata: 'Indexed: file list', content: 'Indexed: keywords', fulltext: 'Indexed: full text' } as any)[m] || m
const indexBadgeClass = (m: string) => ({
  none: 'bg-amber-50 text-amber-700 dark:bg-amber-900/30 dark:text-amber-300',
  metadata: 'bg-gray-100 text-gray-600 dark:bg-gray-800 dark:text-gray-300',
  content: 'bg-green-50 text-green-700 dark:bg-green-900/30 dark:text-green-300',
  fulltext: 'bg-emerald-100 text-emerald-800 dark:bg-emerald-900/40 dark:text-emerald-200',
} as any)[m] || 'bg-gray-100 text-gray-600'
</script>
