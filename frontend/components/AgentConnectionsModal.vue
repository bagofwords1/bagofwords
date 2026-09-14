<template>
    <UModal v-model="isOpen" :ui="{ width: 'sm:max-w-xl' }">
        <div class="p-6 max-h-[calc(100dvh-4rem)] overflow-y-auto">
            <div class="mb-6">
                <div class="flex items-center justify-between">
                    <div class="flex items-center gap-3 min-w-0">
                        <DataSourceIcon v-if="agentInfo" :type="agentInfo.type || connections[0]?.type" :icon="agentInfo.icon" class="w-7 h-7 shrink-0" />
                        <div class="min-w-0">
                            <h3 class="text-lg font-semibold text-gray-900 dark:text-white break-words">{{ agentInfo?.name || $t('data.connectionsTitle') }}</h3>
                            <p v-if="agentInfo?.description" class="mt-1 text-sm leading-5 text-gray-500 whitespace-pre-wrap break-words">{{ agentInfo.description }}</p>
                            <p v-if="agentInfo" class="mt-2 text-xs text-gray-400">{{ $t('data.connectionsTitle') }}</p>
                        </div>
                    </div>
                    <div class="flex items-center gap-2">
                        <UButton
                            v-if="canLinkConnections"
                            color="blue"
                            variant="soft"
                            size="xs"
                            @click="openLinkModal"
                        >
                            <UIcon name="heroicons-plus" class="w-3.5 h-3.5 me-1" />
                            {{ $t('data.linkConnection') }}
                        </UButton>
                        <UButton color="gray" variant="ghost" size="xs" icon="i-heroicons-x-mark" @click="isOpen = false" />
                    </div>
                </div>
            </div>

            <div v-if="!ready" class="py-6 text-center text-sm text-gray-400">{{ $t('common.loading') }}</div>

            <div v-else-if="connections.length === 0" class="py-8 text-center">
                <UIcon name="heroicons-link" class="w-8 h-8 mx-auto mb-2 text-gray-300 dark:text-gray-600" />
                <p class="text-sm text-gray-500 dark:text-gray-400">{{ $t('data.noLinkedConnections') }}</p>
                <UButton v-if="canLinkConnections" color="blue" variant="soft" size="sm" class="mt-3" @click="openLinkModal">
                    {{ $t('data.linkConnection') }}
                </UButton>
            </div>

            <div v-else class="divide-y divide-gray-100 dark:divide-gray-800">
                <div
                    v-for="conn in connections"
                    :key="conn.id"
                    class="py-4 first:pt-0 last:pb-0"
                >
                    <div class="flex items-start justify-between gap-3">
                        <div class="flex items-center gap-3 min-w-0">
                            <DataSourceIcon :type="conn.type" :connector-key="conn.connector_key" class="w-5 h-5 mt-0.5 flex-shrink-0" />
                            <div class="min-w-0">
                                <div class="text-sm font-medium text-gray-900 dark:text-white truncate">{{ conn.name }}</div>
                                <div class="mt-1 flex items-center gap-1.5 text-xs text-gray-500" role="status">
                                    <span class="w-1.5 h-1.5 rounded-full shrink-0" :class="statusDotClass(getConnectionEffective(conn))" />
                                    {{ getStatusLabel(conn) }}
                                </div>
                                <div v-if="connectionCounts(conn).length" class="mt-2 flex flex-wrap gap-x-3 gap-y-1 text-xs text-gray-500">
                                    <span v-for="count in connectionCounts(conn)" :key="count.key">{{ $t(count.key, { n: count.value }, count.value) }}</span>
                                </div>
                            </div>
                        </div>
                        <div class="flex flex-wrap items-center justify-end gap-1.5 shrink-0 max-w-[45%]">
                            <button
                                v-if="needsConnectionSignIn(conn)"
                                @click="signInConnection(conn)"
                                :disabled="!!signingInId"
                                :aria-busy="signingInId === conn.id"
                                class="inline-flex items-center gap-1.5 px-2 py-1 rounded text-xs text-blue-600 bg-blue-50 hover:bg-blue-100 disabled:opacity-50"
                            >
                                <Spinner v-if="signingInId === conn.id" class="w-3 h-3" />
                                {{ $t('data.signIn') }}
                            </button>
                            <button
                                v-if="canManageConnection(conn)"
                                @click="testConnection(conn.id)"
                                :disabled="testingConnectionId === conn.id"
                                class="p-1.5 rounded hover:bg-gray-100 dark:hover:bg-gray-700 disabled:opacity-50"
                                :title="$t('data.testConnection')"
                            >
                                <Spinner v-if="testingConnectionId === conn.id" class="w-4 h-4" />
                                <UIcon v-else name="heroicons-arrow-path" class="w-4 h-4 text-gray-400" />
                            </button>
                            <UButton
                                v-if="canManageConnection(conn)"
                                color="gray" variant="ghost" size="xs"
                                @click="openEditModal(conn)"
                            >
                                <UIcon name="heroicons-pencil" class="w-4 h-4" />
                            </UButton>
                            <UButton
                                v-if="canLinkConnections && connections.length > 1"
                                color="red" variant="ghost" size="xs"
                                @click="unlinkConnection(conn.id)"
                                :title="$t('data.unlink')"
                            >
                                <UIcon name="heroicons-link-slash" class="w-4 h-4" />
                            </UButton>
                        </div>
                    </div>

                    <!-- Test result -->
                    <div v-if="testResults[conn.id]" class="mt-2 ms-10 text-xs">
                        <span :class="testResults[conn.id]?.success ? 'text-green-600' : 'text-red-600'">
                            {{ testResults[conn.id]?.success ? $t('data.connectionSuccessful') : (testResults[conn.id]?.message || $t('data.connectionFailed')) }}
                        </span>
                    </div>

                    <!-- Indexing progress -->
                    <div v-if="conn.indexing" class="mt-2 ms-10">
                        <ConnectionIndexingProgress :indexing="conn.indexing" :show-logs="true" />
                        <div v-if="conn.indexing.status === 'failed' && canManageConnection(conn)" class="mt-2">
                            <UButton size="xs" color="amber" variant="soft" @click="reindexConnection(conn.id)">
                                {{ $t('data.retry') }}
                            </UButton>
                        </div>
                    </div>
                </div>
            </div>
        </div>
    </UModal>

    <UserDataSourceCredentialsModal v-model="showCredentials" :data-source="credentialSource" @saved="onCredentialsSaved" />

    <!-- Link connection modal -->
    <UModal v-model="showLinkModal" :ui="{ width: 'sm:max-w-md' }">
        <UCard>
            <template #header>
                <div class="flex items-center justify-between">
                    <h3 class="text-sm font-semibold">{{ $t('data.linkConnection') }}</h3>
                    <UButton color="gray" variant="ghost" size="xs" icon="i-heroicons-x-mark" @click="showLinkModal = false" />
                </div>
            </template>

            <div v-if="loadingOrgConnections" class="flex items-center justify-center py-6">
                <Spinner class="w-5 h-5" />
            </div>
            <div v-else-if="availableConnections.length === 0" class="py-6 text-center text-sm text-gray-500 dark:text-gray-400">
                {{ $t('data.noConnectionsToLink') }}
            </div>
            <div v-else class="space-y-2 max-h-64 overflow-y-auto">
                <label
                    v-for="conn in availableConnections"
                    :key="conn.id"
                    class="flex items-center gap-3 p-3 border border-gray-200 dark:border-gray-700 rounded-lg cursor-pointer hover:bg-gray-50 dark:hover:bg-gray-800"
                    :class="{ 'border-blue-400 bg-blue-50 dark:bg-blue-950': selectedConnectionId === conn.id }"
                    @click="selectedConnectionId = conn.id"
                >
                    <input type="radio" name="link-conn" :value="conn.id" v-model="selectedConnectionId" class="sr-only" />
                    <DataSourceIcon :type="conn.type" :connector-key="conn.connector_key" class="h-5 flex-shrink-0" />
                    <div class="min-w-0 flex-1">
                        <div class="text-sm font-medium text-gray-900 dark:text-white truncate">{{ conn.name }}</div>
                        <div class="mt-1 flex items-center gap-1.5 text-xs text-gray-500" role="status">
                                    <span class="w-1.5 h-1.5 rounded-full shrink-0" :class="statusDotClass(getConnectionEffective(conn))" />
                                    {{ getStatusLabel(conn) }}
                                </div>
                                <div v-if="connectionCounts(conn).length" class="mt-2 flex flex-wrap gap-x-3 gap-y-1 text-xs text-gray-500">
                                    <span v-for="count in connectionCounts(conn)" :key="count.key">{{ $t(count.key, { n: count.value }, count.value) }}</span>
                                </div>
                    </div>
                    <UIcon v-if="selectedConnectionId === conn.id" name="heroicons-check-circle-solid" class="w-4 h-4 text-blue-500" />
                </label>
            </div>

            <template #footer>
                <div class="flex justify-end gap-2">
                    <UButton color="gray" variant="ghost" size="sm" @click="showLinkModal = false">{{ $t('data.cancel') }}</UButton>
                    <UButton color="blue" size="sm" :disabled="!selectedConnectionId || isLinking" :loading="isLinking" @click="linkConnection">
                        {{ $t('data.link') }}
                    </UButton>
                </div>
            </template>
        </UCard>
    </UModal>

    <!-- MCP / Custom API / integration connections have their own edit forms -->
    <AddMCPModal v-model="showMcpEditModal" :editConnection="editingConnection" @created="handleEditSuccess" />
    <AddCustomAPIModal v-model="showCustomApiEditModal" :editConnection="editingConnection" @created="handleEditSuccess" />
    <UModal v-model="showIntegrationEditModal" :ui="{ width: 'sm:max-w-lg' }">
        <div class="p-6">
            <div class="flex items-center gap-2 mb-4">
                <DataSourceIcon v-if="editingConnection" :type="editingConnection.type" :connector-key="editingConnection.connector_key" class="h-5" />
                <h3 class="text-sm font-semibold">{{ $t('data.editConnection') }}</h3>
            </div>
            <IntegrationConnectionForm
                v-if="showIntegrationEditModal && editingConnection"
                :integration-type="editingConnection.type"
                :integration-title="editingConnection.name"
                :editConnection="editingConnection"
                @saved="handleEditSuccess"
                @cancel="showIntegrationEditModal = false"
            />
        </div>
    </UModal>

    <!-- Edit connection modal -->
    <EditConnectionModal v-model="showEditModal" :connection="editingConnection" :initial-values="editingConnection ? getEditFormValues(editingConnection) : null" :loading="loadingEditDetails" :last-test="editingConnection ? testResults[editingConnection.id] : null" @success="refresh" @tested="refresh" @deleted="handleEditSuccess" />

</template>

<script setup lang="ts">
import UserDataSourceCredentialsModal from '~/components/UserDataSourceCredentialsModal.vue'
import { useConnectionSignIn } from '~/composables/useConnectionSignIn'
import Spinner from '~/components/Spinner.vue'
import EditConnectionModal from '~/components/EditConnectionModal.vue'
import ConnectionIndexingProgress from '~/components/ConnectionIndexingProgress.vue'
import AddMCPModal from '~/components/AddMCPModal.vue'
import AddCustomAPIModal from '~/components/AddCustomAPIModal.vue'
import IntegrationConnectionForm from '~/components/IntegrationConnectionForm.vue'
import { useCan } from '~/composables/usePermissions'
import {
    getEffectiveStatus as deriveStatus,
    statusDotClass,
    statusLabelKey,
    needsConnectionSignIn,
} from '~/composables/useConnectionStatus'
import type { Ref } from 'vue'

const props = defineProps<{
    modelValue: boolean
    // When used standalone (e.g. KnowledgeExplorer) the parent passes the
    // agent id + its connections directly. When omitted, we fall back to the
    // injected `integration` provided by the legacy agents layout.
    agent?: any
    dsId?: string
    connections?: any[]
}>()
const emit = defineEmits<{
    (e: 'update:modelValue', val: boolean): void
    (e: 'changed'): void
}>()

const isOpen = computed({
    get: () => props.modelValue,
    set: (v) => emit('update:modelValue', v),
})

const route = useRoute()
const toast = useToast()
const { t } = useI18n()

const integration = inject<Ref<any>>('integration', ref(null))
const fetchIntegration = inject<() => Promise<void>>('fetchIntegration', async () => {})

// Prefer explicit props (standalone use); fall back to the injected integration.
const dsId = computed(() => props.dsId ?? String(route.params.id || ''))
const connections = computed(() => props.connections ?? (integration.value?.connections || []))
const agentInfo = computed(() => props.agent ?? integration.value)
const ready = computed(() => props.dsId != null || !!integration.value)

// Linking/unlinking a connection to THIS agent is an agent-management action:
// anyone who can manage the agent may attach connections they have access to
// (the picker only lists connections the caller can see, and the API enforces
// per-connection read access on link). This is distinct from managing the
// shared connection object below.
const canLinkConnections = computed(() =>
    useCan('manage', { type: 'data_source', id: dsId.value })
)

// Editing / testing / reindexing mutates the SHARED connection (config,
// credentials, catalog) used by every agent it backs, so it stays gated by the
// per-connection `manage_connection` permission (org `manage_connections` and
// full_admin imply it) rather than agent-management.
function canManageConnection(conn: any) {
    return useCan('manage_connection', { type: 'connection', id: conn.id })
}

// Refresh both the legacy layout (via inject) and the standalone parent (via emit).
async function refresh() {
    await fetchIntegration()
    emit('changed')
}

const signIn = useConnectionSignIn()
const signingInId = ref<string | null>(null)
const showCredentials = ref(false)
const credentialSource = ref<any>(null)
async function signInConnection(conn: any) {
    if (signingInId.value) return
    signingInId.value = conn.id
    let redirecting = false
    try {
        const result = await signIn.triggerUserSignIn(conn, { returnTo: route.fullPath })
        redirecting = result.redirecting
        if (redirecting) return
        if (result.error) toast.add({ title: t('agentsPage.toastSignInFailed'), description: result.error, color: 'red' })
        credentialSource.value = { id: dsId.value, type: conn.type, connection: conn, connections: [conn] }
        showCredentials.value = true
    } catch (error: any) {
        toast.add({ title: t('agentsPage.toastSignInFailed'), description: error?.message || String(error), color: 'red' })
    } finally {
        if (!redirecting) signingInId.value = null
    }
}
async function onCredentialsSaved() {
    showCredentials.value = false
    await refresh()
}

const testingConnectionId = ref<string | null>(null)
const testResults = ref<Record<string, any>>({})
const showEditModal = ref(false)
const editingConnection = ref<any>(null)
const showLinkModal = ref(false)
const selectedConnectionId = ref<string | null>(null)
const loadingOrgConnections = ref(false)
const orgConnections = ref<any[]>([])
const isLinking = ref(false)

// Offer only connections the caller (a) hasn't already linked and (b) may build
// agents on — per-connection `create_data_sources`, the same permission the link
// API enforces. Keeps the picker in sync with what the backend will accept.
const availableConnections = computed(() => {
    const linked = new Set(connections.value.map((c: any) => c.id))
    return orgConnections.value.filter((c) =>
        !linked.has(c.id) &&
        useCan('create_data_sources', { type: 'connection', id: c.id })
    )
})

function getConnectionEffective(conn: any) {
    const local = testResults.value[conn.id]
    if (local) return local.success ? 'success' : 'error'
    return deriveStatus(conn)
}

function connectionCounts(conn: any) {
    // Embedded counts are viewer-scoped; do not substitute a shared discovery
    // total for personal access, or call a files catalog "tables".
    if (needsConnectionSignIn(conn)) return []
    const values = [
        { key: 'agentsPage.countTools', value: conn.tool_count },
        { key: 'agentsPage.countFiles', value: conn.file_count ?? (conn.data_shape === 'files' ? conn.table_count : undefined) },
        { key: 'agentsPage.countTables', value: conn.data_shape === 'files' ? undefined : conn.table_count },
    ]
    return values.filter(c => typeof c.value === 'number' && c.value > 0)
}
function getStatusLabel(conn: any) { return t(statusLabelKey(getConnectionEffective(conn) as any)) }

function getEditFormValues(conn: any) {
    // Prefer the freshly-fetched detail: the embedded connection payload can
    // lack config, and has_credentials must reflect the server state (it
    // decides whether the credentials section opens locked).
    const d = editingDetails.value
    return {
        management_auth: d?.management_auth,
        last_connection_status: d?.last_connection_status,
        last_connection_checked_at: d?.last_connection_checked_at,
        name: d?.name ?? conn.name,
        config: d?.config ?? conn.config ?? {},
        auth_policy: d?.auth_policy ?? conn.auth_policy ?? 'system_only',
        has_credentials: d?.has_credentials ?? true,
        credentials: d?.credentials_meta ?? {},
    }
}

async function testConnection(connectionId: string) {
    if (testingConnectionId.value) return
    testingConnectionId.value = connectionId
    testResults.value[connectionId] = null
    try {
        const { data, error } = await useMyFetch(`/connections/${connectionId}/test`, { method: 'POST' })
        // useMyFetch resolves (never throws) on HTTP errors — surface them as a
        // failed test instead of silently showing nothing.
        if (error.value) {
            const detail = (error.value as any)?.data?.detail || (error.value as any)?.message
            testResults.value[connectionId] = { success: false, message: detail || t('data.testFailed') }
        } else {
            testResults.value[connectionId] = (data as any)?.value || null
        }
        await refresh()
    } finally {
        testingConnectionId.value = null
    }
}

async function reindexConnection(connectionId: string) {
    const { error } = await useMyFetch(`/connections/${connectionId}/reindex`, { method: 'POST' })
    if (error.value) {
        toast.add({ title: t('data.reindexFailed'), description: (error.value as any)?.data?.detail, color: 'red' })
        return
    }
    await refresh()
}

const loadingEditDetails = ref(false)
const editingDetails = ref<any>(null)
const showMcpEditModal = ref(false)
const showCustomApiEditModal = ref(false)
const showIntegrationEditModal = ref(false)
// Integration-managed types whose config is NOT the SQL-flavored ConnectForm
// shape. Same routing as ConnectionDetailModal.openEdit.
const _INTEGRATION_TYPES = ['onedrive', 'google_drive', 'outlook_mail', 'gmail_mail']
async function openEditModal(conn: any) {
    editingConnection.value = conn
    editingDetails.value = null
    // MCP / Custom API / OAuth integrations store different config shapes —
    // the generic ConnectForm would rewrite their config with the wrong fields.
    if (conn?.type === 'mcp') { showMcpEditModal.value = true; return }
    if (conn?.type === 'custom_api') { showCustomApiEditModal.value = true; return }
    if (_INTEGRATION_TYPES.includes(conn?.type)) { showIntegrationEditModal.value = true; return }
    showEditModal.value = true
    loadingEditDetails.value = true
    try {
        const { data, error } = await useMyFetch(`/connections/${conn.id}`, { method: 'GET' })
        if (!error.value && data.value) editingDetails.value = data.value
    } finally {
        loadingEditDetails.value = false
    }
}

function handleEditSuccess() {
    showEditModal.value = false
    showMcpEditModal.value = false
    showCustomApiEditModal.value = false
    showIntegrationEditModal.value = false
    editingConnection.value = null
    editingDetails.value = null
    refresh()
}

async function openLinkModal() {
    showLinkModal.value = true
    selectedConnectionId.value = null
    loadingOrgConnections.value = true
    try {
        const response = await useMyFetch('/connections', { method: 'GET' })
        orgConnections.value = (response.data as any)?.value || []
    } finally {
        loadingOrgConnections.value = false
    }
}

async function linkConnection() {
    if (!selectedConnectionId.value || isLinking.value) return
    isLinking.value = true
    try {
        // useMyFetch resolves (never throws) on HTTP errors — check error.value,
        // otherwise a failed link would still toast "Connection linked".
        const { error } = await useMyFetch(`/data_sources/${dsId.value}/connections/${selectedConnectionId.value}`, { method: 'POST' })
        if (error.value) {
            toast.add({ title: t('data.linkFailed'), description: (error.value as any)?.data?.detail, color: 'red' })
            return
        }
        toast.add({ title: t('data.connectionLinked'), color: 'green' })
        showLinkModal.value = false
        selectedConnectionId.value = null
        await refresh()
    } finally {
        isLinking.value = false
    }
}

async function unlinkConnection(connectionId: string) {
    if (!confirm(t('data.unlinkConfirm'))) return
    const { error } = await useMyFetch(`/data_sources/${dsId.value}/connections/${connectionId}`, { method: 'DELETE' })
    if (error.value) {
        toast.add({ title: t('data.unlinkFailed'), description: (error.value as any)?.data?.detail, color: 'red' })
        return
    }
    toast.add({ title: t('data.connectionUnlinked'), color: 'green' })
    await refresh()
}
</script>
