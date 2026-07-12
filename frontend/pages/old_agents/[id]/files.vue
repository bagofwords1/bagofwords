<template>
  <div class="py-6">
    <div v-if="injectedFetchError" />
    <div v-else class="space-y-6">

      <!-- Uploaded files (agent-level, imperative add/remove) -->
      <section class="border border-gray-200 dark:border-gray-700 rounded-lg p-5">
        <div class="flex items-center justify-between mb-3">
          <div>
            <h3 class="text-sm font-semibold text-gray-800 dark:text-gray-200">Uploaded</h3>
            <p class="text-xs text-gray-500 dark:text-gray-400">Files you attach directly to this agent.</p>
          </div>
          <input ref="fileInput" type="file" class="hidden" multiple @change="onFileInput" />
          <button v-if="canUpdate" :disabled="uploading" @click="triggerUpload"
                  class="text-xs px-3 py-1.5 rounded-md border border-gray-200 dark:border-gray-700 hover:bg-gray-50 dark:hover:bg-gray-800 disabled:opacity-50">
            {{ uploading ? 'Uploading…' : '+ Upload files' }}
          </button>
        </div>
        <div v-if="files.length === 0" class="text-xs text-gray-400 dark:text-gray-500 py-3">No uploaded files yet.</div>
        <ul v-else class="divide-y divide-gray-100 dark:divide-gray-800">
          <li v-for="f in files" :key="f.id" class="flex items-center justify-between py-2 text-sm group">
            <span class="flex items-center gap-2 min-w-0">
              <UIcon name="i-heroicons-document" class="w-4 h-4 text-gray-400 shrink-0" />
              <span class="truncate text-gray-700 dark:text-gray-300">{{ f.filename }}</span>
            </span>
            <button v-if="canUpdate" @click="removeFile(f)"
                    class="opacity-0 group-hover:opacity-100 text-gray-300 hover:text-red-500">
              <UIcon name="i-heroicons-x-mark" class="w-4 h-4" />
            </button>
          </li>
        </ul>
      </section>

      <!-- Directory connections (scope-based file sources) -->
      <section v-for="conn in fileConnections" :key="conn.id"
               class="border border-gray-200 dark:border-gray-700 rounded-lg p-5">
        <div class="flex items-center justify-between">
          <div class="flex items-center gap-2">
            <DataSourceIcon :type="conn.type" class="w-4 h-4" />
            <span class="text-sm font-semibold text-gray-900 dark:text-gray-100">{{ conn.name }}</span>
            <span class="text-[10px] uppercase tracking-wide text-gray-400 dark:text-gray-500">{{ conn.type }}</span>
          </div>
          <div class="flex items-center gap-2">
            <span :class="badgeClass(indexModeOf(conn))" class="text-[10px] px-2 py-0.5 rounded-full font-medium">{{ badgeLabel(indexModeOf(conn)) }}</span>
            <button class="text-xs px-2 py-1 rounded-md border border-gray-200 dark:border-gray-700 hover:bg-gray-50 dark:hover:bg-gray-800"
                    @click="editConn(conn)">Edit scope</button>
          </div>
        </div>

        <div class="mt-3 grid grid-cols-[70px_1fr] gap-x-3 gap-y-1.5 text-xs">
          <span class="text-gray-400 dark:text-gray-500">Base</span>
          <span class="font-mono text-gray-700 dark:text-gray-300 break-all">{{ baseOf(conn) }}</span>
          <span class="text-gray-400 dark:text-gray-500">Scope</span>
          <div>
            <template v-if="globsOf(conn).length">
              <code v-for="g in globsOf(conn)" :key="g" class="inline-block mr-1.5 mb-1 px-1.5 py-0.5 rounded bg-blue-50 dark:bg-blue-900/30 text-blue-700 dark:text-blue-300 border border-blue-100 dark:border-blue-800">{{ g }}</code>
            </template>
            <span v-else class="text-gray-500 italic">whole path</span>
          </div>
        </div>

        <div class="mt-4 border-t border-gray-100 dark:border-gray-800 pt-3">
          <div class="flex items-center justify-between mb-2">
            <span class="text-xs text-gray-500 dark:text-gray-400">
              {{ browse[conn.id]?.total ?? '…' }} files match · agent reads ONLY these · denials audited
            </span>
          </div>
          <ul class="text-xs font-mono text-gray-600 dark:text-gray-400 space-y-0.5 max-h-56 overflow-auto">
            <li v-for="name in (browse[conn.id]?.names || [])" :key="name" class="truncate">{{ name }}</li>
            <li v-if="(browse[conn.id]?.total || 0) > (browse[conn.id]?.names?.length || 0)" class="text-gray-400 italic">
              … {{ (browse[conn.id].total - browse[conn.id].names.length) }} more
            </li>
            <li v-if="browse[conn.id] && browse[conn.id].total === 0" class="text-gray-400 italic">
              {{ indexModeOf(conn) === 'none' ? 'Live source — files are read on demand, not cached.' : 'No files match.' }}
            </li>
          </ul>
        </div>
      </section>

      <div v-if="fileConnections.length === 0 && files.length === 0" class="text-sm text-gray-500 dark:text-gray-400 text-center py-6">
        No files or file connections yet.
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
definePageMeta({ auth: true, layout: 'data' })
import DataSourceIcon from '~/components/DataSourceIcon.vue'
import { useCan } from '~/composables/usePermissions'
import type { Ref } from 'vue'

const route = useRoute()
const toast = useToast()
const id = computed(() => String(route.params.id || ''))
const injectedFetchError = inject<Ref<number | null>>('fetchError', ref(null))
const canUpdate = computed(() => useCan('manage', { type: 'data_source', id: id.value }))

const connections = ref<any[]>([])
const registryByType = ref<Record<string, any>>({})
const files = ref<any[]>([])
const browse = ref<Record<string, { names: string[]; total: number }>>({})
const uploading = ref(false)
const fileInput = ref<HTMLInputElement | null>(null)

const fileConnections = computed(() =>
  connections.value.filter((c) => registryByType.value[c.type]?.data_shape === 'files'))

const cfg = (c: any) => c?.config || {}
const baseOf = (c: any) => cfg(c).bucket ? `s3://${cfg(c).bucket}/${cfg(c).prefix || ''}` : (cfg(c).root_path || '—')
const globsOf = (c: any) => String(cfg(c).include_globs || '').split(/[,\n]/).map((s) => s.trim()).filter(Boolean)
const indexModeOf = (c: any) => cfg(c).index_mode || (cfg(c).index_content === false ? 'metadata' : 'content')
const badgeLabel = (m: string) => ({ none: 'Live', metadata: 'Indexed: list', content: 'Indexed: contents' } as any)[m] || m
const badgeClass = (m: string) => ({
  none: 'bg-amber-50 text-amber-700 dark:bg-amber-900/30 dark:text-amber-300',
  metadata: 'bg-gray-100 text-gray-600 dark:bg-gray-800 dark:text-gray-300',
  content: 'bg-green-50 text-green-700 dark:bg-green-900/30 dark:text-green-300',
} as any)[m] || 'bg-gray-100 text-gray-600'

async function loadAll() {
  if (!id.value) return
  const [reg, conns, ups] = await Promise.all([
    useMyFetch('/available_data_sources', { method: 'GET' }),
    useMyFetch(`/data_sources/${id.value}/connections`, { method: 'GET' }),
    useMyFetch(`/data_sources/${id.value}/files`, { method: 'GET' }),
  ])
  for (const e of (reg.data.value as any[]) || []) registryByType.value[e.type] = e
  connections.value = (conns.data.value as any[]) || []
  files.value = (ups.data.value as any[]) || []
  // Browse each file connection's cached catalog (the matching files).
  for (const c of fileConnections.value) {
    try {
      const res = await useMyFetch(`/data_sources/${id.value}/full_schema?page=1&page_size=30&connection_filter=${c.id}`, { method: 'GET' })
      const d: any = res.data.value || {}
      browse.value[c.id] = { names: (d.tables || []).map((t: any) => t.name), total: d.total ?? (d.tables || []).length }
    } catch { browse.value[c.id] = { names: [], total: 0 } }
  }
}

function triggerUpload() { fileInput.value?.click() }
async function onFileInput(e: Event) {
  const input = e.target as HTMLInputElement
  if (!input.files?.length) return
  uploading.value = true
  try {
    for (const file of Array.from(input.files)) {
      const fd = new FormData(); fd.append('file', file)
      const { data, error } = await useMyFetch(`/data_sources/${id.value}/files`, { method: 'POST', body: fd })
      if (error.value || !data.value) { toast.add({ title: 'Upload failed', description: file.name, color: 'red' }); continue }
      files.value.push(data.value as any)
    }
  } finally { uploading.value = false; if (input) input.value = '' }
}
async function removeFile(f: any) {
  try { await useMyFetch(`/data_sources/${id.value}/files/${f.id}`, { method: 'DELETE' }); files.value = files.value.filter((x) => x.id !== f.id) }
  catch { toast.add({ title: 'Failed to remove file', color: 'red' }) }
}
function editConn(conn: any) {
  // Scope is edited on the connection itself; deep-link to its detail.
  navigateTo(`/integrations/${conn.id}/connection`)
}

watch(id, loadAll, { immediate: true })
</script>
