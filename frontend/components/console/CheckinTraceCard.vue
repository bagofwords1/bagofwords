<template>
    <!-- Planner said no: one muted line, expandable to the planner's reason. -->
    <div v-if="checkin.status === 'not_proposed'" class="ps-1" :data-testid="`checkin-card-${checkin.id}`" data-status="not_proposed">
        <button type="button" class="flex items-center gap-1.5 text-[11px] text-gray-400 dark:text-gray-500 hover:text-gray-600 dark:hover:text-gray-300" @click="open = !open">
            <UIcon name="i-heroicons-arrow-path-rounded-square" class="w-3.5 h-3.5" />
            <span>{{ $t('traceModal.checkin.noFollowUp') }}</span>
            <UIcon :name="open ? 'i-heroicons-chevron-up-20-solid' : 'i-heroicons-chevron-down-20-solid'" class="w-3 h-3" />
            <span v-if="usageLabel(checkin.planner_llm_tokens, checkin.planner_llm_cost_usd)" class="text-[10px] font-mono">{{ usageLabel(checkin.planner_llm_tokens, checkin.planner_llm_cost_usd) }}</span>
        </button>
        <p v-if="open" class="mt-1 ms-5 text-[11px] text-gray-500 dark:text-gray-400 whitespace-pre-line" dir="auto">{{ checkin.plan_reason || $t('traceModal.checkin.noReason') }}</p>
    </div>

    <!-- Every other decision: the full lifecycle card. -->
    <div v-else class="rounded-lg border border-indigo-100 dark:border-indigo-500/30 bg-indigo-50/40 dark:bg-indigo-500/5 px-3 py-2.5 space-y-2" :data-testid="`checkin-card-${checkin.id}`" :data-status="checkin.status">
        <div class="flex items-center gap-1.5 flex-wrap">
            <UIcon name="i-heroicons-arrow-path-rounded-square" class="w-3.5 h-3.5 text-indigo-500" />
            <span class="text-xs font-medium text-gray-800 dark:text-gray-200">{{ $t('traceModal.checkin.title') }}</span>
            <span :class="['inline-flex items-center px-1.5 py-px rounded-full text-[10px] font-medium', statusClass]">
                {{ $t(`traceModal.checkin.status.${checkin.status}`) }}<template v-if="checkin.status_reason">&nbsp;· {{ reasonLabel(checkin.status_reason) }}</template>
            </span>
            <span v-if="checkin.due_at" class="ms-auto text-[10px] text-gray-400 dark:text-gray-500">{{ $t('traceModal.checkin.due', { when: formatDate(checkin.due_at) }) }}</span>
        </div>

        <div v-if="checkin.note">
            <div class="text-[10px] uppercase tracking-wide text-gray-500 dark:text-gray-400">{{ $t('traceModal.checkin.note') }}</div>
            <p class="text-[11px] text-gray-700 dark:text-gray-300 whitespace-pre-line" dir="auto">{{ checkin.note }}</p>
        </div>
        <div v-if="checkin.plan_reason">
            <div class="text-[10px] uppercase tracking-wide text-gray-500 dark:text-gray-400">{{ $t('traceModal.checkin.planReason') }}</div>
            <p class="text-[11px] text-gray-700 dark:text-gray-300" dir="auto">{{ checkin.plan_reason }}</p>
        </div>

        <div v-if="checkin.judge_decision" class="rounded-md bg-white/70 dark:bg-gray-900/50 border border-gray-100 dark:border-gray-800 px-2 py-1.5">
            <div class="flex items-center gap-1.5">
                <span class="text-[10px] uppercase tracking-wide text-gray-500 dark:text-gray-400">{{ $t('traceModal.checkin.judge') }}</span>
                <span :class="['px-1.5 py-px rounded-full text-[10px] font-semibold', checkin.judge_decision === 'run' ? 'bg-green-50 text-green-700 dark:bg-green-500/10 dark:text-green-300' : 'bg-gray-100 text-gray-600 dark:bg-gray-800 dark:text-gray-300']">
                    {{ $t(`traceModal.checkin.decision.${checkin.judge_decision}`) }}
                </span>
                <span v-if="checkin.judged_at" class="ms-auto text-[10px] text-gray-400 dark:text-gray-500">{{ formatDate(checkin.judged_at) }}</span>
            </div>
            <p class="mt-0.5 text-[11px] text-gray-700 dark:text-gray-300" dir="auto">{{ checkin.judge_reason }}</p>
            <p v-if="checkin.judge_focus" class="mt-0.5 text-[11px] text-gray-500 dark:text-gray-400" dir="auto"><span class="font-medium">{{ $t('traceModal.checkin.focus') }}:</span> {{ checkin.judge_focus }}</p>
        </div>

        <div class="flex items-center gap-1.5 text-[11px]">
            <UIcon :name="outcomeIcon" :class="['w-3.5 h-3.5', outcomeIconClass]" />
            <span class="text-gray-700 dark:text-gray-300" dir="auto">{{ outcomeText }}</span>
        </div>

        <div v-if="plannerUsage || judgeUsage" class="flex items-center gap-3 text-[10px] text-gray-400 dark:text-gray-500 font-mono">
            <span v-if="plannerUsage">{{ $t('traceModal.checkin.plannerUsage') }} {{ plannerUsage }}</span>
            <span v-if="judgeUsage">{{ $t('traceModal.checkin.judgeUsage') }} {{ judgeUsage }}</span>
        </div>
    </div>
</template>

<script setup lang="ts">
export interface CheckinTrace {
    id: string
    status: string
    status_reason?: string | null
    source_completion_id?: string | null
    run_completion_id?: string | null
    note?: string | null
    plan_reason?: string | null
    due_at?: string | null
    judge_decision?: string | null
    judge_reason?: string | null
    judge_focus?: string | null
    judged_at?: string | null
    notified: boolean
    notify_subject?: string | null
    sent_at?: string | null
    planner_llm_tokens?: number | null
    planner_llm_cost_usd?: number | null
    judge_llm_tokens?: number | null
    judge_llm_cost_usd?: number | null
    created_at?: string | null
}

const props = defineProps<{ checkin: CheckinTrace }>()
const { t, te } = useI18n()
const open = ref(false)
const _df = useFormatDate()
const formatDate = (d: string) => _df.formatDateTime(d)

function reasonLabel(code: string): string {
    const key = `traceModal.checkin.reason.${code}`
    return te(key) ? t(key) : code
}

function usageLabel(tokens?: number | null, cost?: number | null): string {
    const parts: string[] = []
    if (tokens) parts.push(t('traceModal.tokensCount', { count: tokens >= 1000 ? `${(tokens / 1000).toFixed(1)}k` : String(tokens) }))
    if (cost) parts.push(`$${cost < 0.01 ? cost.toFixed(4) : cost.toFixed(2)}`)
    return parts.join(' · ')
}
const plannerUsage = computed(() => usageLabel(props.checkin.planner_llm_tokens, props.checkin.planner_llm_cost_usd))
const judgeUsage = computed(() => usageLabel(props.checkin.judge_llm_tokens, props.checkin.judge_llm_cost_usd))

const statusClass = computed(() => {
    switch (props.checkin.status) {
        case 'sent': return 'bg-blue-100 text-blue-700 dark:bg-blue-500/15 dark:text-blue-300'
        case 'ran_quiet': return 'bg-gray-100 text-gray-700 dark:bg-gray-800 dark:text-gray-300'
        case 'planned':
        case 'running': return 'bg-indigo-100 text-indigo-700 dark:bg-indigo-500/15 dark:text-indigo-300'
        case 'failed': return 'bg-red-100 text-red-700 dark:bg-red-500/15 dark:text-red-300'
        case 'skipped':
        case 'cancelled':
        case 'rejected': return 'bg-amber-50 text-amber-700 dark:bg-amber-500/10 dark:text-amber-300'
        default: return 'bg-gray-100 text-gray-600 dark:bg-gray-800 dark:text-gray-300'
    }
})

const outcomeText = computed(() => {
    const c = props.checkin
    switch (c.status) {
        case 'sent': return c.notify_subject ? t('traceModal.checkin.outcome.sentWithSubject', { subject: c.notify_subject }) : t('traceModal.checkin.outcome.sent')
        case 'ran_quiet': return t('traceModal.checkin.outcome.ran_quiet')
        case 'planned': return t('traceModal.checkin.outcome.planned')
        case 'running': return t('traceModal.checkin.outcome.running')
        case 'skipped': return t('traceModal.checkin.outcome.skipped')
        case 'failed': return t('traceModal.checkin.outcome.failed')
        case 'rejected': return t('traceModal.checkin.outcome.rejected', { reason: reasonLabel(c.status_reason || '') })
        case 'cancelled': return t('traceModal.checkin.outcome.cancelled', { reason: reasonLabel(c.status_reason || '') })
        default: return c.status
    }
})
const outcomeIcon = computed(() => {
    switch (props.checkin.status) {
        case 'sent': return 'i-heroicons-bell-alert'
        case 'ran_quiet': return 'i-heroicons-check-circle'
        case 'planned': return 'i-heroicons-clock'
        case 'running': return 'i-heroicons-arrow-path'
        case 'failed': return 'i-heroicons-x-circle'
        default: return 'i-heroicons-no-symbol'
    }
})
const outcomeIconClass = computed(() => {
    switch (props.checkin.status) {
        case 'sent': return 'text-blue-500'
        case 'ran_quiet': return 'text-gray-400'
        case 'failed': return 'text-red-500'
        case 'planned':
        case 'running': return 'text-indigo-500'
        default: return 'text-amber-500'
    }
})
</script>
