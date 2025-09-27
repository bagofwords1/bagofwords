<template>
    <div class="py-6">
        <div class="border border-gray-200 rounded-xl p-6 bg-white">
            <div class="flex">
                <!-- Left tabs -->
                <aside class="w-56 border-r border-gray-200">
                    <nav class="py-3 space-y-1">
                        <button
                            v-for="t in tabs"
                            :key="t.key"
                            @click="activeTab = t.key"
                            :class="[
                                'w-full text-left px-3 py-2 text-sm rounded-lg transition-colors cursor-pointer',
                                activeTab === t.key ? 'text-gray-900 bg-gray-50' : 'text-gray-600 hover:bg-gray-50'
                            ]"
                        >
                            {{ t.label }}
                        </button>
                    </nav>
                </aside>

                <!-- Right content -->
                <main class="flex-1 p-6">
                    <!-- General -->
                    <section v-if="activeTab === 'general'" class="space-y-8">
                        <!-- Name -->
                        <div class="space-y-2">
                            <label class="block text-sm font-medium text-gray-800">Data source name</label>
                            <div class="flex items-center gap-2">
                                <input v-model="form.name" type="text" :disabled="!canUpdateDataSource" class="border border-gray-200 rounded-lg px-3 py-2 w-full h-9 text-sm focus:outline-none focus:ring-2 focus:ring-blue-200 disabled:bg-gray-50 disabled:text-gray-500" placeholder="Name" />
                                <button v-if="canUpdateDataSource" @click="saveName" :disabled="saving.name || form.name.trim() === '' || form.name === original.name" :class="['px-3 py-1.5 text-xs rounded-lg border transition-colors', (saving.name || form.name.trim() === '' || form.name === original.name) ? 'border-gray-200 text-gray-400 bg-gray-50 cursor-not-allowed' : 'border-gray-300 text-gray-700 hover:bg-gray-50']">{{ saving.name ? 'Saving…' : 'Save' }}</button>
                            </div>
                        </div>

                        <!-- Visibility -->
                        <div class="space-y-2">
                            <label class="block text-sm font-medium text-gray-800">Access</label>
                            <div class="flex items-center space-x-3">
                                <UToggle v-model="form.isPublic" @update:model-value="onTogglePublic" :disabled="!canUpdateDataSource" />
                                <span class="text-xs text-gray-500">Public access allows all organization members to use this data source. <span v-if="!form.isPublic">Control individual access via the members tab.</span></span>
                            </div>
                        </div>

                        <!-- User auth required -->
                        <div class="space-y-2">
                            <label class="block text-sm font-medium text-gray-800">Require User Auth</label>
                            <div class="flex items-center space-x-3">
                                <UToggle v-model="form.userRequired" @update:model-value="onToggleUserAuth" :disabled="!canUpdateDataSource" />
                                <span class="text-xs text-gray-500">If on, each user must provide their own credentials.</span>
                            </div>
                        </div>

                        <!-- Danger zone -->
                        <div v-if="canUpdateDataSource" class="border border-red-200 p-4 rounded-lg bg-red-50/40">
                            <div class="text-sm font-medium text-red-700">Danger zone</div>
                            <div class="text-xs text-gray-600 mt-1">Deleting a data source is irreversible.</div>
                            <div class="mt-3">
                                <button @click="showDelete = true" class="px-3 py-1.5 text-xs border border-red-300 text-red-700 rounded-lg hover:bg-red-50 transition-colors">Delete data source</button>
                            </div>
                        </div>

                        <UModal v-model="showDelete" :ui="{ width: 'sm:max-w-md' }">
                            <div class="p-5">
                                <div class="text-sm font-medium text-gray-900">Delete data source?</div>
                                <div class="text-xs text-gray-600 mt-2">This action cannot be undone.</div>
                                <div class="flex justify-end gap-2 mt-5">
                                    <button @click="showDelete = false" class="px-3 py-1.5 text-xs border border-gray-300 text-gray-700 rounded-lg">Cancel</button>
                                    <button @click="confirmDelete" :disabled="deleting" class="px-3 py-1.5 text-xs border border-red-300 text-red-700 rounded-lg hover:bg-red-50">{{ deleting ? 'Deleting…' : 'Delete' }}</button>
                                </div>
                            </div>
                        </UModal>
                    </section>

                    <!-- Members -->
                    <section v-else-if="activeTab === 'members'" class="space-y-4">
                        <div v-if="!original.isPublic">
                        <div class="flex items-center justify-between mb-4">
                            <h3 class="text-sm font-medium text-gray-900">Members</h3>
                            <button v-if="canUpdateDataSource" @click="openAdd" class="px-2.5 py-1.5 text-xs bg-blue-500 text-white rounded-lg hover:bg-blue-600">Add member</button>
                        </div>

                        <div class="border border-gray-200 rounded-lg overflow-hidden">
                            <div :class="['text-xs text-gray-600 border-b border-gray-200 bg-gray-50 grid', canUpdateDataSource ? 'grid-cols-3' : 'grid-cols-2']">
                                <div class="px-3 py-2">User</div>
                                <div class="px-3 py-2">Role</div>
                                <div v-if="canUpdateDataSource" class="px-3 py-2">Actions</div>
                            </div>
                            <div v-for="m in members" :key="m.id" :class="['text-sm text-gray-800 border-t border-gray-200 grid', canUpdateDataSource ? 'grid-cols-3' : 'grid-cols-2']">
                                <div class="px-3 py-2">
                                    <div class="font-medium">{{ displayName(m.id) }}</div>
                                    <div class="text-xs text-gray-500" v-if="displayEmail(m.id)">{{ displayEmail(m.id) }}</div>
                                </div>
                                <div class="px-3 py-2">{{ m.role }}</div>
                                <div v-if="canUpdateDataSource" class="px-3 py-2">
                                    <button @click="removeMember(m.id)" class="text-xs border border-gray-300 text-gray-700 rounded-lg px-2 py-0.5 hover:bg-gray-50">Remove</button>
                                </div>
                            </div>
                            <div v-if="members.length === 0" class="px-3 py-6 text-xs text-gray-500">No members yet.</div>
                        </div>

                        <UModal v-model="showAddModal" :ui="{ width: 'sm:max-w-md' }">
                            <div class="p-4">
                                <div class="text-sm font-medium text-gray-900 mb-2">Add members</div>
                                <div class="text-xs text-gray-600 mb-3">Select users to grant access.</div>

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
                                    <button @click="addSelectedUsers" :disabled="selectedUsers.length === 0 || adding" class="px-3 py-1.5 text-xs border border-gray-300 text-gray-700 rounded-lg">{{ adding ? 'Adding…' : 'Add' }}</button>
                                </div>
                            </div>
                        </UModal>
                    </div>
                    <div v-else>
                        <div class="text-sm font-medium text-gray-900">Members</div>
                        <div class="text-xs text-gray-600 mt-1">This data source is public. 
                            
                            <span v-if="!original.userRequired">All organization members can use it.</span>
                            <span v-else>Each user must provide their own credentials.</span>
                        </div>
                    </div>
                    </section>
                </main>
            </div>
        </div>
    </div>
</template>

<script setup lang="ts">
definePageMeta({ auth: true, layout: 'integrations' })
import { useCan } from '~/composables/usePermissions'

type TabKey = 'general' | 'members'
const tabs: { key: TabKey, label: string }[] = [
    { key: 'general', label: 'General' },
    { key: 'members', label: 'Members' }
]
const activeTab = ref<TabKey>('general')

const route = useRoute()
const router = useRouter()
const toast = useToast?.()

const form = reactive({
    name: '',
    isPublic: false,
    userRequired: false,
    authTypes: [] as string[]
})

const original = reactive({
    name: '',
    isPublic: false,
    userRequired: false,
})

const saving = reactive({ name: false, public: false, auth: false })
const deleting = ref(false)
const ready = ref(false)
const showDelete = ref(false)
const adding = ref(false)
const canUpdateDataSource = computed(() => useCan('update_data_source'))

async function loadDataSource() {
    const id = route.params.id as string
    const { data, error } = await useMyFetch(`/data_sources/${id}`, { method: 'GET' })
    if (error?.value) return
    const ds = data.value as any
    form.name = ds?.name || ''
    form.isPublic = !!ds?.is_public
    form.userRequired = (ds?.auth_policy === 'user_required')
    original.name = form.name
    original.isPublic = form.isPublic
    original.userRequired = form.userRequired
    ready.value = true
}

onMounted(loadDataSource)

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
    // keep a full list for lookups
    ;(allUsers as any).value = all
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
    if (ok) original.name = form.name
    saving.name = false
}

async function onTogglePublic(value: boolean) {
    if (!ready.value) return
    saving.public = true
    const ok = await updateDataSource({ is_public: value })
    if (ok) original.isPublic = value
    saving.public = false
}

async function onToggleUserAuth(value: boolean) {
    if (!ready.value) return
    saving.auth = true
    const ok = await updateDataSource({ auth_policy: value ? 'user_required' : 'system_only' })
    if (ok) original.userRequired = value
    saving.auth = false
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
    const user = (allUsers.value || []).find(u => u.id === userId) || availableUsers.value.find(u => u.id === userId)
    return user?.display_name || user?.email || 'User'
}

function displayEmail(userId: string) {
    const user = (allUsers.value || []).find(u => u.id === userId) || availableUsers.value.find(u => u.id === userId)
    return user?.email || ''
}

async function confirmDelete() {
    if (deleting.value) return
    deleting.value = true
    const id = route.params.id as string
    const { error } = await useMyFetch(`/data_sources/${id}`, { method: 'DELETE' })
    deleting.value = false
    if (!error?.value) {
        toast?.add?.({ title: 'Data source deleted' })
        showDelete.value = false
        router.push('/integrations')
    } else {
        toast?.add?.({ title: 'Failed to delete', description: String(error.value), color: 'red' })
    }
}

// initial members
onMounted(async () => {
    await loadMembers()
    await loadAvailableUsers()
})
</script>

