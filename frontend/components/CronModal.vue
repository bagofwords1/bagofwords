<template>
    <UTooltip text="Schedule or rerun report">
        <button @click="cronModalOpen = true"
            class="text-lg items-center flex gap-1 hover:bg-gray-100 px-2 py-1 rounded">
            <Icon name="heroicons:clock" />
        </button>
    </UTooltip>


    <UModal v-model="cronModalOpen">
        <div class="p-4 relative">
            <button @click="cronModalOpen = false"
                class="absolute top-2 right-2 text-gray-500 hover:text-gray-700 outline-none">
                <Icon name="heroicons:x-mark" class="w-5 h-5" />
            </button>
            <h1 class="text-lg font-semibold">Schedule and rerun report</h1>
            <p class="text-sm text-gray-500">Schedule this report to run on a regular basis</p>
            <hr class="my-4" />
            <div>
                <div class="mt-4">
                    <label class="block text-sm font-medium text-gray-700 mb-2">Schedule Frequency</label>
                    <select v-model="selectedSchedule" class="w-full rounded-md border border-gray-200 px-3 py-2 text-sm">
                        <option v-for="option in cronOptions" :key="option.value" :value="option.value">
                            {{ option.label }}
                        </option>
                    </select>
                </div>

                <p v-if="report.last_run_at" class="mt-4 text-sm text-gray-500">
                    Last run: {{ formatDate(report.last_run_at) }}
                </p>
            </div>

            <!-- Notification subscribers (save-based, not send-now) -->
            <div v-if="smtpEnabled && selectedSchedule !== 'None'" class="border-t border-gray-200 pt-4 mt-4">
                <div class="flex items-center gap-2 text-sm font-medium text-gray-700 mb-1">
                    <Icon name="heroicons:envelope" class="w-4 h-4" />
                    Notify after each run
                </div>
                <p class="text-xs text-gray-400 mb-3">Recipients will receive an email with results after each scheduled run.</p>

                <!-- Recipient input -->
                <div class="flex flex-wrap items-center gap-1.5 border border-gray-200 rounded-md px-2 py-1.5 min-h-[38px] focus-within:ring-1 focus-within:ring-blue-500 focus-within:border-blue-500 bg-white">
                    <span v-for="(sub, idx) in subscribers" :key="idx"
                        class="inline-flex items-center gap-1 bg-gray-100 text-gray-700 text-xs px-2 py-0.5 rounded-full">
                        {{ sub.type === 'user' ? getMemberName(sub.id) : sub.address }}
                        <button @click="removeSubscriber(idx)" class="hover:text-red-500 outline-none">
                            <Icon name="heroicons:x-mark" class="w-3 h-3" />
                        </button>
                    </span>
                    <div class="relative flex-1 min-w-[140px]">
                        <input ref="inputRef" v-model="inputValue" type="text"
                            class="w-full border-none outline-none text-sm bg-transparent p-0"
                            placeholder="Type email or pick a member..."
                            @keydown.enter.prevent="handleEnter"
                            @keydown.,.prevent="handleComma"
                            @keydown.backspace="handleBackspace"
                            @input="onInput"
                            @focus="showDropdown = true"
                            @blur="onBlur" />
                        <!-- Autocomplete dropdown -->
                        <div v-if="showDropdown && filteredMembers.length > 0"
                            class="absolute left-0 top-full mt-1 w-64 bg-white border border-gray-200 rounded-md shadow-lg z-50 max-h-40 overflow-y-auto">
                            <button v-for="member in filteredMembers" :key="member.id"
                                class="w-full text-left px-3 py-2 text-sm hover:bg-gray-50 flex flex-col"
                                @mousedown.prevent="addMember(member)">
                                <span class="text-gray-900">{{ member.name || member.email }}</span>
                                <span v-if="member.name" class="text-xs text-gray-400">{{ member.email }}</span>
                            </button>
                        </div>
                    </div>
                </div>
            </div>

            <div class="border-t border-gray-200 pt-4 mt-8">
                <div class="flex justify-end space-x-2">
                    <button
                        @click="cronModalOpen = false"
                        class="px-3 py-1.5 text-xs font-medium text-gray-700 bg-white border border-gray-300 rounded-md hover:bg-gray-50"
                    >
                        Cancel
                    </button>
                    <button
                        @click="scheduleReport"
                        :disabled="isSaving"
                        class="inline-flex items-center gap-1.5 px-3 py-1.5 text-xs font-medium text-white bg-blue-500 border border-transparent rounded-md hover:bg-blue-600 disabled:opacity-40"
                    >
                        <Spinner v-if="isSaving" class="w-3.5 h-3.5" />
                        Schedule
                    </button>
                </div>
            </div>
        </div>
    </UModal>
</template>

<script lang="ts" setup>
const cronModalOpen = ref(false);
const toast = useToast();
const { smtpEnabled } = useAppSettings();
const props = defineProps<{
    report: any
}>();

const report = ref(props.report);
const isSaving = ref(false);

const reportUrl = computed(() => `${window.location.origin}/r/${report.value.id}`);

const selectedSchedule = ref(props.report.cron_schedule || 'None');

function formatDate(date: string) {
    return new Date(date).toLocaleString();
}

const cronOptions = ref([
    { value: 'None', label: 'None' },
    //{ value: '*/5 * * * * *', label: 'Every 5 Seconds' },
    { value: '*/15 * * * *', label: 'Every 15 Minutes' },
    { value: '0 * * * *', label: 'Hourly' },
    { value: '0 0 * * *', label: 'Daily (Midnight)' },
    { value: '0 0 * * 1', label: 'Weekly (Monday Midnight)' },
]);

// ---- Subscriber management ----

type Subscriber = { type: 'user'; id: string } | { type: 'email'; address: string }

// Initialize from existing report data
const subscribers = ref<Subscriber[]>(
    (props.report.notification_subscribers || []).map((s: any) => ({ ...s }))
);

const inputRef = ref<HTMLInputElement | null>(null);
const inputValue = ref('');
const showDropdown = ref(false);

// Fetch org members for autocomplete
const members = ref<{ id: string; name: string; email: string }[]>([]);
const fetchMembers = async () => {
    try {
        const res = await useMyFetch('/organization/members');
        if (res.data.value) {
            members.value = (res.data.value as any[]).map((u: any) => ({
                id: u.id,
                name: u.name || '',
                email: u.email,
            }));
        }
    } catch {}
};
fetchMembers();

const getMemberName = (userId: string | undefined) => {
    if (!userId) return 'Unknown';
    const m = members.value.find((m) => m.id === userId);
    return m ? (m.name || m.email) : userId;
};

const subscriberEmails = computed(() => {
    return subscribers.value.map((s) => {
        if (s.type === 'email') return s.address;
        const m = members.value.find((m) => m.id === (s as any).id);
        return m?.email;
    });
});

const filteredMembers = computed(() => {
    const q = inputValue.value.toLowerCase().trim();
    if (!q) return [];
    return members.value.filter(
        (m) =>
            !subscriberEmails.value.includes(m.email) &&
            (m.email.toLowerCase().includes(q) || m.name.toLowerCase().includes(q))
    ).slice(0, 5);
});

const isValidEmail = (email: string) => /^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(email);

const addEmail = (email: string) => {
    const clean = email.trim().toLowerCase();
    if (clean && isValidEmail(clean) && !subscriberEmails.value.includes(clean)) {
        subscribers.value.push({ type: 'email', address: clean });
        inputValue.value = '';
    }
};

const addMember = (member: { id: string; name: string; email: string }) => {
    if (!subscribers.value.some((s) => s.type === 'user' && (s as any).id === member.id)) {
        subscribers.value.push({ type: 'user', id: member.id });
    }
    inputValue.value = '';
    showDropdown.value = false;
};

const removeSubscriber = (idx: number) => {
    subscribers.value.splice(idx, 1);
};

const handleEnter = () => {
    if (filteredMembers.value.length > 0) {
        addMember(filteredMembers.value[0]);
    } else {
        addEmail(inputValue.value);
    }
};

const handleComma = () => {
    addEmail(inputValue.value);
};

const handleBackspace = () => {
    if (!inputValue.value && subscribers.value.length > 0) {
        subscribers.value.pop();
    }
};

const onInput = () => {
    showDropdown.value = true;
};

const onBlur = () => {
    setTimeout(() => {
        showDropdown.value = false;
        if (inputValue.value && isValidEmail(inputValue.value)) {
            addEmail(inputValue.value);
        }
    }, 200);
};

// ---- Schedule (saves subscribers too) ----

async function scheduleReport() {
    isSaving.value = true;
    try {
        const response = await useMyFetch(`/api/reports/${report.value.id}/schedule`, {
            method: 'POST',
            body: {
                cron_expression: selectedSchedule.value,
                notification_subscribers: subscribers.value.length > 0 ? subscribers.value : null,
            },
        });
        if (response.data.value) {
            toast.add({
                title: 'Report scheduled',
                color: 'green',
                description: subscribers.value.length > 0
                    ? `Scheduled with ${subscribers.value.length} notification recipient(s)`
                    : 'Report scheduled successfully',
            });
            cronModalOpen.value = false;
        } else {
            toast.add({
                title: 'Error',
                color: 'red',
                description: 'Failed to schedule report',
            });
        }
    } catch {
        toast.add({
            title: 'Error',
            color: 'red',
            description: 'Failed to schedule report',
        });
    } finally {
        isSaving.value = false;
    }
}
</script>
