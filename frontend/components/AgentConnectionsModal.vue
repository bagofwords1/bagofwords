<template>
    <UModal v-model="isOpen" :ui="{ width: 'sm:max-w-2xl' }">
        <UCard>
            <template #header>
                <div class="flex items-center justify-between">
                    <h3 class="text-sm font-semibold text-gray-900 dark:text-white">Connections</h3>
                    <div class="flex items-center gap-2">
                        <UButton
                            v-if="canManageConnections"
                            color="blue"
                            variant="soft"
                            size="xs"
                            @click="openLinkModal"
                        >
                            <UIcon name="heroicons-plus" class="w-3.5 h-3.5 me-1" />
                            Link connection
                        </UButton>
                        <UButton color="gray" variant="ghost" size="xs" icon="i-heroicons-x-mark" @click="isOpen = false" />
                    </div>
                </div>
            </template>

            <div v-if="!ready" class="py-6 text-center text-sm text-gray-400">Loading…</div>

            <div v-else-if="connections.length === 0" class="py-8 text-center">
                <UIcon name="heroicons-link" class="w-8 h-8 mx-auto mb-2 text-gray-300 dark:text-gray-600" />
                <p class="text-sm text-gray-500 dark:text-gray-400">No connections linked to this agent.</p>
                <UButton v-if="canManageConnections" color="blue" variant="soft" size="sm" class="mt-3" @click="openLinkModal">
                    Link a connection
                </UButton>
            </div>

            <div v-else class="space-y-3">
                <div
                    v-for="conn in connections"
                    :key="conn.id"
                    class="border border-gray-200 dark:border-gray-700 rounded-lg p-3"
                >
                    <div class="flex items-center justify-between gap-3">
                        <div class="flex items-center gap-3 min-w-0">
                            <DataSourceIcon :type="conn.type" class="h-7 flex-shrink-0" />
                            <div class="min-w-0">
                                <div class="text-sm font-medium text-gray-900 dark:text-white truncate">{{ conn.name }}</div>
                                <div class="text-xs text-gray-400">{{ conn.type }}</div>
                            </div>
                        </div>
                        <div class="flex items-center gap-1.5 flex-shrink-0">
                            <span :class="['px-2 py-0.5 rounded text-xs border', getStatusClass(conn)]">
                                {{ getStatusLabel(conn) }}
                            </span>
                            <button
                                v-if="canManageConnections"
                                @click="testConnection(conn.id)"
                                :disabled="testingConnectionId === conn.id"
                                class="p-1.5 rounded hover:bg-gray-100 dark:hover:bg-gray-700 disabled:opacity-50"
                                title="Test connection"
                            >
                                <Spinner v-if="testingConnectionId === conn.id" class="w-4 h-4" />
                                <UIcon v-else name="heroicons-arrow-path" class="w-4 h-4 text-gray-400" />
                            </button>
                            <UButton
                                v-if="canManageConnections"
                                color="gray" variant="ghost" size="xs"
                                @click="openEditModal(conn)"
                            >
                                <UIcon name="heroicons-pencil" class="w-4 h-4" />
                            </UButton>
                            <UButton
                                v-if="canManageConnections && connections.length > 1"
                                color="red" variant="ghost" size="xs"
                                @click="unlinkConnection(conn.id)"
                                title="Unlink"
                            >
                                <UIcon name="heroicons-link-slash" class="w-4 h-4" />
                            </UButton>
                        </div>
                    </div>

                    <!-- Test result -->
                    <div v-if="testResults[conn.id]" class="mt-2 ms-10 text-xs">
                        <span :class="testResults[conn.id]?.success ? 'text-green-600' : 'text-red-600'">
                            {{ testResults[conn.id]?.success ? 'Connection successful' : (testResults[conn.id]?.message || 'Connection failed') }}
                        </span>
                    </div>

                    <!-- Indexing progress -->
                    <div v-if="conn.indexing" class="mt-2 ms-10">
                        <ConnectionIndexingProgress :indexing="conn.indexing" :show-logs="true" />
                        <div v-if="conn.indexing.status === 'failed' && canManageConnections" class="mt-2">
                            <UButton size="xs" color="amber" variant="soft" @click="reindexConnection(conn.id)">
                                Retry
                            </UButton>
                        </div>
                    </div>
                </div>
            </div>
        </UCard>
    </UModal>

    <!-- Link connection modal -->
    <UModal v-model="showLinkModal" :ui="{ width: 'sm:max-w-md' }">
        <UCard>
            <template #header>
                <div class="flex items-center justify-between">
                    <h3 class="text-sm font-semibold">Link connection</h3>
                    <UButton color="gray" variant="ghost" size="xs" icon="i-heroicons-x-mark" @click="showLinkModal = false" />
                </div>
            </template>

            <div v-if="loadingOrgConnections" class="flex items-center justify-center py-6">
                <Spinner class="w-5 h-5" />
            </div>
            <div v-else-if="availableConnections.length === 0" class="py-6 text-center text-sm text-gray-500 dark:text-gray-400">
                All connections are already linked to this agent.
            </div>
            <div v-else class="space-y-2 max-h-64 overflow-y-auto">
                <label
                    v-for="conn in availableConnections"
                    :key="conn.id"
                    class="flex items-center gap-3 p-3 border border-gray-200 dark:border-gray-700 rounded-lg cursor-pointer hover:bg-gray-50 dark:hover:bg-gray-800"
                    :class="{ 'border-blue-400 bg-blue-50 dark:bg-blue-950': selectedConnectionId === conn.id }"
                >
                    <input type="radio" name="link-conn" :value="conn.id" v-model="selectedConnectionId" class="sr-only" />
                    <DataSourceIcon :type="conn.type" class="h-5 flex-shrink-0" />
                    <div class="min-w-0 flex-1">
                        <div class="text-sm font-medium text-gray-900 dark:text-white truncate">{{ conn.name }}</div>
                        <div class="text-xs text-gray-400">{{ conn.type }}</div>
                    </div>
                    <UIcon v-if="selectedConnectionId === conn.id" name="heroicons-check-circle-solid" class="w-4 h-4 text-blue-500" />
                </label>
            </div>

            <template #footer>
                <div class="flex justify-end gap-2">
                    <UButton color="gray" variant="ghost" size="sm" @click="showLinkModal = false">Cancel</UButton>
                    <UButton color="blue" size="sm" :disabled="!selectedConnectionId || isLinking" :loading="isLinking" @click="linkConnection">
                        Link
                    </UButton>
                </div>
            </template>
        </UCard>
    </UModal>

    <!-- Edit connection modal -->
    <UModal v-model="showEditModal" :ui="{ width: 'sm:max-w-xl' }">
        <UCard>
            <template #header>
                <div class="flex items-center justify-between">
                    <div class="flex items-center gap-2">
                        <DataSourceIcon v-if="editingConnection" :type="editingConnection.type" class="h-5" />
                        <h3 class="text-sm font-semibold">Edit connection</h3>
                    </div>
                    <UButton color="gray" variant="ghost" size="xs" icon="i-heroicons-x-mark" @click="showEditModal = false" />
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
</template>

<script setup lang="ts">
import Spinner from '~/components/Spinner.vue'
import ConnectForm from '~/components/datasources/ConnectForm.vue'
import ConnectionIndexingProgress from '~/components/ConnectionIndexingProgress.vue'
import { useCan } from '~/composables/usePermissions'
import {
    getEffectiveStatus as deriveStatus,
    statusBadgeClass,
    statusLabel,
} from '~/composables/useConnectionStatus'
import type { Ref } from 'vue'

const props = defineProps<{
    modelValue: boolean
    // When used standalone (e.g. KnowledgeExplorer) the parent passes the
    // agent id + its connections directly. When omitted, we fall back to the
    // injected `integration` provided by the legacy agents layout.
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
const { getErrorMessage } = useErrorMessage()
const canManageConnections = computed(() => useCan('manage_connections'))

const integration = inject<Ref<any>>('integration', ref(null))
const fetchIntegration = inject<() => Promise<void>>('fetchIntegration', async () => {})

// Prefer explicit props (standalone use); fall back to the injected integration.
const dsId = computed(() => props.dsId ?? String(route.params.id || ''))
const connections = computed(() => props.connections ?? (integration.value?.connections || []))
const ready = computed(() => props.dsId != null || !!integration.value)

// Refresh both the legacy layout (via inject) and the standalone parent (via emit).
async function refresh() {
    await fetchIntegration()
    emit('changed')
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

const availableConnections = computed(() => {
    const linked = new Set(connections.value.map((c: any) => c.id))
    return orgConnections.value.filter((c) => !linked.has(c.id))
})

function getConnectionEffective(conn: any) {
    const local = testResults.value[conn.id]
    if (local) return local.success ? 'success' : 'error'
    return deriveStatus(conn)
}

function getStatusClass(conn: any) { return statusBadgeClass(getConnectionEffective(conn) as any) }
function getStatusLabel(conn: any) { return statusLabel(getConnectionEffective(conn) as any) }

function getEditFormValues(conn: any) {
    return { name: conn.name, config: conn.config || {}, auth_policy: conn.auth_policy || 'system_only', has_credentials: true, credentials: {} }
}

async function testConnection(connectionId: string) {
    if (testingConnectionId.value) return
    testingConnectionId.value = connectionId
    testResults.value[connectionId] = null
    try {
        const response = await useMyFetch(`/connections/${connectionId}/test`, { method: 'POST' })
        testResults.value[connectionId] = (response.data as any)?.value || null
        await refresh()
    } finally {
        testingConnectionId.value = null
    }
}

async function reindexConnection(connectionId: string) {
    try {
        await useMyFetch(`/connections/${connectionId}/reindex`, { method: 'POST' })
        await refresh()
    } catch (e: any) {
        toast.add({ title: 'Failed to restart indexing', color: 'red' })
    }
}

function openEditModal(conn: any) {
    editingConnection.value = conn
    showEditModal.value = true
}

function handleEditSuccess() {
    showEditModal.value = false
    editingConnection.value = null
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
        await useMyFetchStrict(`/data_sources/${dsId.value}/connections/${selectedConnectionId.value}`, { method: 'POST' })
        toast.add({ title: 'Connection linked', color: 'green' })
        showLinkModal.value = false
        selectedConnectionId.value = null
        await refresh()
    } catch (e: any) {
        toast.add({ title: 'Failed to link connection', description: getErrorMessage(e), color: 'red' })
    } finally {
        isLinking.value = false
    }
}

async function unlinkConnection(connectionId: string) {
    if (!confirm('Unlink this connection?')) return
    try {
        await useMyFetchStrict(`/data_sources/${dsId.value}/connections/${connectionId}`, { method: 'DELETE' })
        toast.add({ title: 'Connection unlinked', color: 'green' })
        await refresh()
    } catch (e: any) {
        toast.add({ title: 'Failed to unlink connection', description: getErrorMessage(e), color: 'red' })
    }
}
</script>
