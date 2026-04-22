<template>
    <div class="mt-4">
        <!-- Header with search and actions -->
        <div class="flex flex-col md:flex-row md:items-center md:justify-between gap-4 mb-6">
            <div class="flex-1 max-w-md w-full">
                <div class="relative">
                    <input
                        v-model="searchQuery"
                        type="text"
                        placeholder="Search groups..."
                        class="w-full ps-10 pe-4 py-2 border border-gray-300 rounded-md focus:outline-none focus:ring-2 focus:ring-blue-500 focus:border-blue-500"
                    />
                    <UIcon
                        name="i-heroicons-magnifying-glass"
                        class="absolute start-3 top-2.5 h-4 w-4 text-gray-400"
                    />
                </div>
            </div>
            <div class="flex items-center justify-end gap-2 w-full md:w-auto">
                <UButton
                    v-if="useCan('manage_groups')"
                    color="blue"
                    variant="solid"
                    size="xs"
                    icon="i-heroicons-plus"
                    @click="openCreateModal"
                >
                    New Group
                </UButton>
            </div>
        </div>

        <!-- Table card -->
        <div class="bg-white shadow-sm border border-gray-200 rounded-lg">
            <div class="overflow-x-auto">
                <table class="min-w-full divide-y divide-gray-200">
                    <thead class="bg-gray-50">
                        <tr>
                            <th class="px-6 py-3 text-start text-xs font-medium text-gray-500 uppercase tracking-wider">Name</th>
                            <th class="px-6 py-3 text-start text-xs font-medium text-gray-500 uppercase tracking-wider">Description</th>
                            <th class="px-6 py-3 text-start text-xs font-medium text-gray-500 uppercase tracking-wider">Roles</th>
                            <th class="px-6 py-3 text-start text-xs font-medium text-gray-500 uppercase tracking-wider">Members</th>
                            <th class="px-6 py-3 text-start text-xs font-medium text-gray-500 uppercase tracking-wider">Source</th>
                            <th v-if="useCan('manage_groups')" class="px-6 py-3 text-start text-xs font-medium text-gray-500 uppercase tracking-wider">Actions</th>
                        </tr>
                    </thead>
                    <tbody class="bg-white divide-y divide-gray-200">
                        <!-- Loading state -->
                        <tr v-if="isLoading">
                            <td :colspan="useCan('manage_groups') ? 6 : 5" class="px-6 py-12 text-center">
                                <div class="flex items-center justify-center text-gray-500">
                                    <Spinner class="w-4 h-4 me-2" />
                                    <span class="text-sm">Loading...</span>
                                </div>
                            </td>
                        </tr>
                        <template v-else>
                            <tr v-for="group in filteredGroups" :key="group.id" class="hover:bg-gray-50">
                                <td class="px-6 py-4 whitespace-nowrap">
                                    <div class="flex items-center gap-2">
                                        <Icon name="heroicons:user-group" class="h-5 w-5 text-gray-400" />
                                        <span class="text-sm font-medium text-gray-900">{{ group.name }}</span>
                                        <UBadge v-if="group.external_provider" size="xs" color="gray">
                                            {{ group.external_provider }}
                                        </UBadge>
                                    </div>
                                </td>
                                <td class="px-6 py-4 text-sm text-gray-500">
                                    {{ group.description || '-' }}
                                </td>
                                <td class="px-6 py-4">
                                    <USelectMenu
                                        v-if="useCan('manage_role_assignments') && availableRoles.length"
                                        :model-value="getGroupRoleIds(group)"
                                        :options="availableRoles"
                                        multiple
                                        option-attribute="name"
                                        value-attribute="id"
                                        size="sm"
                                        :ui-menu="{ width: 'w-48' }"
                                        :popper="{ placement: 'bottom-start', strategy: 'fixed' }"
                                        @update:model-value="updateGroupRoles(group, $event)"
                                    >
                                        <template #label>
                                            <div class="flex gap-1 flex-wrap">
                                                <UBadge v-for="r in getGroupRoles(group)" :key="r.id" size="xs" color="gray">
                                                    {{ r.name }}
                                                </UBadge>
                                                <span v-if="getGroupRoles(group).length === 0" class="text-gray-400 text-sm italic">None</span>
                                            </div>
                                        </template>
                                    </USelectMenu>
                                    <div v-else class="flex gap-1 flex-wrap">
                                        <UBadge v-for="r in getGroupRoles(group)" :key="r.id" size="xs" color="gray">
                                            {{ r.name }}
                                        </UBadge>
                                        <span v-if="getGroupRoles(group).length === 0" class="text-gray-400 text-sm italic">None</span>
                                    </div>
                                </td>
                                <td class="px-6 py-4 whitespace-nowrap">
                                    <button
                                        @click="openMembersModal(group)"
                                        class="text-blue-600 hover:text-blue-800 text-sm font-medium"
                                    >
                                        {{ group.member_count }} {{ group.member_count === 1 ? 'member' : 'members' }}
                                    </button>
                                </td>
                                <td class="px-6 py-4 whitespace-nowrap text-sm text-gray-500">
                                    <UBadge v-if="group.external_provider" size="xs" color="blue" variant="subtle">
                                        {{ group.external_provider }}
                                    </UBadge>
                                    <span v-else class="text-gray-400 italic">Manual</span>
                                </td>
                                <td class="px-6 py-4 whitespace-nowrap">
                                    <div class="flex gap-2">
                                        <UButton
                                            v-if="useCan('manage_groups')"
                                            variant="ghost"
                                            size="xs"
                                            icon="i-heroicons-pencil"
                                            @click="openEditModal(group)"
                                        />
                                        <UButton
                                            v-if="useCan('manage_groups') && !group.external_provider"
                                            variant="ghost"
                                            size="xs"
                                            color="red"
                                            icon="i-heroicons-trash"
                                            @click="deleteGroup(group)"
                                        />
                                    </div>
                                </td>
                            </tr>
                            <!-- Empty state -->
                            <tr v-if="filteredGroups.length === 0">
                                <td colspan="6" class="px-6 py-12 text-center text-gray-500 text-sm">
                                    <div class="flex flex-col items-center">
                                        <Icon
                                            name="heroicons:user-group"
                                            class="mx-auto h-12 w-12 text-gray-400"
                                        />
                                        <h3 class="mt-2 text-sm font-medium text-gray-900">
                                            No groups found
                                        </h3>
                                        <p class="mt-1 text-sm text-gray-500">
                                            Create a group to organize members.
                                        </p>
                                    </div>
                                </td>
                            </tr>
                        </template>
                    </tbody>
                </table>
            </div>
        </div>

        <!-- Create/Edit Modal -->
        <UModal v-model="showFormModal">
            <div class="p-6 relative">
                <button @click="showFormModal = false" class="absolute top-4 end-4 text-gray-500 hover:text-gray-700 outline-none">
                    <Icon name="heroicons:x-mark" class="w-5 h-5" />
                </button>
                <h3 class="text-lg font-semibold">
                    {{ editingGroup ? 'Edit Group' : 'Create Group' }}
                </h3>
                <p class="text-sm text-gray-500">{{ editingGroup ? 'Update group details' : 'Create a new group to organize members' }}</p>
                <hr class="my-4" />

                <form @submit.prevent="saveGroup" class="space-y-4">
                    <div class="flex flex-col">
                        <label class="text-sm font-medium text-gray-700 mb-2">Name</label>
                        <UInput v-model="form.name" placeholder="e.g. Engineering" required />
                    </div>

                    <div class="flex flex-col">
                        <label class="text-sm font-medium text-gray-700 mb-2">Description</label>
                        <UInput v-model="form.description" placeholder="Optional description" />
                    </div>

                    <div class="flex justify-end space-x-2 pt-4">
                        <UButton variant="ghost" type="button" @click="showFormModal = false">
                            Cancel
                        </UButton>
                        <UButton type="submit" color="blue" :loading="saving">
                            {{ editingGroup ? 'Save' : 'Create' }}
                        </UButton>
                    </div>
                </form>
            </div>
        </UModal>

        <!-- Members Modal -->
        <UModal v-model="showMembersModal" :ui="{ width: 'sm:max-w-lg' }">
            <div class="p-6 relative">
                <button @click="showMembersModal = false" class="absolute top-4 end-4 text-gray-500 hover:text-gray-700 outline-none">
                    <Icon name="heroicons:x-mark" class="w-5 h-5" />
                </button>
                <h3 class="text-lg font-semibold">{{ selectedGroup?.name }} — Members</h3>
                <p class="text-sm text-gray-500 mb-4">Manage who belongs to this group</p>

                <!-- Add member -->
                <div v-if="useCan('manage_groups')" class="flex gap-2 mb-4">
                    <USelectMenu
                        v-model="memberToAdd"
                        :options="addableMemberOptions"
                        option-attribute="label"
                        value-attribute="value"
                        searchable
                        placeholder="Add a member..."
                        class="flex-1"
                        size="sm"
                    />
                    <UButton
                        color="blue"
                        size="sm"
                        :disabled="!memberToAdd"
                        @click="addMember"
                    >
                        Add
                    </UButton>
                </div>

                <!-- Member list -->
                <div class="border rounded-lg divide-y divide-gray-200 max-h-80 overflow-y-auto">
                    <div v-if="groupMembersLoading" class="px-4 py-8 text-center text-gray-500 text-sm">
                        <Spinner class="w-4 h-4 me-2 inline" />
                        Loading...
                    </div>
                    <div
                        v-else-if="groupMembers.length === 0"
                        class="px-4 py-8 text-center text-gray-500 text-sm"
                    >
                        No members in this group yet.
                    </div>
                    <div
                        v-for="member in groupMembers"
                        :key="member.user_id"
                        class="flex items-center justify-between px-4 py-3"
                    >
                        <div class="flex items-center gap-3">
                            <div class="h-8 w-8 rounded-full bg-gray-200 flex items-center justify-center">
                                <span class="text-gray-500 text-sm font-medium">
                                    {{ (member.user_name || member.user_email || '?')[0].toUpperCase() }}
                                </span>
                            </div>
                            <div>
                                <div class="text-sm font-medium text-gray-900">{{ member.user_name || 'Unknown' }}</div>
                                <div class="text-xs text-gray-500">{{ member.user_email }}</div>
                            </div>
                        </div>
                        <UButton
                            v-if="useCan('manage_groups')"
                            variant="ghost"
                            size="xs"
                            color="red"
                            icon="i-heroicons-x-mark"
                            @click="removeMember(member.user_id)"
                        />
                    </div>
                </div>
            </div>
        </UModal>
    </div>
</template>

<script setup lang="ts">
import Spinner from '@/components/Spinner.vue'
import { useCan } from '~/composables/usePermissions'

interface GroupData {
    id: string
    name: string
    description?: string
    external_id?: string
    external_provider?: string
    member_count: number
}

interface GroupMember {
    user_id: string
    user_name?: string
    user_email?: string
}

interface OrgMember {
    id: string
    user_id?: string
    user?: { id: string; name?: string; email: string }
    email?: string
}

const props = defineProps<{
    organization: { id: string; name?: string }
}>()

const organizationId = props.organization.id
const toast = useToast()

interface RoleInfo {
    id: string
    name: string
}

interface RoleAssignment {
    id: string
    role_id: string
    principal_type: string
    principal_id: string
    role?: RoleInfo
}

// State
const groups = ref<GroupData[]>([])
const isLoading = ref(true)
const searchQuery = ref('')
const showFormModal = ref(false)
const editingGroup = ref<GroupData | null>(null)
const saving = ref(false)
const form = reactive({ name: '', description: '' })
const availableRoles = ref<RoleInfo[]>([])
const groupRoleAssignments = ref<Record<string, RoleAssignment[]>>({})

// Members modal state
const showMembersModal = ref(false)
const selectedGroup = ref<GroupData | null>(null)
const groupMembers = ref<GroupMember[]>([])
const groupMembersLoading = ref(false)
const memberToAdd = ref<string | null>(null)
const orgMembers = ref<OrgMember[]>([])

const filteredGroups = computed(() => {
    const query = searchQuery.value.toLowerCase()
    if (!query) return groups.value
    return groups.value.filter(g =>
        g.name.toLowerCase().includes(query) ||
        (g.description || '').toLowerCase().includes(query)
    )
})

const addableMemberOptions = computed(() => {
    const existingIds = new Set(groupMembers.value.map(m => m.user_id))
    return orgMembers.value
        .filter(m => {
            const userId = m.user_id || m.user?.id
            return userId && !existingIds.has(userId)
        })
        .map(m => ({
            value: m.user_id || m.user?.id || '',
            label: m.user?.name || m.user?.email || m.email || 'Unknown',
        }))
})

function getGroupRoleIds(group: GroupData): string[] {
    return (groupRoleAssignments.value[group.id] || []).map(a => a.role_id)
}

function getGroupRoles(group: GroupData): RoleInfo[] {
    return (groupRoleAssignments.value[group.id] || [])
        .map(a => a.role || availableRoles.value.find(r => r.id === a.role_id))
        .filter(Boolean) as RoleInfo[]
}

async function loadAvailableRoles() {
    try {
        const { data } = await useMyFetch(`/organizations/${organizationId}/roles`)
        if (data.value) {
            availableRoles.value = (data.value as any[]).map(r => ({ id: r.id, name: r.name }))
        }
    } catch (e) {
        // Roles endpoint may not be available
    }
}

async function loadGroupRoleAssignments() {
    try {
        const { data } = await useMyFetch(
            `/organizations/${organizationId}/role-assignments?principal_type=group`
        )
        if (data.value) {
            const map: Record<string, RoleAssignment[]> = {}
            for (const assignment of data.value as RoleAssignment[]) {
                if (!map[assignment.principal_id]) map[assignment.principal_id] = []
                map[assignment.principal_id].push(assignment)
            }
            groupRoleAssignments.value = map
        }
    } catch (e) {
        // Non-fatal
    }
}

async function updateGroupRoles(group: GroupData, selectedRoleIds: string[]) {
    try {
        const currentAssignments = groupRoleAssignments.value[group.id] || []
        const currentRoleIds = currentAssignments.map(a => a.role_id)

        const added = selectedRoleIds.filter(id => !currentRoleIds.includes(id))
        const removed = currentAssignments.filter(a => !selectedRoleIds.includes(a.role_id))

        for (const roleId of added) {
            await useMyFetch(`/organizations/${organizationId}/role-assignments`, {
                method: 'POST',
                body: { role_id: roleId, principal_type: 'group', principal_id: group.id },
            })
        }

        for (const assignment of removed) {
            await useMyFetch(`/organizations/${organizationId}/role-assignments/${assignment.id}`, {
                method: 'DELETE',
            })
        }

        await loadGroupRoleAssignments()
        toast.add({ title: 'Group roles updated', color: 'green' })
    } catch (e: any) {
        const detail = e?.data?.detail || e?.message || 'Failed to update group roles'
        toast.add({ title: detail, color: 'red' })
    }
}

async function loadGroups() {
    isLoading.value = true
    try {
        const { data } = await useMyFetch(`/organizations/${organizationId}/groups`)
        if (data.value) {
            groups.value = data.value as GroupData[]
        }
    } catch (e) {
        // Groups endpoint may not be available
    } finally {
        isLoading.value = false
    }
}

async function loadOrgMembers() {
    try {
        const { data } = await useMyFetch(`/organizations/${organizationId}/members`)
        if (data.value) {
            orgMembers.value = data.value as OrgMember[]
        }
    } catch (e) {
        // Non-fatal
    }
}

function openCreateModal() {
    editingGroup.value = null
    form.name = ''
    form.description = ''
    showFormModal.value = true
}

function openEditModal(group: GroupData) {
    editingGroup.value = group
    form.name = group.name
    form.description = group.description || ''
    showFormModal.value = true
}

async function saveGroup() {
    saving.value = true
    try {
        const body = {
            name: form.name,
            description: form.description || null,
        }

        if (editingGroup.value) {
            const { error } = await useMyFetch(`/organizations/${organizationId}/groups/${editingGroup.value.id}`, {
                method: 'PUT',
                body,
            })
            if (error.value) {
                toast.add({ title: error.value.data?.detail || 'Failed to update group', color: 'red' })
                return
            }
            toast.add({ title: 'Group updated', color: 'green' })
        } else {
            const { error } = await useMyFetch(`/organizations/${organizationId}/groups`, {
                method: 'POST',
                body,
            })
            if (error.value) {
                toast.add({ title: error.value.data?.detail || 'Failed to create group', color: 'red' })
                return
            }
            toast.add({ title: 'Group created', color: 'green' })
        }

        showFormModal.value = false
        await loadGroups()
    } catch (e: any) {
        const detail = e?.data?.detail || e?.message || 'Failed to save group'
        toast.add({ title: detail, color: 'red' })
    } finally {
        saving.value = false
    }
}

async function deleteGroup(group: GroupData) {
    if (!confirm(`Delete group "${group.name}"?`)) return
    try {
        const { error } = await useMyFetch(`/organizations/${organizationId}/groups/${group.id}`, {
            method: 'DELETE',
        })
        if (error.value) {
            toast.add({ title: error.value.data?.detail || 'Failed to delete group', color: 'red' })
            return
        }
        toast.add({ title: 'Group deleted', color: 'green' })
        await loadGroups()
    } catch (e: any) {
        const detail = e?.data?.detail || e?.message || 'Failed to delete group'
        toast.add({ title: detail, color: 'red' })
    }
}

async function openMembersModal(group: GroupData) {
    selectedGroup.value = group
    showMembersModal.value = true
    groupMembersLoading.value = true
    memberToAdd.value = null

    try {
        const [membersResult] = await Promise.all([
            useMyFetch(`/organizations/${organizationId}/groups/${group.id}/members`),
            orgMembers.value.length === 0 ? loadOrgMembers() : Promise.resolve(),
        ])
        if (membersResult.data.value) {
            groupMembers.value = membersResult.data.value as GroupMember[]
        }
    } catch (e) {
        groupMembers.value = []
    } finally {
        groupMembersLoading.value = false
    }
}

async function addMember() {
    if (!memberToAdd.value || !selectedGroup.value) return
    try {
        await useMyFetch(`/organizations/${organizationId}/groups/${selectedGroup.value.id}/members`, {
            method: 'POST',
            body: { user_id: memberToAdd.value },
        })
        toast.add({ title: 'Member added to group', color: 'green' })
        memberToAdd.value = null
        // Reload group members and group list (for count update)
        await Promise.all([
            openMembersModal(selectedGroup.value),
            loadGroups(),
        ])
    } catch (e: any) {
        const detail = e?.data?.detail || e?.message || 'Failed to add member'
        toast.add({ title: detail, color: 'red' })
    }
}

async function removeMember(userId: string) {
    if (!selectedGroup.value) return
    try {
        await useMyFetch(`/organizations/${organizationId}/groups/${selectedGroup.value.id}/members/${userId}`, {
            method: 'DELETE',
        })
        toast.add({ title: 'Member removed from group', color: 'green' })
        await Promise.all([
            openMembersModal(selectedGroup.value),
            loadGroups(),
        ])
    } catch (e: any) {
        const detail = e?.data?.detail || e?.message || 'Failed to remove member'
        toast.add({ title: detail, color: 'red' })
    }
}

onMounted(async () => {
    await Promise.all([loadGroups(), loadAvailableRoles(), loadGroupRoleAssignments()])
})
</script>
