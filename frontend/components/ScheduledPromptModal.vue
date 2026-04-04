<template>
    <UModal v-model="isOpen" :ui="{ width: 'sm:max-w-xl' }">
        <UCard :ui="{ body: { padding: 'px-4 py-3 sm:p-4' }, header: { padding: 'px-4 py-3 sm:px-4 sm:py-3' }, footer: { padding: 'px-4 py-2 sm:px-4 sm:py-2' } }">
            <template #header>
                <div class="flex items-center justify-between">
                    <h3 class="text-sm font-semibold text-gray-900">{{ isEditing ? 'Edit scheduled task' : 'Schedule a task' }}</h3>
                    <UButton color="gray" variant="ghost" icon="i-heroicons-x-mark-20-solid" size="xs" @click="isOpen = false" />
                </div>
            </template>

            <!-- Prompt input -->
            <PromptBoxV2
                ref="promptBoxRef"
                :report_id="reportId"
                :initialSelectedDataSources="initialDataSources"
                :initialMode="initialMode"
                :initialModel="initialModel"
                :textareaContent="initialContent"
                :hideScheduleButton="true"
                :hideSubmitButton="true"
                :compact="true"
                @submitCompletion="handlePromptSubmit"
            />

            <!-- Schedule -->
            <div class="mt-3">
                <div class="text-xs text-gray-500 mb-1.5">Schedule</div>

                <div class="flex gap-0.5 p-0.5 bg-gray-100 rounded w-fit mb-2">
                    <button
                        v-for="t in scheduleTypes"
                        :key="t.value"
                        class="px-2 py-0.5 text-[11px] rounded transition-colors"
                        :class="scheduleType === t.value ? 'bg-white text-gray-900 shadow-sm font-medium' : 'text-gray-400 hover:text-gray-600'"
                        @click="scheduleType = t.value"
                    >
                        {{ t.label }}
                    </button>
                </div>

                <div v-if="scheduleType === 'once'" class="flex items-center gap-1.5 text-xs text-gray-600">
                    <span>Run in</span>
                    <input v-model.number="delayAmount" type="number" min="1" class="w-14 rounded border border-gray-200 px-1.5 py-1 text-xs text-center" />
                    <select v-model="delayUnit" class="rounded border border-gray-200 px-1.5 py-1 text-xs">
                        <option value="minutes">min</option>
                        <option value="hours">hr</option>
                        <option value="days">days</option>
                    </select>
                </div>

                <div v-else class="flex items-center gap-1.5 text-xs text-gray-600 flex-wrap">
                    <span>Every</span>
                    <template v-if="recurInterval === 'minutes' || recurInterval === 'hours'">
                        <input v-model.number="recurEveryN" type="number" min="1" :max="recurInterval === 'minutes' ? 59 : 23"
                            class="w-12 rounded border border-gray-200 px-1 py-1 text-xs text-center" />
                    </template>
                    <select v-model="recurInterval" class="rounded border border-gray-200 px-1.5 py-1 text-xs">
                        <option value="minutes">minutes</option>
                        <option value="hours">hours</option>
                        <option value="day">day</option>
                        <option value="weekdays">weekdays</option>
                        <option value="week">week</option>
                        <option value="month">month</option>
                    </select>
                    <template v-if="recurInterval === 'day' || recurInterval === 'weekdays' || recurInterval === 'week' || recurInterval === 'month'">
                        <span>at</span>
                        <select v-model="recurHour" class="rounded border border-gray-200 px-1.5 py-1 text-xs">
                            <option v-for="h in 24" :key="h - 1" :value="h - 1">{{ String(h - 1).padStart(2, '0') }}:00</option>
                        </select>
                    </template>
                    <template v-if="recurInterval === 'week'">
                        <span>on</span>
                        <select v-model="recurDay" class="rounded border border-gray-200 px-1.5 py-1 text-xs">
                            <option v-for="d in weekdays" :key="d.value" :value="d.value">{{ d.label }}</option>
                        </select>
                    </template>
                    <template v-if="recurInterval === 'month'">
                        <span>on day</span>
                        <select v-model="recurDayOfMonth" class="rounded border border-gray-200 px-1.5 py-1 text-xs">
                            <option v-for="d in 28" :key="d" :value="d">{{ d }}</option>
                        </select>
                    </template>
                </div>
            </div>

            <!-- Active toggle (edit mode) -->
            <div v-if="isEditing" class="mt-3 flex items-center justify-between">
                <span class="text-xs text-gray-500">Active</span>
                <button
                    @click="isActive = !isActive"
                    class="relative inline-flex h-4 w-7 items-center rounded-full transition-colors"
                    :class="isActive ? 'bg-blue-500' : 'bg-gray-300'"
                >
                    <span class="inline-block h-3 w-3 rounded-full bg-white transition-transform" :class="isActive ? 'translate-x-3.5' : 'translate-x-0.5'" />
                </button>
            </div>

            <!-- Notification subscribers -->
            <div v-if="smtpEnabled" class="border-t border-gray-100 pt-3 mt-3">
                <div class="flex items-center gap-1.5 text-xs text-gray-500 mb-1.5">
                    <Icon name="heroicons:envelope" class="w-3 h-3" />
                    Notify after each run
                </div>
                <div class="flex flex-wrap items-center gap-1 border border-gray-200 rounded px-2 py-1 min-h-[30px] focus-within:ring-1 focus-within:ring-blue-500 focus-within:border-blue-500 bg-white">
                    <span v-for="(sub, idx) in subscribers" :key="idx"
                        class="inline-flex items-center gap-0.5 bg-gray-100 text-gray-600 text-[11px] px-1.5 py-0.5 rounded-full">
                        {{ sub.type === 'user' ? getMemberName(sub.id) : sub.address }}
                        <button @click="removeSubscriber(idx)" class="hover:text-red-500 outline-none">
                            <Icon name="heroicons:x-mark" class="w-2.5 h-2.5" />
                        </button>
                    </span>
                    <div class="relative flex-1 min-w-[120px]">
                        <input ref="inputRef" v-model="inputValue" type="text"
                            class="w-full border-none outline-none text-xs bg-transparent p-0"
                            placeholder="Email or member..."
                            @keydown.enter.prevent="handleEnter"
                            @keydown.,.prevent="handleComma"
                            @keydown.backspace="handleBackspace"
                            @input="onMemberInput"
                            @focus="showMemberDropdown = true"
                            @blur="onBlur" />
                        <div v-if="showMemberDropdown && filteredMembers.length > 0"
                            class="absolute left-0 top-full mt-1 w-56 bg-white border border-gray-200 rounded shadow-lg z-50 max-h-32 overflow-y-auto">
                            <button v-for="member in filteredMembers" :key="member.id"
                                class="w-full text-left px-2 py-1.5 text-xs hover:bg-gray-50 flex flex-col"
                                @mousedown.prevent="addMember(member)">
                                <span class="text-gray-900">{{ member.name || member.email }}</span>
                                <span v-if="member.name" class="text-[10px] text-gray-400">{{ member.email }}</span>
                            </button>
                        </div>
                    </div>
                </div>
            </div>

            <template #footer>
                <div class="flex justify-end gap-2">
                    <UButton color="gray" variant="ghost" size="xs" @click="isOpen = false">Cancel</UButton>
                    <UButton color="blue" size="xs" :loading="isSaving" @click="saveFromCurrentState">{{ isEditing ? 'Update' : 'Schedule' }}</UButton>
                </div>
            </template>
        </UCard>
    </UModal>
</template>

<script lang="ts" setup>
import Spinner from '@/components/Spinner.vue'
import PromptBoxV2 from '@/components/prompt/PromptBoxV2.vue'

const toast = useToast()
const { smtpEnabled } = useAppSettings()

const props = defineProps<{
    reportId: string
    scheduledPrompt?: any
    initialDataSources?: any[]
    draftContent?: string
    draftMode?: 'chat' | 'deep'
    draftModel?: string
}>()

const emit = defineEmits(['saved'])

const isOpen = defineModel<boolean>({ default: false })
const isSaving = ref(false)
const promptBoxRef = ref<InstanceType<typeof PromptBoxV2> | null>(null)

const isEditing = computed(() => !!props.scheduledPrompt)

const initialContent = computed(() => props.scheduledPrompt?.prompt?.content || props.draftContent || '')
const initialMode = computed(() => (props.scheduledPrompt?.prompt?.mode as 'chat' | 'deep') || props.draftMode || 'chat')
const initialModel = computed(() => props.scheduledPrompt?.prompt?.model_id || props.draftModel || '')
const initialDataSources = computed(() => props.initialDataSources || [])

const isActive = ref(props.scheduledPrompt?.is_active ?? true)

// Schedule type: one-time or recurring
const scheduleTypes = [
    { value: 'once' as const, label: 'One-time' },
    { value: 'recurring' as const, label: 'Recurring' },
]
const scheduleType = ref<'once' | 'recurring'>('recurring')
const delayAmount = ref(1)
const delayUnit = ref<'minutes' | 'hours' | 'days'>('hours')

// Recurring structured inputs
type RecurInterval = 'minutes' | 'hours' | 'day' | 'weekdays' | 'week' | 'month'
const recurInterval = ref<RecurInterval>('day')
const recurEveryN = ref(15)
const recurHour = ref(8)
const recurDay = ref(1)
const recurDayOfMonth = ref(1)
const weekdays = [
    { value: 0, label: 'Sun' }, { value: 1, label: 'Mon' }, { value: 2, label: 'Tue' },
    { value: 3, label: 'Wed' }, { value: 4, label: 'Thu' }, { value: 5, label: 'Fri' }, { value: 6, label: 'Sat' },
]

function parseCronToStructured(cron: string) {
    if (!cron) return
    const parts = cron.split(' ')
    if (parts.length < 5) return
    const [min, hour, dom, , dow] = parts
    if (min.startsWith('*/')) {
        recurInterval.value = 'minutes'
        recurEveryN.value = parseInt(min.slice(2)) || 15
    } else if (hour.startsWith('*/')) {
        recurInterval.value = 'hours'
        recurEveryN.value = parseInt(hour.slice(2)) || 1
    } else if (dow === '1-5') {
        recurInterval.value = 'weekdays'
        recurHour.value = parseInt(hour) || 0
    } else if (dom !== '*' && dow === '*') {
        recurInterval.value = 'month'
        recurHour.value = parseInt(hour) || 0
        recurDayOfMonth.value = parseInt(dom) || 1
    } else if (dow !== '*') {
        recurInterval.value = 'week'
        recurHour.value = parseInt(hour) || 0
        recurDay.value = parseInt(dow) || 1
    } else {
        recurInterval.value = 'day'
        recurHour.value = parseInt(hour) || 0
    }
}

// Reset form when scheduledPrompt changes
watch(() => props.scheduledPrompt, (sp) => {
    isActive.value = sp?.is_active ?? true
    subscribers.value = (sp?.notification_subscribers || []).map((s: any) => ({ ...s }))
    scheduleType.value = 'recurring'
    if (sp?.cron_schedule) {
        parseCronToStructured(sp.cron_schedule)
    } else {
        recurInterval.value = 'day'
        recurEveryN.value = 15
        recurHour.value = 8
        recurDay.value = 1
        recurDayOfMonth.value = 1
    }
})

// ---- Handle PromptBoxV2 submit (for new scheduled prompts) ----

async function handlePromptSubmit(payload: { text: string; mentions: any[]; mode: string; model_id: string; files?: any[] }) {
    await saveScheduledPrompt({
        content: payload.text,
        mentions: payload.mentions,
        mode: payload.mode,
        model_id: payload.model_id,
    })
}

async function saveFromCurrentState() {
    const box = promptBoxRef.value
    const fallback = props.scheduledPrompt?.prompt || {}
    await saveScheduledPrompt({
        content: box?.getText?.() || fallback.content || '',
        mentions: box?.getMentions?.() || fallback.mentions,
        mode: box?.getMode?.() || fallback.mode || 'chat',
        model_id: box?.getModel?.() || fallback.model_id,
    })
}

function computeCronSchedule(): string {
    if (scheduleType.value === 'once') {
        const now = new Date()
        const multiplier = delayUnit.value === 'minutes' ? 1 : delayUnit.value === 'hours' ? 60 : 1440
        const target = new Date(now.getTime() + delayAmount.value * multiplier * 60_000)
        return `${target.getMinutes()} ${target.getHours()} ${target.getDate()} ${target.getMonth() + 1} *`
    }
    if (recurInterval.value === 'minutes') return `*/${recurEveryN.value} * * * *`
    if (recurInterval.value === 'hours') return `0 */${recurEveryN.value} * * *`
    if (recurInterval.value === 'weekdays') return `0 ${recurHour.value} * * 1-5`
    if (recurInterval.value === 'week') return `0 ${recurHour.value} * * ${recurDay.value}`
    if (recurInterval.value === 'month') return `0 ${recurHour.value} ${recurDayOfMonth.value} * *`
    return `0 ${recurHour.value} * * *`
}

async function saveScheduledPrompt(prompt: { content: string; mentions?: any[]; mode?: string; model_id?: string }) {
    isSaving.value = true
    try {
        const body: any = {
            prompt,
            cron_schedule: computeCronSchedule(),
            is_active: isActive.value,
            notification_subscribers: subscribers.value.length > 0 ? subscribers.value : null,
        }

        let response
        if (isEditing.value) {
            response = await useMyFetch(`/api/reports/${props.reportId}/scheduled-prompts/${props.scheduledPrompt.id}`, {
                method: 'PUT',
                body,
            })
        } else {
            response = await useMyFetch(`/api/reports/${props.reportId}/scheduled-prompts`, {
                method: 'POST',
                body,
            })
        }

        if (response.data.value) {
            toast.add({
                title: isEditing.value ? 'Scheduled prompt updated' : 'Prompt scheduled',
                color: 'green',
            })
            isOpen.value = false
            emit('saved')
        } else {
            toast.add({ title: 'Error', color: 'red', description: 'Failed to save scheduled prompt' })
        }
    } catch {
        toast.add({ title: 'Error', color: 'red', description: 'Failed to save scheduled prompt' })
    } finally {
        isSaving.value = false
    }
}

// ---- Subscriber management ----

type Subscriber = { type: 'user'; id: string } | { type: 'email'; address: string }

const subscribers = ref<Subscriber[]>(
    (props.scheduledPrompt?.notification_subscribers || []).map((s: any) => ({ ...s }))
)

const inputRef = ref<HTMLInputElement | null>(null)
const inputValue = ref('')
const showMemberDropdown = ref(false)

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
    } catch {}
}
fetchMembers()

const getMemberName = (userId: string | undefined) => {
    if (!userId) return 'Unknown'
    const m = members.value.find((m) => m.id === userId)
    return m ? (m.name || m.email) : userId
}

const subscriberEmails = computed(() => {
    return subscribers.value.map((s) => {
        if (s.type === 'email') return s.address
        const m = members.value.find((m) => m.id === (s as any).id)
        return m?.email
    })
})

const filteredMembers = computed(() => {
    const q = inputValue.value.toLowerCase().trim()
    if (!q) return []
    return members.value.filter(
        (m) =>
            !subscriberEmails.value.includes(m.email) &&
            (m.email.toLowerCase().includes(q) || m.name.toLowerCase().includes(q))
    ).slice(0, 5)
})

const isValidEmail = (email: string) => /^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(email)

const addEmail = (email: string) => {
    const clean = email.trim().toLowerCase()
    if (clean && isValidEmail(clean) && !subscriberEmails.value.includes(clean)) {
        subscribers.value.push({ type: 'email', address: clean })
        inputValue.value = ''
    }
}

const addMember = (member: { id: string; name: string; email: string }) => {
    if (!subscribers.value.some((s) => s.type === 'user' && (s as any).id === member.id)) {
        subscribers.value.push({ type: 'user', id: member.id })
    }
    inputValue.value = ''
    showMemberDropdown.value = false
}

const removeSubscriber = (idx: number) => {
    subscribers.value.splice(idx, 1)
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
    if (!inputValue.value && subscribers.value.length > 0) {
        subscribers.value.pop()
    }
}

const onMemberInput = () => {
    showMemberDropdown.value = true
}

const onBlur = () => {
    setTimeout(() => {
        showMemberDropdown.value = false
        if (inputValue.value && isValidEmail(inputValue.value)) {
            addEmail(inputValue.value)
        }
    }, 200)
}
</script>
