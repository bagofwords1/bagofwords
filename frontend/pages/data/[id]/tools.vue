<template>
    <div class="py-6">
        <div v-if="injectedFetchError" />
        <div v-else>
            <ToolsSelector
                :ds-id="id"
                :connections="mcpConnections"
                :can-update="canUpdateDataSource"
                @add-mcp="showMCPModal = true"
                @edit-connection="openEditModal"
                @delete-connection="confirmDelete"
            />
        </div>

        <!-- Add MCP Modal -->
        <AddMCPModal v-model="showMCPModal" @created="onConnectionCreated" />

        <!-- Edit MCP Modal -->
        <AddMCPModal v-model="showEditModal" :edit-connection="editingConnection" @created="onConnectionUpdated" />

        <!-- Delete confirmation -->
        <UModal v-model="showDeleteModal" :ui="{ width: 'sm:max-w-sm' }">
            <div class="p-6">
                <h3 class="text-sm font-semibold text-gray-900 mb-2">Remove MCP Connection</h3>
                <p class="text-xs text-gray-500 mb-4">
                    Remove <strong>{{ deletingConnection?.name }}</strong> and all its discovered tools from this data source?
                </p>
                <div class="flex justify-end gap-2">
                    <UButton color="gray" variant="ghost" size="xs" @click="showDeleteModal = false">Cancel</UButton>
                    <UButton color="red" size="xs" :loading="deleting" @click="deleteConnection">Remove</UButton>
                </div>
            </div>
        </UModal>
    </div>
</template>

<script setup lang="ts">
definePageMeta({ auth: true, layout: 'data' })
import ToolsSelector from '@/components/datasources/ToolsSelector.vue'
import AddMCPModal from '@/components/AddMCPModal.vue'
import { useCan } from '~/composables/usePermissions'
import type { Ref } from 'vue'

const route = useRoute()
const toast = useToast()
const id = computed(() => String(route.params.id || ''))

const injectedIntegration = inject<Ref<any>>('integration', ref(null))
const injectedFetchError = inject<Ref<number | null>>('fetchError', ref(null))
const fetchIntegration = inject<() => Promise<void>>('fetchIntegration', async () => {})

const canUpdateDataSource = computed(() => useCan('update_data_source'))

const showMCPModal = ref(false)
const showEditModal = ref(false)
const editingConnection = ref<any>(null)
const showDeleteModal = ref(false)
const deletingConnection = ref<any>(null)
const deleting = ref(false)

const mcpConnections = computed(() => {
    const connections = injectedIntegration.value?.connections || []
    return connections.filter((c: any) => c.type === 'mcp' || c.type === 'custom_api')
})

async function onConnectionCreated(conn: any) {
    try {
        await useMyFetch(`/data_sources/${id.value}/connections/${conn.id}`, { method: 'POST' })
    } catch { /* link endpoint may not exist yet */ }
    try {
        await useMyFetch(`/connections/${conn.id}/refresh-tools`, { method: 'POST' })
    } catch {}
    await fetchIntegration()
}

async function onConnectionUpdated() {
    editingConnection.value = null
    await fetchIntegration()
}

function openEditModal(conn: any) {
    editingConnection.value = conn
    showEditModal.value = true
}

function confirmDelete(conn: any) {
    deletingConnection.value = conn
    showDeleteModal.value = true
}

async function deleteConnection() {
    if (!deletingConnection.value) return
    deleting.value = true
    try {
        await useMyFetch(`/connections/${deletingConnection.value.id}`, { method: 'DELETE' })
        toast.add({ title: 'Connection removed', color: 'green' })
        showDeleteModal.value = false
        deletingConnection.value = null
        await fetchIntegration()
    } catch (e: any) {
        toast.add({ title: 'Failed to remove connection', description: e?.data?.detail, color: 'red' })
    } finally {
        deleting.value = false
    }
}
</script>
