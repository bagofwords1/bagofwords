<template>
    <UTooltip :text="buttonLabel">
        <button @click="openModal"
            :class="[
                'text-xs items-center flex gap-1 hover:bg-gray-100 px-2 py-1 rounded border',
                isShared
                    ? 'border-green-200 bg-green-50 text-green-700'
                    : 'border-gray-200 bg-gray-50 text-gray-600'
            ]">
            <Icon :name="buttonIcon" class="w-3.5 h-3.5" />
            <span class="text-xs">{{ buttonLabel }}</span>
        </button>
    </UTooltip>

    <UModal v-model="modalOpen" :ui="{ width: 'sm:max-w-lg' }">
        <div class="p-5 relative">
            <!-- Header -->
            <div class="flex items-center justify-between mb-4">
                <h2 class="text-base font-semibold text-gray-900">{{ title }}</h2>
                <div class="flex items-center gap-2">
                    <button v-if="isShared" @click="copyLink"
                        class="inline-flex items-center gap-1 text-sm text-blue-600 hover:text-blue-700">
                        <Icon name="heroicons:link" class="w-4 h-4" />
                        {{ copyLabel }}
                    </button>
                    <button @click="modalOpen = false"
                        class="text-gray-400 hover:text-gray-600 outline-none">
                        <Icon name="heroicons:x-mark" class="w-5 h-5" />
                    </button>
                </div>
            </div>

            <!-- Invite people input -->
            <div class="flex gap-2 mb-5">
                <div class="flex-1 flex flex-wrap items-center gap-1.5 border border-gray-300 rounded-lg px-3 py-2 min-h-[40px] focus-within:ring-2 focus-within:ring-blue-500 focus-within:border-blue-500 bg-white">
                    <span v-for="(user, idx) in pendingUsers" :key="user.id || user.email"
                        class="inline-flex items-center gap-1 bg-blue-50 text-blue-700 text-xs px-2 py-0.5 rounded-full">
                        {{ user.name || user.email }}
                        <button @click="removePendingUser(idx)" class="hover:text-red-500 outline-none">
                            <Icon name="heroicons:x-mark" class="w-3 h-3" />
                        </button>
                    </span>
                    <div class="relative flex-1 min-w-[160px]">
                        <input ref="inputRef" v-model="inputValue" type="text"
                            class="w-full border-none outline-none text-sm bg-transparent p-0"
                            placeholder="Add people by email..."
                            @keydown.enter.prevent="handleEnter"
                            @keydown.,.prevent="handleComma"
                            @keydown.backspace="handleBackspace"
                            @input="onInput"
                            @focus="showDropdown = true"
                            @blur="onBlur" />
                        <div v-if="showDropdown && filteredMembers.length > 0"
                            class="absolute left-0 top-full mt-1 w-72 bg-white border border-gray-200 rounded-lg shadow-lg z-50 max-h-48 overflow-y-auto">
                            <button v-for="member in filteredMembers" :key="member.id"
                                class="w-full text-left px-3 py-2 text-sm hover:bg-gray-50 flex items-center gap-3"
                                @mousedown.prevent="addMember(member)">
                                <div class="w-7 h-7 bg-gray-200 rounded-full flex items-center justify-center text-xs font-medium text-gray-600">
                                    {{ (member.name || member.email).charAt(0).toUpperCase() }}
                                </div>
                                <div class="flex flex-col">
                                    <span class="text-gray-900">{{ member.name || member.email }}</span>
                                    <span v-if="member.name" class="text-xs text-gray-400">{{ member.email }}</span>
                                </div>
                            </button>
                        </div>
                    </div>
                </div>
                <button @click="inviteUsers" :disabled="pendingUsers.length === 0 || isSaving"
                    class="px-4 py-2 text-sm font-medium text-white bg-blue-600 rounded-lg hover:bg-blue-700 disabled:opacity-40 disabled:cursor-not-allowed whitespace-nowrap">
                    Invite
                </button>
            </div>

            <!-- General access -->
            <div class="mb-3">
                <div class="text-xs font-medium text-gray-500 uppercase tracking-wider mb-2">General access</div>
                <div class="flex items-center justify-between py-2 px-3 rounded-lg hover:bg-gray-50">
                    <div class="flex items-center gap-3">
                        <div class="w-8 h-8 bg-gray-100 rounded-full flex items-center justify-center">
                            <Icon :name="accessIcon" class="w-4 h-4 text-gray-500" />
                        </div>
                        <span class="text-sm text-gray-700">{{ accessLabel }}</span>
                    </div>
                    <USelectMenu v-model="currentVisibility" :options="visibilityOptions"
                        value-attribute="value" option-attribute="label"
                        class="w-40" size="sm"
                        @change="onVisibilityChange" />
                </div>
            </div>

            <!-- Who has access list -->
            <div v-if="sharedUsers.length > 0 || true">
                <div class="text-xs font-medium text-gray-500 uppercase tracking-wider mb-2">People with access</div>
                <div class="space-y-1 max-h-48 overflow-y-auto">
                    <!-- Owner -->
                    <div class="flex items-center justify-between py-2 px-3 rounded-lg">
                        <div class="flex items-center gap-3">
                            <div class="w-8 h-8 bg-blue-100 rounded-full flex items-center justify-center text-xs font-medium text-blue-700">
                                {{ (ownerName || 'O').charAt(0).toUpperCase() }}
                            </div>
                            <div class="flex flex-col">
                                <span class="text-sm text-gray-900">{{ ownerName || 'Owner' }} <span v-if="isCurrentUserOwner" class="text-gray-400">(you)</span></span>
                                <span class="text-xs text-gray-400">{{ ownerEmail }}</span>
                            </div>
                        </div>
                        <span class="text-xs text-gray-400 font-medium">Owner</span>
                    </div>
                    <!-- Shared users -->
                    <div v-for="user in sharedUsers" :key="user.user_id"
                        class="flex items-center justify-between py-2 px-3 rounded-lg hover:bg-gray-50 group">
                        <div class="flex items-center gap-3">
                            <div class="w-8 h-8 bg-gray-100 rounded-full flex items-center justify-center text-xs font-medium text-gray-600">
                                {{ (user.user_name || user.user_email || '?').charAt(0).toUpperCase() }}
                            </div>
                            <div class="flex flex-col">
                                <span class="text-sm text-gray-900">{{ user.user_name || user.user_email }}</span>
                                <span v-if="user.user_name" class="text-xs text-gray-400">{{ user.user_email }}</span>
                            </div>
                        </div>
                        <div class="flex items-center gap-2">
                            <span class="text-xs text-gray-400">Can view</span>
                            <button @click="removeSharedUser(user)"
                                class="opacity-0 group-hover:opacity-100 text-gray-400 hover:text-red-500 transition-opacity">
                                <Icon name="heroicons:x-mark" class="w-4 h-4" />
                            </button>
                        </div>
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

const props = defineProps<{
    report: any
    shareType: 'artifact' | 'conversation'
    title: string
}>()

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

// Current visibility state
const currentVisibility = ref('none')

const visibilityOptions = [
    { value: 'none', label: 'No access' },
    { value: 'shared', label: 'Invited only' },
    { value: 'internal', label: 'Organization' },
    { value: 'public', label: 'Anyone with link' },
]

// Computed props
const visibilityField = computed(() =>
    props.shareType === 'artifact' ? 'artifact_visibility' : 'conversation_visibility'
)

const isShared = computed(() => currentVisibility.value !== 'none')

const buttonLabel = computed(() => {
    if (!isShared.value) return props.shareType === 'artifact' ? 'Share Dashboard' : 'Share'
    const opt = visibilityOptions.find(o => o.value === currentVisibility.value)
    return opt ? opt.label : 'Shared'
})

const buttonIcon = computed(() => {
    switch (currentVisibility.value) {
        case 'public': return 'heroicons:globe-alt'
        case 'internal': return 'heroicons:building-office'
        case 'shared': return 'heroicons:user-group'
        default: return 'heroicons:lock-closed'
    }
})

const accessIcon = computed(() => {
    switch (currentVisibility.value) {
        case 'public': return 'heroicons:globe-alt'
        case 'internal': return 'heroicons:building-office'
        case 'shared': return 'heroicons:user-group'
        default: return 'heroicons:lock-closed'
    }
})

const accessLabel = computed(() => {
    switch (currentVisibility.value) {
        case 'public': return 'Anyone with the link'
        case 'internal': return 'Organization members'
        case 'shared': return 'Only invited people'
        default: return 'Only you'
    }
})

const shareUrl = computed(() => {
    if (props.shareType === 'artifact') {
        return `${window.location.origin}/r/${props.report.id}`
    }
    const token = props.report.conversation_share_token
    return token ? `${window.location.origin}/c/${token}` : ''
})

const ownerName = computed(() => props.report?.user?.name || '')
const ownerEmail = computed(() => props.report?.user?.email || '')
const isCurrentUserOwner = computed(() => {
    const { data } = useAuth()
    return data.value?.id === props.report?.user?.id
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
    // Try to match to an org member
    const member = members.value.find(m => m.email.toLowerCase() === clean)
    if (member) {
        addMember(member)
    } else {
        // External email - can't share with non-org members yet
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

        // Update parent report object for reactivity
        if (props.report) {
            props.report[visibilityField.value] = visibility
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
    currentVisibility.value = value
    const userIds = currentVisibility.value === 'shared'
        ? sharedUsers.value.map(u => u.user_id)
        : undefined
    await saveVisibility(value, userIds)
}

const inviteUsers = async () => {
    if (pendingUsers.value.length === 0) return

    // If visibility is 'none', auto-set to 'shared'
    if (currentVisibility.value === 'none') {
        currentVisibility.value = 'shared'
    }

    // Combine existing shared users with new invites
    const allUserIds = [
        ...sharedUsers.value.map(u => u.user_id),
        ...pendingUsers.value.map(u => u.id).filter(Boolean),
    ]

    await saveVisibility(currentVisibility.value === 'shared' ? 'shared' : currentVisibility.value, allUserIds)

    // Refresh shares list
    await fetchShares()
    pendingUsers.value = []
}

const removeSharedUser = async (user: any) => {
    const remaining = sharedUsers.value
        .filter(u => u.user_id !== user.user_id)
        .map(u => u.user_id)

    if (remaining.length === 0 && currentVisibility.value === 'shared') {
        // No more shared users, set to none
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
    // Sync from props first
    currentVisibility.value = props.report?.[visibilityField.value] || 'none'
    // Then fetch fresh data
    await Promise.all([fetchMembers(), fetchVisibility(), fetchShares()])
}

watch(() => props.report?.id, () => {
    currentVisibility.value = props.report?.[visibilityField.value] || 'none'
})
</script>
