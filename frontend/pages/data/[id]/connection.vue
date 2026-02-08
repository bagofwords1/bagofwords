<template>
    <div class="py-6">
        <!-- Hide content when there's a fetch error (layout shows error state) -->
        <div v-if="injectedFetchError" />
        <div v-else>
            <!-- Loading state -->
            <div v-if="!integration" class="text-sm text-gray-500">Loading...</div>

            <!-- Main content -->
            <div v-else>
                <!-- Header with Add button -->
                <div class="flex items-center justify-between mb-4" v-if="canManageConnections">
                    <h2 class="text-sm font-medium text-gray-700">Connections</h2>
                    <UButton
                        color="blue"
                        variant="soft"
                        size="xs"
                        @click="openLinkModal"
                    >
                        <UIcon name="heroicons-plus" class="w-4 h-4 mr-1" />
                        Link Another Connection
                    </UButton>
                </div>

                <!-- Connections list -->
                <div class="space-y-4">
                    <div
                        v-for="conn in connections"
                        :key="conn.id"
                        class="border border-gray-200 rounded-lg p-4"
                    >
                        <div class="flex items-center justify-between">
                            <div class="flex items-center gap-3">
                                <DataSourceIcon :type="conn.type" class="h-8" />
                                <div>
                                    <div class="font-semibold text-gray-900">{{ conn.name }}</div>
                                    <div class="text-xs text-gray-500">{{ conn.type }}</div>
                                </div>
                            </div>
                            <div class="flex items-center gap-2">
                                <!-- Connection status badge -->
                                <span :class="['px-2 py-0.5 rounded text-xs border flex items-center gap-1', getStatusClass(conn)]">
                                    {{ getStatusLabel(conn) }}
                                </span>
                                <!-- Last checked time -->
                                <span v-if="getLastChecked(conn)" class="text-[10px] text-gray-400">
                                    {{ getLastChecked(conn) }}
                                </span>
                                <!-- Test/Refresh button -->
                                <button
                                    @click="testConnection(conn.id)"
                                    :disabled="testingConnectionId === conn.id"
                                    class="p-1.5 rounded hover:bg-gray-100 disabled:opacity-50"
                                    title="Test connection"
                                >
                                    <Spinner v-if="testingConnectionId === conn.id" class="w-4 h-4" />
                                    <UIcon v-else name="heroicons-arrow-path" class="w-4 h-4 text-gray-500" />
                                </button>
                                <!-- Edit button -->
                                <UButton
                                    v-if="canManageConnections"
                                    color="gray"
                                    variant="ghost"
                                    size="xs"
                                    @click="openEditModal(conn)"
                                >
                                    <UIcon name="heroicons-pencil" class="w-4 h-4" />
                                </UButton>
                                <!-- Unlink button (only if more than 1 connection) -->
                                <UButton
                                    v-if="canManageConnections && connections.length > 1"
                                    color="red"
                                    variant="ghost"
                                    size="xs"
                                    @click="unlinkConnection(conn.id)"
                                    title="Unlink connection"
                                >
                                    <UIcon name="heroicons-link-slash" class="w-4 h-4" />
                                </UButton>
                            </div>
                        </div>

                        <!-- Test result (inline) -->
                        <div v-if="testResults[conn.id]" class="mt-2 ml-11 text-xs">
                            <span :class="testResults[conn.id]?.success ? 'text-green-600' : 'text-red-600'">
                                {{ testResults[conn.id]?.success ? 'Connection successful' : (testResults[conn.id]?.message || 'Connection failed') }}
                            </span>
                        </div>

                        <!-- User Connection (only for user_required auth, non-admin) -->
                        <div class="mt-4 ml-11" v-if="conn.auth_policy === 'user_required' && !isAdmin">
                            <div class="text-sm text-gray-800 flex items-center space-x-3">
                                <template v-if="conn.user_status?.has_user_credentials">
                                    <span class="inline-flex items-center text-green-700 text-xs">
                                        <UIcon name="heroicons-check-circle" class="w-3 h-3 mr-1" />
                                        Connected as {{ connectedUserDisplay }}
                                    </span>
                                    <UButton size="xs" color="gray" variant="ghost" :loading="testingUserConnectionId === conn.id" @click="testUserConnection(conn.id)">
                                        <UIcon name="heroicons-play" class="w-4 h-4" />
                                    </UButton>
                                    <UButton size="xs" color="red" variant="ghost" @click="disconnectUserCredentials(conn.id)">Disconnect</UButton>
                                </template>
                                <template v-else>
                                    <span class="inline-flex items-center text-gray-500 text-xs">
                                        <UIcon name="heroicons-exclamation-circle" class="w-3 h-3 mr-1" />
                                        User credentials required
                                    </span>
                                    <UButton size="xs" color="blue" variant="soft" @click="openAddCredentials(conn.id)">Connect</UButton>
                                </template>
                            </div>
                        </div>
                    </div>

                    <!-- Empty state -->
                    <div v-if="connections.length === 0" class="text-center py-8 text-gray-500">
                        <UIcon name="heroicons-link" class="w-8 h-8 mx-auto mb-2 text-gray-400" />
                        <p class="text-sm">No connections linked to this domain.</p>
                        <UButton
                            v-if="canManageConnections"
                            color="blue"
                            variant="soft"
                            size="sm"
                            class="mt-3"
                            @click="openLinkModal"
                        >
                            Link a Connection
                        </UButton>
                    </div>
                </div>
            </div>
        </div>
    </div>

    <!-- Link Connection Modal -->
    <UModal v-model="showLinkModal" :ui="{ width: 'sm:max-w-md' }">
        <UCard>
            <template #header>
                <div class="flex items-center justify-between">
                    <h3 class="text-lg font-semibold">Link Connection</h3>
                    <UButton color="gray" variant="ghost" icon="i-heroicons-x-mark" @click="showLinkModal = false" />
                </div>
            </template>

            <div class="space-y-4">
                <p class="text-sm text-gray-600">Select a connection to link to this domain.</p>

                <!-- Loading state -->
                <div v-if="loadingOrgConnections" class="flex items-center justify-center py-4">
                    <Spinner class="w-5 h-5" />
                </div>

                <!-- Connection list -->
                <div v-else-if="availableConnections.length > 0" class="space-y-2 max-h-64 overflow-y-auto">
                    <label
                        v-for="conn in availableConnections"
                        :key="conn.id"
                        class="flex items-center p-3 border border-gray-200 rounded-lg cursor-pointer hover:bg-gray-50"
                        :class="{ 'border-blue-500 bg-blue-50': selectedConnectionId === conn.id }"
                    >
                        <input
                            type="radio"
                            name="connection"
                            :value="conn.id"
                            v-model="selectedConnectionId"
                            class="sr-only"
                        />
                        <DataSourceIcon :type="conn.type" class="w-6 h-6 mr-3" />
                        <div class="flex-1 min-w-0">
                            <div class="font-medium text-gray-900 truncate">{{ conn.name }}</div>
                            <div class="text-xs text-gray-500">{{ conn.type }}</div>
                        </div>
                        <UIcon
                            v-if="selectedConnectionId === conn.id"
                            name="heroicons-check-circle-solid"
                            class="w-5 h-5 text-blue-600"
                        />
                    </label>
                </div>

                <!-- No connections available -->
                <div v-else class="text-center py-4 text-gray-500">
                    <p class="text-sm">No available connections to link.</p>
                    <p class="text-xs mt-1">All organization connections are already linked to this domain.</p>
                </div>
            </div>

            <template #footer>
                <div class="flex justify-end gap-2">
                    <UButton color="gray" variant="ghost" @click="showLinkModal = false">Cancel</UButton>
                    <UButton
                        color="blue"
                        :disabled="!selectedConnectionId || isLinking"
                        :loading="isLinking"
                        @click="linkConnection"
                    >
                        Link Connection
                    </UButton>
                </div>
            </template>
        </UCard>
    </UModal>

    <!-- Edit Connection Modal -->
    <UModal v-model="showEditModal" :ui="{ width: 'sm:max-w-xl' }">
        <UCard>
            <template #header>
                <div class="flex items-center justify-between">
                    <div class="flex items-center gap-2">
                        <DataSourceIcon :type="editingConnection?.type" class="h-5 w-5" />
                        <h3 class="text-lg font-semibold">Edit Connection</h3>
                    </div>
                    <UButton color="gray" variant="ghost" icon="i-heroicons-x-mark" @click="showEditModal = false" />
                </div>
            </template>

            <ConnectForm
                v-if="showEditModal && editingConnection"
                mode="edit"
                :connection-id="editingConnection.id"
                :initial-type="editingConnection.type"
                :initial-values="getEditFormValues(editingConnection)"
                :show-test-button="true"
                :show-llm-toggle="false"
                :allow-name-edit="true"
                :force-show-system-credentials="true"
                :show-require-user-auth-toggle="true"
                :hide-header="true"
                @success="handleEditSuccess"
            />
        </UCard>
    </UModal>

    <!-- Modal for managing user credentials -->
    <UserDataSourceCredentialsModal v-model="showCredsModal" :data-source="integration" @saved="onCredsSaved" />
</template>

<script setup lang="ts">
definePageMeta({ auth: true, layout: 'data' })
import ConnectForm from '~/components/datasources/ConnectForm.vue'
import UserDataSourceCredentialsModal from '~/components/UserDataSourceCredentialsModal.vue'
import Spinner from '~/components/Spinner.vue'
import { useCan } from '~/composables/usePermissions'
import { useOrganization } from '~/composables/useOrganization'
import type { Ref } from 'vue'

const route = useRoute()
const toast = useToast()
const dsId = computed(() => String(route.params.id || ''))
const canManageConnections = computed(() => useCan('manage_connections'))
const { data: currentUser } = useAuth()
const { organization } = useOrganization()

// Inject integration data from layout (avoid duplicate API calls)
const injectedIntegration = inject<Ref<any>>('integration', ref(null))
const injectedFetchIntegration = inject<() => Promise<void>>('fetchIntegration', async () => {})
const injectedFetchError = inject<Ref<number | null>>('fetchError', ref(null))

// Use injected data
const integration = injectedIntegration

// Connections from integration
const connections = computed(() => integration.value?.connections || [])

// State
const testingConnectionId = ref<string | null>(null)
const testingUserConnectionId = ref<string | null>(null)
const testResults = ref<Record<string, any>>({})
const showEditModal = ref(false)
const showLinkModal = ref(false)
const showCredsModal = ref(false)
const editingConnection = ref<any>(null)
const selectedConnectionId = ref<string | null>(null)
const loadingOrgConnections = ref(false)
const orgConnections = ref<any[]>([])
const isLinking = ref(false)

const connectedUserDisplay = computed(() => {
  const u = (currentUser.value as any) || {}
  return u.name || u.email || 'You'
})

const isAdmin = computed(() => {
  const orgs = (((currentUser.value as any) || {}).organizations || [])
  const org = orgs.find((o: any) => o.id === organization.value?.id)
  return org?.role === 'admin'
})

// Available connections (org connections not already linked)
const availableConnections = computed(() => {
  const linkedIds = new Set(connections.value.map((c: any) => c.id))
  return orgConnections.value.filter(c => !linkedIds.has(c.id))
})

// Status helpers
function getStatusClass(conn: any) {
    const status = String(conn.user_status?.connection || '').toLowerCase()
    if (status === 'success') return 'bg-green-50 text-green-700 border-green-200'
    if (status === 'not_connected' || status === 'offline') return 'bg-red-50 text-red-700 border-red-200'
    return 'bg-gray-50 text-gray-700 border-gray-200'
}

function getStatusLabel(conn: any) {
    const status = String(conn.user_status?.connection || '').toLowerCase()
    if (status === 'success') return 'Connected'
    if (status === 'not_connected') return 'Not connected'
    if (status === 'offline') return 'Offline'
    return 'Unknown'
}

function getLastChecked(conn: any) {
    const lastCheckedAt = conn.user_status?.last_checked_at
    if (!lastCheckedAt) return null
    return `Checked ${timeAgo(lastCheckedAt)}`
}

function timeAgo(date: string) {
    const seconds = Math.floor((Date.now() - new Date(date).getTime()) / 1000)
    if (seconds < 60) return 'just now'
    if (seconds < 3600) return `${Math.floor(seconds / 60)}m ago`
    if (seconds < 86400) return `${Math.floor(seconds / 3600)}h ago`
    return `${Math.floor(seconds / 86400)}d ago`
}

function getEditFormValues(conn: any) {
    return {
        name: conn.name,
        config: conn.config || {},
        auth_policy: conn.auth_policy || 'system_only',
        has_credentials: true,
        credentials: {}
    }
}

// Actions
async function testConnection(connectionId: string) {
  if (!dsId.value || testingConnectionId.value) return
  testingConnectionId.value = connectionId
  testResults.value[connectionId] = null
  try {
    const response = await useMyFetch(`/data_sources/${dsId.value}/test_connection`, { method: 'GET' })
    testResults.value[connectionId] = (response.data as any)?.value || null
    await injectedFetchIntegration()
  } finally {
    testingConnectionId.value = null
  }
}

async function testUserConnection(connectionId: string) {
  if (!dsId.value || testingUserConnectionId.value) return
  testingUserConnectionId.value = connectionId
  try {
    const response = await useMyFetch(`/data_sources/${dsId.value}/test_connection`, { method: 'GET' })
    testResults.value[connectionId] = (response.data as any)?.value || null
    await injectedFetchIntegration()
  } finally {
    testingUserConnectionId.value = null
  }
}

function openEditModal(conn: any) {
    editingConnection.value = conn
    showEditModal.value = true
}

function handleEditSuccess() {
  showEditModal.value = false
  editingConnection.value = null
  injectedFetchIntegration()
}

function openAddCredentials(connectionId: string) {
  showCredsModal.value = true
}

async function disconnectUserCredentials(connectionId: string) {
  if (!dsId.value) return
  try {
    await useMyFetch(`/data_sources/${dsId.value}/my-credentials`, { method: 'DELETE' })
    await injectedFetchIntegration()
  } catch (e) {
    // no-op
  }
}

async function onCredsSaved() {
  showCredsModal.value = false
  await injectedFetchIntegration()
}

// Link connection
async function openLinkModal() {
    showLinkModal.value = true
    selectedConnectionId.value = null
    loadingOrgConnections.value = true
    try {
        const response = await useMyFetch('/connections', { method: 'GET' })
        orgConnections.value = (response.data as any)?.value || []
    } catch (e) {
        orgConnections.value = []
    } finally {
        loadingOrgConnections.value = false
    }
}

async function linkConnection() {
    if (!selectedConnectionId.value || !dsId.value || isLinking.value) return
    isLinking.value = true
    try {
        await useMyFetch(`/data_sources/${dsId.value}/connections/${selectedConnectionId.value}`, {
            method: 'POST'
        })
        toast.add({ title: 'Connection linked', color: 'green' })
        showLinkModal.value = false
        selectedConnectionId.value = null
        await injectedFetchIntegration()
    } catch (e: any) {
        toast.add({
            title: 'Failed to link connection',
            description: e?.message || 'An error occurred',
            color: 'red'
        })
    } finally {
        isLinking.value = false
    }
}

async function unlinkConnection(connectionId: string) {
    if (!dsId.value) return
    if (!confirm('Are you sure you want to unlink this connection?')) return
    try {
        await useMyFetch(`/data_sources/${dsId.value}/connections/${connectionId}`, {
            method: 'DELETE'
        })
        toast.add({ title: 'Connection unlinked', color: 'green' })
        await injectedFetchIntegration()
    } catch (e: any) {
        toast.add({
            title: 'Failed to unlink connection',
            description: e?.message || 'An error occurred',
            color: 'red'
        })
    }
}
</script>
