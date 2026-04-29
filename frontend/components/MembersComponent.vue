<template>
    <div class="mt-4">
        <!-- Header with search and actions -->
        <div class="flex flex-col md:flex-row md:items-center md:justify-between gap-4 mb-6">
            <div class="flex-1 max-w-md w-full">
                <div class="relative">
                    <input
                        v-model="searchQuery"
                        type="text"
                        :placeholder="$t('settings.members.searchPlaceholder')"
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
                    v-if="useCan('add_organization_members')"
                    color="blue"
                    variant="solid"
                    size="xs"
                    icon="i-heroicons-plus"
                    @click="inviteModalOpen = true"
                >
                    {{ $t('settings.members.addMember') }}
                </UButton>
            </div>
        </div>

        <!-- Filters row -->
        <div class="flex flex-wrap items-center gap-3 mb-5 text-xs">
            <USelectMenu
                :model-value="statusFilter"
                @update:model-value="statusFilter = $event"
                :options="statusFilterOptions"
                value-attribute="value"
                option-attribute="label"
                size="sm"
                class="w-36"
            >
                <template #label>
                    <span class="text-sm">{{ selectedStatusLabel }}</span>
                </template>
                <template #option="{ option }">
                    <span class="text-sm">{{ option.label }}</span>
                </template>
            </USelectMenu>
            <USelectMenu
                v-if="groups.length > 0"
                :model-value="groupFilter"
                @update:model-value="groupFilter = $event"
                :options="groupFilterOptions"
                value-attribute="value"
                option-attribute="label"
                size="sm"
                class="w-44"
            >
                <template #label>
                    <span class="flex items-center gap-1.5 text-sm">
                        <Icon name="heroicons:user-group" class="h-4 w-4" />
                        {{ selectedGroupLabel }}
                    </span>
                </template>
                <template #option="{ option }">
                    <span class="text-sm">{{ option.label }}</span>
                </template>
            </USelectMenu>
        </div>

        <!-- Table card -->
        <div class="bg-white shadow-sm border border-gray-200 rounded-lg">
            <div class="overflow-x-auto">
                <table class="min-w-full divide-y divide-gray-200">
                    <thead class="bg-gray-50">
                        <tr>
                            <th class="px-6 py-3 text-start text-xs font-medium text-gray-500 uppercase tracking-wider">{{ $t('settings.members.colUser') }}</th>
                            <th class="px-6 py-3 text-start text-xs font-medium text-gray-500 uppercase tracking-wider">{{ $t('settings.members.colRole') }}</th>
                            <th class="px-6 py-3 text-start text-xs font-medium text-gray-500 uppercase tracking-wider">{{ $t('settings.members.colGroups') }}</th>
                            <th class="px-6 py-3 text-start text-xs font-medium text-gray-500 uppercase tracking-wider">{{ $t('settings.members.colStatus') }}</th>
                            <th class="px-6 py-3 text-start text-xs font-medium text-gray-500 uppercase tracking-wider">{{ $t('settings.members.colExternalPlatforms') }}</th>
                            <th class="px-6 py-3 text-start text-xs font-medium text-gray-500 uppercase tracking-wider">{{ $t('settings.members.colLastSeen') }}</th>
                            <th class="px-6 py-3 text-start text-xs font-medium text-gray-500 uppercase tracking-wider">{{ $t('settings.members.colDate') }}</th>
                            <th
                                v-if="useCan('remove_organization_members')"
                                class="px-6 py-3 text-start text-xs font-medium text-gray-500 uppercase tracking-wider"
                            >{{ $t('settings.members.colActions') }}</th>
                        </tr>
                    </thead>
                    <tbody class="bg-white divide-y divide-gray-200">
                        <!-- Loading state -->
                        <tr v-if="isLoading">
                            <td :colspan="useCan('remove_organization_members') ? 8 : 7" class="px-6 py-12 text-center">
                                <div class="flex items-center justify-center text-gray-500">
                                    <Spinner class="w-4 h-4 me-2" />
                                    <span class="text-sm">{{ $t('common.loading') }}</span>
                                </div>
                            </td>
                        </tr>
                        <!-- Data rows -->
                        <template v-else>
                            <tr v-for="member in filteredMembers" :key="member.id" class="hover:bg-gray-50">
                                <td class="px-6 py-4 whitespace-nowrap">
                                    <div v-if="member.user" class="flex items-center">
                                        <div class="flex-shrink-0 h-10 w-10">
                                            <div class="h-10 w-10 rounded-full bg-gray-200 flex items-center justify-center">
                                                <span class="text-gray-500 font-medium">
                                                    {{ member.user.name?.[0]?.toUpperCase() || member.user.email[0].toUpperCase() }}
                                                </span>
                                            </div>
                                        </div>
                                        <div class="ms-4">
                                            <div class="text-sm font-medium text-gray-900">{{ member.user.name }}</div>
                                            <div class="text-sm text-gray-500">{{ member.user.email }}</div>
                                        </div>
                                    </div>
                                    <div v-else class="text-sm text-gray-900">{{ member.email }}</div>
                                </td>
                                <td class="px-6 py-4">
                                    <template v-if="member.roles?.length">
                                        <USelectMenu
                                            v-if="useCan('update_organization_members')"
                                            :model-value="getDirectRoleIds(member)"
                                            :options="availableRoles"
                                            multiple
                                            option-attribute="name"
                                            value-attribute="id"
                                            size="sm"
                                            :ui-menu="{ width: 'w-48' }"
                                            :popper="{ placement: 'bottom-start', strategy: 'fixed' }"
                                            @update:model-value="updateMemberRoles(member, $event)"
                                        >
                                            <template #label>
                                                <div class="flex gap-1 flex-wrap">
                                                    <UBadge v-for="r in member.roles" :key="r.id" size="xs" :color="r.source === 'direct' ? 'gray' : 'blue'" :variant="r.source === 'direct' ? 'solid' : 'subtle'">
                                                        {{ r.name }}
                                                        <span v-if="r.source && r.source !== 'direct'" class="ms-1 opacity-70 text-[10px]">via {{ r.source.replace('group:', '') }}</span>
                                                    </UBadge>
                                                </div>
                                            </template>
                                        </USelectMenu>
                                        <div v-else class="flex gap-1 flex-wrap">
                                            <UBadge v-for="r in member.roles" :key="r.id" size="xs" :color="r.source === 'direct' ? 'gray' : 'blue'" :variant="r.source === 'direct' ? 'solid' : 'subtle'">
                                                {{ r.name }}
                                                <span v-if="r.source && r.source !== 'direct'" class="ms-1 opacity-70 text-[10px]">via {{ r.source.replace('group:', '') }}</span>
                                            </UBadge>
                                        </div>
                                    </template>
                                    <template v-else>
                                        <UBadge size="xs" color="gray">
                                            {{ member.role?.charAt(0).toUpperCase() + member.role?.slice(1) }}
                                        </UBadge>
                                    </template>
                                </td>
                                <td class="px-6 py-4 whitespace-nowrap">
                                    <div class="flex gap-1 flex-wrap">
                                        <UBadge
                                            v-for="group in getMemberGroups(member)"
                                            :key="group.id"
                                            size="xs"
                                            color="blue"
                                            variant="subtle"
                                        >
                                            {{ group.name }}
                                        </UBadge>
                                        <span v-if="getMemberGroups(member).length === 0" class="text-gray-400 text-sm italic">{{ $t('settings.members.emptyNone') }}</span>
                                    </div>
                                </td>
                                <td class="px-6 py-4 whitespace-nowrap">
                                    <span v-if="member.user"
                                          class="px-2 inline-flex text-xs leading-5 font-semibold rounded-full bg-green-100 text-green-800">
                                        {{ $t('settings.members.statusActive') }}
                                    </span>
                                    <span v-else
                                          class="px-2 inline-flex text-xs leading-5 font-semibold rounded-full bg-yellow-100 text-yellow-800">
                                        {{ $t('settings.members.statusPending') }}
                                    </span>
                                </td>
                                <td class="px-6 py-4 whitespace-nowrap">
                                    <div v-if="member.user?.external_user_mappings.length > 0">
                                        <div v-for="mapping in member.user?.external_user_mappings" :key="mapping.id">
                                            <UTooltip :text="mapping.is_verified ? $t('settings.members.verified') : $t('settings.members.unverified')">
                                                <img :src="`/icons/${mapping.platform_type}.png`" class="h-4 inline me-2" />
                                            </UTooltip>
                                        </div>
                                    </div>
                                    <div v-else>
                                        <span class="text-gray-400 italic">{{ $t('settings.members.emptyNone') }}</span>
                                    </div>
                                </td>
                                <td class="px-6 py-4 whitespace-nowrap text-sm text-gray-500">
                                    {{ member.user?.last_seen || '-' }}
                                </td>
                                <td class="px-6 py-4 whitespace-nowrap text-sm text-gray-500">
                                    {{ member.created_at }}
                                </td>
                                <td class="px-6 py-4 whitespace-nowrap text-sm"
                                    v-if="useCan('remove_organization_members')"
                                >
                                    <button
                                        @click="removeMember(member)"
                                        class="text-red-600 hover:text-red-900 font-medium transition-colors duration-150"
                                    >
                                        {{ $t('settings.members.remove') }}
                                    </button>
                                </td>
                            </tr>
                            <!-- Empty state -->
                            <tr v-if="filteredMembers.length === 0">
                                <td
                                    :colspan="useCan('remove_organization_members') ? 8 : 7"
                                    class="px-6 py-12 text-center text-gray-500 text-sm"
                                >
                                    <div class="flex flex-col items-center">
                                        <Icon
                                            name="heroicons:users"
                                            class="mx-auto h-12 w-12 text-gray-400"
                                        />
                                        <h3 class="mt-2 text-sm font-medium text-gray-900">
                                            {{ $t('settings.members.noMembers') }}
                                        </h3>
                                        <p class="mt-1 text-sm text-gray-500">
                                            {{ $t('settings.members.noMembersHint') }}
                                        </p>
                                    </div>
                                </td>
                            </tr>
                        </template>
                    </tbody>
                </table>
            </div>
        </div>
    </div>

    <!-- Invite Modal -->
    <UModal v-model="inviteModalOpen">
        <div class="p-4 relative">
            <button @click="inviteModalOpen = false" class="absolute top-2 end-2 text-gray-500 hover:text-gray-700 outline-none">
                <Icon name="heroicons:x-mark" class="w-5 h-5" />
            </button>
            <h1 class="text-lg font-semibold">{{ $t('settings.members.inviteTitle') }}</h1>
            <p class="text-sm text-gray-500">{{ $t('settings.members.inviteSubtitle') }}</p>
            <hr class="my-4" />

            <form @submit.prevent="inviteMember" class="space-y-4">
                <div class="flex flex-col">
                    <label class="text-sm font-medium text-gray-700 mb-2">{{ $t('settings.members.emailLabel') }}</label>
                    <UInput
                        v-model="inviteForm.email"
                        type="email"
                        required
                        :placeholder="$t('settings.members.emailPlaceholder')"
                    />
                </div>

                <div class="flex flex-col">
                    <label class="text-sm font-medium text-gray-700 mb-2">{{ $t('settings.members.roleLabel') }}</label>
                    <USelectMenu
                        v-model="inviteForm.role"
                        :options="inviteRoleOptions"
                        value-attribute="value"
                        option-attribute="label"
                        size="sm"
                    />
                </div>

                <div class="flex justify-end space-x-2 pt-4">
                    <UButton
                        type="button"
                        variant="ghost"
                        @click="inviteModalOpen = false"
                    >
                        {{ $t('settings.members.cancel') }}
                    </UButton>
                    <UButton
                        type="submit"
                        color="blue"
                    >
                        {{ $t('settings.members.sendInvitation') }}
                    </UButton>
                </div>
            </form>
        </div>
    </UModal>
</template>

<script setup lang="ts">
import Spinner from '@/components/Spinner.vue'
import { useCan } from '~/composables/usePermissions'

const { t } = useI18n()

interface MemberUser {
    id: string
    name?: string
    email: string
    last_seen?: string
    external_user_mappings: { id: string; platform_type: string; is_verified: boolean }[]
}

interface Member {
    id: string
    user_id?: string
    user?: MemberUser
    email?: string
    role?: string
    roles?: { id: string; name: string; source?: string }[]
    created_at: string
}

interface GroupData {
    id: string
    name: string
    description?: string
    member_user_ids?: string[]
}

const props = defineProps<{
    organization: { id: string; name?: string }
}>()

const organizationId = props.organization.id
const members = ref<Member[]>([])
const searchQuery = ref('')
const toast = useToast()
const isLoading = ref(true)
const availableRoles = ref<{ id: string; name: string }[]>([])
const groups = ref<GroupData[]>([])
const groupMemberships = ref<Record<string, string[]>>({}) // groupId -> userIds

// Filters
const statusFilter = ref<'all' | 'active' | 'pending'>('all')
const groupFilter = ref<string | null>(null)

// Filter/role options are computed so their labels re-render when locale flips.
const statusFilterOptions = computed(() => [
    { value: 'all', label: t('settings.members.allStatus') },
    { value: 'active', label: t('settings.members.statusActive') },
    { value: 'pending', label: t('settings.members.statusPending') },
])

const selectedStatusLabel = computed(() => {
    const option = statusFilterOptions.value.find(o => o.value === statusFilter.value)
    return option?.label || t('settings.members.colStatus')
})

const groupFilterOptions = computed(() => {
    const options: { value: string | null; label: string }[] = [
        { value: null, label: t('settings.members.allGroups') },
    ]
    for (const group of groups.value) {
        options.push({ value: group.id, label: group.name })
    }
    return options
})

const selectedGroupLabel = computed(() => {
    if (!groupFilter.value) return t('settings.members.allGroups')
    const group = groups.value.find(g => g.id === groupFilter.value)
    return group?.name || t('settings.members.allGroups')
})

const inviteRoleOptions = computed(() => {
    if (availableRoles.value.length) {
        return availableRoles.value.map(r => ({
            value: r.name,
            label: r.name.charAt(0).toUpperCase() + r.name.slice(1),
        }))
    }
    return [
        { value: 'member', label: t('settings.members.roleLabel') },
        { value: 'admin', label: 'Admin' },
    ]
})

function getDirectRoleIds(member: Member): string[] {
    return (member.roles || []).filter(r => !r.source || r.source === 'direct').map(r => r.id)
}

function getMemberGroups(member: Member): GroupData[] {
    const userId = member.user_id || member.user?.id
    if (!userId) return []
    return groups.value.filter(group => {
        const memberIds = groupMemberships.value[group.id]
        return memberIds?.includes(userId)
    })
}

const filteredMembers = computed(() => {
    let result = members.value as Member[]

    // Search filter
    const query = searchQuery.value.toLowerCase()
    if (query) {
        result = result.filter(member => {
            const name = member.user?.name?.toLowerCase() || ''
            const email = (member.user?.email || member.email || '').toLowerCase()
            return name.includes(query) || email.includes(query)
        })
    }

    // Status filter
    if (statusFilter.value === 'active') {
        result = result.filter(member => !!member.user)
    } else if (statusFilter.value === 'pending') {
        result = result.filter(member => !member.user)
    }

    // Group filter
    if (groupFilter.value) {
        const memberIds = groupMemberships.value[groupFilter.value] || []
        result = result.filter(member => {
            const userId = member.user_id || member.user?.id
            return userId && memberIds.includes(userId)
        })
    }

    return result
})

async function loadAvailableRoles() {
    try {
        const { data } = await useMyFetch(`/organizations/${organizationId}/roles`)
        if (data.value) {
            availableRoles.value = (data.value as any[]).map((r) => ({ id: r.id, name: r.name }))
        }
    } catch (e) {
        // Roles endpoint may not be available yet (backward compat)
    }
}

async function loadGroups() {
    try {
        const { data } = await useMyFetch(`/organizations/${organizationId}/groups`)
        if (data.value) {
            const groupList = data.value as any[]
            groups.value = groupList.map(g => ({ id: g.id, name: g.name, description: g.description }))
            const membershipsMap: Record<string, string[]> = {}
            for (const group of groupList) {
                membershipsMap[group.id] = group.member_user_ids ?? []
            }
            groupMemberships.value = membershipsMap
        }
    } catch (e) {
        // Groups endpoint may not be available (non-enterprise)
    }
}

async function updateMemberRoles(member: any, selectedRoleIds: string[]) {
    try {
        const currentRoleIds = (member.roles || []).filter((r: any) => !r.source || r.source === 'direct').map((r: any) => r.id)
        const added = selectedRoleIds.filter((id: string) => !currentRoleIds.includes(id))
        const removed = currentRoleIds.filter((id: string) => !selectedRoleIds.includes(id))

        for (const roleId of added) {
            await useMyFetch(`/organizations/${organizationId}/role-assignments`, {
                method: 'POST',
                body: { role_id: roleId, principal_type: 'user', principal_id: member.user_id || member.user?.id },
            })
        }

        if (removed.length) {
            const { data: assignments } = await useMyFetch(
                `/organizations/${organizationId}/role-assignments?principal_type=user&principal_id=${member.user_id || member.user?.id}`
            )
            if (assignments.value) {
                for (const assignment of assignments.value as any[]) {
                    if (removed.includes(assignment.role_id)) {
                        const resp = await useMyFetch(`/organizations/${organizationId}/role-assignments/${assignment.id}`, {
                            method: 'DELETE',
                        })
                        if (resp.error?.value) {
                            const detail = resp.error.value.data?.detail || t('settings.members.failedToRemoveRole')
                            toast.add({ title: detail, color: 'red' })
                            const membersResp = await useMyFetch(`/organizations/${organizationId}/members`)
                            members.value = membersResp.data.value as Member[]
                            return
                        }
                    }
                }
            }
        }

        const inheritedRoles = (member.roles || []).filter((r: any) => r.source && r.source !== 'direct')
        const newDirectRoles = availableRoles.value
            .filter((r) => selectedRoleIds.includes(r.id))
            .map((r) => ({ id: r.id, name: r.name, source: 'direct' }))
        member.roles = [...newDirectRoles, ...inheritedRoles]

        toast.add({ title: t('settings.members.rolesUpdated'), color: 'green' })
    } catch (error: any) {
        const detail = error?.data?.detail || error?.message || t('settings.members.failedToUpdateRoles')
        toast.add({ title: detail, color: 'red' })
    }
}

onMounted(async () => {
    isLoading.value = true
    try {
        const response = await useMyFetch(`/organizations/${organizationId}/members`)
        members.value = (response.data.value || []) as Member[]
        await Promise.all([loadAvailableRoles(), loadGroups()])
    } finally {
        isLoading.value = false
    }
})

const inviteModalOpen = ref(false)
const inviteForm = ref({
    email: '',
    role: 'member',
    organization_id: organizationId
})

const removeMember = async (member: Member) => {
    const name = member.user?.name || member.email || ''
    const confirmed = window.confirm(t('settings.members.confirmRemove', { name }))
    if (!confirmed) return

    try {
        const response = await useMyFetch(`/organizations/${organizationId}/members/${member.id}`, {
            method: 'DELETE'
        })

        if (response.error.value) {
            const errorDetail = response.error.value.data?.detail
            toast.add({
                title: t('common.error'),
                description: errorDetail || t('settings.members.failedToRemove'),
                color: 'red'
            })
            throw new Error(errorDetail || t('settings.members.failedToRemove'))
        }

        const updatedMembers = await useMyFetch(`/organizations/${organizationId}/members`)
        members.value = (updatedMembers.data.value || []) as Member[]

        toast.add({
            title: t('common.success'),
            description: t('settings.members.successRemoved', { name }),
            color: 'green'
        })
    } catch (error: any) {
        const errorDetail = error.data?.detail || error.message
        toast.add({
            title: t('common.error'),
            description: errorDetail || t('settings.members.failedToRemove'),
            color: 'red'
        })
    }
}

const inviteMember = async () => {
    try {
        const response = await useMyFetch(`/organizations/${organizationId}/members`, {
            method: 'POST',
            body: inviteForm.value
        })

        if (response.error.value) {
            const errorDetail = response.error.value.data?.detail
            toast.add({
                title: t('common.error'),
                description: errorDetail || t('settings.members.failedToInvite'),
                color: 'red'
            })
            throw new Error(errorDetail || t('settings.members.failedToInvite'))
        }

        const membersResponse = await useMyFetch(`/organizations/${organizationId}/members`)
        members.value = (membersResponse.data.value || []) as Member[]

        toast.add({
            title: t('common.success'),
            description: t('settings.members.successInvited', { email: inviteForm.value.email }),
            color: 'green'
        })

        inviteForm.value = { email: '', role: 'member', organization_id: organizationId }
        inviteModalOpen.value = false
    } catch (error) {
        console.error('Failed to invite member:', error)
    }
}
</script>
