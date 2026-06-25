<template>
    <div class="mb-6 flex items-center gap-3 flex-wrap">
        <UPopover :popper="{ placement: 'bottom-start' }">
            <UButton color="white" variant="solid" size="sm" icon="i-heroicons-funnel">
                {{ $t('monitoring.filters.title') }}
                <span
                    v-if="activeCount > 0"
                    class="ms-1 inline-flex items-center justify-center min-w-[18px] h-[18px] px-1 rounded-full bg-blue-100 dark:bg-blue-900/50 text-blue-700 dark:text-blue-300 text-[11px] font-semibold"
                >{{ activeCount }}</span>
                <UIcon name="i-heroicons-chevron-down-20-solid" class="w-4 h-4 ms-0.5 text-gray-400" />
            </UButton>

            <template #panel>
                <!-- Native <select>/inputs and inline lists only — no teleporting
                     dropdowns inside the popover, so interacting never dismisses it. -->
                <div class="p-4 w-72 space-y-4 text-sm">
                    <!-- Time period -->
                    <div>
                        <label class="block text-[11px] font-semibold uppercase tracking-wide text-gray-500 dark:text-gray-400 mb-1.5">
                            {{ $t('monitoring.overview.timePeriod') }}
                        </label>
                        <select
                            :value="selectedPeriod.value"
                            @change="onPeriodSelect"
                            class="w-full rounded-md border border-gray-300 dark:border-gray-700 bg-white dark:bg-gray-800 px-2 py-1.5 text-sm text-gray-700 dark:text-gray-200 focus:outline-none focus:border-blue-500"
                        >
                            <option v-for="o in periodOptions" :key="o.value" :value="o.value">{{ o.label }}</option>
                        </select>
                        <div v-if="selectedPeriod.value === 'custom'" class="mt-2 flex items-center gap-2">
                            <input
                                type="date" :value="dateRange.start" :max="dateRange.end || undefined"
                                :aria-label="$t('monitoring.overview.startDate')"
                                @change="onDate('start', $event)"
                                class="flex-1 min-w-0 rounded-md border border-gray-300 dark:border-gray-700 bg-white dark:bg-gray-800 px-2 py-1 text-xs text-gray-700 dark:text-gray-200"
                            />
                            <span class="text-gray-400">–</span>
                            <input
                                type="date" :value="dateRange.end" :min="dateRange.start || undefined"
                                :aria-label="$t('monitoring.overview.endDate')"
                                @change="onDate('end', $event)"
                                class="flex-1 min-w-0 rounded-md border border-gray-300 dark:border-gray-700 bg-white dark:bg-gray-800 px-2 py-1 text-xs text-gray-700 dark:text-gray-200"
                            />
                        </div>
                    </div>

                    <!-- Agent (multi-select, mirrors the global agent context) -->
                    <div>
                        <label class="block text-[11px] font-semibold uppercase tracking-wide text-gray-500 dark:text-gray-400 mb-1.5">
                            {{ $t('monitoring.filters.agent') }}
                        </label>
                        <div class="max-h-40 overflow-y-auto rounded-md border border-gray-200 dark:border-gray-700 divide-y divide-gray-100 dark:divide-gray-800">
                            <button
                                type="button"
                                @click="toggleAgent(null)"
                                class="w-full flex items-center gap-2 px-2 py-1.5 text-start hover:bg-gray-50 dark:hover:bg-gray-800"
                            >
                                <UIcon :name="isAllAgents ? 'i-heroicons-check' : 'i-heroicons-minus-small'" :class="['w-3.5 h-3.5 flex-shrink-0', isAllAgents ? 'text-blue-600' : 'text-transparent']" />
                                <span :class="['text-xs', isAllAgents ? 'font-medium text-blue-700 dark:text-blue-300' : 'text-gray-700 dark:text-gray-300']">{{ $t('nav.allAgents') }}</span>
                            </button>
                            <button
                                v-for="a in agents"
                                :key="a.id"
                                type="button"
                                @click="toggleAgent(a.id)"
                                class="w-full flex items-center gap-2 px-2 py-1.5 text-start hover:bg-gray-50 dark:hover:bg-gray-800"
                            >
                                <UIcon name="i-heroicons-check" :class="['w-3.5 h-3.5 flex-shrink-0', isAgentSelected(a.id) ? 'text-blue-600' : 'text-transparent']" />
                                <DataSourceIcon v-if="a.connections?.[0]?.type" :type="a.connections[0].type" class="h-3.5 w-3.5 flex-shrink-0" />
                                <span class="text-xs truncate text-gray-700 dark:text-gray-300">{{ a.name }}</span>
                            </button>
                            <div v-if="agents.length === 0" class="px-2 py-2 text-xs text-gray-400">{{ $t('nav.noAgents') }}</div>
                        </div>
                    </div>

                    <!-- User who triggered the execution -->
                    <div>
                        <label class="block text-[11px] font-semibold uppercase tracking-wide text-gray-500 dark:text-gray-400 mb-1.5">
                            {{ $t('monitoring.diagnosis.filterUserLabel') }}
                        </label>
                        <select
                            :value="userId"
                            @change="$emit('update:userId', ($event.target as HTMLSelectElement).value)"
                            class="w-full rounded-md border border-gray-300 dark:border-gray-700 bg-white dark:bg-gray-800 px-2 py-1.5 text-sm text-gray-700 dark:text-gray-200 focus:outline-none focus:border-blue-500"
                        >
                            <option value="">{{ $t('monitoring.diagnosis.filterUserAll') }}</option>
                            <option v-for="u in users" :key="u.id" :value="u.id">{{ u.name || u.email || $t('monitoring.diagnosis.unknownUser') }}</option>
                        </select>
                    </div>

                    <div class="pt-1 border-t border-gray-100 dark:border-gray-800 flex justify-end">
                        <button type="button" @click="resetAll" class="text-xs text-gray-500 hover:text-gray-700 dark:hover:text-gray-300">
                            {{ $t('monitoring.filters.reset') }}
                        </button>
                    </div>
                </div>
            </template>
        </UPopover>

        <!-- Active-filter summary chips -->
        <span v-if="selectedPeriod.value !== 'all_time' && dateRange.start" class="text-xs text-gray-500 dark:text-gray-400">
            {{ formatDateRange() }}
        </span>
        <span v-if="!isAllAgents" class="inline-flex items-center gap-1 text-xs text-gray-500 dark:text-gray-400">
            <UIcon name="i-heroicons-cube" class="w-3.5 h-3.5" /> {{ currentAgentName }}
        </span>
        <span v-if="selectedUserName" class="inline-flex items-center gap-1 text-xs text-gray-500 dark:text-gray-400">
            <UIcon name="i-heroicons-user" class="w-3.5 h-3.5" /> {{ selectedUserName }}
        </span>
    </div>
</template>

<script setup lang="ts">
import DataSourceIcon from '~/components/DataSourceIcon.vue'

interface Period { label: string; value: string }
interface DateRange { start: string; end: string }
interface AgentExecutionUser { id: string; name: string; email?: string; execution_count: number }

const props = defineProps<{
    selectedPeriod: Period
    dateRange: DateRange
    userId: string
}>()

const emit = defineEmits<{
    periodChange: [period: Period]
    customRangeChange: [range: DateRange]
    'update:userId': [userId: string]
}>()

const { t } = useI18n()
const { agents, isAllAgents, currentAgentName, toggleAgent, isAgentSelected, clearSelection, initAgent } = useAgent()

const periodOptions = computed(() => [
    { label: t('monitoring.overview.allTime'), value: 'all_time' },
    { label: t('monitoring.overview.last30d'), value: '30_days' },
    { label: t('monitoring.overview.last90d'), value: '90_days' },
    { label: t('monitoring.overview.customRange'), value: 'custom' },
])

const users = ref<AgentExecutionUser[]>([])
const selectedUserName = computed(() => {
    if (!props.userId) return ''
    const u = users.value.find((x) => x.id === props.userId)
    return u ? (u.name || u.email || t('monitoring.diagnosis.unknownUser')) : ''
})

// Count of non-default filters, shown as a badge on the trigger.
const activeCount = computed(() =>
    (props.selectedPeriod.value !== 'all_time' ? 1 : 0) +
    (!isAllAgents.value ? 1 : 0) +
    (props.userId ? 1 : 0)
)

const onPeriodSelect = (e: Event) => {
    const value = (e.target as HTMLSelectElement).value
    const opt = periodOptions.value.find((o) => o.value === value) || periodOptions.value[0]
    emit('periodChange', opt)
}

const onDate = (field: 'start' | 'end', e: Event) => {
    const value = (e.target as HTMLInputElement).value
    emit('customRangeChange', { ...props.dateRange, [field]: value })
}

const resetAll = () => {
    emit('periodChange', periodOptions.value[0]) // all_time
    clearSelection()
    emit('update:userId', '')
}

const _df = useFormatDate()
const formatDateRange = () => {
    if (!props.dateRange.start || props.selectedPeriod.value === 'all_time') return ''
    return `${_df.formatDate(props.dateRange.start)} - ${_df.formatDate(props.dateRange.end)}`
}

onMounted(async () => {
    await initAgent()
    try {
        const res = await useMyFetch<{ items: AgentExecutionUser[] }>('/api/console/agent_executions/users')
        if (res.data.value?.items) users.value = res.data.value.items
    } catch (error) {
        console.error('Failed to load monitoring users:', error)
    }
})
</script>
