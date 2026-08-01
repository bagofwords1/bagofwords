<template>
    <UModal v-model="isOpen" :ui="{ width: viewMode ? 'sm:max-w-4xl' : 'sm:max-w-2xl' }">
        <UCard :ui="{ body: { padding: 'px-5 py-4 sm:p-5' }, header: { padding: 'px-5 py-3 sm:px-5 sm:py-3' }, footer: { padding: 'px-5 py-3 sm:px-5 sm:py-3' } }">
            <template #header>
                <div class="flex items-center justify-between">
                    <div class="min-w-0">
                        <h3 class="text-sm font-semibold text-gray-900 dark:text-white">
                            {{ viewMode ? $t('scheduledPrompt.viewTitle') : (isEditing ? $t('scheduledPrompt.editTitle') : $t('scheduledPrompt.newTitle')) }}
                        </h3>
                        <NuxtLink
                            v-if="isEditing && reportId"
                            :to="`/reports/${reportId}`"
                            class="mt-0.5 inline-flex items-center gap-1 text-[11px] text-blue-500 hover:text-blue-600"
                            @click="isOpen = false"
                        >
                            <Icon name="heroicons:chat-bubble-left-right" class="w-3 h-3" />
                            {{ reportTitle }}
                        </NuxtLink>
                    </div>
                    <UButton color="gray" variant="ghost" icon="i-heroicons-x-mark-20-solid" size="xs" @click="isOpen = false" />
                </div>
            </template>

            <!-- Two columns while viewing: the task on the left, its history and
                 provenance on the right. Editing collapses to one column so the
                 prompt box keeps its full width. -->
            <div :class="viewMode ? 'grid grid-cols-1 md:grid-cols-5 gap-5' : ''">
            <div :class="viewMode ? 'md:col-span-3 min-w-0' : ''">

            <!-- ── Read-only summary ─────────────────────────────────────── -->
            <template v-if="viewMode">
                <div class="text-sm font-medium text-gray-900 dark:text-white" data-testid="view-title">
                    {{ taskTitle || $t('scheduled.untitledTask') }}
                </div>
                <div class="mt-2 rounded-lg border border-gray-100 dark:border-gray-800 bg-gray-50/60 dark:bg-gray-900/40 px-3 py-2 text-sm text-gray-700 dark:text-gray-300 whitespace-pre-wrap" dir="auto" data-testid="view-prompt">
                    {{ promptText || $t('scheduled.untitledTask') }}
                </div>

                <dl class="mt-4 space-y-2.5">
                    <div class="flex items-start gap-2">
                        <dt class="w-24 shrink-0 text-xs text-gray-400">{{ $t('scheduledPrompt.schedule') }}</dt>
                        <dd class="text-xs text-gray-700 dark:text-gray-300">{{ getCronLabel(props.scheduledPrompt?.cron_schedule) }}</dd>
                    </div>
                    <div class="flex items-start gap-2">
                        <dt class="w-24 shrink-0 text-xs text-gray-400">{{ $t('scheduledPrompt.agents') }}</dt>
                        <dd class="text-xs text-gray-700 dark:text-gray-300" data-testid="view-agents">
                            <span v-if="viewAgents.length">{{ viewAgents.map((a) => a.name).join(', ') }}</span>
                            <span v-else class="text-gray-400">{{ $t('scheduledPrompt.noAgents') }}</span>
                        </dd>
                    </div>
                    <div v-if="viewFiles.length" class="flex items-start gap-2">
                        <dt class="w-24 shrink-0 text-xs text-gray-400">{{ $t('scheduledPrompt.files') }}</dt>
                        <dd class="text-xs text-gray-700 dark:text-gray-300">{{ viewFiles.map((f) => f.filename || f.name).join(', ') }}</dd>
                    </div>
                    <div class="flex items-start gap-2">
                        <dt class="w-24 shrink-0 text-xs text-gray-400">{{ $t('scheduledPrompt.model') }}</dt>
                        <dd class="text-xs text-gray-700 dark:text-gray-300" data-testid="view-model">{{ viewModelLabel }}</dd>
                    </div>
                    <div class="flex items-start gap-2">
                        <dt class="w-24 shrink-0 text-xs text-gray-400">{{ $t('scheduledPrompt.mode') }}</dt>
                        <dd class="text-xs text-gray-700 dark:text-gray-300">
                            {{ viewMode2 === 'deep' ? $t('scheduledPrompt.modeDeep') : $t('scheduledPrompt.modeChat') }}
                        </dd>
                    </div>
                    <div class="flex items-start gap-2">
                        <dt class="w-24 shrink-0 text-xs text-gray-400">{{ $t('scheduledPrompt.outputLabel') }}</dt>
                        <dd class="text-xs text-gray-700 dark:text-gray-300">
                            {{ spawnNewReport ? $t('scheduledPrompt.outputNewReport') : $t('scheduledPrompt.outputSameReport') }}
                        </dd>
                    </div>
                    <div v-if="smtpEnabled" class="flex items-start gap-2">
                        <dt class="w-24 shrink-0 text-xs text-gray-400">{{ $t('scheduledPrompt.emailLabel') }}</dt>
                        <dd class="text-xs text-gray-700 dark:text-gray-300">
                            <template v-if="subscribers.length">
                                {{ subscribers.map((s) => s.type === 'user' ? getMemberName(s.id) : s.address).join(', ') }}
                            </template>
                            <span v-else class="text-gray-400">{{ $t('scheduledPrompt.notifyNobody') }}</span>
                        </dd>
                    </div>
                    <div class="flex items-center gap-2">
                        <dt class="w-24 shrink-0 text-xs text-gray-400">{{ $t('scheduledPrompt.active') }}</dt>
                        <dd>
                            <button
                                @click="toggleActiveInPlace"
                                :disabled="isSaving"
                                data-testid="view-active"
                                class="relative inline-flex h-4 w-7 items-center rounded-full transition-colors disabled:opacity-50"
                                :class="isActive ? 'bg-blue-500' : 'bg-gray-300 dark:bg-gray-700'"
                                :aria-pressed="isActive"
                            >
                                <span class="inline-block h-3 w-3 rounded-full bg-white transition-transform" :class="isActive ? 'translate-x-3.5' : 'translate-x-0.5'" />
                            </button>
                        </dd>
                    </div>
                </dl>
            </template>

            <!-- ── Edit form ─────────────────────────────────────────────── -->
            <template v-else>

            <!-- Title -->
            <div class="mb-3">
                <div class="text-xs text-gray-500 dark:text-gray-400 mb-1.5">{{ $t('scheduledPrompt.titleLabel') }}</div>
                <input
                    v-model="taskTitle"
                    type="text"
                    maxlength="120"
                    :placeholder="$t('scheduledPrompt.titlePlaceholder')"
                    class="w-full text-sm border border-gray-200 dark:border-gray-700 rounded px-3 py-2 bg-white dark:bg-gray-900 dark:text-gray-100 dark:placeholder-gray-500 focus:outline-none focus:ring-1 focus:ring-blue-500 focus:border-blue-500"
                    data-testid="scheduled-task-title"
                />
            </div>

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
                :flush="true"
                :rows="5"
                @submitCompletion="handlePromptSubmit"
                @update:modelValue="onPromptTextChange"
            />

            <!-- Schedule -->
            <div class="mt-3">
                <div class="text-xs text-gray-500 dark:text-gray-400 mb-1.5">{{ $t('scheduledPrompt.schedule') }}</div>

                <div class="flex gap-0.5 p-0.5 bg-gray-100 dark:bg-gray-800 rounded w-fit mb-2">
                    <button
                        v-for="t in scheduleTypes"
                        :key="t.value"
                        class="px-2 py-0.5 text-[11px] rounded transition-colors"
                        :class="scheduleType === t.value ? 'bg-white dark:bg-gray-900 text-gray-900 dark:text-white shadow-sm font-medium' : 'text-gray-400 hover:text-gray-600'"
                        @click="scheduleType = t.value"
                    >
                        {{ t.label }}
                    </button>
                </div>

                <div v-if="scheduleType === 'once'" class="flex items-center gap-1.5 text-xs text-gray-600 dark:text-gray-400">
                    <span>{{ $t('scheduledPrompt.runIn') }}</span>
                    <input v-model.number="delayAmount" type="number" min="1" class="w-14 rounded border border-gray-200 dark:border-gray-700 px-1.5 py-1 text-xs text-center" />
                    <select v-model="delayUnit" class="rounded border border-gray-200 dark:border-gray-700 px-1.5 py-1 text-xs">
                        <option value="minutes">{{ $t('scheduledPrompt.unitMinutes') }}</option>
                        <option value="hours">{{ $t('scheduledPrompt.unitHours') }}</option>
                        <option value="days">{{ $t('scheduledPrompt.unitDays') }}</option>
                    </select>
                </div>

                <div v-else class="flex items-center gap-1.5 text-xs text-gray-600 dark:text-gray-400 flex-wrap">
                    <span>{{ $t('scheduledPrompt.every') }}</span>
                    <template v-if="recurInterval === 'minutes' || recurInterval === 'hours'">
                        <input v-model.number="recurEveryN" type="number" min="1" :max="recurInterval === 'minutes' ? 59 : 23"
                            class="w-12 rounded border border-gray-200 dark:border-gray-700 px-1 py-1 text-xs text-center" />
                    </template>
                    <select v-model="recurInterval" class="rounded border border-gray-200 dark:border-gray-700 px-1.5 py-1 text-xs">
                        <option value="minutes">{{ $t('scheduledPrompt.intervalMinutes') }}</option>
                        <option value="hours">{{ $t('scheduledPrompt.intervalHours') }}</option>
                        <option value="day">{{ $t('scheduledPrompt.intervalDay') }}</option>
                        <option value="weekdays">{{ $t('scheduledPrompt.intervalWeekdays') }}</option>
                        <option value="week">{{ $t('scheduledPrompt.intervalWeek') }}</option>
                        <option value="month">{{ $t('scheduledPrompt.intervalMonth') }}</option>
                    </select>
                    <template v-if="recurInterval === 'day' || recurInterval === 'weekdays' || recurInterval === 'week' || recurInterval === 'month'">
                        <span>{{ $t('scheduledPrompt.at') }}</span>
                        <select v-model="recurHour" class="rounded border border-gray-200 dark:border-gray-700 px-1.5 py-1 text-xs">
                            <option v-for="h in 24" :key="h - 1" :value="h - 1">{{ String(h - 1).padStart(2, '0') }}:00</option>
                        </select>
                    </template>
                    <template v-if="recurInterval === 'week'">
                        <span>{{ $t('scheduledPrompt.on') }}</span>
                        <div class="flex items-center gap-1">
                            <button
                                v-for="d in weekdays"
                                :key="d.value"
                                type="button"
                                @click="toggleRecurDay(d.value)"
                                :title="d.label"
                                :aria-pressed="recurDays.includes(d.value)"
                                class="flex items-center justify-center w-6 h-6 rounded-full text-[11px] font-medium border transition-colors"
                                :class="recurDays.includes(d.value)
                                    ? 'bg-blue-500 text-white border-blue-500'
                                    : 'bg-white dark:bg-gray-900 text-gray-500 dark:text-gray-400 border-gray-200 dark:border-gray-700 hover:border-gray-300 dark:hover:border-gray-600'"
                            >
                                {{ d.short }}
                            </button>
                        </div>
                    </template>
                    <template v-if="recurInterval === 'month'">
                        <span>{{ $t('scheduledPrompt.onDay') }}</span>
                        <select v-model="recurDayOfMonth" class="rounded border border-gray-200 dark:border-gray-700 px-1.5 py-1 text-xs">
                            <option v-for="d in 28" :key="d" :value="d">{{ d }}</option>
                        </select>
                    </template>
                </div>
            </div>

            <!-- Output routing: run in this report vs a fresh report per run -->
            <div class="mt-3">
                <div class="text-xs text-gray-500 dark:text-gray-400 mb-1.5">{{ $t('scheduledPrompt.outputLabel') }}</div>
                <div class="flex gap-0.5 p-0.5 bg-gray-100 dark:bg-gray-800 rounded w-fit" data-testid="output-routing">
                    <button
                        type="button"
                        class="inline-flex items-center gap-1 px-2 py-0.5 text-[11px] rounded transition-colors"
                        :class="!spawnNewReport ? 'bg-white dark:bg-gray-900 text-gray-900 dark:text-white shadow-sm font-medium' : 'text-gray-400 hover:text-gray-600'"
                        data-testid="routing-same-report"
                        @click="spawnNewReport = false"
                    >
                        <Icon name="heroicons:chat-bubble-left-right" class="w-3 h-3" />
                        {{ $t('scheduledPrompt.outputSameReport') }}
                    </button>
                    <button
                        type="button"
                        class="inline-flex items-center gap-1 px-2 py-0.5 text-[11px] rounded transition-colors"
                        :class="spawnNewReport ? 'bg-white dark:bg-gray-900 text-gray-900 dark:text-white shadow-sm font-medium' : 'text-gray-400 hover:text-gray-600'"
                        data-testid="routing-new-report"
                        @click="spawnNewReport = true"
                    >
                        <Icon name="heroicons:document-plus" class="w-3 h-3" />
                        {{ $t('scheduledPrompt.outputNewReport') }}
                    </button>
                </div>
                <p class="mt-1 text-[11px] text-gray-400">
                    {{ spawnNewReport ? $t('scheduledPrompt.outputNewReportHint') : $t('scheduledPrompt.outputSameReportHint') }}
                </p>
            </div>

            <!-- Active toggle (edit mode) -->
            <div v-if="isEditing" class="mt-3 flex items-center justify-between">
                <span class="text-xs text-gray-500 dark:text-gray-400">{{ $t('scheduledPrompt.active') }}</span>
                <button
                    @click="isActive = !isActive"
                    class="relative inline-flex h-4 w-7 items-center rounded-full transition-colors"
                    :class="isActive ? 'bg-blue-500' : 'bg-gray-300'"
                >
                    <span class="inline-block h-3 w-3 rounded-full bg-white transition-transform" :class="isActive ? 'translate-x-3.5' : 'translate-x-0.5'" />
                </button>
            </div>

            <!-- Notification subscribers -->
            <div v-if="smtpEnabled" class="border-t border-gray-100 dark:border-gray-800 pt-3 mt-3">
                <label class="flex items-start gap-2 cursor-pointer select-none">
                    <UCheckbox v-model="sendSummaryEmail" @change="userTouchedEmailToggle = true" class="mt-0.5" />
                    <span class="flex items-center gap-1.5 text-xs text-gray-600 dark:text-gray-400">
                        <Icon name="heroicons:envelope" class="w-3 h-3" />
                        {{ $t('scheduledPrompt.notifyAfterRun') }}
                    </span>
                </label>
                <p v-if="promptMentionsEmail" class="text-[11px] text-amber-600 mt-1 ms-6">
                    {{ $t('scheduledPrompt.promptSendsEmailHint') }}
                </p>
                <div v-if="sendSummaryEmail" class="flex flex-wrap items-center gap-1 border border-gray-200 dark:border-gray-700 rounded px-2 py-1 min-h-[30px] mt-2 ms-6 focus-within:ring-1 focus-within:ring-blue-500 focus-within:border-blue-500 bg-white dark:bg-gray-900">
                    <span v-for="(sub, idx) in subscribers" :key="idx"
                        class="inline-flex items-center gap-0.5 bg-gray-100 dark:bg-gray-800 text-gray-600 dark:text-gray-400 text-[11px] px-1.5 py-0.5 rounded-full">
                        {{ sub.type === 'user' ? getMemberName(sub.id) : sub.address }}
                        <button @click="removeSubscriber(idx)" class="hover:text-red-500 outline-none">
                            <Icon name="heroicons:x-mark" class="w-2.5 h-2.5" />
                        </button>
                    </span>
                    <div class="relative flex-1 min-w-[120px]">
                        <input ref="inputRef" v-model="inputValue" type="text"
                            class="w-full border-none outline-none text-xs bg-transparent p-0"
                            :placeholder="$t('scheduledPrompt.emailOrMemberPlaceholder')"
                            @keydown.enter.prevent="handleEnter"
                            @keydown.,.prevent="handleComma"
                            @keydown.backspace="handleBackspace"
                            @input="onMemberInput"
                            @focus="showMemberDropdown = true"
                            @blur="onBlur" />
                        <div v-if="showMemberDropdown && filteredMembers.length > 0"
                            class="absolute start-0 top-full mt-1 w-56 bg-white dark:bg-gray-900 border border-gray-200 dark:border-gray-700 rounded shadow-lg z-50 max-h-32 overflow-y-auto">
                            <button v-for="member in filteredMembers" :key="member.id"
                                class="w-full text-start px-2 py-1.5 text-xs hover:bg-gray-50 dark:hover:bg-gray-800 flex flex-col"
                                @mousedown.prevent="addMember(member)">
                                <span class="text-gray-900 dark:text-white">{{ member.name || member.email }}</span>
                                <span v-if="member.name" class="text-[10px] text-gray-400">{{ member.email }}</span>
                            </button>
                        </div>
                    </div>
                </div>
            </div>

            </template>
            </div>

            <!-- ── History + provenance (view mode only) ─────────────────── -->
            <aside v-if="viewMode" class="md:col-span-2 min-w-0 md:border-s md:ps-5 border-gray-100 dark:border-gray-800" data-testid="runs-column">
                <div class="flex items-baseline justify-between">
                    <h4 class="text-[11px] font-semibold uppercase tracking-wider text-gray-400">{{ $t('scheduledPrompt.previousRuns') }}</h4>
                    <span v-if="runsTotal" class="text-[11px] text-gray-300 dark:text-gray-600">{{ runsTotal }}</span>
                </div>

                <div v-if="runsLoading" class="mt-2 text-[11px] text-gray-400 inline-flex items-center">
                    <Spinner class="me-1 w-3 h-3" /> {{ $t('scheduled.loading') }}
                </div>
                <template v-else>
                    <!-- Host-report mode appends every run to one report, so
                         there is no per-run history to list. -->
                    <p v-if="!runsSpawnReports" class="mt-2 text-[11px] text-gray-400 leading-relaxed">
                        {{ $t('scheduledPrompt.runsInHostReport') }}
                    </p>
                    <ul v-else-if="runs.length" class="mt-1.5 -mx-2">
                        <li v-for="run in runs" :key="run.report_id">
                            <NuxtLink
                                :to="`/reports/${run.report_id}`"
                                class="flex items-center gap-2 px-2 py-1.5 rounded hover:bg-gray-50 dark:hover:bg-gray-800/60"
                                @click="isOpen = false"
                            >
                                <AutomationsRunStatusDot :report-id="run.report_id" :status="run.status" />
                                <span class="flex-1 min-w-0 truncate text-[12px] text-gray-700 dark:text-gray-300">{{ run.title || $t('scheduled.untitledReport') }}</span>
                                <span class="shrink-0 text-[10px] text-gray-400">{{ formatRunDate(run.created_at) }}</span>
                            </NuxtLink>
                        </li>
                    </ul>
                    <p v-else class="mt-2 text-[11px] text-gray-400">{{ $t('scheduledPrompt.noRuns') }}</p>

                    <NuxtLink
                        v-if="runsSpawnReports && runsTotal > runs.length"
                        :to="`/reports/${reportId}`"
                        class="mt-2 inline-block text-[11px] text-blue-500 hover:text-blue-600"
                        @click="isOpen = false"
                    >{{ $t('scheduledPrompt.runsMore', { n: runsTotal - runs.length }) }}</NuxtLink>
                </template>

                <!-- Provenance -->
                <dl class="mt-5 pt-4 border-t border-gray-100 dark:border-gray-800 space-y-2">
                    <div v-if="nextRunLabel" class="flex items-start gap-2">
                        <dt class="w-20 shrink-0 text-[11px] text-gray-400">{{ $t('scheduledPrompt.nextRun') }}</dt>
                        <dd class="text-[11px] text-gray-600 dark:text-gray-300">{{ nextRunLabel }}</dd>
                    </div>
                    <!-- Only where the runs list cannot show it: in host-report
                         mode every run appends to one report, so there are no
                         dated rows above to read the last run from. -->
                    <div v-if="!runsSpawnReports" class="flex items-start gap-2">
                        <dt class="w-20 shrink-0 text-[11px] text-gray-400">{{ $t('scheduledPrompt.lastRun') }}</dt>
                        <dd class="text-[11px] text-gray-600 dark:text-gray-300">
                            {{ props.scheduledPrompt?.last_run_at ? formatRunDate(props.scheduledPrompt.last_run_at) : $t('scheduledPrompt.never') }}
                        </dd>
                    </div>
                    <div class="flex items-start gap-2">
                        <dt class="w-20 shrink-0 text-[11px] text-gray-400">{{ $t('scheduledPrompt.created') }}</dt>
                        <dd class="text-[11px] text-gray-600 dark:text-gray-300">{{ formatRunDate(props.scheduledPrompt?.created_at) }}</dd>
                    </div>
                    <div v-if="props.scheduledPrompt?.user_name" class="flex items-start gap-2">
                        <dt class="w-20 shrink-0 text-[11px] text-gray-400">{{ $t('scheduledPrompt.createdBy') }}</dt>
                        <dd class="text-[11px] text-gray-600 dark:text-gray-300 truncate">{{ props.scheduledPrompt.user_name }}</dd>
                    </div>
                </dl>
            </aside>
            </div>

            <template #footer>
                <div class="flex items-center justify-between gap-2">
                    <UButton
                        v-if="isEditing"
                        color="red"
                        variant="ghost"
                        size="xs"
                        icon="i-heroicons-trash"
                        :loading="isDeleting"
                        @click="deleteScheduledPrompt"
                    >{{ $t('scheduledPrompt.delete') }}</UButton>
                    <span v-else />
                    <div class="flex justify-end gap-2">
                        <UButton color="gray" variant="ghost" size="xs" @click="onCancel">{{ $t('scheduledPrompt.cancel') }}</UButton>
                        <UButton
                            color="gray"
                            variant="soft"
                            size="xs"
                            icon="i-heroicons-play"
                            :loading="isRunning"
                            :disabled="isSaving"
                            @click="runNow"
                        >{{ $t('scheduledPrompt.runNow') }}</UButton>
                        <UButton v-if="viewMode" color="blue" size="xs" icon="i-heroicons-pencil-square" data-testid="view-edit" @click="viewMode = false">
                            {{ $t('scheduledPrompt.edit') }}
                        </UButton>
                        <UButton v-else color="blue" size="xs" :loading="isSaving" :disabled="isRunning" @click="saveFromCurrentState">{{ isEditing ? $t('scheduledPrompt.update') : $t('scheduledPrompt.scheduleAction') }}</UButton>
                    </div>
                </div>
            </template>
        </UCard>
    </UModal>
</template>

<script lang="ts" setup>
import Spinner from '@/components/Spinner.vue'
import PromptBoxV2 from '@/components/prompt/PromptBoxV2.vue'
import { buildRecurringCron, parseRecurringCron, type RecurInterval } from '@/composables/useScheduleBuilder'

const { t } = useI18n()
const toast = useToast()
const { smtpEnabled } = useAppSettings()
const { data: currentUser } = useAuth()

const props = defineProps<{
    reportId: string
    scheduledPrompt?: any
    initialDataSources?: any[]
    draftContent?: string
    draftMode?: 'chat' | 'deep'
    draftModel?: string
}>()

const emit = defineEmits(['saved', 'deleted'])

const isOpen = defineModel<boolean>({ default: false })
const isSaving = ref(false)
const isRunning = ref(false)
const isDeleting = ref(false)
const promptBoxRef = ref<InstanceType<typeof PromptBoxV2> | null>(null)

const isEditing = computed(() => !!props.scheduledPrompt)
const reportTitle = computed(() => props.scheduledPrompt?.report?.title || t('scheduledPrompt.viewReport'))

const initialContent = computed(() => props.scheduledPrompt?.prompt?.content || props.draftContent || '')
const initialMode = computed(() => (props.scheduledPrompt?.prompt?.mode as 'chat' | 'deep') || props.draftMode || 'chat')
const initialModel = computed(() => props.scheduledPrompt?.prompt?.model_id || props.draftModel || '')
const initialDataSources = computed(() => props.initialDataSources || [])

const taskTitle = ref<string>(props.scheduledPrompt?.title || '')
const isActive = ref(props.scheduledPrompt?.is_active ?? true)

// An existing task opens read-only — you usually come here to check on it, not
// to change it. A new one goes straight to the form.
const viewMode = ref(!!props.scheduledPrompt)
const { getCronLabel } = useCronLabel()
// Handles naive-UTC strings and renders in the org's timezone.
const { formatDateTime } = useFormatDate()

// ── Previous runs: the reports this schedule produced ──────────────────────
const { fetchActivity } = useReportActivity()
const runs = ref<any[]>([])
const runsTotal = ref(0)
const runsSpawnReports = ref(true)
const runsLoading = ref(false)

async function fetchRuns() {
    const sp = props.scheduledPrompt
    if (!sp?.id || !props.reportId) { runs.value = []; runsTotal.value = 0; return }
    runsLoading.value = true
    try {
        const { data } = await useMyFetch(`/reports/${props.reportId}/scheduled-prompts/${sp.id}/runs`)
        const d = data.value as any
        runs.value = d?.runs || []
        runsTotal.value = d?.total || 0
        runsSpawnReports.value = d?.spawns_reports !== false
        // Track these reports so a run that is executing right now shows a
        // spinner instead of the verdict of its previous turn.
        fetchActivity(runs.value.map((r: any) => r.report_id))
    } catch {
        runs.value = []; runsTotal.value = 0
    } finally {
        runsLoading.value = false
    }
}

function formatRunDate(value?: string): string {
    return value ? formatDateTime(value) : ''
}

// ── Run spec shown as facts in the summary ─────────────────────────────────
// Agents and files live on the report the schedule runs against; mode and model
// live on the prompt (they are what actually executes), falling back to the
// report's own defaults.
const viewAgents = ref<any[]>([])
const viewFiles = ref<any[]>([])
const viewModels = ref<any[]>([])
const viewReportMode = ref<string>('')
const viewReportModelId = ref<string>('')

async function fetchViewDetails() {
    if (!props.reportId) return
    const [rep, files, models] = await Promise.all([
        useMyFetch(`/reports/${props.reportId}`).catch(() => null),
        useMyFetch(`/reports/${props.reportId}/files`).catch(() => null),
        viewModels.value.length ? Promise.resolve(null) : useMyFetch('/llm/models?is_enabled=true').catch(() => null),
    ])
    const r = (rep as any)?.data?.value
    viewAgents.value = r?.data_sources || []
    viewReportMode.value = r?.mode || ''
    viewReportModelId.value = r?.model_id || ''
    viewFiles.value = ((files as any)?.data?.value as any[]) || []
    const m = (models as any)?.data?.value
    if (m) viewModels.value = (m as any[]) || []
}

const viewMode2 = computed(() => props.scheduledPrompt?.prompt?.mode || viewReportMode.value || 'chat')

const viewModelLabel = computed(() => {
    const id = props.scheduledPrompt?.prompt?.model_id || viewReportModelId.value
    if (!id) return t('scheduledPrompt.modelAuto')
    const m = viewModels.value.find((x: any) => x.id === id)
    return m?.name || t('scheduledPrompt.modelAuto')
})

const nextRunLabel = computed(() => {
    const next = props.scheduledPrompt?.next_run_at
    if (!next || !isActive.value) return ''
    return formatRunDate(next)
})

// Pausing from the read-only view is a one-field write — no need to enter the
// form for it.
async function toggleActiveInPlace() {
    const sp = props.scheduledPrompt
    if (!sp?.id || isSaving.value) return
    const next = !isActive.value
    isSaving.value = true
    isActive.value = next
    try {
        const res = await useMyFetch(`/api/reports/${props.reportId}/scheduled-prompts/${sp.id}`, {
            method: 'PUT', body: { is_active: next },
        })
        if ((res as any).error?.value) throw new Error('update failed')
        emit('saved')
    } catch {
        isActive.value = !next
        toast.add({ title: t('scheduledPrompt.toastError'), color: 'red', description: t('scheduledPrompt.toastSaveFailed') })
    } finally {
        isSaving.value = false
    }
}

// Cancel returns to the summary when editing an existing task; otherwise closes.
function onCancel() {
    if (!viewMode.value && props.scheduledPrompt) { viewMode.value = true; return }
    isOpen.value = false
}
// Output routing: false = run in this report (keeps cross-run memory),
// true = spawn a fresh, dated report per run (clean snapshots).
const spawnNewReport = ref<boolean>(props.scheduledPrompt?.spawn_new_report ?? false)

// ---- Summary-email toggle + prompt-intent heuristic ----
// Phrases that signal the prompt itself asks to email/notify the user. When the
// prompt expresses email intent, the agent's send_email tool delivers the
// message during the run, so we default the static summary email OFF to avoid
// sending two emails for one run. The user can always override the checkbox.
const EMAIL_INTENT_RE = /\b(e-?mail\s+(me|us|to\s+me)|(send|mail|notify|alert|ping|message|text)\s+(me|us)\b|let\s+(me|us)\s+know|(send|shoot|drop)\s+.{0,40}?\be-?mail\b|\be-?mail\b.{0,40}?\b(summary|report|results?|me|us)\b)/i

const promptText = ref(initialContent.value)
const userTouchedEmailToggle = ref(isEditing.value)
const sendSummaryEmail = ref(
    isEditing.value
        ? (props.scheduledPrompt?.notification_subscribers || []).length > 0
        : !EMAIL_INTENT_RE.test(initialContent.value)
)

const promptMentionsEmail = computed(() => EMAIL_INTENT_RE.test(promptText.value))

function onPromptTextChange(text: string) {
    promptText.value = text || ''
    // Until the user manually toggles the checkbox, keep it in sync with intent.
    if (!userTouchedEmailToggle.value) {
        sendSummaryEmail.value = !promptMentionsEmail.value
    }
}

// Schedule type: one-time or recurring
const scheduleTypes = computed(() => [
    { value: 'once' as const, label: t('scheduledPrompt.typeOnce') },
    { value: 'recurring' as const, label: t('scheduledPrompt.typeRecurring') },
])
const scheduleType = ref<'once' | 'recurring'>('recurring')
const delayAmount = ref(1)
const delayUnit = ref<'minutes' | 'hours' | 'days'>('hours')

// Recurring structured inputs
const recurInterval = ref<RecurInterval>('day')
const recurEveryN = ref(15)
const recurHour = ref(8)
const recurDays = ref<number[]>([1])
const recurDayOfMonth = ref(1)
const weekdays = computed(() => [
    { value: 0, label: t('scheduledPrompt.dowSun'), short: t('scheduledPrompt.dowSunShort') },
    { value: 1, label: t('scheduledPrompt.dowMon'), short: t('scheduledPrompt.dowMonShort') },
    { value: 2, label: t('scheduledPrompt.dowTue'), short: t('scheduledPrompt.dowTueShort') },
    { value: 3, label: t('scheduledPrompt.dowWed'), short: t('scheduledPrompt.dowWedShort') },
    { value: 4, label: t('scheduledPrompt.dowThu'), short: t('scheduledPrompt.dowThuShort') },
    { value: 5, label: t('scheduledPrompt.dowFri'), short: t('scheduledPrompt.dowFriShort') },
    { value: 6, label: t('scheduledPrompt.dowSat'), short: t('scheduledPrompt.dowSatShort') },
])

function toggleRecurDay(value: number) {
    const idx = recurDays.value.indexOf(value)
    if (idx === -1) {
        recurDays.value = [...recurDays.value, value].sort((a, b) => a - b)
    } else if (recurDays.value.length > 1) {
        // Keep at least one day selected so the cron stays valid.
        recurDays.value = recurDays.value.filter((d) => d !== value)
    }
}

function parseCronToStructured(cron: string) {
    const patch = parseRecurringCron(cron)
    if (!patch) return
    if (patch.interval !== undefined) recurInterval.value = patch.interval
    if (patch.everyN !== undefined) recurEveryN.value = patch.everyN
    if (patch.hour !== undefined) recurHour.value = patch.hour
    if (patch.days !== undefined) recurDays.value = patch.days
    if (patch.dayOfMonth !== undefined) recurDayOfMonth.value = patch.dayOfMonth
}

// Hydrate the schedule form from the existing cron on setup. The watch below is
// not `immediate` (and can't be — its callback touches refs declared further
// down), so without this the first mount would keep the default schedule and
// show e.g. "day at 08:00" instead of the task's saved time.
if (props.scheduledPrompt?.cron_schedule) {
    parseCronToStructured(props.scheduledPrompt.cron_schedule)
}

// Reset form when scheduledPrompt changes
// Refresh the run history whenever the modal opens on a task.
watch(isOpen, (open) => {
    if (open) {
        viewMode.value = !!props.scheduledPrompt
        if (props.scheduledPrompt) { fetchRuns(); fetchViewDetails() }
    }
}, { immediate: true })

watch(() => props.scheduledPrompt, (sp) => {
    taskTitle.value = sp?.title || ''
    isActive.value = sp?.is_active ?? true
    viewMode.value = !!sp
    if (sp && isOpen.value) { fetchRuns(); fetchViewDetails() }
    spawnNewReport.value = sp?.spawn_new_report ?? false
    subscribers.value = (sp?.notification_subscribers || []).map((s: any) => ({ ...s }))
    promptText.value = sp?.prompt?.content || props.draftContent || ''
    if (sp) {
        // Editing an existing task: honor its saved email setting, don't re-guess.
        sendSummaryEmail.value = (sp.notification_subscribers || []).length > 0
        userTouchedEmailToggle.value = true
    } else {
        userTouchedEmailToggle.value = false
        sendSummaryEmail.value = !EMAIL_INTENT_RE.test(promptText.value)
    }
    scheduleType.value = 'recurring'
    if (sp?.cron_schedule) {
        parseCronToStructured(sp.cron_schedule)
    } else {
        recurInterval.value = 'day'
        recurEveryN.value = 15
        recurHour.value = 8
        recurDays.value = [1]
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

function getCurrentPrompt(): { content: string; mentions?: any[]; mode?: string; model_id?: string } {
    const box = promptBoxRef.value
    const fallback = props.scheduledPrompt?.prompt || {}
    return {
        content: box?.getText?.() || fallback.content || '',
        mentions: box?.getMentions?.() || fallback.mentions,
        mode: box?.getMode?.() || fallback.mode || 'chat',
        // Trust the box when mounted — its getModel() maps Auto to null. Use
        // `??` so a deliberate Auto (null) isn't overridden by the saved model.
        model_id: box ? (box.getModel?.() ?? undefined) : fallback.model_id,
    }
}

async function saveFromCurrentState() {
    await saveScheduledPrompt(getCurrentPrompt())
}

// Persist the current state (create or update) and run it once immediately.
// We save first so the on-demand run uses the latest prompt/schedule, then hit
// the trigger endpoint and take the user to the report to watch it run.
async function runNow() {
    if (isRunning.value || isSaving.value) return
    const prompt = getCurrentPrompt()
    if (!prompt.content?.trim()) {
        toast.add({ title: t('scheduledPrompt.toastError'), color: 'red', description: t('scheduledPrompt.runNeedsPrompt') })
        return
    }
    isRunning.value = true
    try {
        const response = await persistScheduledPrompt(prompt)
        const saved = response.data.value as any
        if (!saved?.id) {
            toast.add({ title: t('scheduledPrompt.toastError'), color: 'red', description: t('scheduledPrompt.toastSaveFailed') })
            return
        }
        const triggerRes = await useMyFetch(`/api/reports/${props.reportId}/scheduled-prompts/${saved.id}/trigger`, {
            method: 'POST',
        })
        if ((triggerRes as any).error?.value) throw new Error('Trigger failed')
        toast.add({ title: t('scheduledPrompt.toastRunStarted'), color: 'green' })
        isOpen.value = false
        emit('saved')
        await navigateTo(`/reports/${props.reportId}`)
    } catch {
        toast.add({ title: t('scheduledPrompt.toastError'), color: 'red', description: t('scheduledPrompt.toastRunFailed') })
    } finally {
        isRunning.value = false
    }
}

function computeCronSchedule(): string {
    if (scheduleType.value === 'once') {
        const now = new Date()
        const multiplier = delayUnit.value === 'minutes' ? 1 : delayUnit.value === 'hours' ? 60 : 1440
        const target = new Date(now.getTime() + delayAmount.value * multiplier * 60_000)
        return `${target.getMinutes()} ${target.getHours()} ${target.getDate()} ${target.getMonth() + 1} *`
    }
    return buildRecurringCron({
        interval: recurInterval.value,
        everyN: recurEveryN.value,
        hour: recurHour.value,
        days: recurDays.value,
        dayOfMonth: recurDayOfMonth.value,
    })
}

function buildNotificationSubscribers(): Subscriber[] | null {
    if (!smtpEnabled.value || !sendSummaryEmail.value) return null
    if (subscribers.value.length > 0) return subscribers.value
    // Checkbox on but no explicit recipients: default to the current user.
    const me = currentUser.value as any
    if (me?.id) return [{ type: 'user', id: String(me.id) }]
    if (me?.email) return [{ type: 'email', address: String(me.email) }]
    return null
}

// Scheduled runs execute against the report, so the agents/data sources the run
// will use come from `report.data_sources` — not the prompt. Keep them in sync
// with what's selected in the modal so the run hits the chosen data sources.
// (model + mode live on the prompt; files are uploaded to the report already.)
async function syncReportDataSources() {
    const box = promptBoxRef.value
    const ids = ((box?.getDataSources?.() as any[]) || []).map((d: any) => d?.id).filter(Boolean)
    // Guard against the modal's async data-source hydration: an empty list here
    // usually means "not loaded yet", so don't wipe the report's data sources.
    if (ids.length === 0) return
    try {
        await useMyFetch(`/api/reports/${props.reportId}`, {
            method: 'PUT',
            body: { data_sources: ids },
        })
    } catch {
        // Best-effort: the scheduled prompt still saves; surface nothing here.
    }
}

// Persist the current form state (create or update) and return the raw fetch
// response. Side-effect free so both the Save button and Run now can reuse it.
async function persistScheduledPrompt(prompt: { content: string; mentions?: any[]; mode?: string; model_id?: string }) {
    // Apply data-source selection to the report first so an immediate "Run now"
    // (which reads report.data_sources at trigger time) uses the chosen agents.
    await syncReportDataSources()

    const body: any = {
        prompt,
        // Always a string: empty clears the title (falls back to report title).
        title: taskTitle.value.trim(),
        cron_schedule: computeCronSchedule(),
        is_active: isActive.value,
        spawn_new_report: spawnNewReport.value,
        notification_subscribers: buildNotificationSubscribers(),
    }

    if (isEditing.value) {
        return await useMyFetch(`/api/reports/${props.reportId}/scheduled-prompts/${props.scheduledPrompt.id}`, {
            method: 'PUT',
            body,
        })
    }
    return await useMyFetch(`/api/reports/${props.reportId}/scheduled-prompts`, {
        method: 'POST',
        body,
    })
}

async function saveScheduledPrompt(prompt: { content: string; mentions?: any[]; mode?: string; model_id?: string }) {
    isSaving.value = true
    try {
        const response = await persistScheduledPrompt(prompt)

        if (response.data.value) {
            toast.add({
                title: isEditing.value ? t('scheduledPrompt.toastUpdated') : t('scheduledPrompt.toastScheduled'),
                color: 'green',
            })
            isOpen.value = false
            emit('saved')
        } else {
            toast.add({ title: t('scheduledPrompt.toastError'), color: 'red', description: t('scheduledPrompt.toastSaveFailed') })
        }
    } catch {
        toast.add({ title: t('scheduledPrompt.toastError'), color: 'red', description: t('scheduledPrompt.toastSaveFailed') })
    } finally {
        isSaving.value = false
    }
}

async function deleteScheduledPrompt() {
    if (!isEditing.value || isDeleting.value) return
    if (!confirm(t('scheduledPrompt.deleteConfirm'))) return
    isDeleting.value = true
    try {
        const response = await useMyFetch(`/api/reports/${props.reportId}/scheduled-prompts/${props.scheduledPrompt.id}`, {
            method: 'DELETE',
        })
        if ((response as any).error?.value) throw new Error('Delete failed')
        toast.add({ title: t('scheduledPrompt.toastDeleted'), color: 'green' })
        isOpen.value = false
        emit('deleted', props.scheduledPrompt.id)
        emit('saved')
    } catch {
        toast.add({ title: t('scheduledPrompt.toastError'), color: 'red', description: t('scheduledPrompt.toastDeleteFailed') })
    } finally {
        isDeleting.value = false
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
    if (!userId) return t('scheduledPrompt.unknownMember')
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
