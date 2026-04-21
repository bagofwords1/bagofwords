<template>
    <UTooltip :text="buttonLabel">
        <button @click="openModal"
            :class="[
                'items-center flex gap-1 hover:bg-gray-100 rounded border border-gray-200 bg-gray-50 text-gray-600',
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
                <h2 class="text-base font-semibold text-gray-900">{{ title }}</h2>
                <button @click="modalOpen = false"
                    class="text-gray-400 hover:text-gray-600 outline-none">
                    <Icon name="heroicons:x-mark" class="w-5 h-5" />
                </button>
            </div>
            <p class="text-sm text-gray-400 mb-6">{{ shareDescription }}</p>

            <!-- Visibility dropdown -->
            <label class="text-xs font-medium text-gray-500 uppercase tracking-wider mb-2 block">Access</label>
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
                        <Icon :name="selectedOption.icon" class="w-3.5 h-3.5 text-gray-500 flex-shrink-0" />
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

            <!-- Share link -->
            <div v-if="isShared && shareUrl" class="flex items-center gap-2 mb-6">
                <input :value="shareUrl" type="text"
                    class="flex-1 h-[32px] px-2.5 border border-gray-200 rounded-lg text-xs text-gray-500 bg-gray-50 min-w-0"
                    readonly />
                <button @click="copyLink"
                    class="flex-shrink-0 h-[32px] w-[32px] flex items-center justify-center border border-gray-200 rounded-lg hover:bg-gray-50 text-gray-500">
                    <Icon :name="copyLabel === 'Copied!' ? 'heroicons:check' : 'heroicons:clipboard-document'" class="w-3.5 h-3.5" />
                </button>
            </div>

            <!-- Share with people (only when 'shared' selected) -->
            <div v-if="currentVisibility === 'shared'">
                <label class="text-xs font-medium text-gray-500 uppercase tracking-wider mb-2 block">Share with</label>
                <div class="flex items-start gap-2 mb-4">
                    <div class="flex-1 flex flex-wrap items-center gap-1.5 border border-gray-200 rounded-lg px-2.5 py-1.5 min-h-[32px] focus-within:ring-2 focus-within:ring-blue-500 focus-within:border-blue-500 bg-white">
                        <span v-for="(user, idx) in pendingUsers" :key="user.id || user.email"
                            class="inline-flex items-center gap-1 bg-blue-50 text-blue-700 text-xs px-2 py-0.5 rounded-full whitespace-nowrap">
                            {{ user.name || user.email }}
                            <button @click="removePendingUser(idx)" class="hover:text-red-500 outline-none">
                                <Icon name="heroicons:x-mark" class="w-3 h-3" />
                            </button>
                        </span>
                        <div class="relative flex-1 min-w-[120px]">
                            <input ref="inputRef" v-model="inputValue" type="text"
                                class="w-full border-none outline-none text-xs bg-transparent p-0"
                                placeholder="Name or email..."
                                @keydown.enter.prevent="handleEnter"
                                @keydown.,.prevent="handleComma"
                                @keydown.backspace="handleBackspace"
                                @input="onInput"
                                @focus="showDropdown = true"
                                @blur="onBlur" />
                            <div v-if="showDropdown && filteredMembers.length > 0"
                                class="absolute start-0 top-full mt-1 w-64 bg-white border border-gray-200 rounded-lg shadow-lg z-50 max-h-40 overflow-y-auto">
                                <button v-for="member in filteredMembers" :key="member.id"
                                    class="w-full text-start px-3 py-2 text-sm hover:bg-gray-50 flex items-center gap-2.5"
                                    @mousedown.prevent="addMember(member)">
                                    <div class="w-6 h-6 bg-gray-200 rounded-full flex items-center justify-center text-xs font-medium text-gray-600 flex-shrink-0">
                                        {{ (member.name || member.email).charAt(0).toUpperCase() }}
                                    </div>
                                    <div class="flex flex-col min-w-0">
                                        <span class="text-gray-900 truncate">{{ member.name || member.email }}</span>
                                        <span v-if="member.name" class="text-xs text-gray-400 truncate">{{ member.email }}</span>
                                    </div>
                                </button>
                            </div>
                        </div>
                    </div>
                    <button @click="inviteUsers" :disabled="pendingUsers.length === 0 || isSaving"
                        class="flex-shrink-0 px-3 h-[32px] text-xs font-medium text-white bg-blue-600 rounded-lg hover:bg-blue-700 disabled:opacity-40 disabled:cursor-not-allowed">
                        Share
                    </button>
                </div>

                <!-- People with access -->
                <div v-if="sharedUsers.length > 0" class="space-y-0.5">
                    <div v-for="user in sharedUsers" :key="user.user_id"
                        class="flex items-center justify-between py-2 px-2 rounded-lg hover:bg-gray-50 group">
                        <div class="flex items-center gap-2.5">
                            <div class="w-7 h-7 bg-gray-100 rounded-full flex items-center justify-center text-xs font-medium text-gray-600">
                                {{ (user.user_name || user.user_email || '?').charAt(0).toUpperCase() }}
                            </div>
                            <div class="flex flex-col">
                                <span class="text-sm text-gray-700">{{ user.user_name || user.user_email }}</span>
                                <span v-if="user.user_name && user.user_email" class="text-xs text-gray-400">{{ user.user_email }}</span>
                            </div>
                        </div>
                        <button @click="removeSharedUser(user)"
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
const { smtpEnabled } = useAppSettings()
const modalOpen = ref(false)
const isSaving = ref(false)
const inputRef = ref<HTMLInputElement | null>(null)
const inputValue = ref('')
const showDropdown = ref(false)
const pendingUsers = ref<{ id?: string; name?: string; email: string }[]>([])
const sharedUsers = ref<any[]>([])
const copyLabel = ref('Copy link')

const currentVisibility = ref('none')
const conversationShareToken = ref<string | null>(null)

const visibilityOptions = [
    { value: 'none', label: 'Private', description: 'Only you can access', icon: 'heroicons:lock-closed' },
    { value: 'shared', label: 'Specific people', description: 'Only invited people can access', icon: 'heroicons:user-group' },
    { value: 'internal', label: 'Organization', description: 'All org members can access', icon: 'heroicons:building-office' },
    { value: 'public', label: 'Anyone with link', description: 'Anyone can view', icon: 'heroicons:globe-alt' },
]

const visibilityField = computed(() =>
    props.shareType === 'artifact' ? 'artifact_visibility' : 'conversation_visibility'
)

const isShared = computed(() => currentVisibility.value !== 'none')

const shareDescription = computed(() =>
    props.shareType === 'artifact'
        ? 'Control who can view this dashboard.'
        : 'Control who can view this conversation and its messages.'
)

const selectedOption = computed(() =>
    visibilityOptions.find(o => o.value === currentVisibility.value) || visibilityOptions[0]
)

const buttonLabel = computed(() => {
    if (!isShared.value) return props.shareType === 'artifact' ? 'Share Dashboard' : 'Share'
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

// Org members for autocomplete
const members = ref<{ id: string; name: string; email: string }[]>([])
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

const filteredMembers = computed(() => {
    const q = inputValue.value.toLowerCase().trim()
    if (!q) return []
    const existingIds = new Set([
        ...sharedUsers.value.map(u => u.user_id),
        ...pendingUsers.value.map(u => u.id),
    ])
    return members.value.filter(
        m => !existingIds.has(m.id) &&
            m.id !== props.report?.user?.id &&
            (m.email.toLowerCase().includes(q) || m.name.toLowerCase().includes(q))
    ).slice(0, 6)
})

const isValidEmail = (email: string) => /^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(email)

const addMember = (member: { id: string; name: string; email: string }) => {
    if (!pendingUsers.value.find(u => u.id === member.id)) {
        pendingUsers.value.push({ id: member.id, name: member.name, email: member.email })
    }
    inputValue.value = ''
    showDropdown.value = false
}

const addEmailAsPending = (email: string) => {
    const clean = email.trim().toLowerCase()
    if (!clean || !isValidEmail(clean)) return
    const member = members.value.find(m => m.email.toLowerCase() === clean)
    if (member) {
        addMember(member)
    } else {
        toast.add({ title: 'User not found in organization', color: 'orange' })
    }
}

const removePendingUser = (idx: number) => pendingUsers.value.splice(idx, 1)

const handleEnter = () => {
    if (filteredMembers.value.length > 0) {
        addMember(filteredMembers.value[0])
    } else {
        addEmailAsPending(inputValue.value)
    }
}

const handleComma = () => addEmailAsPending(inputValue.value)

const handleBackspace = () => {
    if (!inputValue.value && pendingUsers.value.length > 0) pendingUsers.value.pop()
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
            sharedUsers.value = res.data.value as any[]
        }
    } catch { /* silent */ }
}

const saveVisibility = async (visibility: string, userIds?: string[]) => {
    isSaving.value = true
    try {
        const body: any = { visibility }
        if (userIds) body.shared_user_ids = userIds
        const res = await useMyFetch(`/reports/${props.report.id}/visibility/${props.shareType}`, {
            method: 'PUT',
            body,
        })
        if (res.error.value) throw res.error.value

        if (props.report) {
            props.report[visibilityField.value] = visibility
        }

        const data = res.data.value as any
        if (props.shareType === 'conversation' && data) {
            const token = data.conversation_share_token ?? null
            conversationShareToken.value = token
            if (props.report) props.report.conversation_share_token = token
        }

        toast.add({
            title: visibility === 'none' ? 'Sharing disabled' : 'Sharing updated',
            color: 'green',
        })
    } catch {
        toast.add({ title: 'Failed to update sharing', color: 'red' })
    } finally {
        isSaving.value = false
    }
}

const onVisibilityChange = async (value: string) => {
    const prev = props.report?.[visibilityField.value] || 'none'
    if (value === prev) return

    const userIds = value === 'shared'
        ? sharedUsers.value.map(u => u.user_id)
        : undefined
    await saveVisibility(value, userIds)
}

const inviteUsers = async () => {
    if (pendingUsers.value.length === 0) return

    if (currentVisibility.value === 'none') {
        currentVisibility.value = 'shared'
    }

    const allUserIds = [
        ...sharedUsers.value.map(u => u.user_id),
        ...pendingUsers.value.map(u => u.id).filter(Boolean),
    ]

    await saveVisibility(currentVisibility.value === 'shared' ? 'shared' : currentVisibility.value, allUserIds)
    await fetchShares()
    pendingUsers.value = []
}

const removeSharedUser = async (user: any) => {
    const remaining = sharedUsers.value
        .filter(u => u.user_id !== user.user_id)
        .map(u => u.user_id)

    if (remaining.length === 0 && currentVisibility.value === 'shared') {
        currentVisibility.value = 'none'
        await saveVisibility('none')
    } else {
        await saveVisibility('shared', remaining)
    }

    await fetchShares()
}

const copyLink = async () => {
    try {
        await navigator.clipboard.writeText(shareUrl.value)
        copyLabel.value = 'Copied!'
        setTimeout(() => { copyLabel.value = 'Copy link' }, 2000)
    } catch {
        toast.add({ title: 'Failed to copy', color: 'red' })
    }
}

const openModal = async () => {
    modalOpen.value = true
    currentVisibility.value = props.report?.[visibilityField.value] || 'none'
    conversationShareToken.value = props.report?.conversation_share_token ?? null
    await Promise.all([fetchMembers(), fetchVisibility(), fetchShares()])
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
