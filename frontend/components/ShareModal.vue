<template>
    <UTooltip :text="buttonLabel">
        <button @click="openModal"
            :class="[
                'items-center flex gap-1 hover:bg-gray-100 dark:hover:bg-gray-700 rounded border border-gray-200 dark:border-gray-700 bg-gray-50 dark:bg-gray-900 text-gray-600 dark:text-gray-400',
                compact ? 'p-1.5' : 'px-2 py-1 text-xs'
            ]">
            <div class="relative">
                <Icon :name="buttonIcon" :class="compact ? 'w-4 h-4' : 'w-3.5 h-3.5'" />
                <span v-if="isShared" class="absolute -top-0.5 -end-0.5 w-1.5 h-1.5 bg-green-500 rounded-full"></span>
            </div>
            <span v-if="!compact" class="text-xs whitespace-nowrap">{{ buttonLabel }}</span>
        </button>
    </UTooltip>

    <UModal v-model="modalOpen" :ui="{ width: 'sm:max-w-md' }">
        <div class="p-6">
            <!-- Header -->
            <div class="flex items-center justify-between mb-1">
                <h2 class="text-base font-semibold text-gray-900 dark:text-white">{{ title }}</h2>
                <button @click="modalOpen = false"
                    class="text-gray-400 hover:text-gray-600 dark:hover:text-gray-300 outline-none">
                    <Icon name="heroicons:x-mark" class="w-5 h-5" />
                </button>
            </div>
            <p class="text-sm text-gray-400 mb-6">{{ shareDescription }}</p>

            <!-- Visibility dropdown -->
            <label class="text-xs font-medium text-gray-500 dark:text-gray-400 uppercase tracking-wider mb-2 block">{{ $t('share.access') }}</label>
            <USelectMenu
                v-model="currentVisibility"
                :options="visibilityOptions"
                value-attribute="value"
                option-attribute="label"
                size="xs"
                class="mb-5"
                :ui="{ rounded: 'rounded-lg', size: { xs: 'text-xs' }, padding: { xs: 'px-2.5 py-1.5' } }"
                @change="onVisibilityChange"
            >
                <template #label>
                    <div class="flex items-center gap-2 text-xs">
                        <Icon :name="selectedOption.icon" class="w-3.5 h-3.5 text-gray-500 dark:text-gray-400 flex-shrink-0" />
                        <span>{{ selectedOption.label }}</span>
                    </div>
                </template>
                <template #option="{ option }">
                    <div class="flex items-start gap-3 py-1 px-1">
                        <Icon :name="option.icon" class="w-4 h-4 text-gray-400 flex-shrink-0 mt-0.5" />
                        <div class="flex flex-col">
                            <span class="text-xs">{{ option.label }}</span>
                            <span class="text-[11px] text-gray-400">{{ option.description }}</span>
                        </div>
                    </div>
                </template>
            </USelectMenu>

            <!-- Public link on a per-user-data dashboard: the link is open to
                 anyone, but data resolves per signed-in viewer — say so. -->
            <p v-if="shareType === 'artifact' && currentVisibility === 'public' && showRunIdentity"
                class="text-[11px] text-gray-400 -mt-3 mb-5">
                {{ $t('share.publicPerUserNote') }}
            </p>

            <!-- Share link -->
            <div v-if="isShared && shareUrl" class="flex items-center gap-2 mb-6">
                <input :value="shareUrl" type="text"
                    class="flex-1 h-[32px] px-2.5 border border-gray-200 dark:border-gray-700 rounded-lg text-xs text-gray-500 dark:text-gray-400 bg-gray-50 dark:bg-gray-900 min-w-0"
                    readonly />
                <button @click="copyLink"
                    class="flex-shrink-0 h-[32px] w-[32px] flex items-center justify-center border border-gray-200 dark:border-gray-700 rounded-lg hover:bg-gray-50 dark:hover:bg-gray-800 text-gray-500 dark:text-gray-400">
                    <Icon :name="copied ? 'heroicons:check' : 'heroicons:clipboard-document'" class="w-3.5 h-3.5" />
                </button>
            </div>

            <!-- Include Data Tab option (dashboards only): whether viewers of
                 the shared artifact page see the Data tab listing the queries
                 behind the report. The conversation share has no such tab. -->
            <div v-if="shareType === 'artifact' && isShared" class="flex items-start justify-between gap-3 mb-6 p-3 bg-gray-50 dark:bg-gray-800 rounded-lg">
                <div class="flex flex-col min-w-0 flex-1">
                    <span class="text-xs font-medium text-gray-700 dark:text-gray-300">{{ $t('share.includeDataTab') }}</span>
                    <span class="text-[11px] text-gray-400">{{ $t('share.includeDataTabDesc') }}</span>
                </div>
                <UToggle v-model="includeDataTab" size="sm" :disabled="isSaving" class="flex-shrink-0 mt-0.5"
                    @update:model-value="onIncludeDataTabChange" />
            </div>

            <!-- Chat on the shared artifact page (dashboards only): the owner's
                 toggle plus the agent scope viewer chat may query. Viewer
                 threads are private per viewer and never touch this report's
                 own conversation. -->
            <div v-if="shareType === 'artifact' && isShared" class="mb-6 p-3 bg-gray-50 dark:bg-gray-800 rounded-lg" data-testid="chat-settings">
                <div class="flex items-start justify-between gap-3">
                    <div class="flex flex-col min-w-0">
                        <span class="text-xs font-medium text-gray-700 dark:text-gray-300">{{ $t('share.allowChat') }}</span>
                        <span class="text-[11px] text-gray-400">{{ $t('share.allowChatDesc') }}</span>
                    </div>
                    <UToggle v-model="chatEnabled" size="sm" :disabled="isSaving" class="flex-shrink-0 mt-0.5"
                        data-testid="chat-enabled-toggle" @update:model-value="onChatEnabledChange" />
                </div>
                <div v-if="chatEnabled" class="mt-3 space-y-2">
                    <label class="text-[11px] font-medium text-gray-500 dark:text-gray-400 uppercase tracking-wider block">{{ $t('share.chatScope') }}</label>
                    <label class="flex items-start gap-2 cursor-pointer" data-testid="chat-scope-agents">
                        <input type="radio" value="agents" v-model="chatScope" :disabled="isSaving || reportAgents.length === 0"
                            class="mt-0.5" @change="onChatScopeChange" />
                        <span class="flex flex-col">
                            <span class="text-xs text-gray-700 dark:text-gray-300">{{ $t('share.chatScopeAgents') }}</span>
                            <span class="text-[11px] text-gray-400">{{ $t('share.chatScopeAgentsDesc') }}</span>
                        </span>
                    </label>
                    <div v-if="chatScope === 'agents' && reportAgents.length > 0" class="ms-6 space-y-1">
                        <label v-for="agent in reportAgents" :key="agent.id" class="flex items-center gap-2 cursor-pointer">
                            <input type="checkbox" :value="agent.id" v-model="chatAgentIds" :disabled="isSaving"
                                @change="onChatAgentsChange" />
                            <span class="text-xs text-gray-600 dark:text-gray-400">{{ agent.name }}</span>
                        </label>
                    </div>
                    <label class="flex items-start gap-2 cursor-pointer" data-testid="chat-scope-data-only">
                        <input type="radio" value="data_only" v-model="chatScope" :disabled="isSaving"
                            class="mt-0.5" @change="onChatScopeChange" />
                        <span class="flex flex-col">
                            <span class="text-xs text-gray-700 dark:text-gray-300">{{ $t('share.chatScopeDataOnly') }}</span>
                            <span class="text-[11px] text-gray-400">{{ $t('share.chatScopeDataOnlyDesc') }}</span>
                        </span>
                    </label>
                    <p v-if="reportAgents.length === 0" class="text-[11px] text-gray-400">{{ $t('share.chatNoAgents') }}</p>
                </div>
            </div>

            <!-- Viewer run identity (dashboards only): whose credentials a
                 viewer's "Run" uses. Results are always stored per viewer.
                 Only shown when the choice exists — user-scoped sources
                 (toggleable) or RLS (visible but disabled, to explain why
                 runs are always per-viewer). -->
            <div v-if="shareType === 'artifact' && isShared && showRunIdentity" class="flex items-start justify-between gap-3 mb-6">
                <div class="flex flex-col min-w-0">
                    <span class="text-xs font-medium text-gray-700 dark:text-gray-300">{{ $t('share.runOnBehalf') }}</span>
                    <span class="text-[11px] text-gray-400">{{ hasRls ? $t('share.runOnBehalfRlsDisabled') : $t('share.runOnBehalfDesc') }}</span>
                </div>
                <UToggle v-model="runAsCreator" size="sm" :disabled="isSaving || hasRls" class="flex-shrink-0 mt-0.5" @update:model-value="onRunIdentityChange" />
            </div>

            <!-- Share with people (only when 'shared' selected) -->
            <div v-if="currentVisibility === 'shared'">
                <label class="text-xs font-medium text-gray-500 dark:text-gray-400 uppercase tracking-wider mb-2 block">{{ $t('share.shareWith') }}</label>
                <div class="flex items-start gap-2 mb-4">
                    <div class="flex-1 flex flex-wrap items-center gap-1.5 border border-gray-200 dark:border-gray-700 rounded-lg px-2.5 py-1.5 min-h-[32px] focus-within:ring-2 focus-within:ring-blue-500 focus-within:border-blue-500 bg-white dark:bg-gray-900">
                        <span v-for="(principal, idx) in pendingPrincipals" :key="principal.kind + ':' + (principal.id || principal.email)"
                            class="inline-flex items-center gap-1 bg-blue-50 dark:bg-blue-950 text-blue-700 text-xs px-2 py-0.5 rounded-full whitespace-nowrap">
                            <Icon v-if="principal.kind === 'group'" name="heroicons:user-group" class="w-3 h-3" />
                            {{ principal.name || principal.email }}
                            <button @click="removePendingPrincipal(idx)" class="hover:text-red-500 outline-none">
                                <Icon name="heroicons:x-mark" class="w-3 h-3" />
                            </button>
                        </span>
                        <div class="relative flex-1 min-w-[120px]">
                            <input ref="inputRef" v-model="inputValue" type="text"
                                class="w-full border-none outline-none text-xs bg-transparent p-0"
                                :placeholder="groups.length > 0 ? $t('share.nameEmailOrGroup') : $t('share.nameOrEmail')"
                                @keydown.enter.prevent="handleEnter"
                                @keydown.,.prevent="handleComma"
                                @keydown.backspace="handleBackspace"
                                @input="onInput"
                                @focus="showDropdown = true"
                                @blur="onBlur" />
                            <div v-if="showDropdown && filteredOptions.length > 0"
                                class="absolute start-0 top-full mt-1 w-64 bg-white dark:bg-gray-900 border border-gray-200 dark:border-gray-700 rounded-lg shadow-lg z-50 max-h-40 overflow-y-auto">
                                <button v-for="option in filteredOptions" :key="option.kind + ':' + option.id"
                                    class="w-full text-start px-3 py-2 text-sm hover:bg-gray-50 dark:hover:bg-gray-800 flex items-center gap-2.5"
                                    @mousedown.prevent="addPrincipal(option)">
                                    <div class="w-6 h-6 bg-gray-200 dark:bg-gray-700 rounded-full flex items-center justify-center text-xs font-medium text-gray-600 dark:text-gray-400 flex-shrink-0">
                                        <Icon v-if="option.kind === 'group'" name="heroicons:user-group" class="w-3.5 h-3.5" />
                                        <template v-else>{{ (option.name || option.email).charAt(0).toUpperCase() }}</template>
                                    </div>
                                    <div class="flex flex-col min-w-0">
                                        <span class="text-gray-900 dark:text-white truncate">{{ option.name || option.email }}</span>
                                        <span v-if="option.kind === 'group'" class="text-xs text-gray-400 truncate">{{ $t('share.groupMembers', option.memberCount || 0) }}</span>
                                        <span v-else-if="option.name" class="text-xs text-gray-400 truncate">{{ option.email }}</span>
                                    </div>
                                </button>
                            </div>
                        </div>
                    </div>
                    <button @click="invitePrincipals" :disabled="pendingPrincipals.length === 0 || isSaving"
                        class="flex-shrink-0 px-3 h-[32px] text-xs font-medium text-white bg-blue-600 rounded-lg hover:bg-blue-700 disabled:opacity-40 disabled:cursor-not-allowed">
                        {{ $t('share.share') }}
                    </button>
                </div>

                <!-- People and groups with access -->
                <div v-if="sharedEntries.length > 0" class="space-y-0.5">
                    <div v-for="entry in sharedEntries" :key="entry.id"
                        class="flex items-center justify-between py-2 px-2 rounded-lg hover:bg-gray-50 dark:hover:bg-gray-800 group">
                        <div class="flex items-center gap-2.5">
                            <div class="w-7 h-7 bg-gray-100 dark:bg-gray-800 rounded-full flex items-center justify-center text-xs font-medium text-gray-600 dark:text-gray-400">
                                <Icon v-if="entry.principal_type === 'group'" name="heroicons:user-group" class="w-4 h-4" />
                                <template v-else>{{ (entry.user_name || entry.user_email || '?').charAt(0).toUpperCase() }}</template>
                            </div>
                            <div class="flex flex-col">
                                <span class="text-sm text-gray-700 dark:text-gray-300">{{ entry.principal_type === 'group' ? entry.group_name : (entry.user_name || entry.user_email) }}</span>
                                <span v-if="entry.principal_type === 'group'" class="text-xs text-gray-400">{{ $t('share.groupMembers', entry.member_count || 0) }}</span>
                                <span v-else-if="entry.user_name && entry.user_email" class="text-xs text-gray-400">{{ entry.user_email }}</span>
                            </div>
                        </div>
                        <button @click="removeSharedEntry(entry)"
                            class="opacity-0 group-hover:opacity-100 text-gray-400 hover:text-red-500 transition-opacity p-1">
                            <Icon name="heroicons:x-mark" class="w-3.5 h-3.5" />
                        </button>
                    </div>
                </div>
            </div>

            <!-- Notify recipients (optional email) -->
            <NotifyRecipientPicker
                v-if="smtpEnabled && isShared"
                :report-id="report.id"
                :notification-type="shareType === 'artifact' ? 'share_dashboard' : 'share_conversation'"
                :share-url="shareUrl" />
        </div>
    </UModal>
</template>

<script lang="ts" setup>
import { ref, computed, watch } from 'vue'

const props = withDefaults(defineProps<{
    report: any
    shareType: 'artifact' | 'conversation'
    title: string
    compact?: boolean
}>(), {
    compact: false,
})

const toast = useToast()
const { t } = useI18n()
const { smtpEnabled } = useAppSettings()
const modalOpen = ref(false)
const isSaving = ref(false)
const inputRef = ref<HTMLInputElement | null>(null)
const inputValue = ref('')
const showDropdown = ref(false)
// Pending picks not yet saved: individual users or whole groups
const pendingPrincipals = ref<{ kind: 'user' | 'group'; id?: string; name?: string; email?: string; memberCount?: number }[]>([])
const sharedEntries = ref<any[]>([])
const copied = ref(false)
// Include Data Tab when sharing (show historical execution data)
const includeDataTab = ref(true)

// Chat on the shared artifact page: toggle + agent scope. Scope maps onto the
// backend's artifact_chat_data_source_ids: 'agents' with everything checked =
// null (inherit the roster, sent as the ["*"] reset sentinel), a subset = that
// list, 'data_only' = [].
const chatEnabled = ref(false)
const chatScope = ref<'agents' | 'data_only'>('agents')
const chatAgentIds = ref<string[]>([])
const reportAgents = ref<{ id: string; name: string }[]>([])

const currentVisibility = ref('none')
const conversationShareToken = ref<string | null>(null)
// Whose credentials a shared-dashboard viewer's "Run" uses:
// off = the viewer's own ('viewer'), on = on behalf of the owner ('creator')
const runAsCreator = ref(false)
// RLS dashboards force per-viewer identity — creator mode is disabled.
const hasRls = ref(false)
// Only user-scoped (user_required) sources make the run-identity toggle
// meaningful; on system-only credentials both identities resolve to the same
// credentials, so the control is hidden. RLS still shows it (disabled) to
// explain why runs are always per-viewer.
const hasUserScoped = ref(false)
const showRunIdentity = computed(() => hasUserScoped.value || hasRls.value)

const visibilityOptions = computed(() => [
    { value: 'none', label: t('share.visibilityPrivate'), description: t('share.visibilityPrivateDesc'), icon: 'heroicons:lock-closed' },
    { value: 'shared', label: t('share.visibilityShared'), description: t('share.visibilitySharedDesc'), icon: 'heroicons:user-group' },
    { value: 'internal', label: t('share.visibilityInternal'), description: t('share.visibilityInternalDesc'), icon: 'heroicons:building-office' },
    { value: 'public', label: t('share.visibilityPublic'), description: t('share.visibilityPublicDesc'), icon: 'heroicons:globe-alt' },
])

const visibilityField = computed(() =>
    props.shareType === 'artifact' ? 'artifact_visibility' : 'conversation_visibility'
)

const isShared = computed(() => currentVisibility.value !== 'none')

const shareDescription = computed(() =>
    props.shareType === 'artifact'
        ? t('share.shareDashboardDesc')
        : t('share.shareConversationDesc')
)

const selectedOption = computed(() =>
    visibilityOptions.value.find(o => o.value === currentVisibility.value) || visibilityOptions.value[0]
)

const buttonLabel = computed(() => {
    if (!isShared.value) return props.shareType === 'artifact' ? t('share.shareDashboard') : t('share.share')
    return selectedOption.value.label
})

const buttonIcon = computed(() => selectedOption.value.icon)

const shareUrl = computed(() => {
    if (props.shareType === 'artifact') {
        return `${window.location.origin}/r/${props.report.id}`
    }
    const token = conversationShareToken.value || props.report?.conversation_share_token
    return token ? `${window.location.origin}/c/${token}` : ''
})

// Org members and groups for autocomplete
const members = ref<{ id: string; name: string; email: string }[]>([])
const groups = ref<{ id: string; name: string; memberCount: number }[]>([])
const fetchMembers = async () => {
    try {
        const res = await useMyFetch('/organization/members')
        if (res.data.value) {
            members.value = (res.data.value as any[]).map((u: any) => ({
                id: u.id,
                name: u.name || '',
                email: u.email,
            }))
        }
    } catch { /* silent */ }
}

const fetchGroups = async () => {
    try {
        const res = await useMyFetch('/organization/groups')
        if (res.data.value) {
            groups.value = (res.data.value as any[]).map((g: any) => ({
                id: g.id,
                name: g.name || '',
                memberCount: g.member_count || 0,
            }))
        }
    } catch { /* silent */ }
}

const filteredOptions = computed(() => {
    const q = inputValue.value.toLowerCase().trim()
    if (!q) return []
    const existingUserIds = new Set([
        ...sharedEntries.value.filter(e => e.principal_type !== 'group').map(e => e.user_id),
        ...pendingPrincipals.value.filter(p => p.kind === 'user').map(p => p.id),
    ])
    const existingGroupIds = new Set([
        ...sharedEntries.value.filter(e => e.principal_type === 'group').map(e => e.group_id),
        ...pendingPrincipals.value.filter(p => p.kind === 'group').map(p => p.id),
    ])
    const groupMatches = groups.value.filter(
        g => !existingGroupIds.has(g.id) && g.name.toLowerCase().includes(q)
    ).map(g => ({ kind: 'group' as const, id: g.id, name: g.name, email: '', memberCount: g.memberCount }))
    const userMatches = members.value.filter(
        m => !existingUserIds.has(m.id) &&
            m.id !== props.report?.user?.id &&
            (m.email.toLowerCase().includes(q) || m.name.toLowerCase().includes(q))
    ).map(m => ({ kind: 'user' as const, id: m.id, name: m.name, email: m.email, memberCount: 0 }))
    return [...groupMatches, ...userMatches].slice(0, 6)
})

const isValidEmail = (email: string) => /^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(email)

const addPrincipal = (option: { kind: 'user' | 'group'; id: string; name: string; email?: string; memberCount?: number }) => {
    if (!pendingPrincipals.value.find(p => p.kind === option.kind && p.id === option.id)) {
        pendingPrincipals.value.push({ kind: option.kind, id: option.id, name: option.name, email: option.email, memberCount: option.memberCount })
    }
    inputValue.value = ''
    showDropdown.value = false
}

const addEmailAsPending = (email: string) => {
    const clean = email.trim().toLowerCase()
    if (!clean) return
    if (!isValidEmail(clean)) {
        // Not an email — maybe an exact group name was typed.
        const group = groups.value.find(g => g.name.toLowerCase() === clean)
        if (group) addPrincipal({ kind: 'group', ...group })
        return
    }
    const member = members.value.find(m => m.email.toLowerCase() === clean)
    if (member) {
        addPrincipal({ kind: 'user', ...member })
    } else {
        toast.add({ title: t('share.userNotFound'), color: 'orange' })
    }
}

const removePendingPrincipal = (idx: number) => pendingPrincipals.value.splice(idx, 1)

const handleEnter = () => {
    if (filteredOptions.value.length > 0) {
        addPrincipal(filteredOptions.value[0])
    } else {
        addEmailAsPending(inputValue.value)
    }
}

const handleComma = () => addEmailAsPending(inputValue.value)

const handleBackspace = () => {
    if (!inputValue.value && pendingPrincipals.value.length > 0) pendingPrincipals.value.pop()
}

const onInput = () => { showDropdown.value = true }

const onBlur = () => {
    setTimeout(() => {
        showDropdown.value = false
        if (inputValue.value && isValidEmail(inputValue.value)) addEmailAsPending(inputValue.value)
    }, 200)
}

// API calls
const fetchVisibility = async () => {
    try {
        const res = await useMyFetch(`/reports/${props.report.id}`, { method: 'GET' })
        if (res.data.value) {
            const data = res.data.value as any
            currentVisibility.value = data[visibilityField.value] || 'none'
            hasRls.value = !!data.has_rls
            hasUserScoped.value = !!data.has_user_scoped
            if (data.shared_run_identity !== undefined) {
                runAsCreator.value = data.shared_run_identity === 'creator'
                if (props.report) props.report.shared_run_identity = data.shared_run_identity
            }
            if (data.include_data_tab !== undefined) {
                includeDataTab.value = data.include_data_tab !== false
                if (props.report) props.report.include_data_tab = data.include_data_tab
            }
            if (data.artifact_chat_enabled !== undefined) {
                chatEnabled.value = data.artifact_chat_enabled === true
                if (props.report) props.report.artifact_chat_enabled = data.artifact_chat_enabled
            }
            // Candidate agents = what the report actually uses (attached
            // roster, or recovered from its runs for Auto reports) — not the
            // raw attachment list, which is empty under Auto.
            try {
                const agentsRes = await useMyFetch(`/reports/${props.report.id}/artifact_chat/agents`)
                const payload = agentsRes.data.value as any
                reportAgents.value = (payload?.agents || []).map((a: any) => ({ id: a.id, name: a.name }))
            } catch {
                reportAgents.value = (data.data_sources || []).map((ds: any) => ({ id: ds.id, name: ds.name }))
            }
            const storedIds = data.artifact_chat_data_source_ids
            if (storedIds === null || storedIds === undefined) {
                chatScope.value = reportAgents.value.length > 0 ? 'agents' : 'data_only'
                chatAgentIds.value = reportAgents.value.map(a => a.id)
            } else if (Array.isArray(storedIds) && storedIds.length === 0) {
                chatScope.value = 'data_only'
                chatAgentIds.value = reportAgents.value.map(a => a.id)
            } else {
                chatScope.value = 'agents'
                chatAgentIds.value = storedIds
            }
            if (data.conversation_share_token !== undefined) {
                conversationShareToken.value = data.conversation_share_token
                if (props.report) props.report.conversation_share_token = data.conversation_share_token
            }
        }
    } catch { /* silent */ }
}

const fetchShares = async () => {
    try {
        const res = await useMyFetch(`/reports/${props.report.id}/shares/${props.shareType}`)
        if (res.data.value) {
            sharedEntries.value = res.data.value as any[]
        }
    } catch { /* silent */ }
}

const sharedUserIds = () => sharedEntries.value
    .filter(e => e.principal_type !== 'group')
    .map(e => e.user_id)
const sharedGroupIds = () => sharedEntries.value
    .filter(e => e.principal_type === 'group')
    .map(e => e.group_id)

const saveVisibility = async (visibility: string, userIds?: string[], groupIds?: string[]) => {
    isSaving.value = true
    try {
        const body: any = { visibility }
        if (userIds) body.shared_user_ids = userIds
        if (groupIds) body.shared_group_ids = groupIds
        // include_data_tab is deliberately NOT sent here. It has its own write
        // path (onIncludeDataTabChange), and omitting it means the backend
        // leaves the stored value alone — so a visibility change or an invite
        // can never overwrite the setting with a stale local ref.
        const res = await useMyFetch(`/reports/${props.report.id}/visibility/${props.shareType}`, {
            method: 'PUT',
            body,
        })
        if (res.error.value) throw res.error.value

        if (props.report) {
            props.report[visibilityField.value] = visibility
        }
        // Surface the resulting report_shared / artifact_shared session-event strip.
        window.dispatchEvent(new CustomEvent('report:mutated', { detail: { reportId: props.report?.id, kind: 'share' } }))

        const data = res.data.value as any
        if (props.shareType === 'conversation' && data) {
            const token = data.conversation_share_token ?? null
            conversationShareToken.value = token
            if (props.report) props.report.conversation_share_token = token
        }

        toast.add({
            title: visibility === 'none' ? t('share.sharingDisabled') : t('share.sharingUpdated'),
            color: 'green',
        })
    } catch {
        toast.add({ title: t('share.sharingFailed'), color: 'red' })
    } finally {
        isSaving.value = false
    }
}

const onRunIdentityChange = async (value: boolean) => {
    const identity = value ? 'creator' : 'viewer'
    isSaving.value = true
    try {
        const res = await useMyFetch(`/reports/${props.report.id}/visibility/artifact`, {
            method: 'PUT',
            // Re-sends the current visibility unchanged; omitting
            // shared_user_ids leaves the recipient list untouched.
            body: { visibility: currentVisibility.value, run_identity: identity },
        })
        if (res.error.value) throw res.error.value
        if (props.report) props.report.shared_run_identity = identity
        toast.add({ title: t('share.sharingUpdated'), color: 'green' })
    } catch {
        runAsCreator.value = identity !== 'creator'
        toast.add({ title: t('share.sharingFailed'), color: 'red' })
    } finally {
        isSaving.value = false
    }
}

const onIncludeDataTabChange = async (value: boolean) => {
    isSaving.value = true
    try {
        const res = await useMyFetch(`/reports/${props.report.id}/visibility/artifact`, {
            method: 'PUT',
            // Re-sends the current visibility unchanged; omitting
            // shared_user_ids leaves the recipient list untouched.
            body: { visibility: currentVisibility.value, include_data_tab: value },
        })
        if (res.error.value) throw res.error.value
        if (props.report) props.report.include_data_tab = value
        toast.add({ title: t('share.sharingUpdated'), color: 'green' })
    } catch {
        // Put the box back where it was — the setting did not change.
        includeDataTab.value = !value
        toast.add({ title: t('share.sharingFailed'), color: 'red' })
    } finally {
        isSaving.value = false
    }
}

// One PUT per chat-settings change, mirroring onIncludeDataTabChange: resend
// the current visibility unchanged and only the field being written.
const saveChatSettings = async (body: Record<string, any>, revert: () => void) => {
    isSaving.value = true
    try {
        const res = await useMyFetch(`/reports/${props.report.id}/visibility/artifact`, {
            method: 'PUT',
            body: { visibility: currentVisibility.value, ...body },
        })
        if (res.error.value) throw res.error.value
        toast.add({ title: t('share.sharingUpdated'), color: 'green' })
    } catch {
        revert()
        toast.add({ title: t('share.sharingFailed'), color: 'red' })
    } finally {
        isSaving.value = false
    }
}

const chatScopeIdsPayload = (): string[] => {
    if (chatScope.value === 'data_only') return []
    const all = reportAgents.value.map(a => a.id)
    const picked = chatAgentIds.value.filter(id => all.includes(id))
    // Everything checked = inherit the roster (["*"] resets to null server-side).
    if (picked.length === all.length && all.length > 0) return ['*']
    return picked
}

const onChatEnabledChange = async (value: boolean) => {
    if (props.report) props.report.artifact_chat_enabled = value
    await saveChatSettings(
        { artifact_chat_enabled: value },
        () => { chatEnabled.value = !value; if (props.report) props.report.artifact_chat_enabled = !value },
    )
}

const onChatScopeChange = async () => {
    if (chatScope.value === 'agents' && chatAgentIds.value.length === 0) {
        chatAgentIds.value = reportAgents.value.map(a => a.id)
    }
    await saveChatSettings({ artifact_chat_data_source_ids: chatScopeIdsPayload() }, () => {})
}

const onChatAgentsChange = async () => {
    await saveChatSettings({ artifact_chat_data_source_ids: chatScopeIdsPayload() }, () => {})
}

const onVisibilityChange = async (value: string) => {
    const prev = props.report?.[visibilityField.value] || 'none'
    if (value === prev) return

    const userIds = value === 'shared' ? sharedUserIds() : undefined
    const groupIds = value === 'shared' ? sharedGroupIds() : undefined
    await saveVisibility(value, userIds, groupIds)
}

const invitePrincipals = async () => {
    if (pendingPrincipals.value.length === 0) return

    if (currentVisibility.value === 'none') {
        currentVisibility.value = 'shared'
    }

    const allUserIds = [
        ...sharedUserIds(),
        ...pendingPrincipals.value.filter(p => p.kind === 'user').map(p => p.id).filter(Boolean),
    ]
    const allGroupIds = [
        ...sharedGroupIds(),
        ...pendingPrincipals.value.filter(p => p.kind === 'group').map(p => p.id).filter(Boolean),
    ]

    await saveVisibility(currentVisibility.value === 'shared' ? 'shared' : currentVisibility.value, allUserIds, allGroupIds)
    await fetchShares()
    pendingPrincipals.value = []
}

const removeSharedEntry = async (entry: any) => {
    const remainingUsers = sharedEntries.value
        .filter(e => e.id !== entry.id && e.principal_type !== 'group')
        .map(e => e.user_id)
    const remainingGroups = sharedEntries.value
        .filter(e => e.id !== entry.id && e.principal_type === 'group')
        .map(e => e.group_id)

    if (remainingUsers.length === 0 && remainingGroups.length === 0 && currentVisibility.value === 'shared') {
        currentVisibility.value = 'none'
        await saveVisibility('none')
    } else {
        await saveVisibility('shared', remainingUsers, remainingGroups)
    }

    await fetchShares()
}

const copyLink = async () => {
    try {
        await navigator.clipboard.writeText(shareUrl.value)
        copied.value = true
        setTimeout(() => { copied.value = false }, 2000)
    } catch {
        toast.add({ title: t('share.copyFailed'), color: 'red' })
    }
}

const openModal = async () => {
    modalOpen.value = true
    currentVisibility.value = props.report?.[visibilityField.value] || 'none'
    conversationShareToken.value = props.report?.conversation_share_token ?? null
    runAsCreator.value = props.report?.shared_run_identity === 'creator'
    includeDataTab.value = props.report?.include_data_tab !== false
    chatEnabled.value = props.report?.artifact_chat_enabled === true
    await Promise.all([fetchMembers(), fetchGroups(), fetchVisibility(), fetchShares()])
}

// Keep button in sync when report data loads/changes (e.g. after page reload)
watch(
    () => props.report?.[visibilityField.value],
    (val) => {
        if (val && !modalOpen.value) {
            currentVisibility.value = val
        }
    },
    { immediate: true }
)
</script>
