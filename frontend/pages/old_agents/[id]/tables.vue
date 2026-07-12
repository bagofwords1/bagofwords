<template>
    <div class="py-6">
        <!-- Hide content when there's a fetch error (layout shows error state) -->
        <div v-if="injectedFetchError" />
        <div v-else>

            <!-- Connection digest -->
            <div v-if="connections.length > 0" class="flex items-center gap-3 mb-3 flex-wrap">
                <div
                    v-for="conn in connections.slice(0, 3)"
                    :key="conn.id"
                    class="inline-flex items-center gap-1.5 text-xs text-gray-600 dark:text-gray-400"
                >
                    <span :class="['w-1.5 h-1.5 rounded-full flex-shrink-0', statusDotClass(getEffectiveStatus(conn))]" />
                    <DataSourceIcon :type="conn.type" class="h-3.5" />
                    <span>{{ conn.name }}</span>
                </div>
                <span v-if="connections.length > 3" class="text-xs text-gray-400">
                    +{{ connections.length - 3 }}
                </span>
                <button
                    class="text-xs text-gray-400 hover:text-gray-600 transition-colors"
                    @click="showManageModal = true"
                >
                    {{ t('agentPage.tables.manageConnections') }}
                </button>
            </div>

            <AgentConnectionsModal v-model="showManageModal" />

            <!-- Files digest (auto-attached to new reports for this agent) -->
            <div class="flex items-center gap-3 mb-3 flex-wrap">
                <div
                    v-for="file in files.slice(0, 3)"
                    :key="file.id"
                    class="inline-flex items-center gap-1.5 text-xs text-gray-600 dark:text-gray-400 group"
                >
                    <UIcon name="i-heroicons-paper-clip" class="w-3 h-3 flex-shrink-0 text-gray-400" />
                    <UTooltip :text="file.filename">
                        <span class="truncate max-w-[160px]">{{ file.filename }}</span>
                    </UTooltip>
                    <button
                        v-if="canUpdateDataSource"
                        type="button"
                        class="text-gray-300 dark:text-gray-600 hover:text-gray-600 transition-colors opacity-0 group-hover:opacity-100"
                        :title="t('agentPage.tables.removeFile')"
                        @click="removeFile(file)"
                    >
                        <UIcon name="i-heroicons-x-mark" class="w-3 h-3" />
                    </button>
                </div>
                <span v-if="files.length > 3" class="text-xs text-gray-400">
                    +{{ files.length - 3 }}
                </span>
                <input
                    ref="fileInput"
                    type="file"
                    class="hidden"
                    multiple
                    @change="onFileInput"
                />
                <button
                    v-if="canUpdateDataSource"
                    class="text-xs text-gray-400 hover:text-gray-600 transition-colors disabled:opacity-50"
                    :disabled="uploading"
                    @click="triggerUpload"
                >
                    {{ uploading ? t('agentPage.tables.filesUploading') : (files.length === 0 ? t('agentPage.tables.addFiles') : t('agentPage.tables.manageFiles')) }}
                </button>
            </div>

            <!-- Schema indexing in progress -->
            <div
                v-if="anyIndexing"
                class="mb-3 flex items-center gap-2 rounded-md border border-blue-200 bg-blue-50 dark:bg-blue-950 px-3 py-2 text-xs text-blue-800"
            >
                <UIcon name="heroicons-arrow-path" class="w-4 h-4 animate-spin" />
                <span>{{ t('agentPage.tables.schemaRefreshing') }}</span>
            </div>

            <!-- Sign-in required: this agent's catalog needs the current user
                 to OAuth before anything can populate. Show a focused prompt
                 instead of an "empty catalog" UI that reads as broken. -->
            <div
                v-if="needsSignIn"
                class="border border-gray-200 dark:border-gray-700 rounded-lg p-10 text-center bg-gray-50 dark:bg-gray-900"
            >
                <DataSourceIcon :type="pendingSignInConn?.type" class="h-10 mx-auto mb-3" />
                <h3 class="text-base font-semibold text-gray-900 dark:text-white">Sign in to access your {{ shapeNoun.plural }}</h3>
                <p class="text-sm text-gray-600 dark:text-gray-400 mt-1">
                    {{ pendingSignInConn?.name || 'This connection' }} uses per-user authentication —
                    your own {{ shapeNoun.plural }} will load after you sign in with {{ signInProviderName }}.
                </p>
                <UButton size="sm" color="blue" class="mt-4" @click="startSignIn">
                    Sign in with {{ signInProviderName }}
                </UButton>
            </div>

            <div v-else class="border border-gray-200 dark:border-gray-700 rounded-lg p-6">
                <CatalogSelector
                    :ds-id="id"
                    :connections="scopeConnections"
                    :registry-by-type="registryByType"
                    :can-update="canUpdateDataSource"
                    @add-mcp="showMCPModal = true"
                    @add-custom-api="showCustomAPIModal = true"
                    @edit-connection="openEditModal"
                    @delete-connection="confirmDelete"
                    @saved="onSaved" />
            </div>

            <UserDataSourceCredentialsModal v-model="showCredsModal" :data-source="selectedConnForSignIn" />

            <!-- Tool (MCP / Custom API) management, merged from the old Tools tab -->
            <AddMCPModal v-model="showMCPModal" :existing-connections="availableMcpConnections" @created="onConnectionCreated" />
            <AddCustomAPIModal v-model="showCustomAPIModal" :existing-connections="availableCustomApiConnections" @created="onConnectionCreated" />
            <AddMCPModal v-if="editingConnection?.type === 'mcp'" v-model="showEditModal" :edit-connection="editingConnection" @created="onConnectionUpdated" />
            <AddCustomAPIModal v-else-if="editingConnection?.type === 'custom_api'" v-model="showEditModal" :edit-connection="editingConnection" @created="onConnectionUpdated" />
            <UModal v-model="showDeleteModal" :ui="{ width: 'sm:max-w-sm' }">
                <div class="p-6">
                    <h3 class="text-sm font-semibold text-gray-900 dark:text-white mb-2">Remove Connection</h3>
                    <p class="text-xs text-gray-500 dark:text-gray-400 mb-4">
                        Remove <strong>{{ deletingConnection?.name }}</strong> from this agent? The connection will remain available for other agents.
                    </p>
                    <div class="flex justify-end gap-2">
                        <UButton color="gray" variant="ghost" size="xs" @click="showDeleteModal = false">Cancel</UButton>
                        <UButton color="red" size="xs" :loading="deleting" @click="deleteConnection">Remove</UButton>
                    </div>
                </div>
            </UModal>
        </div>
    </div>

</template>

<script setup lang="ts">
definePageMeta({ auth: true, layout: 'data' })
import CatalogSelector from '@/components/datasources/CatalogSelector.vue'
const { t } = useI18n()
import AgentConnectionsModal from '~/components/AgentConnectionsModal.vue'
import UserDataSourceCredentialsModal from '~/components/UserDataSourceCredentialsModal.vue'
import AddMCPModal from '@/components/AddMCPModal.vue'
import AddCustomAPIModal from '@/components/AddCustomAPIModal.vue'
import DataSourceIcon from '~/components/DataSourceIcon.vue'
import { useCan, usePermissionsLoaded } from '~/composables/usePermissions'
import { hasAnyActiveIndexing, getEffectiveStatus, statusDotClass } from '~/composables/useConnectionStatus'
import type { Ref } from 'vue'

const toast = useToast()
const route = useRoute()
const id = computed(() => String(route.params.id || ''))

// Inject integration data from layout (avoid duplicate API calls)
const injectedIntegration = inject<Ref<any>>('integration', ref(null))
const injectedFetchError = inject<Ref<number | null>>('fetchError', ref(null))
const fetchIntegration = inject<() => Promise<void>>('fetchIntegration', async () => {})

const showManageModal = ref(false)

// Connections WITH config (for CatalogSelector's per-connection file scope).
// injectedIntegration's connections don't carry config.
const scopeConnections = ref<any[]>([])
async function loadScopeConnections() {
  if (!id.value) return
  try {
    const { data } = await useMyFetch(`/data_sources/${id.value}/connections`, { method: 'GET' })
    scopeConnections.value = (data.value as any[]) || []
  } catch { /* non-fatal */ }
}

const loading = ref(false)
const schemaMode = ref<'full' | 'user'>('full')

const connections = computed(() => injectedIntegration.value?.connections || [])
const anyIndexing = computed(() => hasAnyActiveIndexing(injectedIntegration.value?.connections))

// Map connection.type → data_shape so we can label the agent surface
// correctly (Files / Tables / Objects / Tools) without hardcoding type lists.
const registryByType = ref<Record<string, any>>({})
onMounted(async () => {
  try {
    const { data } = await useMyFetch('/available_data_sources', { method: 'GET' })
    for (const entry of (data.value as any[]) || []) {
      registryByType.value[entry.type] = entry
    }
  } catch {}
})

// Pick a single shape for this agent's catalog UI. If all attached connections
// share a shape, use it; otherwise default to "tables" (SQL-style is the
// historical default and the heterogeneous case is rare).
const agentDataShape = computed<string>(() => {
  const shapes = new Set(
    connections.value
      .map((c: any) => registryByType.value[c.type]?.data_shape)
      .filter(Boolean)
  )
  if (shapes.size === 1) return Array.from(shapes)[0] as string
  return 'tables'
})

// Pluralised noun for headings — "files" / "tables" / "objects" / "tools".
const shapeNoun = computed(() => {
  if (agentDataShape.value === 'files') return { sing: 'file', plural: 'files' }
  if (agentDataShape.value === 'objects') return { sing: 'collection', plural: 'collections' }
  if (agentDataShape.value === 'tools') return { sing: 'tool', plural: 'tools' }
  return { sing: 'table', plural: 'tables' }
})

const headerTitle = computed(() => `Select ${shapeNoun.value.plural}`)
const headerSubtitle = computed(() => `Choose which ${shapeNoun.value.plural} to enable`)

// Sign-in gating: when any attached connection is user_required AND the user
// hasn't completed OAuth yet, the catalog is empty by design — show a clear
// "Sign in to access your files" state instead of an "empty catalog" UI that
// looks broken.
const pendingSignInConn = computed(() => {
  for (const conn of connections.value as any[]) {
    if (conn.auth_policy !== 'user_required') continue
    if (conn.user_status?.has_user_credentials) continue
    // effective_auth === 'system' means the user can run via system/service-
    // principal creds (owner/admin fallback) — no personal sign-in needed.
    // Mirror DataSourceSelector's isUsable so admins aren't forced to OAuth a
    // source they can already query.
    if (conn.user_status?.effective_auth === 'system') continue
    return conn
  }
  return null
})
const needsSignIn = computed(() => !!pendingSignInConn.value)
const signInProviderName = computed(() => {
  const t = pendingSignInConn.value?.type
  if (t === 'onedrive' || t === 'sharepoint') return 'Microsoft'
  if (t === 'google_drive' || t === 'bigquery') return 'Google'
  if (t === 'powerbi') return 'Power BI'
  if (t === 'ms_fabric') return 'Microsoft Fabric'
  return 'the provider'
})

const showCredsModal = ref(false)
const selectedConnForSignIn = ref<any>(null)
const signIn = useConnectionSignIn()
async function startSignIn() {
  if (!pendingSignInConn.value) return
  // If oauth is the only user-allowed auth mode, redirect immediately.
  // Otherwise, fall back to the credentials modal so the user can pick.
  const result = await signIn.triggerUserSignIn(pendingSignInConn.value)
  if (result.redirecting) return
  if (result.error) {
    toast.add({ title: 'Sign-in failed to start', description: result.error, color: 'red' })
  }
  selectedConnForSignIn.value = {
    name: pendingSignInConn.value.name,
    type: pendingSignInConn.value.type,
    connection: pendingSignInConn.value,
  }
  showCredsModal.value = true
}

const permissionsLoaded = usePermissionsLoaded()
// Editing this agent's tables requires `manage` on the data source
// (full_admin bypasses; otherwise a per-resource `manage` grant).
const canUpdateDataSource = computed(() => useCan('manage', { type: 'data_source', id: id.value }))

// Tables state is managed by TablesSelector component

// Files attached to this agent. Auto-snapshotted into reports created
// against this data source by the backend (see ReportService.create_report).
type AgentFile = { id: string; filename: string; content_type?: string }
const files = ref<AgentFile[]>([])
const uploading = ref(false)
const fileInput = ref<HTMLInputElement | null>(null)

async function loadFiles() {
    if (!id.value) return
    try {
        const { data } = await useMyFetch(`/data_sources/${id.value}/files`, { method: 'GET' })
        files.value = (data.value as AgentFile[]) || []
    } catch (e) {
        console.error('Failed to load agent files', e)
    }
}

function triggerUpload() {
    fileInput.value?.click()
}

async function onFileInput(e: Event) {
    const input = e.target as HTMLInputElement
    const list = input.files
    if (!list || list.length === 0) return
    uploading.value = true
    try {
        for (const file of Array.from(list)) {
            const formData = new FormData()
            formData.append('file', file)
            const { data, error } = await useMyFetch(`/data_sources/${id.value}/files`, {
                method: 'POST',
                body: formData,
            })
            if (error.value || !data.value) {
                toast.add({ title: t('agentPage.tables.fileUploadFailed'), description: file.name, color: 'red' })
                continue
            }
            files.value.push(data.value as AgentFile)
        }
    } finally {
        uploading.value = false
        if (input) input.value = ''
    }
}

async function removeFile(file: AgentFile) {
    try {
        await useMyFetch(`/data_sources/${id.value}/files/${file.id}`, { method: 'DELETE' })
        files.value = files.value.filter(f => f.id !== file.id)
    } catch (e) {
        console.error('Failed to remove file', e)
        toast.add({ title: t('agentPage.tables.fileRemoveFailed'), color: 'red' })
    }
}

watch(id, () => { loadFiles(); loadScopeConnections() }, { immediate: true })

// --- Tool (MCP / custom_api) management, merged from the old Tools tab ---
const showMCPModal = ref(false)
const showCustomAPIModal = ref(false)
const showEditModal = ref(false)
const editingConnection = ref<any>(null)
const showDeleteModal = ref(false)
const deletingConnection = ref<any>(null)
const deleting = ref(false)
const allOrgToolConnections = ref<any[]>([])

const mcpConnections = computed(() =>
  (connections.value as any[]).filter((c: any) => c.type === 'mcp' || c.type === 'custom_api')
)
async function fetchOrgToolConnections() {
  try {
    const res = await useMyFetch('/connections', { method: 'GET' })
    if (res.data.value) {
      allOrgToolConnections.value = (res.data.value as any[]).filter(
        (c: any) => c.type === 'mcp' || c.type === 'custom_api'
      )
    }
  } catch {}
}
onMounted(fetchOrgToolConnections)
const availableMcpConnections = computed(() => {
  const linked = new Set(mcpConnections.value.map((c: any) => String(c.id)))
  return allOrgToolConnections.value.filter((c: any) => c.type === 'mcp' && !linked.has(String(c.id)))
})
const availableCustomApiConnections = computed(() => {
  const linked = new Set(mcpConnections.value.map((c: any) => String(c.id)))
  return allOrgToolConnections.value.filter((c: any) => c.type === 'custom_api' && !linked.has(String(c.id)))
})
async function onConnectionCreated(conn: any) {
  try { await useMyFetch(`/data_sources/${id.value}/connections/${conn.id}`, { method: 'POST' }) } catch {}
  try { await useMyFetch(`/connections/${conn.id}/refresh-tools`, { method: 'POST' }) } catch {}
  await fetchIntegration(); await fetchOrgToolConnections(); await loadScopeConnections()
}
async function onConnectionUpdated() { editingConnection.value = null; await fetchIntegration() }
function openEditModal(conn: any) { editingConnection.value = conn; showEditModal.value = true }
function confirmDelete(conn: any) { deletingConnection.value = conn; showDeleteModal.value = true }
async function deleteConnection() {
  if (!deletingConnection.value) return
  deleting.value = true
  try {
    await useMyFetch(`/data_sources/${id.value}/connections/${deletingConnection.value.id}`, { method: 'DELETE' })
    toast.add({ title: 'Connection removed', color: 'green' })
    showDeleteModal.value = false; deletingConnection.value = null
    await fetchIntegration(); await fetchOrgToolConnections(); await loadScopeConnections()
  } catch (e: any) {
    toast.add({ title: 'Failed to remove connection', description: e?.data?.detail, color: 'red' })
  } finally { deleting.value = false }
}

// Set schema mode based on permissions - wait for permissions to load
watch([injectedIntegration, permissionsLoaded], ([ds, loaded]) => {
    if (ds && loaded) {
        schemaMode.value = canUpdateDataSource.value ? 'full' : 'user'
    }
}, { immediate: true })

function onSaved() { toast.add({ title: 'Saved', description: 'Schema updated', color: 'green' }) }

// Auto-refresh the per-user catalog on first visit when the user has signed
// in but the catalog hasn't been fetched yet. Bridges the gap between
// "OAuth completed" (token saved) and "Files tab populated" without the
// user having to know about the Reload button.
const triedAutoRefresh = ref(false)
async function maybeAutoRefreshUserCatalog() {
  if (triedAutoRefresh.value) return
  if (!id.value) return
  // Only trigger when at least one connection is per_user-owned AND the
  // current user already has credentials on it. Per_user catalogs are the
  // only ones whose admin-side schema is meaningless on its own.
  const hasPerUserSignedIn = connections.value.some((c: any) => {
    const entry = registryByType.value[c.type]
    return entry?.catalog_ownership === 'per_user'
      && c.auth_policy === 'user_required'
      && c.user_status?.has_user_credentials
  })
  if (!hasPerUserSignedIn) return
  // Need both the registry map and connections to be populated before
  // deciding to refresh.
  if (Object.keys(registryByType.value).length === 0) return
  triedAutoRefresh.value = true
  try {
    await useMyFetch(`/data_sources/${id.value}/refresh_schema`, { method: 'GET' })
    // TablesSelector reads from /full_schema on its own — emit nothing,
    // the next user interaction (or Reload click) will see the fresh rows.
  } catch (e) {
    console.warn('Auto-refresh of per-user catalog failed', e)
  }
}

watch([connections, registryByType], () => maybeAutoRefreshUserCatalog(), { immediate: true, deep: true })
</script>


