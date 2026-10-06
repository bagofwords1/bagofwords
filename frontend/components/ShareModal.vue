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

            <!-- Share link. A dashboard's readable name (/r/{slug}) is edited
                 in its settings (DashboardSettingsModal); here it is shown and copied. -->
            <div v-if="isShared && shareUrl" class="mb-6">
                <div class="flex items-center gap-2">
                    <input :value="shareUrl" type="text" dir="ltr" data-testid="share-link"
                        class="flex-1 h-[32px] px-2.5 border border-gray-200 dark:border-gray-700 rounded-lg text-xs text-gray-500 dark:text-gray-400 bg-gray-50 dark:bg-gray-900 min-w-0"
                        readonly />
                    <button @click="copyLink"
                        class="flex-shrink-0 h-[32px] w-[32px] flex items-center justify-center border border-gray-200 dark:border-gray-700 rounded-lg hover:bg-gray-50 dark:hover:bg-gray-800 text-gray-500 dark:text-gray-400">
                        <Icon :name="copied ? 'heroicons:check' : 'heroicons:clipboard-document'" class="w-3.5 h-3.5" />
                    </button>
                </div>
                <button v-if="perArtifact" type="button" data-testid="change-link-name"
                    class="mt-1 text-[11px] text-blue-600 hover:text-blue-700 dark:text-blue-400"
                    @click="modalOpen = false; emit('edit-link-name')">
                    {{ $t('share.linkNameChange') }}
                </button>
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
                :artifact-id="artifactId || undefined"
                :notification-type="shareType === 'artifact' ? 'share_dashboard' : 'share_conversation'"
                :share-url="shareUrl" />
        </div>
    </UModal>
</template>

<script lang="ts" setup>
import { ref, computed, watch } from 'vue'

const emit = defineEmits<{
    // "Change link name": the name is edited in the dashboard's settings.
    (e: 'edit-link-name'): void
}>()

const props = withDefaults(defineProps<{
    report: any
    shareType: 'artifact' | 'conversation'
    title: string
    compact?: boolean
    // The dashboard being shared (parent artifact id, stable across
    // versions). Dashboards are shared one by one; without it the modal falls
    // back to sharing every dashboard of the report at once.
    artifactId?: string | null
}>(), {
    compact: false,
    artifactId: null,
})

const perArtifact = computed(() => props.shareType === 'artifact' && !!props.artifactId)
// This dashboard's own visibility as last saved (perArtifact only).
const artifactVisibility = ref('none')

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
const currentVisibility = ref('none')
const conversationShareToken = ref<string | null>(null)
const hasRls = ref(false)
// Per-user data (user-scoped sources or RLS): a public link still resolves
// data per signed-in viewer, which the public note says.
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

const shareDescription = computed(() => {
    if (perArtifact.value) return t('share.thisDashboardOnlyDesc')
    return props.shareType === 'artifact'
        ? t('share.shareDashboardDesc')
        : t('share.shareConversationDesc')
})

const selectedOption = computed(() =>
    visibilityOptions.value.find(o => o.value === currentVisibility.value) || visibilityOptions.value[0]
)

const buttonLabel = computed(() => {
    if (!isShared.value) return props.shareType === 'artifact' ? t('share.shareDashboard') : t('share.share')
    return selectedOption.value.label
})

const buttonIcon = computed(() => selectedOption.value.icon)

// Readable share-link name for this dashboard (/r/{slug}), edited in the
// dashboard's settings; read from its sharing state.
const linkSlug = ref<string | null>(null)
const linkPrefix = computed(() => `${window.location.origin}/r/`)

const shareUrl = computed(() => {
    if (props.shareType === 'artifact') {
        if (perArtifact.value && linkSlug.value) return `${linkPrefix.value}${linkSlug.value}`
        const base = `${window.location.origin}/r/${props.report.id}`
        return perArtifact.value ? `${base}?artifact=${encodeURIComponent(props.artifactId as string)}` : base
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
            if (!perArtifact.value) currentVisibility.value = data[visibilityField.value] || 'none'
            hasRls.value = !!data.has_rls
            hasUserScoped.value = !!data.has_user_scoped
            if (data.conversation_share_token !== undefined) {
                conversationShareToken.value = data.conversation_share_token
                if (props.report) props.report.conversation_share_token = data.conversation_share_token
            }
        }
    } catch { /* silent */ }
}

const fetchShares = async () => {
    if (perArtifact.value) return fetchArtifactSharing()
    try {
        const res = await useMyFetch(`/reports/${props.report.id}/shares/${props.shareType}`)
        if (res.data.value) {
            sharedEntries.value = res.data.value as any[]
        }
    } catch { /* silent */ }
}

// One dashboard's visibility + people. Owner-only endpoint: anyone else
// (an admin or project viewer looking at the report) just sees no state.
const fetchArtifactSharing = async () => {
    const artifactId = props.artifactId
    if (!artifactId) return
    try {
        const res = await useMyFetch(`/reports/${props.report.id}/artifacts/${artifactId}/sharing`)
        // The user may have switched dashboards while this was in flight.
        if (res.error.value || !res.data.value || artifactId !== props.artifactId) return
        const data = res.data.value as any
        artifactVisibility.value = data.visibility || 'none'
        currentVisibility.value = artifactVisibility.value
        linkSlug.value = data.slug ?? null
        sharedEntries.value = data.shares || []
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
        const url = perArtifact.value
            ? `/reports/${props.report.id}/artifacts/${props.artifactId}/visibility`
            : `/reports/${props.report.id}/visibility/${props.shareType}`
        const res = await useMyFetch(url, {
            method: 'PUT',
            body,
        })
        if (res.error.value) throw res.error.value

        if (perArtifact.value) {
            artifactVisibility.value = visibility
            // The report keeps the most open of its dashboards' settings.
            const reportVisibility = (res.data.value as any)?.report_artifact_visibility
            if (props.report && reportVisibility) props.report.artifact_visibility = reportVisibility
        } else if (props.report) {
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

const onVisibilityChange = async (value: string) => {
    const prev = perArtifact.value ? artifactVisibility.value : (props.report?.[visibilityField.value] || 'none')
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
    currentVisibility.value = perArtifact.value ? artifactVisibility.value : (props.report?.[visibilityField.value] || 'none')
    conversationShareToken.value = props.report?.conversation_share_token ?? null
    await Promise.all([fetchMembers(), fetchGroups(), fetchVisibility(), fetchShares()])
}

// Keep button in sync when report data loads/changes (e.g. after page reload)
watch(
    () => props.report?.[visibilityField.value],
    (val) => {
        if (val && !modalOpen.value && !perArtifact.value) {
            currentVisibility.value = val
        }
    },
    { immediate: true }
)

// Per dashboard, the button reflects the selected dashboard's own sharing.
watch(
    () => props.artifactId,
    () => {
        if (!perArtifact.value) return
        artifactVisibility.value = 'none'
        if (!modalOpen.value) currentVisibility.value = 'none'
        sharedEntries.value = []
        linkSlug.value = null
        fetchArtifactSharing()
    },
    { immediate: true }
)
</script>
