<template>
  <div class="space-y-5">
    <!-- Uploaded files -->
    <section @dragover.prevent @drop.prevent="uploadFiles(Array.from($event.dataTransfer?.files || []))">
      <div class="flex items-center justify-between mb-2">
        <div>
          <h3 class="text-sm font-semibold text-gray-800 dark:text-gray-200">Uploaded</h3>
          <p v-if="!folderId" class="text-xs text-gray-500 dark:text-gray-400">Files attached directly to this agent.</p>
        </div>
        <input ref="fileInput" type="file" class="hidden" multiple @change="onFileInput" />
        <button v-if="canUpdate" :disabled="uploading" @click="triggerUpload"
                class="text-xs px-3 py-1.5 rounded-md border border-gray-200 dark:border-gray-700 hover:bg-gray-50 dark:hover:bg-gray-800 disabled:opacity-50">
          {{ uploading ? 'Uploading…' : '+ Upload files' }}
        </button>
      </div>
      <div v-if="files.length === 0" class="text-xs text-ink-muted py-2">No uploaded files yet.</div>
      <ul v-else class="divide-y divide-gray-100 dark:divide-gray-800">
        <li v-for="f in files" :key="f.id" class="flex items-center justify-between py-1.5 text-sm group">
          <span class="flex items-center gap-2 min-w-0"><UIcon name="i-heroicons-document" class="w-4 h-4 text-ink-muted shrink-0" /><button v-if="folderId" class="truncate hover:underline" @click="emit('open', f)">{{ f.filename }}</button><span v-else class="truncate text-gray-700 dark:text-gray-300">{{ f.filename }}</span></span>
          <button v-if="canUpdate" @click="removeFile(f)" class="opacity-0 group-hover:opacity-100 text-gray-300 hover:text-red-500"><UIcon name="i-heroicons-x-mark" class="w-4 h-4" /></button>
        </li>
      </ul>
    </section>

    <!-- Directory connections -->
    <section v-for="conn in fileConnections" :key="conn.id" class="border border-gray-200 dark:border-gray-700 rounded-lg p-4">
      <div class="flex items-center justify-between">
        <div class="flex items-center gap-2">
          <DataSourceIcon :type="conn.type" class="w-4 h-4" />
          <span class="text-sm font-semibold text-gray-900 dark:text-gray-100">{{ conn.name }}</span>
          <span class="text-2xs uppercase tracking-wide text-ink-muted">{{ conn.type }}</span>
        </div>
        <div class="flex items-center gap-2">
          <span :class="badgeClass(indexModeOf(conn))" class="text-2xs px-2 py-0.5 rounded-full font-medium">{{ badgeLabel(indexModeOf(conn)) }}</span>
          <button v-if="conn.type === 'slack_messages' && canUpdate" data-testid="slack-channels-button"
                  class="text-xs px-2 py-1 rounded-md border border-gray-200 dark:border-gray-700 hover:bg-gray-50 dark:hover:bg-gray-800"
                  @click="openChannels(conn)">{{ $t('slackChannels.button') }}</button>
          <button class="text-xs px-2 py-1 rounded-md border border-gray-200 dark:border-gray-700 hover:bg-gray-50 dark:hover:bg-gray-800" @click="editScope(conn)">Edit scope</button>
        </div>
      </div>
      <p v-if="LIVE_TYPES.includes(conn.type)" class="mt-2 text-xs text-gray-500 dark:text-gray-400">{{ $t('agentsPage.liveSourceHint') }}</p>
      <p v-if="conn.type === 'slack_messages'" class="mt-1 text-xs text-gray-600 dark:text-gray-300" data-testid="slack-scope-summary">
        {{ slackScopeSummary(conn) }}
      </p>
      <div v-else class="mt-3 grid grid-cols-[64px_1fr] gap-x-3 gap-y-1.5 text-xs">
        <span class="text-ink-muted">Base</span>
        <span class="font-mono text-gray-700 dark:text-gray-300 break-all">{{ baseOf(conn) }}</span>
        <span class="text-ink-muted">Scope</span>
        <div>
          <span v-if="conn.type === 'kms_lighthouse'" class="text-gray-500 italic">{{ $t('kmsFileScope.browsingOnly') }}</span>
          <template v-else-if="globsOf(conn).length"><code v-for="g in globsOf(conn)" :key="g" class="inline-block mr-1.5 mb-1 px-1.5 py-0.5 rounded bg-blue-50 dark:bg-blue-900/30 text-blue-700 dark:text-blue-300 border border-blue-100 dark:border-blue-800">{{ g }}</code></template>
          <span v-else class="text-gray-500 italic">whole path</span>
        </div>
      </div>
      <div class="mt-3 border-t border-gray-100 dark:border-gray-800 pt-3">
        <ConnectionFileBrowser :key="`${conn.id}:${browserKey}`" :ds-id="dsId!" :connection-id="conn.id" :connection-type="conn.type" />
      </div>
    </section>

    <SlackChannelPicker v-if="channelsConn && dsId" v-model="channelsOpen" :ds-id="dsId" :connection-id="channelsConn.id"
                        @saved="onChannelsSaved" />

    <div v-if="!folderId && fileConnections.length === 0 && files.length === 0" class="text-sm text-gray-500 dark:text-gray-400 text-center py-4">No files or file connections yet.</div>
  </div>
</template>

<script setup lang="ts">
import DataSourceIcon from '~/components/DataSourceIcon.vue'
import ConnectionFileBrowser from '~/components/datasources/ConnectionFileBrowser.vue'
import SlackChannelPicker from '~/components/datasources/SlackChannelPicker.vue'
const props = defineProps<{ dsId?: string; folderId?: string; canUpdate?: boolean }>()
// `changed` tells the host tree to re-read the uploaded-file list: the two
// views keep separate copies and neither can see the other's mutations.
const emit = defineEmits(['edit-connection', 'changed', 'open'])
const toast = useToast()
const { t } = useI18n()

const connections = ref<any[]>([])
const registryByType = ref<Record<string, any>>({})
const files = ref<any[]>([])
const uploading = ref(false)
const fileInput = ref<HTMLInputElement | null>(null)

const fileConnections = computed(() => connections.value.filter((c) => registryByType.value[c.type]?.data_shape === 'files'))
const cfg = (c: any) => c?.config || {}
const baseOf = (c: any) => {
  const g = cfg(c)
  if (c.type === 'sharepoint_onprem') return [g.site_url, g.drive_name, g.folder_path].filter(Boolean).join(' / ')
  if (c.type === 'documentum') return [g.rest_url, g.repository, g.root_path].filter(Boolean).join(' / ')
  if (c.type === 'azure_blob' && g.container) return `az://${g.account_name ? g.account_name + '/' : ''}${g.container}/${g.prefix || ''}`
  return g.bucket ? `${c.type === 'gcs' ? 'gs' : 's3'}://${g.bucket}/${g.prefix || ''}` : (g.root_path || '—')
}
const globsOf = (c: any) => String(cfg(c).include_globs || '').split(/[,\n]/).map((s) => s.trim()).filter(Boolean)
// Live per-user sources (mailboxes, Teams) keep no catalog at all: every list,
// search and read goes to the provider with the user's own token. They carry
// no index_mode, which used to fall through to "Indexed: contents".
const LIVE_TYPES = ['outlook_mail', 'gmail_mail', 'teams_messages', 'slack_messages']
const indexModeOf = (c: any) => LIVE_TYPES.includes(c.type) ? 'none'
  : (cfg(c).index_mode || (cfg(c).index_content === false ? 'metadata' : 'content'))
const badgeLabel = (m: string) => ({ none: 'Live', metadata: 'Indexed: list', content: 'Indexed: keywords', fulltext: 'Indexed: full text' } as any)[m] || m
const badgeClass = (m: string) => ({ none: 'bg-amber-50 text-amber-700 dark:bg-amber-900/30 dark:text-amber-300', metadata: 'bg-gray-100 text-gray-600 dark:bg-gray-800 dark:text-gray-300', content: 'bg-green-50 text-green-700 dark:bg-green-900/30 dark:text-green-300', fulltext: 'bg-emerald-100 text-emerald-800 dark:bg-emerald-900/40 dark:text-emerald-200' } as any)[m] || 'bg-gray-100 text-gray-600'

const filesPath = computed(() => props.folderId ? `/folders/${props.folderId}/knowledge/files` : `/data_sources/${props.dsId}/files`)
async function loadAll() {
  if (props.folderId) {
    const {data, error} = await useMyFetch<any[]>(filesPath.value)
    if (error.value) toast.add({title: error.value.message, color:'red'})
    files.value = (data.value || []).map(f => ({...f, filename:f.name}))
    return
  }
  if (!props.dsId) return
  const [reg, conns, ups] = await Promise.all([
    useMyFetch('/available_data_sources', { method: 'GET' }),
    useMyFetch(`/data_sources/${props.dsId}/file-connections`, { method: 'GET' }),
    useMyFetch(`/data_sources/${props.dsId}/files`, { method: 'GET' }),
  ])
  for (const e of (reg.data.value as any[]) || []) registryByType.value[e.type] = e
  connections.value = (conns.data.value as any[]) || []
  files.value = (ups.data.value as any[]) || []
  await loadSlackScopes()
}

// Per-agent Slack channel scope (see SlackChannelPicker).
const slackScopes = ref<Record<string, any>>({})
const channelsOpen = ref(false)
const channelsConn = ref<any>(null)
const browserKey = ref(0)
async function loadSlackScopes() {
  const slack = connections.value.filter(c => c.type === 'slack_messages')
  await Promise.all(slack.map(async (c) => {
    const { data } = await useMyFetch(`/data_sources/${props.dsId}/connections/${c.id}/channel-scope`, { method: 'GET' })
    if (data.value) slackScopes.value[c.id] = data.value
  }))
}
function slackScopeSummary(conn: any) {
  const s = slackScopes.value[conn.id]
  const channels = s?.allowed_channels || []
  const dms = s?.include_direct_messages !== false
  if (!channels.length) return dms ? t('slackChannels.summaryAll') : t('slackChannels.summaryAllNoDms')
  // Isolate the LTR channel list so "#general" keeps its "#" in RTL locales.
  const names = '\u2066' + channels.map((c: any) => '#' + (c.name || c.id)).join(', ') + '\u2069'
  return dms ? t('slackChannels.summarySomeWithDms', { names }) : t('slackChannels.summarySome', { names })
}
function openChannels(conn: any) { channelsConn.value = conn; channelsOpen.value = true }
function onChannelsSaved(scope: any) {
  if (channelsConn.value) slackScopes.value[channelsConn.value.id] = scope
  browserKey.value++  // re-list messages under the new scope
}
function triggerUpload() { fileInput.value?.click() }
async function onFileInput(e: Event) {
  const input = e.target as HTMLInputElement
  const selected = Array.from(input.files || [])
  input.value = ''
  await uploadFiles(selected)
}
async function uploadFiles(selected: File[]) {
  if (!props.canUpdate || uploading.value || !selected.length) return
  uploading.value = true
  let ok = 0
  try {
    for (const file of selected) {
      const fd = new FormData(); fd.append('file', file)
      const { data, error } = await useMyFetch(filesPath.value, { method: 'POST', body: fd })
      if (error.value || !data.value) { toast.add({ title: 'Upload failed', description: file.name, color: 'red' }); continue }
      const item = data.value as any
      files.value.push({...item, filename: item.filename || item.name})
      ok++
    }
  } finally { uploading.value = false }
  if (ok) emit('changed')
}
async function removeFile(f: any) {
  try {
    const {error} = await useMyFetch(filesPath.value + '/' + f.id, { method: 'DELETE' })
    if (error.value) throw error.value
    files.value = files.value.filter((x) => x.id !== f.id)
    emit('changed')
  } catch { toast.add({ title: 'Failed to remove file', color: 'red' }) }
}
// Scope lives on the connection — let the host open the ConnectionDetailModal.
function editScope(conn: any) { emit('edit-connection', conn) }
watch(filesPath, loadAll, { immediate: true })
</script>
