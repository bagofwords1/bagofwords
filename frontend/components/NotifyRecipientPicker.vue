<template>
    <div class="border-t border-gray-100 pt-4 mt-5">
        <label class="text-xs font-medium text-gray-500 uppercase tracking-wider mb-2 block">Send via email</label>

        <!-- Recipient input -->
        <div class="flex flex-wrap items-center gap-1.5 border border-gray-200 rounded-lg px-2.5 py-1.5 min-h-[34px] focus-within:ring-1 focus-within:ring-blue-500 focus-within:border-blue-500 bg-white">
            <span v-for="(email, idx) in recipients" :key="email"
                class="inline-flex items-center gap-1 bg-gray-100 text-gray-700 text-xs px-2 py-0.5 rounded-full">
                {{ email }}
                <button @click="removeRecipient(idx)" class="hover:text-red-500 outline-none">
                    <Icon name="heroicons:x-mark" class="w-3 h-3" />
                </button>
            </span>
            <div class="relative flex-1 min-w-[120px]">
                <input ref="inputRef" v-model="inputValue" type="text"
                    class="w-full border-none outline-none text-xs bg-transparent p-0"
                    placeholder="Email or member name..."
                    @keydown.enter.prevent="handleEnter"
                    @keydown.,.prevent="handleComma"
                    @keydown.backspace="handleBackspace"
                    @input="onInput"
                    @focus="showDropdown = true"
                    @blur="onBlur" />
                <!-- Autocomplete dropdown -->
                <div v-if="showDropdown && filteredMembers.length > 0"
                    class="absolute left-0 top-full mt-1 w-60 bg-white border border-gray-200 rounded-lg shadow-lg z-50 max-h-36 overflow-y-auto">
                    <button v-for="member in filteredMembers" :key="member.email"
                        class="w-full text-left px-2.5 py-1.5 text-xs hover:bg-gray-50 flex flex-col"
                        @mousedown.prevent="addMember(member)">
                        <span class="text-gray-900">{{ member.name || member.email }}</span>
                        <span v-if="member.name" class="text-[11px] text-gray-400">{{ member.email }}</span>
                    </button>
                </div>
            </div>
        </div>

        <!-- Optional message -->
        <input v-model="message" type="text" placeholder="Add a note (optional)"
            class="mt-2 w-full border border-gray-200 rounded-lg px-2.5 py-1.5 text-xs outline-none focus:ring-1 focus:ring-blue-500 focus:border-blue-500" />

        <!-- Send button -->
        <div class="flex items-center justify-between mt-3">
            <span v-if="sendStatus" class="text-[11px]" :class="sendStatus === 'sent' ? 'text-green-600' : 'text-red-500'">
                {{ sendStatus === 'sent' ? 'Sent!' : 'Failed to send' }}
            </span>
            <span v-else></span>
            <button @click="send" :disabled="recipients.length === 0 || isSending"
                class="inline-flex items-center gap-1 px-2.5 py-1 text-xs font-medium text-white bg-blue-500 rounded-lg hover:bg-blue-600 disabled:opacity-40 disabled:cursor-not-allowed">
                <Spinner v-if="isSending" class="w-3 h-3" />
                <Icon v-else name="heroicons:paper-airplane" class="w-3 h-3" />
                Send
            </button>
        </div>
    </div>
</template>

<script lang="ts" setup>
import { ref, computed } from 'vue'

const props = defineProps<{
    reportId: string
    notificationType: 'share_dashboard' | 'share_conversation' | 'schedule_report'
    shareUrl: string
}>()

const emit = defineEmits<{
    (e: 'sent'): void
}>()

const toast = useToast()
const inputRef = ref<HTMLInputElement | null>(null)
const inputValue = ref('')
const recipients = ref<string[]>([])
const message = ref('')
const isSending = ref(false)
const sendStatus = ref<'sent' | 'failed' | null>(null)
const showDropdown = ref(false)

// Fetch org members for autocomplete
const members = ref<{ name: string; email: string }[]>([])
const fetchMembers = async () => {
    try {
        const res = await useMyFetch('/organization/members')
        if (res.data.value) {
            members.value = (res.data.value as any[]).map((u: any) => ({
                name: u.name || '',
                email: u.email,
            }))
        }
    } catch {
        // Silent — autocomplete just won't work
    }
}
fetchMembers()

const filteredMembers = computed(() => {
    const q = inputValue.value.toLowerCase().trim()
    if (!q) return []
    return members.value.filter(
        (m) =>
            !recipients.value.includes(m.email) &&
            (m.email.toLowerCase().includes(q) || m.name.toLowerCase().includes(q))
    ).slice(0, 5)
})

const isValidEmail = (email: string) => /^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(email)

const addEmail = (email: string) => {
    const clean = email.trim().toLowerCase()
    if (clean && isValidEmail(clean) && !recipients.value.includes(clean)) {
        recipients.value.push(clean)
        inputValue.value = ''
        sendStatus.value = null
    }
}

const addMember = (member: { name: string; email: string }) => {
    if (!recipients.value.includes(member.email)) {
        recipients.value.push(member.email)
    }
    inputValue.value = ''
    showDropdown.value = false
    sendStatus.value = null
}

const removeRecipient = (idx: number) => {
    recipients.value.splice(idx, 1)
    sendStatus.value = null
}

const handleEnter = () => {
    if (filteredMembers.value.length > 0) {
        addMember(filteredMembers.value[0])
    } else {
        addEmail(inputValue.value)
    }
}

const handleComma = () => {
    addEmail(inputValue.value)
}

const handleBackspace = () => {
    if (!inputValue.value && recipients.value.length > 0) {
        recipients.value.pop()
    }
}

const onInput = () => {
    showDropdown.value = true
    sendStatus.value = null
}

const onBlur = () => {
    // Delay to allow dropdown click to register
    setTimeout(() => {
        showDropdown.value = false
        // Auto-add typed text if it's a valid email
        if (inputValue.value && isValidEmail(inputValue.value)) {
            addEmail(inputValue.value)
        }
    }, 200)
}

const send = async () => {
    if (recipients.value.length === 0) return
    isSending.value = true
    sendStatus.value = null
    try {
        const res = await useMyFetch(`/reports/${props.reportId}/notify`, {
            method: 'POST',
            body: {
                type: props.notificationType,
                channels: ['email'],
                recipients: recipients.value,
                share_url: props.shareUrl,
                message: message.value || undefined,
            },
        })
        if (res.error.value) throw res.error.value
        sendStatus.value = 'sent'
        toast.add({ title: 'Notifications sent', color: 'green' })
        emit('sent')
        // Reset after success
        setTimeout(() => {
            recipients.value = []
            message.value = ''
            sendStatus.value = null
        }, 2000)
    } catch {
        sendStatus.value = 'failed'
        toast.add({ title: 'Failed to send notifications', color: 'red' })
    } finally {
        isSending.value = false
    }
}
</script>
