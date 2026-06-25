<template>
    <div class="mt-6">
        <!-- Date Range Picker with Agent Selector -->
        <DateRangePicker
            :selected-period="selectedPeriod"
            :date-range="dateRange"
            @period-change="handlePeriodChange"
        >
            <AgentSelector :collapsed="false" :show-text="true" :show-label="false" />
        </DateRangePicker>

        <!-- Activity Chart (observability-style daily bars) -->
        <DiagnosisActivityChart
            :points="timeseriesPoints"
            :is-loading="isTimeseriesLoading"
            :selected-date="selectedDay"
            @select-day="handleDaySelect"
        />

        <!-- Summary Cards (matching MetricsCards.vue style) -->
        <div class="grid grid-cols-1 md:grid-cols-4 gap-6 mb-8">
            <!-- Failed Queries -->
            <div class="bg-white dark:bg-gray-900 p-6 border border-gray-200 dark:border-gray-700 rounded-xl shadow-sm">
                <div class="text-2xl font-bold text-gray-900 dark:text-white">
                    {{ dashboardMetrics?.failed_queries || 0 }}
                </div>
                <div class="text-sm font-medium text-gray-600 dark:text-gray-400 mt-1">{{ $t('monitoring.diagnosis.cardFailedQueries') }}</div>
            </div>

            <!-- Negative Feedback -->
            <div class="bg-white dark:bg-gray-900 p-6 border border-gray-200 dark:border-gray-700 rounded-xl shadow-sm">
                <div class="text-2xl font-bold text-gray-900 dark:text-white">
                    {{ dashboardMetrics?.negative_feedback || 0 }}
                </div>
                <div class="text-sm font-medium text-gray-600 dark:text-gray-400 mt-1">{{ $t('monitoring.diagnosis.cardNegativeFeedback') }}</div>
            </div>

            <!-- Instruction Coverage -->
            <div class="bg-white dark:bg-gray-900 p-6 border border-gray-200 dark:border-gray-700 rounded-xl shadow-sm">
                <div class="text-2xl font-bold text-gray-900 dark:text-white">
                    {{ isJudgeEnabled ? (getInstructionsEffectiveness() + '%') : $t('monitoring.diagnosis.naAbbr') }}
                </div>
                <div class="text-sm font-medium text-gray-600 dark:text-gray-400 mt-1 flex items-center">
                    {{ $t('monitoring.diagnosis.cardInstructionCoverage') }}
                    <UTooltip :text="isJudgeEnabled ? $t('monitoring.diagnosis.judgeEnabledTooltip') : $t('monitoring.diagnosis.judgeDisabledTooltip')">
                        <UIcon name="i-heroicons-information-circle" class="w-4 h-4 ms-1 text-gray-400 dark:text-gray-600 cursor-help" />
                    </UTooltip>
                </div>
            </div>

            <!-- Total Items -->
            <div class="bg-white dark:bg-gray-900 p-6 border border-gray-200 dark:border-gray-700 rounded-xl shadow-sm">
                <div class="text-2xl font-bold text-gray-900 dark:text-white">
                    {{ dashboardMetrics?.total_items || 0 }}
                </div>
                <div class="text-sm font-medium text-gray-600 dark:text-gray-400 mt-1">{{ $t('monitoring.diagnosis.cardTotalAgentRuns') }}</div>
            </div>
        </div>

        <!-- Filter Tabs -->
        <div class="mb-6">
            <div class="border-b border-gray-200 dark:border-gray-700">
                <nav class="-mb-px flex space-x-8">
                    <button
                        v-for="filter in filterOptions"
                        :key="filter.value"
                        @click="handleFilterChange(filter)"
                        :class="[
                            selectedFilter.value === filter.value
                                ? 'border-blue-500 text-blue-600'
                                : 'border-transparent text-gray-500 dark:text-gray-400 hover:text-gray-700 hover:border-gray-300',
                            'whitespace-nowrap py-2 px-1 border-b-2 font-medium text-sm'
                        ]"
                    >
                        {{ filter.label }}
                        <span
                            v-if="filter.count !== undefined && filter.count >= 0"
                            :class="[
                                selectedFilter.value === filter.value
                                    ? 'bg-blue-100 dark:bg-blue-900/50 text-blue-600'
                                    : 'bg-gray-100 dark:bg-gray-800 text-gray-600 dark:text-gray-400',
                                'ms-2 py-0.5 px-2 rounded-full text-xs font-medium'
                            ]"
                        >
                            {{ filter.count }}
                        </span>
                    </button>
                </nav>
            </div>
        </div>

        <!-- Day filter indicator (set by clicking a bar in the activity chart) -->
        <div v-if="selectedDay" class="mb-4 -mt-2">
            <button
                @click="clearDayFilter"
                class="inline-flex items-center gap-1.5 px-3 py-1 bg-blue-50 dark:bg-blue-950 text-blue-700 border border-blue-200 rounded-full text-xs font-medium hover:bg-blue-100"
            >
                {{ $t('monitoring.diagnosis.dayFilterLabel', { date: formatDate(selectedDay) }) }}
                <UIcon name="i-heroicons-x-mark" class="w-3.5 h-3.5" />
            </button>
        </div>

        <!-- Loading state -->
        <div v-if="isLoading" class="flex items-center justify-center py-12">
            <div class="flex items-center space-x-2">
                <div class="animate-spin rounded-full h-6 w-6 border-b-2 border-blue-600"></div>
                <span class="text-gray-600 dark:text-gray-400">{{ $t('monitoring.diagnosis.loading') }}</span>
            </div>
        </div>

        <!-- Agent Executions Table -->
        <div v-else class="bg-white dark:bg-gray-900 shadow-sm border border-gray-200 dark:border-gray-700 rounded-lg overflow-hidden">
            <div class="overflow-x-auto">
                <table class="min-w-full divide-y divide-gray-200 dark:divide-gray-700">
                    <thead class="bg-gray-50 dark:bg-gray-900">
                        <tr>
                            <th class="px-6 py-3 text-start text-xs font-medium text-gray-500 dark:text-gray-400 uppercase tracking-wider min-w-[320px] w-[320px]">{{ $t('monitoring.diagnosis.colPrompt') }}</th>
                            <th class="px-6 py-3 text-start text-xs font-medium text-gray-500 dark:text-gray-400 uppercase tracking-wider">{{ $t('monitoring.diagnosis.colStatus') }}</th>
                            <th class="px-6 py-3 text-start text-xs font-medium text-gray-500 dark:text-gray-400 uppercase tracking-wider">{{ $t('monitoring.diagnosis.colData') }}</th>
                            <th class="px-6 py-3 text-start text-xs font-medium text-gray-500 dark:text-gray-400 uppercase tracking-wider">{{ $t('monitoring.diagnosis.colTools') }}</th>
                            <th class="px-6 py-3 text-start text-xs font-medium text-gray-500 dark:text-gray-400 uppercase tracking-wider">{{ $t('monitoring.diagnosis.colFeedback') }}</th>
                            <th class="px-6 py-3 text-start text-xs font-medium text-gray-500 dark:text-gray-400 uppercase tracking-wider">{{ $t('monitoring.diagnosis.colReport') }}</th>
                            <th class="px-6 py-3 text-start text-xs font-medium text-gray-500 dark:text-gray-400 uppercase tracking-wider">{{ $t('monitoring.diagnosis.colUser') }}</th>
                            <th class="px-6 py-3 text-start text-xs font-medium text-gray-500 dark:text-gray-400 uppercase tracking-wider">{{ $t('monitoring.diagnosis.colDate') }}</th>
                        </tr>
                    </thead>
                    <tbody class="bg-white dark:bg-gray-900 divide-y divide-gray-200 dark:divide-gray-700 text-xs">
                        <tr v-for="item in executionItems" :key="item.agent_execution_id" class="hover:bg-gray-50 dark:hover:bg-gray-800 cursor-pointer" @click="openTraceFromAE(item)">
                            <td class="px-6 py-4">
                                <div class="text-xs text-gray-900 dark:text-white">
                                    <div class="relative group max-w-[320px] w-[320px]">
                                        <p class="truncate">{{ truncate(item.prompt || '', 40) }}</p>
                                        <div class="pointer-events-none absolute start-0 top-full mt-1 z-10 hidden group-hover:block bg-white dark:bg-gray-900 border border-gray-200 dark:border-gray-700 rounded-md shadow-sm p-2 text-xs whitespace-pre-wrap max-w-[520px] max-h-56 overflow-auto">
                                            {{ item.prompt || '—' }}
                                        </div>
                                    </div>
                                </div>
                            </td>
                            <td class="px-6 py-4 whitespace-nowrap">
                                <div class="relative inline-block group">
                                    <span class="inline-flex px-2 py-1 text-xs font-medium rounded-full"
                                          :class="item.agent_execution_status === 'error' ? 'bg-red-100 dark:bg-red-900/50 text-red-800' : (item.agent_execution_status === 'completed' || item.agent_execution_status === 'success') ? 'bg-green-100 dark:bg-green-900/50 text-green-800' : 'bg-gray-100 dark:bg-gray-800 text-gray-800'">
                                        {{ item.agent_execution_status === 'error' ? $t('monitoring.diagnosis.statusError') : $t('monitoring.diagnosis.statusSuccess') }}
                                    </span>
                                    <div v-if="item.agent_execution_status === 'error' && item.error_json?.message" class="pointer-events-none absolute start-0 top-full mt-1 z-10 hidden group-hover:block bg-white dark:bg-gray-900 border border-gray-200 dark:border-gray-700 rounded-md shadow-sm p-2 text-xs text-red-700 whitespace-pre-wrap max-w-[520px] max-h-56 overflow-auto">
                                        {{ item.error_json.message }}
                                    </div>
                                </div>
                            </td>
                            <td class="px-6 py-4">
                                <div class="flex flex-wrap gap-1 max-w-md">
                                    <span v-for="(title, idx) in item.step_titles || []" :key="idx" class="px-2 py-0.5 bg-gray-100 dark:bg-gray-800 text-gray-700 dark:text-gray-300 rounded text-[11px]"
                                          @click.stop="openTraceFromAE(item)">
                                        {{ title }}
                                    </span>
                                    <span v-if="(item.step_titles || []).length === 0" class="text-gray-400 dark:text-gray-600">{{ $t('monitoring.diagnosis.none') }}</span>
                                </div>
                            </td>
                            <td class="px-6 py-4">
                                <div class="text-xs text-gray-900 dark:text-white">{{ $t('monitoring.diagnosis.totalPrefix', { n: item.total_tools }) }}</div>
                                <div class="flex items-center space-x-4 mt-1">
                                    <div class="flex items-center space-x-1 text-green-600">
                                        <UIcon name="i-heroicons-check-circle" class="w-4 h-4" />
                                        <span>{{ item.total_successful_tools }}</span>
                                    </div>
                                    <div class="flex items-center space-x-1 text-red-600">
                                        <UIcon name="i-heroicons-x-circle" class="w-4 h-4" />
                                        <span>{{ item.total_failed_tools }}</span>
                                    </div>
                                </div>
                            </td>
                            <td class="px-6 py-4">
                                <div class="flex flex-col">
                                    <span class="inline-flex px-2 py-1 text-xs font-medium rounded-full self-start"
                                          :class="item.feedback_direction > 0 ? 'bg-green-100 dark:bg-green-900/50 text-green-800' : item.feedback_direction < 0 ? 'bg-red-100 dark:bg-red-900/50 text-red-800' : 'bg-gray-100 dark:bg-gray-800 text-gray-800'">
                                        {{ item.feedback_direction > 0 ? $t('monitoring.diagnosis.feedbackPositive') : (item.feedback_direction < 0 ? $t('monitoring.diagnosis.feedbackNegative') : $t('monitoring.diagnosis.feedbackNone')) }}
                                    </span>
                                    <div v-if="item.feedback_direction < 0 && item.feedback_message" class="mt-1 text-xs text-gray-600 dark:text-gray-400 max-w-sm">
                                        <UTooltip :text="item.feedback_message">
                                            <span class="truncate cursor-help">{{ truncate(item.feedback_message, 120) }}</span>
                                        </UTooltip>
                                    </div>
                                </div>
                            </td>
                            <td class="px-6 py-4 whitespace-nowrap">
                                <NuxtLink v-if="item.report_link" :to="item.report_link" class="text-blue-600 hover:underline" @click.stop>
                                    {{ item.report_name || item.report_id }}
                                </NuxtLink>
                                <span v-else>{{ item.report_name || item.report_id }}</span>
                            </td>
                            <td class="px-2 py-1">
                                <div class="text-xs text-gray-900 dark:text-white">{{ item.user_name || '—' }}</div>
                            </td>
                            <td class="px-3 py-1">
                                <span class="text-xs text-gray-500 dark:text-gray-400">{{ formatDate(item.created_at as any) }}</span>
                            </td>
                        </tr>
                    </tbody>
                </table>
            </div>

            <!-- Empty state -->
            <div v-if="executionItems.length === 0 && !isLoading" class="text-center py-12">
                <UIcon name="i-heroicons-clipboard-document-check" class="mx-auto h-12 w-12 text-gray-400 dark:text-gray-600" />
                <h3 class="mt-2 text-sm font-medium text-gray-900 dark:text-white">{{ $t('monitoring.diagnosis.emptyTitle') }}</h3>
                <p class="mt-1 text-sm text-gray-500 dark:text-gray-400">
                    {{ $t('monitoring.diagnosis.emptySubtitle') }}
                </p>
                <div class="mt-2 text-xs text-gray-400 dark:text-gray-600">
                    {{ $t('monitoring.diagnosis.debugPrefix', { info: debugInfo }) }}
                </div>
            </div>
        </div>

        <!-- Pagination -->
        <div v-if="executionItems.length > 0" class="mt-6 flex items-center justify-between">
            <div class="text-sm text-gray-700 dark:text-gray-300">
                {{ $t('monitoring.diagnosis.paginationRange', { start: (currentPage - 1) * pageSize + 1, end: Math.min(currentPage * pageSize, totalItems), total: totalItems }) }}
            </div>

            <div class="flex items-center space-x-2">
                <UButton
                    icon="i-heroicons-chevron-left"
                    color="gray"
                    variant="ghost"
                    size="sm"
                    @click="currentPage--"
                    :disabled="currentPage === 1"
                >
                    {{ $t('monitoring.diagnosis.previous') }}
                </UButton>
                
                <div class="flex items-center space-x-1">
                    <UButton
                        v-for="page in visiblePages"
                        :key="page"
                        :color="page === currentPage ? 'blue' : 'gray'"
                        :variant="page === currentPage ? 'solid' : 'ghost'"
                        size="sm"
                        @click="currentPage = page"
                        class="min-w-[32px]"
                    >
                        {{ page }}
                    </UButton>
                </div>
                
                <UButton
                    icon="i-heroicons-chevron-right"
                    color="gray"
                    variant="ghost"
                    size="sm"
                    @click="currentPage++"
                    :disabled="currentPage === totalPages"
                >
                    {{ $t('monitoring.diagnosis.next') }}
                </UButton>
            </div>
        </div>
        
        <!-- Trace Modal -->
        <TraceModal
            v-model="showTraceModal"
            :report-id="selectedTraceItem?.report_id || ''"
            :completion-id="selectedTraceItem?.completion_id || selectedTraceItem?.id || ''"
        />
    </div>
</template>

<script setup lang="ts">
import DateRangePicker from '~/components/console/DateRangePicker.vue'
import TraceModal from '~/components/console/TraceModal.vue'
import AgentSelector from '~/components/AgentSelector.vue'
import DiagnosisActivityChart from '~/components/console/DiagnosisActivityChart.vue'
const { isJudgeEnabled } = useOrgSettings()
const { selectedAgents, initAgent } = useAgent()
const { t } = useI18n()

definePageMeta({
    auth: true,
    layout: 'monitoring',
    // Admin-only: matches the `manage_settings` gate on the /console/* endpoints.
    permissions: ['manage_settings']
})

// Types for compact issues
interface CompactIssueItem {
    completion_id: string
    created_at: string
    issue_type: string
    summary_text: string
    full_message?: string
    tool_name?: string
    tool_action?: string
    user_name?: string
    user_email?: string
    head_prompt_snippet?: string
    report_id: string
    trace_url?: string
}

interface CompactIssuesResponse {
    items: CompactIssueItem[]
    total_items: number
    date_range: {
        start: string
        end: string
    }
}

interface DateRange {
    start: string
    end: string
}

// State (same as ConsoleOverview)
const isLoading = ref(false)
const metrics = ref<CompactIssuesResponse | null>(null)
const overallMetrics = ref<CompactIssuesResponse | null>(null) // Static metrics for top cards
const diagnosisItems = ref<CompactIssueItem[]>([])
const currentPage = ref(1)
const pageSize = ref(10)
const totalItems = ref(0)
const debugInfo = ref('')
const instructionsEffectiveness = ref<number | null>(null)
// New data for agent execution summaries
const executionItems = ref<any[]>([])
const dashboardMetrics = ref<any>(null)

// Activity chart timeseries (daily agent runs by status)
interface DiagnosisStatusPoint { date: string; success: number; error: number }
const timeseriesPoints = ref<DiagnosisStatusPoint[] | null>(null)
const isTimeseriesLoading = ref(false)
// Day selected by clicking a bar in the activity chart (YYYY-MM-DD); narrows the table only
const selectedDay = ref<string | null>(null)

// Filter state
const filterLabelFor = (value: string): string => {
    switch (value) {
        case 'all': return t('monitoring.diagnosis.filterAll')
        case 'negative_feedback': return t('monitoring.diagnosis.filterNegative')
        case 'failed_queries': return t('monitoring.diagnosis.filterFailed')
        case 'low_confidence': return t('monitoring.diagnosis.filterLowConfidence')
        case 'low_instruction_coverage': return t('monitoring.diagnosis.filterLowCoverage')
        default: return value
    }
}
const selectedFilter = ref({ label: filterLabelFor('all'), value: 'all' })
const filterOptions = ref([
    { label: filterLabelFor('all'), value: 'all', count: 0 },
    { label: filterLabelFor('negative_feedback'), value: 'negative_feedback', count: 0 },
    { label: filterLabelFor('failed_queries'), value: 'failed_queries', count: 0 },
    { label: filterLabelFor('low_confidence'), value: 'low_confidence', count: 0 },
    { label: filterLabelFor('low_instruction_coverage'), value: 'low_instruction_coverage', count: 0 }
])

// Add these to the state section
const showTraceModal = ref(false)
const selectedTraceItem = ref<any | null>(null)

// Date range state (same as ConsoleOverview)
const selectedPeriod = ref({ label: t('monitoring.diagnosis.periodAllTime'), value: 'all_time' })
const dateRange = ref<DateRange>({
    start: '',
    end: ''
})

// Computed
const totalPages = computed(() => Math.ceil(totalItems.value / pageSize.value))

const visiblePages = computed(() => {
    const pages = []
    const total = totalPages.value
    const current = currentPage.value
    
    // Show maximum 5 pages
    let start = Math.max(1, current - 2)
    let end = Math.min(total, start + 4)
    
    // Adjust start if we're near the end
    if (end - start < 4) {
        start = Math.max(1, end - 4)
    }
    
    for (let i = start; i <= end; i++) {
        pages.push(i)
    }
    
    return pages
})

// Methods (same pattern as ConsoleOverview)
const initializeDateRange = () => {
    // Default to all time
    selectedPeriod.value = { label: t('monitoring.diagnosis.periodAllTime'), value: 'all_time' }
    dateRange.value = {
        start: '',
        end: new Date().toISOString().split('T')[0]
    }
}

const handlePeriodChange = (period: { label: string, value: string }) => {
    selectedPeriod.value = period
    
    const end = new Date()
    let start: Date | null = null
    
    switch (period.value) {
        case '30_days':
            start = new Date()
            start.setDate(start.getDate() - 30)
            break
        case '90_days':
            start = new Date()
            start.setDate(start.getDate() - 90)
            break
        case 'all_time':
        default:
            start = null
            break
    }
    
    dateRange.value = {
        start: start ? start.toISOString().split('T')[0] : '',
        end: end.toISOString().split('T')[0]
    }
    
    currentPage.value = 1
    selectedDay.value = null
    // Refresh overall metrics, timeseries, and diagnosis data when date range changes
    Promise.all([
        fetchOverallMetrics(),
        fetchTimeseries(),
        fetchDiagnosisData()
    ])
}



const fetchDiagnosisData = async () => {
    isLoading.value = true
    try {
        const params = new URLSearchParams({
            page: currentPage.value.toString(),
            page_size: pageSize.value.toString()
        })

        appendDateParams(params)

        // Add filter parameter
        if (selectedFilter.value.value !== 'all') {
            params.append('filter', selectedFilter.value.value)
        }

        // Add data source filter
        if (selectedAgents.value.length > 0) {
            params.append('data_source_ids', selectedAgents.value.join(','))
        }

        debugInfo.value = `Fetching with params: ${params.toString()}`

        // Fetch agent execution summaries instead of compact issues
        const diagnosisResponse = await useMyFetch<any>(`/api/console/agent_executions/summaries?${params}`)
        
        if (diagnosisResponse.error.value) {
            console.error('Error fetching diagnosis data:', diagnosisResponse.error.value)
            debugInfo.value = `Error: ${diagnosisResponse.error.value}`
            metrics.value = null
            diagnosisItems.value = []
            totalItems.value = 0
        } else if (diagnosisResponse.data.value) {
            const data = diagnosisResponse.data.value
            executionItems.value = data.items || []
            totalItems.value = data.total_items || 0
            debugInfo.value = `Loaded ${executionItems.value.length} agent executions, total: ${totalItems.value}`
        }
    } catch (error) {
        console.error('Failed to fetch diagnosis data:', error)
        debugInfo.value = `Exception: ${error}`
        metrics.value = null
        executionItems.value = []
        totalItems.value = 0
    } finally {
        isLoading.value = false
    }
}

const getIssueTypeClass = (issueType: string) => {
    switch (issueType) {
        case 'failed_step':
        case 'failed_query':
            return 'bg-red-100 dark:bg-red-900/50 text-red-800'
        case 'validation_error':
            return 'bg-yellow-100 text-yellow-800'
        case 'negative_feedback':
            return 'bg-orange-100 dark:bg-orange-950 text-orange-800'
        case 'no_issue':
            return 'bg-green-100 dark:bg-green-900/50 text-green-800'
        default:
            return 'bg-gray-100 dark:bg-gray-800 text-gray-800'
    }
}

const getIssueTypeLabel = (issueType: string) => {
    switch (issueType) {
        case 'failed_query':
            return 'Failed Query'
        case 'validation_error':
            return 'Validation Error'
        case 'negative_feedback':
            return 'Negative Feedback'
        case 'no_issue':
            return 'OK'
        default:
            return 'Unknown'
    }
}

const _df = useFormatDate()
const formatDate = (dateString: string) => {
    if (!dateString) return ''
    return _df.formatDate(dateString)
}

// Add these methods to the existing script section

// Append start/end date params. When a chart day is selected, narrow to that
// single day — using explicit UTC midnight so the backend (which normalizes a
// date to its full day) lands on the right calendar day regardless of timezone.
const appendDateParams = (params: URLSearchParams) => {
    if (selectedDay.value) {
        params.append('start_date', `${selectedDay.value}T00:00:00.000Z`)
        params.append('end_date', `${selectedDay.value}T00:00:00.000Z`)
        return
    }
    if (dateRange.value.start) {
        params.append('start_date', new Date(dateRange.value.start).toISOString())
    }
    if (dateRange.value.end) {
        params.append('end_date', new Date(dateRange.value.end).toISOString())
    }
}

const fetchOverallMetrics = async () => {
    try {
        const params = new URLSearchParams()
        appendDateParams(params)

        // Add data source filter
        if (selectedAgents.value.length > 0) {
            params.append('data_source_ids', selectedAgents.value.join(','))
        }

        // Fetch dashboard metrics and judge response
        const [dashboardResponse, judgeResponse] = await Promise.all([
            useMyFetch<any>(`/api/console/diagnosis/metrics?${params}`),
            useMyFetch<any>(`/api/console/metrics?${params}`)
        ])
        
        if (dashboardResponse.data.value) {
            dashboardMetrics.value = dashboardResponse.data.value

            // Update filter counts
            filterOptions.value = [
                { label: filterLabelFor('all'), value: 'all', count: dashboardResponse.data.value.total_items },
                { label: filterLabelFor('negative_feedback'), value: 'negative_feedback', count: dashboardResponse.data.value.negative_feedback },
                { label: filterLabelFor('failed_queries'), value: 'failed_queries', count: dashboardResponse.data.value.failed_queries },
                { label: filterLabelFor('low_confidence'), value: 'low_confidence', count: dashboardResponse.data.value.low_confidence || 0 },
                { label: filterLabelFor('low_instruction_coverage'), value: 'low_instruction_coverage', count: dashboardResponse.data.value.low_instruction_coverage || 0 }
            ]
        }
        
        if (judgeResponse.data.value) {
            instructionsEffectiveness.value = judgeResponse.data.value.instructions_effectiveness
        }
    } catch (error) {
        console.error('Failed to fetch overall metrics:', error)
    }
}

const fetchTimeseries = async () => {
    isTimeseriesLoading.value = true
    try {
        const params = new URLSearchParams()
        if (dateRange.value.start) {
            params.append('start_date', new Date(dateRange.value.start).toISOString())
        }
        if (dateRange.value.end) {
            params.append('end_date', new Date(dateRange.value.end).toISOString())
        }
        if (selectedAgents.value.length > 0) {
            params.append('data_source_ids', selectedAgents.value.join(','))
        }

        const response = await useMyFetch<any>(`/api/console/diagnosis/timeseries?${params}`)
        if (response.data.value) {
            timeseriesPoints.value = response.data.value.points || []
        } else {
            timeseriesPoints.value = []
        }
    } catch (error) {
        console.error('Failed to fetch diagnosis timeseries:', error)
        timeseriesPoints.value = []
    } finally {
        isTimeseriesLoading.value = false
    }
}

const getInstructionsEffectiveness = () => {
    if (instructionsEffectiveness.value === null || instructionsEffectiveness.value === undefined) {
        return t('monitoring.diagnosis.naAbbr')
    }
    return Math.round(instructionsEffectiveness.value)
}

const getDateRangeDays = () => {
    if (!dateRange.value.start || !dateRange.value.end) return '30'
    
    const start = new Date(dateRange.value.start)
    const end = new Date(dateRange.value.end)
    const diffTime = Math.abs(end.getTime() - start.getTime())
    const diffDays = Math.ceil(diffTime / (1000 * 60 * 60 * 24))
    
    return diffDays.toString()
}

// Add this method
const openTrace = (item: any) => {
    selectedTraceItem.value = item
    showTraceModal.value = true
}

const openReport = (item: any) => {
    if (item.report_link) {
        window.open(item.report_link, '_blank')
    }
}

const openTraceFromAE = (item: any) => {
    selectedTraceItem.value = {
        report_id: item.report_id,
        completion_id: item.completion_id || '',
        id: item.completion_id || ''
    }
    showTraceModal.value = true
}

const formatFeedback = (dir: number | null | undefined) => {
    if (dir == null) return '0/0'
    if (dir > 0) return '1/0'
    if (dir < 0) return '0/1'
    return '0/0'
}

const truncate = (text: string, length: number) => {
    if (!text) return ''
    if (text.length <= length) return text
    return text.slice(0, length) + '…'
}

// Filter methods
const handleFilterChange = (filter: { label: string, value: string }) => {
    selectedFilter.value = filter
    currentPage.value = 1
    fetchDiagnosisData()
}

// Activity chart bar click -> filter the KPI cards + table to that day (toggle off if same day)
const handleDaySelect = (date: string) => {
    selectedDay.value = selectedDay.value === date ? null : date
    currentPage.value = 1
    Promise.all([
        fetchOverallMetrics(),
        fetchDiagnosisData()
    ])
}

const clearDayFilter = () => {
    selectedDay.value = null
    currentPage.value = 1
    Promise.all([
        fetchOverallMetrics(),
        fetchDiagnosisData()
    ])
}



// Watch for page changes
watch(currentPage, () => {
    fetchDiagnosisData()
})

// Watch for agent selection changes
watch(selectedAgents, () => {
    currentPage.value = 1
    selectedDay.value = null
    Promise.all([
        fetchOverallMetrics(),
        fetchTimeseries(),
        fetchDiagnosisData()
    ])
}, { deep: true })

// Initialize
onMounted(async () => {
    initializeDateRange()
    // Initialize agents for the selector
    await initAgent()
    // Fetch dashboard metrics, timeseries, and diagnosis data on initial load
    await Promise.all([
        fetchOverallMetrics(),
        fetchTimeseries(),
        fetchDiagnosisData()
    ])
})
</script>
