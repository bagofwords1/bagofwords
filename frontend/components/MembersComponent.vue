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
                    color="gray"
                    variant="outline"
                    size="xs"
                    icon="i-heroicons-arrow-up-tray"
                    @click="openImportModal"
                >
                    Import
                </UButton>
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
                            <th v-if="showQuotaColumn" class="px-6 py-3 text-start text-xs font-medium text-gray-500 uppercase tracking-wider">{{ $t('quotaPolicies.colQuota') }}</th>
                            <th class="px-6 py-3 text-start text-xs font-medium text-gray-500 uppercase tracking-wider">{{ $t('settings.members.colStatus') }}</th>
                            <th class="px-6 py-3 text-start text-xs font-medium text-gray-500 uppercase tracking-wider">Note</th>
                            <th class="px-6 py-3 text-start text-xs font-medium text-gray-500 uppercase tracking-wider">{{ $t('settings.members.colExternalPlatforms') }}</th>
                            <th class="px-6 py-3 text-start text-xs font-medium text-gray-500 uppercase tracking-wider">Last Login</th>
                            <th class="px-6 py-3 text-start text-xs font-medium text-gray-500 uppercase tracking-wider">Last Seen</th>
                            <th
                                v-if="useCan('remove_organization_members')"
                                class="px-6 py-3 text-start text-xs font-medium text-gray-500 uppercase tracking-wider"
                            >{{ $t('settings.members.colActions') }}</th>
                        </tr>
                    </thead>
                    <tbody class="bg-white divide-y divide-gray-200">
                        <!-- Loading state -->
                        <tr v-if="isLoading">
                            <td :colspan="membersColspan" class="px-6 py-12 text-center">
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
                                    <div class="flex gap-1 flex-wrap items-center">
                                        <UBadge
                                            v-for="group in getMemberGroups(member).slice(0, 3)"
                                            :key="group.id"
                                            size="xs"
                                            color="blue"
                                            variant="subtle"
                                        >
                                            {{ group.name }}
                                        </UBadge>
                                        <UPopover
                                            v-if="getMemberGroups(member).length > 3"
                                            mode="hover"
                                            :popper="{ placement: 'bottom-start' }"
                                        >
                                            <UBadge size="xs" color="gray" variant="subtle" class="cursor-default">
                                                +{{ getMemberGroups(member).length - 3 }} {{ $t('settings.members.moreGroups') }}
                                            </UBadge>
                                            <template #panel>
                                                <div class="p-2 max-h-48 overflow-y-auto flex flex-col gap-1 min-w-32">
                                                    <UBadge
                                                        v-for="group in getMemberGroups(member).slice(3)"
                                                        :key="group.id"
                                                        size="xs"
                                                        color="blue"
                                                        variant="subtle"
                                                    >
                                                        {{ group.name }}
                                                    </UBadge>
                                                </div>
                                            </template>
                                        </UPopover>
                                        <span v-if="getMemberGroups(member).length === 0" class="text-gray-400 text-sm italic">{{ $t('settings.members.emptyNone') }}</span>
                                    </div>
                                </td>
                                <td v-if="showQuotaColumn" class="px-6 py-4">
                                    <USelectMenu
                                        v-if="member.user_id || member.user?.id"
                                        :model-value="getDirectQuotaId('user', member.user_id || member.user?.id || '')"
                                        :options="quotaSelectOptions"
                                        value-attribute="value"
                                        option-attribute="label"
                                        size="sm"
                                        :ui-menu="{ width: 'w-48' }"
                                        :popper="{ placement: 'bottom-start', strategy: 'fixed' }"
                                        @update:model-value="updatePrincipalQuota('user', member.user_id || member.user?.id || '', $event)"
                                    >
                                        <template #label>
                                            <span class="flex gap-1 flex-wrap items-center">
                                                <UBadge
                                                    v-for="quota in getMemberQuotaPolicies(member).slice(0, 2)"
                                                    :key="quota.id"
                                                    size="xs"
                                                    :color="quota.source === 'direct' ? 'gray' : 'blue'"
                                                    :variant="quota.source === 'direct' ? 'solid' : 'subtle'"
                                                >
                                                    <span v-if="quota.source === 'inherited'" class="me-1 opacity-70">{{ $t('quotaPolicies.inherited') }}</span>
                                                    {{ quota.name }}
                                                </UBadge>
                                                <span v-if="getMemberQuotaPolicies(member).length === 0" class="text-gray-400 text-sm italic">{{ $t('quotaPolicies.unlimited') }}</span>
                                            </span>
                                        </template>
                                        <template #option="{ option }">
                                            <span class="text-sm">{{ option.label }}</span>
                                        </template>
                                    </USelectMenu>
                                    <span
                                        v-else
                                        class="text-gray-400 text-sm italic"
                                    >
                                        {{ $t('quotaPolicies.unlimited') }}
                                    </span>
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
                                <td class="px-6 py-4 max-w-xs">
                                    <input
                                        v-if="useCan('update_organization_members')"
                                        :value="member.note || ''"
                                        @change="onNoteChange(member, ($event.target as HTMLInputElement).value)"
                                        type="text"
                                        maxlength="500"
                                        placeholder="Add a note…"
                                        class="w-full text-sm border border-transparent hover:border-gray-300 focus:border-blue-500 focus:ring-1 focus:ring-blue-500 rounded px-2 py-1 outline-none bg-transparent"
                                    />
                                    <UTooltip v-else-if="member.note" :text="member.note">
                                        <span class="text-sm text-gray-700 truncate block max-w-[16rem]">{{ member.note }}</span>
                                    </UTooltip>
                                    <span v-else class="text-gray-400 italic text-sm">—</span>
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
                                    {{ member.user?.last_login ? new Date(member.user.last_login).toLocaleDateString() : '-' }}
                                </td>
                                <td class="px-6 py-4 whitespace-nowrap text-sm text-gray-500">
                                    {{ member.user?.last_seen ? new Date(member.user.last_seen).toLocaleDateString() : '-' }}
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
                                    :colspan="membersColspan"
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

    <!-- Import Modal -->
    <UModal v-model="importModalOpen" :ui="{ width: 'sm:max-w-2xl' }">
        <div class="p-4 relative">
            <button @click="closeImportModal" class="absolute top-2 end-2 text-gray-500 hover:text-gray-700 outline-none">
                <Icon name="heroicons:x-mark" class="w-5 h-5" />
            </button>
            <h1 class="text-lg font-semibold">Import members</h1>
            <p class="text-sm text-gray-500 mt-1">
                Upload an Excel (.xlsx) or CSV file with columns <code class="text-xs bg-gray-100 px-1 rounded">email</code> (required) and <code class="text-xs bg-gray-100 px-1 rounded">note</code> (optional).
                Re-importing the same file is safe — existing roles and group memberships are never touched; only the note is updated.
            </p>
            <hr class="my-4" />

            <div v-if="!importReport" class="space-y-3">
                <input
                    ref="importFileInput"
                    type="file"
                    accept=".xlsx,.csv"
                    @change="onImportFileSelected"
                    class="block w-full text-sm text-gray-700 file:mr-3 file:py-1.5 file:px-3 file:rounded file:border-0 file:text-sm file:font-medium file:bg-blue-50 file:text-blue-700 hover:file:bg-blue-100"
                />
                <button
                    type="button"
                    @click="downloadImportTemplate"
                    class="text-xs text-blue-600 hover:underline"
                >
                    Download CSV template
                </button>
                <div v-if="importError" class="text-sm text-red-600 bg-red-50 border border-red-200 rounded px-3 py-2">
                    {{ importError }}
                </div>
                <div v-if="importLoading" class="flex items-center gap-2 text-sm text-gray-500">
                    <Spinner class="w-4 h-4" />
                    <span>Validating file…</span>
                </div>
            </div>

            <div v-else class="space-y-3">
                <div class="flex flex-wrap gap-2 text-sm">
                    <UBadge color="blue" variant="subtle">{{ importReport.summary.created }} to create</UBadge>
                    <UBadge color="green" variant="subtle">{{ importReport.summary.updated }} to update</UBadge>
                    <UBadge color="gray" variant="subtle">{{ importReport.summary.unchanged }} unchanged</UBadge>
                    <UBadge v-if="importReport.summary.errors > 0" color="red" variant="subtle">{{ importReport.summary.errors }} error(s)</UBadge>
                    <UBadge v-if="importReport.dry_run" color="yellow" variant="solid">Preview — not applied</UBadge>
                    <UBadge v-else color="green" variant="solid">Applied</UBadge>
                </div>
                <div class="max-h-80 overflow-auto border border-gray-200 rounded">
                    <table class="min-w-full text-sm">
                        <thead class="bg-gray-50 sticky top-0">
                            <tr>
                                <th class="px-3 py-2 text-start font-medium text-gray-500 text-xs uppercase">Row</th>
                                <th class="px-3 py-2 text-start font-medium text-gray-500 text-xs uppercase">Email</th>
                                <th class="px-3 py-2 text-start font-medium text-gray-500 text-xs uppercase">Note</th>
                                <th class="px-3 py-2 text-start font-medium text-gray-500 text-xs uppercase">Status</th>
                            </tr>
                        </thead>
                        <tbody class="divide-y divide-gray-100">
                            <tr v-for="row in importReport.rows" :key="row.row">
                                <td class="px-3 py-2 text-gray-500">{{ row.row }}</td>
                                <td class="px-3 py-2">{{ row.email || '—' }}</td>
                                <td class="px-3 py-2 text-gray-600 truncate max-w-[16rem]">{{ row.note || '—' }}</td>
                                <td class="px-3 py-2">
                                    <UBadge v-if="row.status === 'error'" color="red" variant="subtle" size="xs">{{ row.error }}</UBadge>
                                    <UBadge v-else-if="row.status === 'created'" color="blue" variant="subtle" size="xs">created</UBadge>
                                    <UBadge v-else-if="row.status === 'updated'" color="green" variant="subtle" size="xs">updated</UBadge>
                                    <UBadge v-else color="gray" variant="subtle" size="xs">unchanged</UBadge>
                                </td>
                            </tr>
                        </tbody>
                    </table>
                </div>
            </div>

            <div class="flex justify-end space-x-2 pt-4">
                <UButton type="button" variant="ghost" @click="closeImportModal">
                    {{ importReport && !importReport.dry_run ? 'Close' : $t('settings.members.cancel') }}
                </UButton>
                <UButton
                    v-if="importReport && importReport.dry_run && (importReport.summary.created + importReport.summary.updated) > 0"
                    color="blue"
                    :loading="importLoading"
                    @click="commitImport"
                >
                    Import {{ importReport.summary.created + importReport.summary.updated }} row(s)
                </UButton>
            </div>
        </div>
    </UModal>
</template>

<script setup lang="ts">
import Spinner from '@/components/Spinner.vue'
import { useCan } from '~/composables/usePermissions'
import { useEnterprise } from '~/ee/composables/useEnterprise'

const { t } = useI18n()

interface MemberUser {
    id: string
    name?: string
    email: string
    last_login?: string
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
    note?: string | null
    created_at: string
}

interface MembershipImportRow {
    row: number
    email?: string | null
    note?: string | null
    status: 'created' | 'updated' | 'unchanged' | 'error'
    error?: string | null
}

interface MembershipImportSummary {
    created: number
    updated: number
    unchanged: number
    errors: number
}

interface MembershipImportReport {
    dry_run: boolean
    summary: MembershipImportSummary
    rows: MembershipImportRow[]
}

interface GroupData {
    id: string
    name: string
    description?: string
    member_user_ids?: string[]
}

interface UsagePolicyAssignment {
    principal_type: 'user' | 'group' | 'role'
    principal_id: string
}

type UsagePrincipalType = UsagePolicyAssignment['principal_type']

interface UsagePolicySummary {
    id: string
    name: string
    enabled: boolean
    assignments: UsagePolicyAssignment[]
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
const usagePolicies = ref<UsagePolicySummary[]>([])
const { hasFeature } = useEnterprise()
const showQuotaColumn = computed(() => hasFeature('usage_limits') && useCan('manage_settings'))
const membersColspan = computed(() => 8 + (showQuotaColumn.value ? 1 : 0) + (useCan('remove_organization_members') ? 1 : 0))

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

const quotaSelectOptions = computed(() => [
    { value: null, label: t('quotaPolicies.noDirectQuota') },
    ...usagePolicies.value
        .filter(policy => policy.enabled)
        .map(policy => ({ value: policy.id, label: policy.name })),
])

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

function getMemberQuotaPolicies(member: Member): { id: string; name: string; source: 'direct' | 'inherited' }[] {
    if (!showQuotaColumn.value) return []
    const userId = member.user_id || member.user?.id
    if (!userId) return []
    const direct = getPrincipalQuotaPolicies('user', userId)
    if (direct.length) {
        return direct.map(policy => ({ id: policy.id, name: policy.name, source: 'direct' }))
    }

    const groupIds = getMemberGroups(member).map(group => group.id)
    const roleIds = (member.roles || []).map(role => role.id)
    const inherited = usagePolicies.value.filter(policy =>
        policy.enabled &&
        policy.assignments?.some(assignment =>
            (assignment.principal_type === 'group' && groupIds.includes(assignment.principal_id)) ||
            (assignment.principal_type === 'role' && roleIds.includes(assignment.principal_id))
        )
    )
    return inherited.map(policy => ({ id: policy.id, name: policy.name, source: 'inherited' }))
}

function getPrincipalQuotaPolicies(principalType: UsagePrincipalType, principalId: string): UsagePolicySummary[] {
    return usagePolicies.value.filter(policy =>
        policy.enabled &&
        policy.assignments?.some(assignment =>
            assignment.principal_type === principalType &&
            assignment.principal_id === principalId
        )
    )
}

function getDirectQuotaId(principalType: UsagePrincipalType, principalId: string): string | null {
    return getPrincipalQuotaPolicies(principalType, principalId)[0]?.id || null
}

function applyLocalQuotaAssignment(principalType: UsagePrincipalType, principalId: string, policyId: string | null) {
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

async function updatePrincipalQuota(principalType: UsagePrincipalType, principalId: string, policyId: string | null) {
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

async function loadUsagePolicies() {
    if (!showQuotaColumn.value) return
    try {
        const { data } = await useMyFetch(`/organizations/${organizationId}/usage-policies`)
        usagePolicies.value = (data.value || []) as UsagePolicySummary[]
    } catch (e) {
        usagePolicies.value = []
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
        await Promise.all([loadAvailableRoles(), loadGroups(), loadUsagePolicies()])
    } finally {
        isLoading.value = false
    }
})

watch(showQuotaColumn, (enabled) => {
    if (enabled && usagePolicies.value.length === 0) {
        loadUsagePolicies()
    }
})

const inviteModalOpen = ref(false)
const inviteForm = ref({
    email: '',
    role: 'member',
    organization_id: organizationId
})

const importModalOpen = ref(false)
const importFile = ref<File | null>(null)
const importFileInput = ref<HTMLInputElement | null>(null)
const importReport = ref<MembershipImportReport | null>(null)
const importLoading = ref(false)
const importError = ref<string | null>(null)

function openImportModal() {
    importFile.value = null
    importReport.value = null
    importError.value = null
    importLoading.value = false
    importModalOpen.value = true
}

function closeImportModal() {
    importModalOpen.value = false
    importFile.value = null
    importReport.value = null
    importError.value = null
    importLoading.value = false
    if (importFileInput.value) importFileInput.value.value = ''
}

function downloadImportTemplate() {
    const csv = 'email,note\nalice@example.com,"CFO, focuses on monthly close"\nbob@example.com,"Data analyst, revenue ops"\n'
    const blob = new Blob([csv], { type: 'text/csv;charset=utf-8' })
    const url = URL.createObjectURL(blob)
    const link = document.createElement('a')
    link.href = url
    link.download = 'members-import-template.csv'
    document.body.appendChild(link)
    link.click()
    document.body.removeChild(link)
    URL.revokeObjectURL(url)
}

async function runImport(file: File, dryRun: boolean): Promise<MembershipImportReport | null> {
    const form = new FormData()
    form.append('file', file)
    const { data, error } = await useMyFetch(
        `/organizations/${organizationId}/members/import?dry_run=${dryRun}`,
        { method: 'POST', body: form }
    )
    if (error.value) {
        const detail = (error.value as any)?.data?.detail || 'Import failed'
        importError.value = typeof detail === 'string' ? detail : 'Import failed'
        return null
    }
    return (data.value || null) as MembershipImportReport | null
}

async function onImportFileSelected(event: Event) {
    const input = event.target as HTMLInputElement
    const file = input.files?.[0]
    if (!file) return
    importFile.value = file
    importError.value = null
    importLoading.value = true
    try {
        const report = await runImport(file, true)
        importReport.value = report
    } finally {
        importLoading.value = false
    }
}

async function commitImport() {
    if (!importFile.value) return
    importError.value = null
    importLoading.value = true
    try {
        const report = await runImport(importFile.value, false)
        if (report) {
            importReport.value = report
            const refreshed = await useMyFetch(`/organizations/${organizationId}/members`)
            members.value = (refreshed.data.value || []) as Member[]
            toast.add({
                title: 'Import complete',
                description: `${report.summary.created} created, ${report.summary.updated} updated, ${report.summary.errors} error(s)`,
                color: report.summary.errors > 0 ? 'yellow' : 'green',
            })
        }
    } finally {
        importLoading.value = false
    }
}

async function onNoteChange(member: Member, value: string) {
    const trimmed = value.trim()
    const next = trimmed.length === 0 ? null : trimmed
    if ((member.note || null) === next) return
    const { data, error } = await useMyFetch(
        `/organizations/${organizationId}/members/${member.id}`,
        { method: 'PUT', body: { note: next } }
    )
    if (error.value) {
        const detail = (error.value as any)?.data?.detail || 'Failed to save note'
        toast.add({ title: typeof detail === 'string' ? detail : 'Failed to save note', color: 'red' })
        return
    }
    member.note = (data.value as any)?.note ?? next
    toast.add({ title: 'Note saved', color: 'green' })
}

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
