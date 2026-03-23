<template>
    <div class="py-6">
        <!-- Hide content when there's a fetch error (layout shows error state) -->
        <div v-if="injectedFetchError" />
        <div v-else class="border border-gray-200 rounded-xl p-6 bg-white">
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
                            <p class="text-xs text-gray-500 mt-0.5">Users and groups with access to this domain</p>
                        </div>
                        <button
                            v-if="canUpdateDataSource"
                            @click="openAdd"
                            class="px-2.5 py-1.5 text-xs bg-blue-500 text-white rounded-lg hover:bg-blue-600"
                        >
                            Add member
                        </button>
                    </div>

                    <div class="border border-gray-200 rounded-lg shadow-sm">
                        <table class="min-w-full divide-y divide-gray-200">
                            <thead class="bg-gray-50">
                                <tr>
                                    <th class="px-4 py-2 text-left text-xs font-medium text-gray-500 uppercase">User / Group</th>
                                    <th class="px-4 py-2 text-left text-xs font-medium text-gray-500 uppercase">Permissions</th>
                                    <th v-if="canUpdateDataSource" class="px-4 py-2 text-left text-xs font-medium text-gray-500 uppercase">Actions</th>
                                </tr>
                            </thead>
                            <tbody class="bg-white divide-y divide-gray-200">
                                <tr v-for="m in members" :key="m.grant_id" class="hover:bg-gray-50">
                                    <td class="px-4 py-3">
                                        <div class="flex items-center gap-1.5">
                                            <UIcon
                                                :name="m.principal_type === 'group' ? 'i-heroicons-user-group' : 'i-heroicons-user'"
                                                class="w-3.5 h-3.5 text-gray-400 flex-shrink-0"
                                            />
                                            <div class="min-w-0">
                                                <div class="flex items-center gap-1.5">
                                                    <span class="text-sm font-medium text-gray-900">{{ principalDisplayName(m) }}</span>
                                                    <template v-if="m.principal_type === 'group'">
                                                        <span class="text-xs text-gray-400">({{ groupMemberCount(m) }} {{ groupMemberCount(m) === 1 ? 'member' : 'members' }})</span>
                                                        <button
                                                            @click="toggleGroupExpand(m.principal_id)"
                                                            class="w-4 h-4 flex items-center justify-center rounded text-gray-400 hover:text-gray-600 hover:bg-gray-100 text-xs leading-none"
                                                        >
                                                            {{ expandedGroups.has(m.principal_id) ? '−' : '+' }}
                                                        </button>
                                                    </template>
                                                </div>
                                                <div class="text-xs text-gray-500" v-if="principalEmail(m)">{{ principalEmail(m) }}</div>
                                                <!-- Expanded group members -->
                                                <div v-if="m.principal_type === 'group' && expandedGroups.has(m.principal_id)" class="mt-1.5 pl-1 space-y-1">
                                                    <div
                                                        v-for="gm in (groupMembers[m.principal_id] || [])"
                                                        :key="gm.user_id"
                                                        class="flex items-center gap-1.5 text-xs text-gray-500"
                                                    >
                                                        <UIcon name="i-heroicons-user" class="w-3 h-3 text-gray-300 flex-shrink-0" />
                                                        <span>{{ gm.user_name || gm.user_email }}</span>
                                                        <span v-if="gm.user_name && gm.user_email" class="text-gray-400">{{ gm.user_email }}</span>
                                                    </div>
                                                    <div v-if="!groupMembers[m.principal_id]?.length" class="text-xs text-gray-400 italic">No members</div>
                                                </div>
                                            </div>
                                        </div>
                                    </td>
                                    <td class="px-4 py-3">
                                        <UDropdown
                                            v-if="canUpdateDataSource && isEnterprise"
                                            :items="[dsPermOptions.map(p => ({
                                                label: p,
                                                icon: m.permissions.includes(p) ? 'i-heroicons-check' : undefined,
                                                click: () => {
                                                    const newPerms = m.permissions.includes(p)
                                                        ? m.permissions.filter(x => x !== p)
                                                        : [...m.permissions, p]
                                                    updateMemberPermissions(m, newPerms)
                                                }
                                            }))]"
                                            :popper="{ placement: 'bottom-start' }"
                                        >
                                            <UButton size="xs" color="white" trailing-icon="i-heroicons-chevron-down-20-solid">
                                                {{ m.permissions.join(', ') || 'None' }}
                                            </UButton>
                                        </UDropdown>
                                        <div v-else class="flex gap-1 flex-wrap">
                                            <UBadge
                                                v-for="p in m.permissions"
                                                :key="p"
                                                size="xs"
                                                color="gray"
                                            >
                                                {{ p }}
                                            </UBadge>
                                            <span v-if="!m.permissions?.length" class="text-xs text-gray-400">Member</span>
                                        </div>
                                    </td>
                                    <td v-if="canUpdateDataSource" class="px-4 py-3">
                                        <button @click="removeMember(m)" class="text-xs border border-gray-300 text-gray-700 rounded-lg px-2 py-0.5 hover:bg-gray-50">Remove</button>
                                    </td>
                                </tr>
                                <tr v-if="members.length === 0">
                                    <td :colspan="canUpdateDataSource ? 3 : 2" class="px-4 py-6 text-xs text-gray-500 text-center">
                                        No members yet. All organization members have access by default.
                                    </td>
                                </tr>
                            </tbody>
                        </table>
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
                <div class="text-xs text-gray-600 mb-3">Select users or groups to grant access to this domain.</div>

                <!-- Principal type toggle (only shown with enterprise) -->
                <div v-if="addTabs.length > 1" class="flex gap-2 mb-3">
                    <button
                        v-for="tab in addTabs"
                        :key="tab.key"
                        @click="addPrincipalType = tab.key"
                        :class="[
                            'px-3 py-1 text-xs rounded-lg border transition-colors',
                            addPrincipalType === tab.key
                                ? 'bg-blue-50 border-blue-200 text-blue-700'
                                : 'border-gray-200 text-gray-600 hover:bg-gray-50'
                        ]"
                    >
                        {{ tab.label }}
                    </button>
                </div>

                <!-- Users selector -->
                <USelectMenu
                    v-if="addPrincipalType === 'user'"
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

                <!-- Groups selector -->
                <USelectMenu
                    v-if="addPrincipalType === 'group'"
                    v-model="selectedGroups"
                    :options="availableGroups"
                    multiple
                    searchable
                    searchable-placeholder="Search groups..."
                    option-attribute="name"
                    value-attribute="id"
                    class="w-full"
                />

                <!-- Permission picker (enterprise only) -->
                <div v-if="isEnterprise" class="mt-3">
                    <div class="text-xs font-medium text-gray-700 mb-1">Permissions</div>
                    <div class="flex flex-wrap gap-2">
                        <label
                            v-for="perm in dsPermOptions"
                            :key="perm"
                            class="flex items-center gap-1 text-xs cursor-pointer"
                        >
                            <UCheckbox
                                :model-value="addPermissions.includes(perm)"
                                @update:model-value="toggleAddPermission(perm, $event)"
                            />
                            {{ perm }}
                        </label>
                    </div>
                </div>

                <div class="flex justify-end space-x-2 mt-4">
                    <button @click="showAddModal = false" class="px-3 py-1.5 text-xs border border-gray-300 text-gray-700 rounded-lg">Cancel</button>
                    <button @click="addSelected" :disabled="addDisabled || adding" class="px-3 py-1.5 text-xs bg-blue-500 text-white rounded-lg hover:bg-blue-600 disabled:opacity-50">
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
import { useEnterprise } from '~/ee/composables/useEnterprise'
import Spinner from '@/components/Spinner.vue'
import type { Ref } from 'vue'

const route = useRoute()
const router = useRouter()
const toast = useToast?.()
const { organization } = useOrganization()

// Inject integration data from layout (avoid duplicate API calls)
const injectedIntegration = inject<Ref<any>>('integration', ref(null))
const injectedFetchIntegration = inject<() => Promise<void>>('fetchIntegration', async () => {})
const injectedLoading = inject<Ref<boolean>>('isLoading', ref(true))
const injectedFetchError = inject<Ref<number | null>>('fetchError', ref(null))

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
const { hasFeature } = useEnterprise()
const isEnterprise = computed(() => hasFeature('custom_roles'))

const dsPermOptions = ['query', 'view_schema', 'upload_files', 'manage', 'manage_members']

// ── Member types ────────────────────────────────────────────────────────

interface MemberGrant {
    grant_id: string
    principal_type: string  // "user" | "group"
    principal_id: string
    permissions: string[]
}

const members = ref<MemberGrant[]>([])

// User/group lookup data
const allUsers = ref<{ id: string; display_name: string; email?: string }[]>([])
const allGroups = ref<{ id: string; name: string; member_count: number }[]>([])
const expandedGroups = ref<Set<string>>(new Set())
const groupMembers = ref<Record<string, { user_id: string; user_name: string; user_email: string }[]>>({})

// Initialize form from injected data
watch(injectedIntegration, (ds) => {
    if (ds) {
        form.name = ds?.name || ''
        form.isPublic = ds?.is_public ?? true
        original.name = form.name
        original.isPublic = form.isPublic
    }
}, { immediate: true })

// ── Load data ───────────────────────────────────────────────────────────

async function loadMembers() {
    const id = route.params.id as string
    const { data, error } = await useMyFetch(`/data_sources/${id}/members`, { method: 'GET' })
    if (error?.value) return
    const list = (data.value as any[]) || []
    members.value = list.map((m: any) => ({
        grant_id: m.id,
        principal_type: m.principal_type || 'user',
        principal_id: m.principal_id,
        permissions: m.permissions || ['query', 'view_schema'],
    }))
}

async function loadUsers() {
    const { data, error } = await useMyFetch('/organization/members', { method: 'GET' })
    if (error?.value) return
    allUsers.value = ((data.value as any[]) || []).map(u => ({
        id: u.id,
        display_name: u.display_name || u.name || u.email || 'User',
        email: u.email,
    }))
}

async function loadGroups() {
    if (!organization.value?.id) return
    try {
        const { data } = await useMyFetch(`/organizations/${organization.value.id}/groups`)
        if (data.value) {
            allGroups.value = (data.value as any[]).map(g => ({ id: g.id, name: g.name, member_count: g.member_count || 0 }))
        }
    } catch {
        // Groups endpoint may not be available (non-enterprise)
    }
}

async function toggleGroupExpand(groupId: string) {
    if (expandedGroups.value.has(groupId)) {
        expandedGroups.value.delete(groupId)
        return
    }
    if (!groupMembers.value[groupId] && organization.value?.id) {
        try {
            const { data } = await useMyFetch(`/organizations/${organization.value.id}/groups/${groupId}/members`)
            if (data.value) {
                groupMembers.value[groupId] = data.value as any[]
            }
        } catch {
            // ignore
        }
    }
    expandedGroups.value.add(groupId)
}

function groupMemberCount(m: MemberGrant): number {
    const group = allGroups.value.find(g => g.id === m.principal_id)
    return group?.member_count || 0
}

// ── Display helpers ─────────────────────────────────────────────────────

function principalDisplayName(m: MemberGrant): string {
    if (m.principal_type === 'group') {
        const group = allGroups.value.find(g => g.id === m.principal_id)
        return group?.name || m.principal_name || 'Unknown group'
    }
    const user = allUsers.value.find(u => u.id === m.principal_id)
    return user?.display_name || user?.email || m.principal_name || 'Unknown user'
}

function principalEmail(m: MemberGrant): string {
    if (m.principal_type === 'group') return ''
    const user = allUsers.value.find(u => u.id === m.principal_id)
    return user?.email || ''
}

// ── Update permissions inline (enterprise) ──────────────────────────────

async function updateMemberPermissions(m: MemberGrant, newPerms: string[]) {
    const dsId = route.params.id as string
    // Use the legacy endpoint which now dual-writes
    // For permission updates, we need the resource-grants API
    if (!organization.value?.id) return
    try {
        const { error } = await useMyFetch(
            `/organizations/${organization.value.id}/resource-grants/${m.grant_id}`,
            { method: 'PUT', body: { permissions: newPerms } }
        )
        if (!error?.value) {
            m.permissions = newPerms
            toast?.add?.({ title: 'Permissions updated' })
        } else {
            toast?.add?.({ title: 'Failed to update permissions', color: 'red' })
        }
    } catch {
        toast?.add?.({ title: 'Failed to update permissions', color: 'red' })
    }
}

// ── Remove member ───────────────────────────────────────────────────────

async function removeMember(m: MemberGrant) {
    const dsId = route.params.id as string
    try {
        await useMyFetch(`/data_sources/${dsId}/members/${m.principal_id}`, { method: 'DELETE' })
        members.value = members.value.filter(x => x.grant_id !== m.grant_id)
        toast?.add?.({ title: 'Member removed' })
    } catch {
        toast?.add?.({ title: 'Failed to remove member', color: 'red' })
    }
}

// ── Add member modal ────────────────────────────────────────────────────

const showAddModal = ref(false)
const addPrincipalType = ref<'user' | 'group'>('user')
const selectedUsers = ref<string[]>([])
const selectedGroups = ref<string[]>([])
const addPermissions = ref<string[]>(['query', 'view_schema'])

const addTabs = computed(() => {
    const tabs: { key: 'user' | 'group'; label: string }[] = [
        { key: 'user', label: 'Users' },
    ]
    if (isEnterprise.value) {
        tabs.push({ key: 'group', label: 'Groups' })
    }
    return tabs
})

const availableUsers = computed(() => {
    const memberUserIds = new Set(
        members.value.filter(m => m.principal_type === 'user').map(m => m.principal_id)
    )
    return allUsers.value.filter(u => !memberUserIds.has(u.id))
})

const availableGroups = computed(() => {
    const memberGroupIds = new Set(
        members.value.filter(m => m.principal_type === 'group').map(m => m.principal_id)
    )
    return allGroups.value.filter(g => !memberGroupIds.has(g.id))
})

const addDisabled = computed(() => {
    if (addPrincipalType.value === 'user') return selectedUsers.value.length === 0
    return selectedGroups.value.length === 0
})

function toggleAddPermission(perm: string, checked: boolean) {
    if (checked) {
        if (!addPermissions.value.includes(perm)) addPermissions.value.push(perm)
    } else {
        addPermissions.value = addPermissions.value.filter(p => p !== perm)
    }
}

async function openAdd() {
    await Promise.all([loadUsers(), loadGroups()])
    selectedUsers.value = []
    selectedGroups.value = []
    addPermissions.value = ['query', 'view_schema']
    addPrincipalType.value = 'user'
    showAddModal.value = true
}

async function addSelected() {
    if (addDisabled.value || adding.value) return
    adding.value = true
    const dsId = route.params.id as string

    const principals = addPrincipalType.value === 'user'
        ? selectedUsers.value.map(id => ({ principal_type: 'user', principal_id: id }))
        : selectedGroups.value.map(id => ({ principal_type: 'group', principal_id: id }))

    try {
        await Promise.all(
            principals.map(p =>
                useMyFetch(`/data_sources/${dsId}/members`, {
                    method: 'POST',
                    body: { ...p },
                })
            )
        )
        toast?.add?.({ title: 'Members added' })
        selectedUsers.value = []
        selectedGroups.value = []
        showAddModal.value = false
        await loadMembers()
    } catch {
        toast?.add?.({ title: 'Failed to add members', color: 'red' })
    } finally {
        adding.value = false
    }
}

// ── Data source CRUD ────────────────────────────────────────────────────

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
    await Promise.all([loadMembers(), loadUsers(), loadGroups()])
})
</script>
