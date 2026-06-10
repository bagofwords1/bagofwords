<template>
    <div class="mt-4">
        <!-- Header with search and actions -->
        <div class="flex flex-col md:flex-row md:items-center md:justify-between gap-4 mb-6">
            <div class="flex-1 max-w-md w-full">
                <div class="relative">
                    <input
                        v-model="searchQuery"
                        type="text"
                        :placeholder="$t('groupsManager.searchPlaceholder')"
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
                    {{ $t('groupsManager.newGroup') }}
                </UButton>
            </div>
        </div>

        <!-- Table card -->
        <div class="bg-white border border-gray-200 rounded-lg overflow-hidden">
            <div class="overflow-x-auto">
                <table class="min-w-full divide-y divide-gray-100">
                    <thead class="bg-gray-50/60">
                        <tr>
                            <th class="px-4 py-2 text-start text-xs font-medium text-gray-500">{{ $t('groupsManager.colName') }}</th>
                            <th class="px-4 py-2 text-start text-xs font-medium text-gray-500">{{ $t('groupsManager.colDescription') }}</th>
                            <th class="px-4 py-2 text-start text-xs font-medium text-gray-500">{{ $t('groupsManager.colRoles') }}</th>
                            <th class="px-4 py-2 text-start text-xs font-medium text-gray-500">{{ $t('groupsManager.colMembers') }}</th>
                            <th v-if="showQuotaColumn" class="px-4 py-2 text-start text-xs font-medium text-gray-500">{{ $t('quotaPolicies.colQuota') }}</th>
                            <th class="px-4 py-2 text-start text-xs font-medium text-gray-500">{{ $t('groupsManager.colSource') }}</th>
                            <th v-if="useCan('manage_groups')" class="px-4 py-2 text-start text-xs font-medium text-gray-500">{{ $t('groupsManager.colActions') }}</th>
                        </tr>
                    </thead>
                    <tbody class="bg-white divide-y divide-gray-100">
                        <!-- Loading state -->
                        <tr v-if="isLoading">
                            <td :colspan="groupsColspan" class="px-6 py-12 text-center">
                                <div class="flex items-center justify-center text-gray-500">
                                    <Spinner class="w-4 h-4 me-2" />
                                    <span class="text-sm">{{ $t('groupsManager.loading') }}</span>
                                </div>
                            </td>
                        </tr>
                        <template v-else>
                            <tr v-for="group in filteredGroups" :key="group.id" class="hover:bg-gray-50/70 transition-colors">
                                <td class="px-4 py-2 whitespace-nowrap">
                                    <div class="flex items-center gap-2">
                                        <Icon name="heroicons:user-group" class="h-5 w-5 text-gray-400" />
                                        <span class="text-sm font-medium text-gray-900">{{ group.name }}</span>
                                        <UBadge v-if="group.external_provider" size="xs" color="gray">
                                            {{ group.external_provider }}
                                        </UBadge>
                                    </div>
                                </td>
                                <td class="px-4 py-2 text-xs text-gray-400">
                                    {{ group.description || "-" }}
                                </td>
                                <td class="px-4 py-2">
                                    <USelectMenu
                                        v-if="useCan('manage_role_assignments') && availableRoles.length"
                                        :model-value="getGroupRoleIds(group)"
                                        :options="availableRoles"
                                        multiple
                                        option-attribute="label"
                                        value-attribute="id"
                                        size="sm"
                                        variant="none"
                                        :ui="inlineSelectUi"
                                        :ui-menu="{ width: 'w-48' }"
                                        :popper="{ placement: 'bottom-start', strategy: 'fixed' }"
                                        @update:model-value="updateGroupRoles(group, $event)"
                                    >
                                        <template #label>
                                            <div class="flex gap-1 items-center">
                                                <UBadge v-for="r in getGroupRoles(group)" :key="r.id" size="xs" color="gray">
                                                    {{ cap(r.name) }}
                                                </UBadge>
                                                <span v-if="getGroupRoles(group).length === 0" class="text-gray-400 text-sm italic">{{ $t('groupsManager.none') }}</span>
                                            </div>
                                        </template>
                                    </USelectMenu>
                                    <div v-else class="flex gap-1 items-center">
                                        <UBadge v-for="r in getGroupRoles(group)" :key="r.id" size="xs" color="gray">
                                            {{ cap(r.name) }}
                                        </UBadge>
                                        <span v-if="getGroupRoles(group).length === 0" class="text-gray-400 text-sm italic">{{ $t('groupsManager.none') }}</span>
                                    </div>
                                </td>
                                <td class="px-4 py-2 whitespace-nowrap">
                                    <button
                                        @click="openMembersModal(group)"
                                        class="text-blue-600 hover:text-blue-800 text-sm font-medium"
                                    >
                                        {{ group.member_count === 1 ? $t('groupsManager.memberSingular', { n: group.member_count }) : $t('groupsManager.memberPlural', { n: group.member_count }) }}
                                    </button>
                                </td>
                                <td v-if="showQuotaColumn" class="px-4 py-2">
                                    <USelectMenu
                                        :model-value="getDirectQuotaId('group', group.id)"
                                        :options="quotaSelectOptions"
                                        value-attribute="value"
                                        option-attribute="label"
                                        size="sm"
                                        variant="none"
                                        :ui="inlineSelectUi"
                                        :ui-menu="{ width: 'w-48' }"
                                        :popper="{ placement: 'bottom-start', strategy: 'fixed' }"
                                        @update:model-value="updatePrincipalQuota('group', group.id, $event)"
                                    >
                                        <template #label>
                                            <span class="flex gap-1 items-center">
                                                <UBadge
                                                    v-for="policy in getGroupQuotaPolicies(group).slice(0, 1)"
                                                    :key="policy.id"
                                                    size="xs"
                                                    class="whitespace-nowrap"
                                                    color="blue"
                                                    variant="subtle"
                                                >
                                                    {{ policy.name }}
                                                </UBadge>
                                                <span v-if="getGroupQuotaPolicies(group).length === 0" class="text-gray-400 text-sm italic">{{ $t('quotaPolicies.unlimited') }}</span>
                                            </span>
                                        </template>
                                        <template #option="{ option }">
                                            <span class="text-sm">{{ option.label }}</span>
                                        </template>
                                    </USelectMenu>
                                </td>
                                <td class="px-4 py-2 whitespace-nowrap text-sm text-gray-500">
                                    <UBadge v-if="group.external_provider" size="xs" color="blue" variant="subtle">
                                        {{ group.external_provider }}
                                    </UBadge>
                                    <span v-else class="text-gray-400 italic">{{ $t('groupsManager.manual') }}</span>
                                </td>
                                <td class="px-4 py-2 whitespace-nowrap">
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
                                <td :colspan="groupsColspan" class="px-6 py-12 text-center text-gray-500 text-sm">
                                    <div class="flex flex-col items-center">
                                        <Icon
                                            name="heroicons:user-group"
                                            class="mx-auto h-12 w-12 text-gray-400"
                                        />
                                        <h3 class="mt-2 text-sm font-medium text-gray-900">
                                            {{ $t('groupsManager.noGroupsFound') }}
                                        </h3>
                                        <p class="mt-1 text-sm text-gray-500">
                                            {{ $t('groupsManager.noGroupsHint') }}
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
                    {{ editingGroup ? $t('groupsManager.editGroup') : $t('groupsManager.createGroup') }}
                </h3>
                <p class="text-sm text-gray-500">{{ editingGroup ? $t('groupsManager.updateGroupDetails') : $t('groupsManager.createNewGroupHint') }}</p>
                <hr class="my-4" />

                <form @submit.prevent="saveGroup" class="space-y-4">
                    <div class="flex flex-col">
                        <label class="text-sm font-medium text-gray-700 mb-2">{{ $t('groupsManager.nameLabel') }}</label>
                        <UInput v-model="form.name" :placeholder="$t('groupsManager.namePlaceholder')" required />
                    </div>

                    <div class="flex flex-col">
                        <label class="text-sm font-medium text-gray-700 mb-2">{{ $t('groupsManager.descriptionLabel') }}</label>
                        <UInput v-model="form.description" :placeholder="$t('groupsManager.descriptionPlaceholder')" />
                    </div>

                    <div class="flex justify-end space-x-2 pt-4">
                        <UButton variant="ghost" type="button" @click="showFormModal = false">
                            {{ $t('groupsManager.cancel') }}
                        </UButton>
                        <UButton type="submit" color="blue" :loading="saving">
                            {{ editingGroup ? $t('groupsManager.save') : $t('groupsManager.create') }}
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
                <h3 class="text-lg font-semibold">{{ selectedGroup?.name }} {{ $t('groupsManager.membersSuffix') }}</h3>
                <p class="text-sm text-gray-500 mb-4">{{ $t('groupsManager.manageMembersHint') }}</p>

                <!-- Add member -->
                <div v-if="useCan('manage_groups')" class="flex gap-2 mb-4">
                    <USelectMenu
                        v-model="memberToAdd"
                        :options="addableMemberOptions"
                        option-attribute="label"
                        value-attribute="value"
                        searchable
                        :placeholder="$t('groupsManager.addMemberPlaceholder')"
                        class="flex-1"
                        size="sm"
                    />
                    <UButton
                        color="blue"
                        size="sm"
                        :disabled="!memberToAdd"
                        @click="addMember"
                    >
                        {{ $t('groupsManager.add') }}
                    </UButton>
                </div>

                <!-- Member list -->
                <div class="border rounded-lg divide-y divide-gray-200 max-h-80 overflow-y-auto">
                    <div v-if="groupMembersLoading" class="px-4 py-8 text-center text-gray-500 text-sm">
                        <Spinner class="w-4 h-4 me-2 inline" />
                        {{ $t('groupsManager.loading') }}
                    </div>
                    <div
                        v-else-if="groupMembers.length === 0"
                        class="px-4 py-8 text-center text-gray-500 text-sm"
                    >
                        {{ $t('groupsManager.noMembers') }}
                    </div>
                    <div
                        v-for="member in groupMembers"
                        :key="member.user_id || member.membership_id"
                        class="flex items-center justify-between px-4 py-3"
                    >
                        <div class="flex items-center gap-3">
                            <div class="h-8 w-8 rounded-full bg-gray-200 flex items-center justify-center">
                                <span class="text-gray-500 text-sm font-medium">
                                    {{ (member.user_name || member.user_email || '?')[0].toUpperCase() }}
                                </span>
                            </div>
                            <div>
                                <div class="text-sm font-medium text-gray-900 flex items-center gap-2">
                                    {{ member.user_name || member.user_email || $t('groupsManager.unknown') }}
                                    <UBadge v-if="member.pending" size="xs" color="yellow" variant="subtle">{{ $t('settings.members.statusPending') }}</UBadge>
                                </div>
                                <div class="text-xs text-gray-500">{{ member.user_email }}</div>
                            </div>
                        </div>
                        <UButton
                            v-if="useCan('manage_groups')"
                            variant="ghost"
                            size="xs"
                            color="red"
                            icon="i-heroicons-x-mark"
                            @click="removeMember(member.user_id || member.membership_id || '')"
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
import { useI18n } from 'vue-i18n'
import { useEnterprise } from '~/ee/composables/useEnterprise'

const { t } = useI18n()

interface GroupData {
    id: string
    name: string
    description?: string
    external_id?: string
    external_provider?: string
    member_count: number
}

interface GroupMember {
    user_id?: string
    membership_id?: string
    user_name?: string
    user_email?: string
    pending?: boolean
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
    label?: string
}

// Capitalize role names for display so they read consistently (Admin/Member).
function cap(name?: string): string {
    if (!name) return ''
    return name.charAt(0).toUpperCase() + name.slice(1)
}

// In-table selects render as plain badges, not form fields: borderless, a
// subtle hover background, content width, and a muted chevron.
const inlineSelectUi = {
    base: 'group relative inline-flex w-fit items-center gap-1 text-left cursor-pointer rounded-md transition-colors hover:bg-gray-100 focus:outline-none',
    padding: { sm: 'ps-1.5 pe-5 py-1' },
    trailing: { padding: { sm: 'pe-1' } },
    icon: { base: 'text-gray-300 group-hover:text-gray-500 transition-colors', size: { sm: 'h-3.5 w-3.5' } },
}

interface RoleAssignment {
    id: string
    role_id: string
    principal_type: string
    principal_id: string
    role?: RoleInfo
}

interface UsagePolicySummary {
    id: string
    name: string
    enabled: boolean
    assignments: UsagePolicyAssignment[]
}

interface UsagePolicyAssignment {
    principal_type: 'user' | 'group' | 'role'
    principal_id: string
}

// State
const groups = ref<GroupData[]>([])
const usagePolicies = ref<UsagePolicySummary[]>([])
const isLoading = ref(true)
const searchQuery = ref('')
const showFormModal = ref(false)
const editingGroup = ref<GroupData | null>(null)
const saving = ref(false)
const form = reactive({ name: '', description: '' })
const availableRoles = ref<RoleInfo[]>([])
const groupRoleAssignments = ref<Record<string, RoleAssignment[]>>({})
const { hasFeature } = useEnterprise()
const showQuotaColumn = computed(() => hasFeature('usage_limits') && useCan('manage_settings'))
const groupsColspan = computed(() => 5 + (showQuotaColumn.value ? 1 : 0) + (useCan('manage_groups') ? 1 : 0))

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
    // Already-in-group principals, by composite "user:<id>" / "membership:<id>".
    const existing = new Set(
        groupMembers.value.map(m => m.user_id ? `user:${m.user_id}` : `membership:${m.membership_id}`)
    )
    return orgMembers.value
        .map(m => {
            const userId = m.user_id || m.user?.id
            // Registered members → user principal; pending invites → membership principal.
            const value = userId ? `user:${userId}` : `membership:${m.id}`
            return {
                value,
                label: m.user?.name || m.user?.email || m.email || t('groupsManager.unknown'),
                pending: !userId,
            }
        })
        .filter(opt => !existing.has(opt.value))
})

const quotaSelectOptions = computed(() => [
    { value: null, label: t('quotaPolicies.noDirectQuota') },
    ...usagePolicies.value
        .filter(policy => policy.enabled)
        .map(policy => ({ value: policy.id, label: policy.name })),
])

function getGroupRoleIds(group: GroupData): string[] {
    return (groupRoleAssignments.value[group.id] || []).map(a => a.role_id)
}

function getGroupRoles(group: GroupData): RoleInfo[] {
    return (groupRoleAssignments.value[group.id] || [])
        .map(a => a.role || availableRoles.value.find(r => r.id === a.role_id))
        .filter(Boolean) as RoleInfo[]
}

function getGroupQuotaPolicies(group: GroupData): UsagePolicySummary[] {
    if (!showQuotaColumn.value) return []
    return getPrincipalQuotaPolicies('group', group.id)
}

function getPrincipalQuotaPolicies(principalType: UsagePolicyAssignment['principal_type'], principalId: string): UsagePolicySummary[] {
    return usagePolicies.value.filter(policy =>
        policy.enabled &&
        policy.assignments?.some(assignment =>
            assignment.principal_type === principalType &&
            assignment.principal_id === principalId
        )
    )
}

function getDirectQuotaId(principalType: UsagePolicyAssignment['principal_type'], principalId: string): string | null {
    return getPrincipalQuotaPolicies(principalType, principalId)[0]?.id || null
}

function applyLocalQuotaAssignment(principalType: UsagePolicyAssignment['principal_type'], principalId: string, policyId: string | null) {
    usagePolicies.value = usagePolicies.value.map(policy => {
        const assignments = (policy.assignments || []).filter(
            assignment => assignment.principal_type !== principalType || assignment.principal_id !== principalId
        )
        if (policyId && policy.id === policyId) {
            assignments.push({ principal_type: principalType, principal_id: principalId })
        }
        return { ...policy, assignments }
    })
}

async function updatePrincipalQuota(principalType: UsagePolicyAssignment['principal_type'], principalId: string, policyId: string | null) {
    try {
        const { error } = await useMyFetch(`/organizations/${organizationId}/usage-policy-assignments/principal`, {
            method: 'PUT',
            body: {
                principal_type: principalType,
                principal_id: principalId,
                policy_id: policyId,
            },
        })
        if (error.value) {
            toast.add({ title: error.value.data?.detail || t('quotaPolicies.failedToSave'), color: 'red' })
            return
        }
        applyLocalQuotaAssignment(principalType, principalId, policyId)
        toast.add({ title: t('quotaPolicies.toastAssignmentUpdated'), color: 'green' })
    } catch (e: any) {
        const detail = e?.data?.detail || e?.message || t('quotaPolicies.failedToSave')
        toast.add({ title: detail, color: 'red' })
    }
}

async function loadAvailableRoles() {
    try {
        const { data } = await useMyFetch(`/organizations/${organizationId}/roles`)
        if (data.value) {
            availableRoles.value = (data.value as any[]).map(r => ({ id: r.id, name: r.name, label: cap(r.name) }))
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

async function loadUsagePolicies() {
    if (!showQuotaColumn.value) return
    try {
        const { data } = await useMyFetch(`/organizations/${organizationId}/usage-policies`)
        usagePolicies.value = (data.value || []) as UsagePolicySummary[]
    } catch (e) {
        usagePolicies.value = []
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
        toast.add({ title: t('groupsManager.toastRolesUpdated'), color: 'green' })
    } catch (e: any) {
        const detail = e?.data?.detail || e?.message || t('groupsManager.failedToUpdateRoles')
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
                toast.add({ title: error.value.data?.detail || t('groupsManager.failedToUpdate'), color: 'red' })
                return
            }
            toast.add({ title: t('groupsManager.toastUpdated'), color: 'green' })
        } else {
            const { error } = await useMyFetch(`/organizations/${organizationId}/groups`, {
                method: 'POST',
                body,
            })
            if (error.value) {
                toast.add({ title: error.value.data?.detail || t('groupsManager.failedToCreate'), color: 'red' })
                return
            }
            toast.add({ title: t('groupsManager.toastCreated'), color: 'green' })
        }

        showFormModal.value = false
        await loadGroups()
    } catch (e: any) {
        const detail = e?.data?.detail || e?.message || t('groupsManager.failedToSave')
        toast.add({ title: detail, color: 'red' })
    } finally {
        saving.value = false
    }
}

async function deleteGroup(group: GroupData) {
    if (!confirm(t('groupsManager.confirmDelete', { name: group.name }))) return
    try {
        const { error } = await useMyFetch(`/organizations/${organizationId}/groups/${group.id}`, {
            method: 'DELETE',
        })
        if (error.value) {
            toast.add({ title: error.value.data?.detail || t('groupsManager.failedToDelete'), color: 'red' })
            return
        }
        toast.add({ title: t('groupsManager.toastDeleted'), color: 'green' })
        await loadGroups()
    } catch (e: any) {
        const detail = e?.data?.detail || e?.message || t('groupsManager.failedToDelete')
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
        // memberToAdd is a composite "user:<id>" / "membership:<id>" value.
        const [kind, id] = memberToAdd.value.split(':')
        const body = kind === 'membership' ? { membership_id: id } : { user_id: id }
        await useMyFetch(`/organizations/${organizationId}/groups/${selectedGroup.value.id}/members`, {
            method: 'POST',
            body,
        })
        toast.add({ title: t('groupsManager.toastMemberAdded'), color: 'green' })
        memberToAdd.value = null
        // Reload group members and group list (for count update)
        await Promise.all([
            openMembersModal(selectedGroup.value),
            loadGroups(),
        ])
    } catch (e: any) {
        const detail = e?.data?.detail || e?.message || t('groupsManager.failedToAddMember')
        toast.add({ title: detail, color: 'red' })
    }
}

async function removeMember(userId: string) {
    if (!selectedGroup.value) return
    try {
        await useMyFetch(`/organizations/${organizationId}/groups/${selectedGroup.value.id}/members/${userId}`, {
            method: 'DELETE',
        })
        toast.add({ title: t('groupsManager.toastMemberRemoved'), color: 'green' })
        await Promise.all([
            openMembersModal(selectedGroup.value),
            loadGroups(),
        ])
    } catch (e: any) {
        const detail = e?.data?.detail || e?.message || t('groupsManager.failedToRemoveMember')
        toast.add({ title: detail, color: 'red' })
    }
}

onMounted(async () => {
    await Promise.all([loadGroups(), loadAvailableRoles(), loadGroupRoleAssignments(), loadUsagePolicies()])
})

watch(showQuotaColumn, (enabled) => {
    if (enabled && usagePolicies.value.length === 0) {
        loadUsagePolicies()
    }
})
</script>
