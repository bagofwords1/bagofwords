<template>
    <div class="text-sm px-6 py-5 max-w-2xl">
        <div v-if="!ready" class="inline-flex items-center text-gray-500 dark:text-gray-400 text-xs">
            <Spinner class="w-4 h-4 me-2" />
            Loading settings…
        </div>

        <div v-else>
            <!-- General -->
            <div class="border-b border-gray-100 dark:border-gray-800 pb-5 mb-5">
                <div class="text-[10px] font-semibold uppercase tracking-wider text-gray-400 dark:text-gray-500 mb-2">General</div>
                <label class="block text-[11px] text-gray-400 dark:text-gray-500 mb-1">Agent name</label>
                <div class="flex items-center gap-2">
                    <input
                        v-model="form.name"
                        type="text"
                        :disabled="!canManageDs"
                        class="flex-1 h-8 px-2 text-xs bg-gray-50 border border-gray-200 rounded-md outline-none focus:border-gray-400 focus:bg-white disabled:opacity-60 dark:bg-gray-800 dark:border-gray-700 dark:text-gray-100 dark:placeholder-gray-500"
                        placeholder="Name"
                    />
                    <button
                        v-if="canManageDs"
                        @click="saveName"
                        :disabled="saving.name || form.name.trim() === '' || form.name === original.name"
                        class="h-8 px-3 rounded-lg bg-gray-900 text-white text-xs font-medium hover:bg-black disabled:opacity-40 disabled:cursor-not-allowed"
                    >
                        {{ saving.name ? 'Saving…' : 'Save' }}
                    </button>
                </div>
            </div>

            <!-- Access -->
            <div class="border-b border-gray-100 dark:border-gray-800 pb-5 mb-5">
                <div class="text-[10px] font-semibold uppercase tracking-wider text-gray-400 dark:text-gray-500 mb-2">Access</div>
                <div class="flex items-center gap-3">
                    <UToggle v-model="form.isPublic" @update:model-value="onTogglePublic" :disabled="!canManageDs" />
                    <span class="text-[11px] text-gray-400 dark:text-gray-500">
                        Public access allows all organization members to use this agent.
                    </span>
                </div>
            </div>

            <!-- Members & your access -->
            <div class="border-b border-gray-100 dark:border-gray-800 pb-5 mb-5">
                <div class="flex items-center justify-between mb-2">
                    <div class="text-[10px] font-semibold uppercase tracking-wider text-gray-400 dark:text-gray-500">Members</div>
                    <button
                        v-if="canManageDsMembers && !form.isPublic"
                        @click="openAdd"
                        class="h-7 px-2.5 rounded-lg border border-gray-200 dark:border-gray-800 text-gray-700 dark:text-gray-300 text-xs font-medium hover:bg-gray-50 dark:hover:bg-gray-800/50"
                    >
                        Add member
                    </button>
                </div>
                <p class="text-[11px] text-gray-400 dark:text-gray-500 mb-3">Everyone listed here can query this agent. The role only grants extra management rights — use Remove to revoke access.</p>

                <div class="space-y-1">
                    <!-- Your access (the current user's effective role on this agent) -->
                    <div class="rounded-md border border-blue-200 dark:border-blue-900/60 bg-blue-50/50 dark:bg-blue-900/10 px-3 py-2">
                        <div class="flex items-start gap-1.5">
                            <UIcon name="i-heroicons-user" class="w-3.5 h-3.5 text-blue-500 dark:text-blue-400 flex-shrink-0 mt-0.5" />
                            <div class="min-w-0">
                                <div class="flex items-center gap-1.5 flex-wrap">
                                    <span class="text-xs font-medium text-gray-900 dark:text-white">{{ currentUserName }}</span>
                                    <UBadge size="xs" color="blue" variant="subtle">You</UBadge>
                                    <UBadge v-if="isOwner" size="xs" color="amber" variant="subtle" title="Created this agent">Owner</UBadge>
                                    <UBadge v-else size="xs" :color="myAccessIsPrivileged ? 'blue' : 'gray'" variant="subtle">{{ myAccessLabel }}</UBadge>
                                </div>
                                <div class="mt-1.5 flex gap-1 flex-wrap">
                                    <UBadge v-for="p in myEffectivePerms" :key="p" size="xs" color="gray" variant="subtle">{{ formatPermission(p) }}</UBadge>
                                    <span v-if="!myEffectivePerms.length" class="text-[11px] text-gray-400 dark:text-gray-500" title="You can query this agent but have no extra management rights">Query only</span>
                                </div>
                            </div>
                        </div>
                    </div>

                    <template v-if="!form.isPublic">
                    <div
                        v-for="m in otherMembers"
                        :key="m.grant_id"
                        class="rounded-md border border-gray-100 dark:border-gray-800 px-3 py-2 hover:bg-gray-50 dark:hover:bg-gray-800/50"
                    >
                        <div class="flex items-start justify-between gap-2">
                            <div class="min-w-0 flex items-start gap-1.5">
                                <UIcon :name="principalIcon(m)" class="w-3.5 h-3.5 text-gray-400 dark:text-gray-500 flex-shrink-0 mt-0.5" />
                                <div class="min-w-0">
                                    <div class="flex items-center gap-1.5">
                                        <span class="text-xs font-medium text-gray-900 dark:text-white">{{ principalDisplayName(m) }}</span>
                                        <UBadge v-if="isOwnerPrincipal(m)" size="xs" color="amber" variant="subtle" title="Created this agent">Owner</UBadge>
                                        <template v-if="m.principal_type === 'group'">
                                            <span class="text-[11px] text-gray-400 dark:text-gray-500">({{ groupMemberCount(m) }} {{ groupMemberCount(m) === 1 ? 'member' : 'members' }})</span>
                                            <button
                                                @click="toggleGroupExpand(m.principal_id)"
                                                class="w-4 h-4 flex items-center justify-center rounded text-gray-400 dark:text-gray-500 hover:text-gray-600 dark:hover:text-gray-300 hover:bg-gray-100 dark:hover:bg-gray-800/70 text-xs leading-none"
                                            >
                                                {{ expandedGroups.has(m.principal_id) ? '−' : '+' }}
                                            </button>
                                        </template>
                                    </div>
                                    <div class="text-[11px] text-gray-400 dark:text-gray-500" v-if="principalEmail(m)">{{ principalEmail(m) }}</div>
                                    <!-- Expanded group members -->
                                    <div v-if="m.principal_type === 'group' && expandedGroups.has(m.principal_id)" class="mt-1.5 ps-1 space-y-1">
                                        <div
                                            v-for="gm in (groupMembers[m.principal_id] || [])"
                                            :key="gm.user_id"
                                            class="flex items-center gap-1.5 text-[11px] text-gray-400 dark:text-gray-500"
                                        >
                                            <UIcon name="i-heroicons-user" class="w-3 h-3 text-gray-300 dark:text-gray-600 flex-shrink-0" />
                                            <span>{{ gm.user_name || gm.user_email }}</span>
                                            <span v-if="gm.user_name && gm.user_email" class="text-gray-400 dark:text-gray-500">{{ gm.user_email }}</span>
                                        </div>
                                        <div v-if="!groupMembers[m.principal_id]?.length" class="text-[11px] text-gray-300 dark:text-gray-600 italic">No members</div>
                                    </div>
                                    <!-- Permissions -->
                                    <div class="mt-1.5">
                                        <UDropdown
                                            v-if="canManageDsMembers"
                                            :items="[dsPermOptions.map(p => ({
                                                label: formatPermission(p),
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
                                            <UButton size="2xs" color="white" trailing-icon="i-heroicons-chevron-down-20-solid">
                                                <span v-if="m.permissions?.length" class="flex items-center gap-1 flex-wrap">
                                                    <UBadge v-for="p in m.permissions" :key="p" size="xs" color="gray" variant="subtle">{{ formatPermission(p) }}</UBadge>
                                                </span>
                                                <span v-else class="text-gray-400 dark:text-gray-500" title="This member can query the agent but has no extra management rights">Query only</span>
                                            </UButton>
                                        </UDropdown>
                                        <div v-else class="flex gap-1 flex-wrap">
                                            <UBadge v-for="p in m.permissions" :key="p" size="xs" color="gray" variant="subtle">{{ formatPermission(p) }}</UBadge>
                                            <span v-if="!m.permissions?.length" class="text-[11px] text-gray-400 dark:text-gray-500" title="This member can query the agent but has no extra management rights">Query only</span>
                                        </div>
                                    </div>
                                </div>
                            </div>
                            <button
                                v-if="canManageDsMembers"
                                @click="removeMember(m)"
                                class="shrink-0 h-7 px-2.5 rounded-lg border border-gray-200 dark:border-gray-800 text-gray-700 dark:text-gray-300 text-xs font-medium hover:bg-gray-50 dark:hover:bg-gray-800/50"
                            >
                                Remove
                            </button>
                        </div>
                    </div>
                    <div v-if="otherMembers.length === 0" class="rounded-md border border-gray-100 dark:border-gray-800 px-3 py-3 text-[11px] text-gray-400 dark:text-gray-500 text-center">
                        No other members yet.
                    </div>
                    </template>
                    <div v-else class="rounded-md border border-gray-100 dark:border-gray-800 px-3 py-3 text-[11px] text-gray-400 dark:text-gray-500 text-center">
                        This agent is public — every organization member can query it.
                    </div>
                </div>
            </div>

            <!-- Channels (MCP / Slack / …) — moved to the bottom -->
            <div v-if="connectedChannels.length > 0" class="border-b border-gray-100 dark:border-gray-800 pb-5 mb-5">
                <div class="text-[10px] font-semibold uppercase tracking-wider text-gray-400 dark:text-gray-500 mb-2">Channels</div>
                <p class="text-[11px] text-gray-400 dark:text-gray-500 mb-3">Choose which connected channels can query this agent. Disabled channels won't see it.</p>
                <div class="space-y-2">
                    <div v-for="ch in connectedChannels" :key="ch.key" class="flex items-center gap-3">
                        <UToggle
                            :model-value="channelAvailability[ch.key] !== false"
                            :disabled="!canManageDs || savingChannels"
                            @update:model-value="onToggleChannel(ch.key, $event)"
                        />
                        <span class="inline-flex items-center gap-1.5 text-[11px] text-gray-600 dark:text-gray-400">
                            <img v-if="channelIcon(ch.key).img" :src="channelIcon(ch.key).img" :alt="ch.name" class="w-3.5 h-3.5" />
                            <McpIcon v-else-if="channelIcon(ch.key).mcp" class="w-3.5 h-3.5 text-gray-600 dark:text-gray-400" />
                            <UIcon v-else :name="channelIcon(ch.key).uicon" class="w-3.5 h-3.5 text-gray-400 dark:text-gray-500" />
                            {{ ch.name }}
                        </span>
                    </div>
                </div>
            </div>

            <!-- Danger zone (last section, no border) -->
            <div v-if="canManageDs">
                <div class="text-[10px] font-semibold uppercase tracking-wider text-gray-400 dark:text-gray-500 mb-2">Danger zone</div>
                <p class="text-[11px] text-gray-400 dark:text-gray-500 mb-2">Removing this agent will disconnect it from the data source. You can reconnect later.</p>
                <button
                    @click="showDelete = true"
                    class="h-8 px-3 rounded-lg border border-red-200 dark:border-red-500/30 text-red-600 dark:text-red-400 text-xs font-medium hover:bg-red-50 dark:hover:bg-red-500/10"
                >
                    Remove agent
                </button>
            </div>
        </div>

        <!-- Add member modal -->
        <UModal v-model="showAddModal" :ui="{ width: 'sm:max-w-md' }">
            <div class="p-4">
                <div class="text-sm font-medium text-gray-900 dark:text-white mb-2">Add members</div>
                <div class="text-xs text-gray-500 dark:text-gray-400 mb-3">Select users or groups to grant access to this agent.</div>

                <!-- Principal type toggle (only shown with enterprise) -->
                <div v-if="addTabs.length > 1" class="flex gap-2 mb-3">
                    <button
                        v-for="tab in addTabs"
                        :key="tab.key"
                        @click="addPrincipalType = tab.key"
                        :class="[
                            'px-3 py-1 text-xs rounded-lg border transition-colors',
                            addPrincipalType === tab.key
                                ? 'bg-gray-100 border-gray-300 text-gray-800 dark:bg-gray-800 dark:border-gray-700 dark:text-gray-200'
                                : 'border-gray-200 text-gray-600 hover:bg-gray-50 dark:border-gray-800 dark:text-gray-400 dark:hover:bg-gray-800/50'
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

                <!-- Permission picker (granular per-agent grants) -->
                <div class="mt-3">
                    <div class="text-[11px] text-gray-400 dark:text-gray-500 mb-1">Permissions</div>
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
                            {{ formatPermission(perm) }}
                        </label>
                    </div>
                </div>

                <div class="flex justify-end gap-2 mt-4">
                    <button @click="showAddModal = false" class="h-8 px-3 rounded-lg border border-gray-200 dark:border-gray-800 text-gray-700 dark:text-gray-300 text-xs font-medium hover:bg-gray-50 dark:hover:bg-gray-800/50">Cancel</button>
                    <button @click="addSelected" :disabled="addDisabled || adding" class="h-8 px-3 rounded-lg bg-gray-900 text-white text-xs font-medium hover:bg-black disabled:opacity-50">
                        {{ adding ? 'Adding…' : 'Add' }}
                    </button>
                </div>
            </div>
        </UModal>

        <!-- Delete confirmation modal -->
        <UModal v-model="showDelete" :ui="{ width: 'sm:max-w-md' }">
            <div class="p-5">
                <div class="text-sm font-medium text-gray-900 dark:text-white">Remove agent?</div>
                <div class="text-xs text-gray-500 dark:text-gray-400 mt-2">This will remove the agent and disconnect it from the data source. You can reconnect later.</div>
                <div class="flex justify-end gap-2 mt-5">
                    <button @click="showDelete = false" class="h-8 px-3 rounded-lg border border-gray-200 dark:border-gray-800 text-gray-700 dark:text-gray-300 text-xs font-medium hover:bg-gray-50 dark:hover:bg-gray-800/50">Cancel</button>
                    <button @click="confirmDelete" :disabled="deleting" class="h-8 px-3 rounded-lg bg-red-500 text-white text-xs font-medium hover:bg-red-600 disabled:opacity-50">
                        {{ deleting ? 'Deleting…' : 'Delete' }}
                    </button>
                </div>
            </div>
        </UModal>
    </div>
</template>

<script setup lang="ts">
import { useCan, usePermissions, useResourcePermissions } from '~/composables/usePermissions'
import { useEnterprise } from '~/ee/composables/useEnterprise'
import Spinner from '~/components/Spinner.vue'
import McpIcon from '~/components/icons/McpIcon.vue'

const props = defineProps<{ agentId: string }>()
const emit = defineEmits(['updated', 'deleted'])

const toast = useToast?.()
const { organization } = useOrganization()
const { data: currentUser } = useAuth()

// Agent detail fetched directly (no parent `integration` provide())
const integration = ref<any>(null)
const loading = ref(true)

const form = reactive({
    name: '',
    isPublic: true
})

const original = reactive({
    name: '',
    isPublic: true
})

const saving = reactive({ name: false, public: false })

// ── Channel availability ─────────────────────────────────────────────────
interface ChannelInfo { key: string; name: string; connected: boolean }
const connectedChannels = ref<ChannelInfo[]>([])
const channelAvailability = ref<Record<string, boolean>>({})
const savingChannels = ref(false)

function channelIcon(key: string): { img?: string; mcp?: boolean; uicon?: string } {
    switch (key) {
        case 'slack': return { img: '/icons/slack.png' }
        case 'teams': return { img: '/icons/teams.png' }
        case 'whatsapp': return { img: '/icons/whatsapp.png' }
        case 'email': return { uicon: 'i-heroicons-envelope' }
        case 'mcp': return { mcp: true }
        default: return { uicon: 'i-heroicons-chat-bubble-left-right' }
    }
}

async function loadChannels() {
    try {
        const { data } = await useMyFetch('/data_sources/connected_channels', { method: 'GET' })
        connectedChannels.value = ((data.value as ChannelInfo[]) || []).filter(c => c.connected)
    } catch {
        connectedChannels.value = []
    }
}

async function onToggleChannel(key: string, enabled: boolean) {
    if (!ready.value || savingChannels.value) return
    savingChannels.value = true
    // Build the full map across connected channels so the saved value is
    // self-contained (default true for any channel not explicitly set).
    const map: Record<string, boolean> = {}
    for (const c of connectedChannels.value) {
        map[c.key] = c.key === key ? enabled : (channelAvailability.value[c.key] !== false)
    }
    const ok = await updateDataSource({ channel_availability: map })
    if (ok) {
        channelAvailability.value = map
        if (integration.value) integration.value.channel_availability = map
        emit('updated')
    }
    savingChannels.value = false
}
const deleting = ref(false)
const ready = computed(() => !loading.value && !!integration.value)
const showDelete = ref(false)
const adding = ref(false)
// Per-DS gates. The panel renders for any user with `view` access to the DS,
// but write controls require explicit per-DS grants.
const dsResource = computed(() => ({ type: 'data_source', id: props.agentId }))
const canManageDs = computed(() => useCan('manage', dsResource.value))
const canManageDsMembers = computed(() => useCan('manage', dsResource.value))

// ── Current user's role/access on THIS agent ─────────────────────────────
// Surfaced so anyone (including the owner) can see their effective role here.
const orgPerms = usePermissions()
const resourcePerms = useResourcePermissions()
const currentUserId = computed(() => (currentUser.value as any)?.id || null)
const currentUserName = computed(() => {
    const u = currentUser.value as any
    return u?.name || u?.email || 'You'
})
const ownerUserId = computed(() => integration.value?.owner_user_id || null)
const isFullAdmin = computed(() => orgPerms.value.includes('full_admin_access'))
const isOwner = computed(() => !!currentUserId.value && currentUserId.value === ownerUserId.value)
const isOwnerPrincipal = (m: MemberGrant) =>
    m.principal_type === 'user' && !!ownerUserId.value && m.principal_id === ownerUserId.value

// What a `manage` grant implies — mirrors RESOURCE_PERM_IMPLIES on the backend.
const MANAGE_IMPLIES = ['manage_instructions', 'create_entities', 'manage_evals', 'manage_members']

const myAccessLabel = computed(() => {
    if (isFullAdmin.value) return 'Admin'
    if (isOwner.value) return 'Owner'
    if (canManageDs.value) return 'Manager'
    return 'Member'
})
const myAccessIsPrivileged = computed(() => isFullAdmin.value || isOwner.value || canManageDs.value)
// Effective per-agent capabilities for the current user, expanding `manage`.
const myEffectivePerms = computed<string[]>(() => {
    if (isFullAdmin.value || canManageDs.value) return ['manage', ...MANAGE_IMPLIES]
    return resourcePerms.value[`data_source:${props.agentId}`] || []
})

const { hasFeature } = useEnterprise()
const isEnterprise = computed(() => hasFeature('custom_roles'))

// Sourced from /permissions/registry to stay in sync with backend (loaded in onMounted)
const dsPermOptions = ref<string[]>([
    'manage_instructions', 'create_entities', 'manage_evals', 'manage', 'manage_members'
])

async function loadDsPermOptions() {
    try {
        const { data } = await useMyFetch('/permissions/registry')
        const reg = data.value as any
        if (reg?.resource_permissions?.data_source) {
            dsPermOptions.value = reg.resource_permissions.data_source
        }
    } catch {
        // keep fallback defaults
    }
}

// ── Member types ────────────────────────────────────────────────────────

interface MemberGrant {
    grant_id: string
    principal_type: string  // "user" | "group"
    principal_id: string
    principal_name?: string
    permissions: string[]
}

const members = ref<MemberGrant[]>([])

// Members other than the current user — the current user is shown separately as
// the highlighted "You" row at the top of the members list, so we don't repeat
// their own grant row here.
const otherMembers = computed(() =>
    members.value.filter(m => !(m.principal_type === 'user' && m.principal_id === currentUserId.value))
)

// User/group/role lookup data
const allUsers = ref<{ id: string; display_name: string; email?: string }[]>([])
const allGroups = ref<{ id: string; name: string; member_count: number }[]>([])
const allRoles = ref<{ id: string; name: string }[]>([])
const expandedGroups = ref<Set<string>>(new Set())
const groupMembers = ref<Record<string, { user_id: string; user_name: string; user_email: string }[]>>({})

// ── Load the agent detail itself ─────────────────────────────────────────

async function fetchIntegration() {
    loading.value = true
    try {
        const { data, error } = await useMyFetch(`/data_sources/${props.agentId}`, { method: 'GET' })
        if (!error?.value) {
            integration.value = data.value
        }
    } catch {
        // ignore
    } finally {
        loading.value = false
    }
}

// Initialize form from fetched agent detail
watch(integration, (ds) => {
    if (ds) {
        form.name = ds?.name || ''
        form.isPublic = ds?.is_public ?? false
        original.name = form.name
        original.isPublic = form.isPublic
        channelAvailability.value = (ds?.channel_availability && typeof ds.channel_availability === 'object')
            ? { ...ds.channel_availability }
            : {}
    }
}, { immediate: true })

// Re-fetch on mount and whenever the agentId changes
watch(() => props.agentId, async () => {
    if (!props.agentId) return
    await Promise.all([
        fetchIntegration(),
        loadMembers(),
        loadUsers(),
        loadGroups(),
        loadRoles(),
        loadDsPermOptions(),
        loadChannels(),
    ])
}, { immediate: true })

// ── Load data ───────────────────────────────────────────────────────────

async function loadMembers() {
    const id = props.agentId
    const { data, error } = await useMyFetch(`/data_sources/${id}/members`, { method: 'GET' })
    if (error?.value) return
    const list = (data.value as any[]) || []
    members.value = list.map((m: any) => ({
        grant_id: m.id,
        principal_type: m.principal_type || 'user',
        principal_id: m.principal_id,
        principal_name: m.principal_name || undefined,
        permissions: m.permissions || [],
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

async function loadRoles() {
    if (!organization.value?.id) return
    try {
        const { data } = await useMyFetch(`/organizations/${organization.value.id}/roles`)
        if (data.value) {
            allRoles.value = (data.value as any[]).map(r => ({ id: r.id, name: r.name }))
        }
    } catch {
        // Roles endpoint may not be available
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

// Mirrors PERMISSION_LABELS in RolesManager so per-DS rows render the same
// human-readable names as the role editor.
const PERMISSION_LABELS: Record<string, string> = {
    manage_instructions: 'Manage instructions',
    create_entities: 'Manage entities',
    manage_evals: 'Manage evals',
    manage: 'Manage settings',
    manage_members: 'Manage members',
    view: 'View',
    view_schema: 'View schema',
}

function formatPermission(perm: string): string {
    if (PERMISSION_LABELS[perm]) return PERMISSION_LABELS[perm]
    return perm.replace(/_/g, ' ').replace(/\b\w/g, c => c.toUpperCase())
}

function principalIcon(m: MemberGrant): string {
    if (m.principal_type === 'group') return 'i-heroicons-user-group'
    if (m.principal_type === 'role') return 'i-heroicons-shield-check'
    return 'i-heroicons-user'
}

function principalDisplayName(m: MemberGrant): string {
    if (m.principal_type === 'group') {
        const group = allGroups.value.find(g => g.id === m.principal_id)
        return group?.name || m.principal_name || 'Unknown group'
    }
    if (m.principal_type === 'role') {
        const role = allRoles.value.find(r => r.id === m.principal_id)
        return role?.name || m.principal_name || 'Unknown role'
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
    const dsId = props.agentId
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
const addPermissions = ref<string[]>([])

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
    addPermissions.value = []
    addPrincipalType.value = 'user'
    showAddModal.value = true
}

async function addSelected() {
    if (addDisabled.value || adding.value) return
    adding.value = true
    const dsId = props.agentId

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
        // Membership POST mints a grant with default (query-only) permissions;
        // apply any picked granular permissions via the resource-grant PUT.
        await loadMembers()
        if (addPermissions.value.length) {
            const targetIds = new Set(principals.map(p => p.principal_id))
            await Promise.all(
                members.value
                    .filter(m => targetIds.has(m.principal_id))
                    .map(m => updateMemberPermissions(m, [...addPermissions.value]))
            )
        }
        toast?.add?.({ title: 'Members added' })
        selectedUsers.value = []
        selectedGroups.value = []
        addPermissions.value = []
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
    const id = props.agentId
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
        if (integration.value) integration.value.name = form.name
        emit('updated')
    }
    saving.name = false
}

async function onTogglePublic(value: boolean) {
    if (!ready.value) return
    saving.public = true
    const ok = await updateDataSource({ is_public: value })
    if (ok) {
        original.isPublic = value
        if (integration.value) integration.value.is_public = value
        emit('updated')
    }
    saving.public = false
}

async function confirmDelete() {
    if (deleting.value) return
    deleting.value = true
    const id = props.agentId
    const { error } = await useMyFetch(`/data_sources/${id}`, { method: 'DELETE' })
    deleting.value = false
    if (!error?.value) {
        toast?.add?.({ title: 'Agent deleted' })
        showDelete.value = false
        emit('deleted')
    } else {
        toast?.add?.({ title: 'Failed to delete', description: String(error.value), color: 'red' })
    }
}
</script>
