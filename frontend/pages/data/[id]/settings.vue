<template>
    <div class="py-6">
        <div class="border border-gray-200 rounded-xl p-6 bg-white">
            <div v-if="!ready" class="inline-flex items-center text-gray-500 text-xs">
                <Spinner class="w-4 h-4 mr-2" />
                Loading settings...
            </div>
            
            <div v-else class="space-y-8">
                <!-- Domain Name -->
                <div class="space-y-2">
                    <label class="block text-sm font-medium text-gray-800">Domain name</label>
                    <div class="flex items-center gap-2">
                        <input 
                            v-model="form.name" 
                            type="text" 
                            :disabled="!canUpdateDataSource" 
                            class="border border-gray-200 rounded-lg px-3 py-2 w-full max-w-md h-9 text-sm focus:outline-none focus:ring-2 focus:ring-blue-200 disabled:bg-gray-50 disabled:text-gray-500" 
                            placeholder="Name" 
                        />
                        <button 
                            v-if="canUpdateDataSource" 
                            @click="saveName" 
                            :disabled="saving.name || form.name.trim() === '' || form.name === original.name" 
                            :class="['px-3 py-1.5 text-xs rounded-lg border transition-colors', (saving.name || form.name.trim() === '' || form.name === original.name) ? 'border-gray-200 text-gray-400 bg-gray-50 cursor-not-allowed' : 'border-gray-300 text-gray-700 hover:bg-gray-50']"
                        >
                            {{ saving.name ? 'Saving…' : 'Save' }}
                        </button>
                    </div>
                </div>

                <!-- Access -->
                <div class="space-y-2">
                    <label class="block text-sm font-medium text-gray-800">Access</label>
                    <div class="flex items-center space-x-3">
                        <UToggle v-model="form.isPublic" @update:model-value="onTogglePublic" :disabled="!canUpdateDataSource" />
                        <span class="text-xs text-gray-500">
                            Public access allows all organization members to use this domain.
                        </span>
                    </div>
                </div>

                <!-- Members Section (only shown when not public) -->
                <div v-if="!form.isPublic" class="space-y-4">
                    <div class="flex items-center justify-between">
                        <div>
                            <h3 class="text-sm font-medium text-gray-800">Members</h3>
                            <p class="text-xs text-gray-500 mt-0.5">Users with access to this domain</p>
                        </div>
                        <button 
                            v-if="canUpdateDataSource" 
                            @click="openAdd" 
                            class="px-2.5 py-1.5 text-xs bg-blue-500 text-white rounded-lg hover:bg-blue-600"
                        >
                            Add member
                        </button>
                    </div>

                    <div class="border border-gray-200 rounded-lg overflow-hidden">
                        <div :class="['text-xs text-gray-600 border-b border-gray-200 bg-gray-50 grid', canUpdateDataSource ? 'grid-cols-3' : 'grid-cols-2']">
                            <div class="px-3 py-2">User</div>
                            <div class="px-3 py-2">Role</div>
                            <div v-if="canUpdateDataSource" class="px-3 py-2">Actions</div>
                        </div>
                        <div 
                            v-for="m in members" 
                            :key="m.id" 
                            :class="['text-sm text-gray-800 border-t border-gray-200 grid', canUpdateDataSource ? 'grid-cols-3' : 'grid-cols-2']"
                        >
                            <div class="px-3 py-2">
                                <div class="font-medium">{{ displayName(m.id) }}</div>
                                <div class="text-xs text-gray-500" v-if="displayEmail(m.id)">{{ displayEmail(m.id) }}</div>
                            </div>
                            <div class="px-3 py-2">{{ m.role }}</div>
                            <div v-if="canUpdateDataSource" class="px-3 py-2">
                                <button @click="removeMember(m.id)" class="text-xs border border-gray-300 text-gray-700 rounded-lg px-2 py-0.5 hover:bg-gray-50">Remove</button>
                            </div>
                        </div>
                        <div v-if="members.length === 0" class="px-3 py-6 text-xs text-gray-500 text-center">
                            No members yet. All organization members have access by default.
                        </div>
                    </div>
                </div>

                <!-- Danger zone -->
                <div v-if="canUpdateDataSource" class="max-w-md border border-red-200 p-4 rounded-lg bg-red-50/40">
                    <div class="text-sm font-medium text-red-700">Danger zone</div>
                    <div class="text-xs text-gray-600 mt-1">Removing this domain will disconnect it from the data source. You can reconnect later.</div>
                    <div class="mt-3">
                        <button @click="showDelete = true" class="px-3 py-1.5 text-xs border border-red-300 text-red-700 rounded-lg hover:bg-red-50 transition-colors">
                            Remove domain
                        </button>
                    </div>
                </div>
            </div>
        </div>

        <!-- Add member modal -->
        <UModal v-model="showAddModal" :ui="{ width: 'sm:max-w-md' }">
            <div class="p-4">
                <div class="text-sm font-medium text-gray-900 mb-2">Add members</div>
                <div class="text-xs text-gray-600 mb-3">Select users to grant access to this domain.</div>

                <USelectMenu
                    v-model="selectedUsers"
                    :options="availableUsers"
                    multiple
                    searchable
                    searchable-placeholder="Search users..."
                    option-attribute="display_name"
                    value-attribute="id"
                    class="w-full"
                    :search-attributes="['display_name','email']"
                />

                <div class="flex justify-end space-x-2 mt-4">
                    <button @click="showAddModal = false" class="px-3 py-1.5 text-xs border border-gray-300 text-gray-700 rounded-lg">Cancel</button>
                    <button @click="addSelectedUsers" :disabled="selectedUsers.length === 0 || adding" class="px-3 py-1.5 text-xs bg-blue-500 text-white rounded-lg hover:bg-blue-600 disabled:opacity-50">
                        {{ adding ? 'Adding…' : 'Add' }}
                    </button>
                </div>
            </div>
        </UModal>

        <!-- Delete confirmation modal -->
        <UModal v-model="showDelete" :ui="{ width: 'sm:max-w-md' }">
            <div class="p-5">
                <div class="text-sm font-medium text-gray-900">Remove domain?</div>
                <div class="text-xs text-gray-600 mt-2">This will remove the domain and disconnect it from the data source. You can reconnect later.</div>
                <div class="flex justify-end gap-2 mt-5">
                    <button @click="showDelete = false" class="px-3 py-1.5 text-xs border border-gray-300 text-gray-700 rounded-lg">Cancel</button>
                    <button @click="confirmDelete" :disabled="deleting" class="px-3 py-1.5 text-xs bg-red-500 text-white rounded-lg hover:bg-red-600 disabled:opacity-50">
                        {{ deleting ? 'Deleting…' : 'Delete' }}
                    </button>
                </div>
            </div>
        </UModal>
    </div>
</template>

<script setup lang="ts">
definePageMeta({ auth: true, layout: 'data' })
import { useCan } from '~/composables/usePermissions'
import Spinner from '@/components/Spinner.vue'
import type { Ref } from 'vue'

const route = useRoute()
const router = useRouter()
const toast = useToast?.()

// Inject integration data from layout (avoid duplicate API calls)
const injectedIntegration = inject<Ref<any>>('integration', ref(null))
const injectedFetchIntegration = inject<() => Promise<void>>('fetchIntegration', async () => {})
const injectedLoading = inject<Ref<boolean>>('isLoading', ref(true))

const form = reactive({
    name: '',
    isPublic: true
})

const original = reactive({
    name: '',
    isPublic: true
})

const saving = reactive({ name: false, public: false })
const deleting = ref(false)
const ready = computed(() => !injectedLoading.value && !!injectedIntegration.value)
const showDelete = ref(false)
const adding = ref(false)
const canUpdateDataSource = computed(() => useCan('update_data_source'))

// Initialize form from injected data
watch(injectedIntegration, (ds) => {
    if (ds) {
        form.name = ds?.name || ''
        form.isPublic = ds?.is_public ?? true
        original.name = form.name
        original.isPublic = form.isPublic
    }
}, { immediate: true })

async function loadMembers() {
    const id = route.params.id as string
    const { data, error } = await useMyFetch(`/data_sources/${id}/members`, { method: 'GET' })
    if (error?.value) return
    const list = (data.value as any[]) || []
    members.value = list.map((m: any) => ({ id: m.principal_id, name: m.principal_id, role: 'Member' }))
}

async function loadAvailableUsers() {
    const { data, error } = await useMyFetch('/organization/members', { method: 'GET' })
    if (error?.value) return
    const all = ((data.value as any[]) || []).map(u => ({ id: u.id, display_name: u.display_name || u.name || u.email || 'User', email: u.email }))
    allUsers.value = all
    const memberIds = new Set(members.value.map(m => m.id))
    availableUsers.value = all.filter(u => !memberIds.has(u.id))
}

async function openAdd() {
    await loadAvailableUsers()
    showAddModal.value = true
}

async function updateDataSource(payload: Record<string, any>) {
    const id = route.params.id as string
    const { error } = await useMyFetch(`/data_sources/${id}`, {
        method: 'PUT',
        body: payload,
    })
    if (!error?.value) {
        toast?.add?.({ title: 'Saved', description: 'Settings updated' })
        return true
    } else {
        toast?.add?.({ title: 'Error', description: String(error.value), color: 'red' })
        return false
    }
}

async function saveName() {
    if (!ready.value || form.name.trim() === '' || form.name === original.name) return
    saving.name = true
    const ok = await updateDataSource({ name: form.name })
    if (ok) {
        original.name = form.name
        await injectedFetchIntegration() // Refresh header
    }
    saving.name = false
}

async function onTogglePublic(value: boolean) {
    if (!ready.value) return
    saving.public = true
    const ok = await updateDataSource({ is_public: value })
    if (ok) original.isPublic = value
    saving.public = false
}

interface MemberItem { id: string; name: string; role: string; email?: string }
const members = ref<MemberItem[]>([])

const showAddModal = ref(false)
const selectedUsers = ref<string[]>([])
const availableUsers = ref<{ id: string; display_name: string; email?: string }[]>([])
const allUsers = ref<{ id: string; display_name: string; email?: string }[]>([])

function addSelectedUsers() {
    if (selectedUsers.value.length === 0 || adding.value) return
    adding.value = true
    const id = route.params.id as string
    Promise.all(selectedUsers.value.map(uid => useMyFetch(`/data_sources/${id}/members`, {
        method: 'POST',
        body: { principal_type: 'user', principal_id: uid },
    }))).then(() => {
        toast?.add?.({ title: 'Members added' })
        selectedUsers.value = []
        showAddModal.value = false
        loadMembers()
        loadAvailableUsers()
    }).finally(() => { adding.value = false })
}

function removeMember(id: string) {
    const dsId = route.params.id as string
    useMyFetch(`/data_sources/${dsId}/members/${id}`, { method: 'DELETE' })
        .then(() => {
            members.value = members.value.filter(m => m.id !== id)
            toast?.add?.({ title: 'Member removed' })
            loadAvailableUsers()
        })
}

function displayName(userId: string) {
    const user = allUsers.value.find(u => u.id === userId) || availableUsers.value.find(u => u.id === userId)
    return user?.display_name || user?.email || 'User'
}

function displayEmail(userId: string) {
    const user = allUsers.value.find(u => u.id === userId) || availableUsers.value.find(u => u.id === userId)
    return user?.email || ''
}

async function confirmDelete() {
    if (deleting.value) return
    deleting.value = true
    const id = route.params.id as string
    const { error } = await useMyFetch(`/data_sources/${id}`, { method: 'DELETE' })
    deleting.value = false
    if (!error?.value) {
        toast?.add?.({ title: 'Domain deleted' })
        showDelete.value = false
        router.push('/data')
    } else {
        toast?.add?.({ title: 'Failed to delete', description: String(error.value), color: 'red' })
    }
}

// Load members on mount
onMounted(async () => {
    await loadMembers()
    await loadAvailableUsers()
})
</script>
