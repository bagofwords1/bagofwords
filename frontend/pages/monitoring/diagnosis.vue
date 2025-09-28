<template>
    <div class="mt-6">
        <!-- Date Range Picker (same as ConsoleOverview) -->
        <DateRangePicker
            :selected-period="selectedPeriod"
            :date-range="dateRange"
            @period-change="handlePeriodChange"
        />

        <!-- Summary Cards (matching MetricsCards.vue style) -->
        <div class="grid grid-cols-1 md:grid-cols-4 gap-6 mb-8">
            <!-- Failed Queries -->
            <div class="bg-white p-6 border border-gray-200 rounded-xl shadow-sm">
                <div class="text-2xl font-bold text-gray-900">
                    {{ dashboardMetrics?.failed_queries || 0 }}
                </div>
                <div class="text-sm font-medium text-gray-600 mt-1">Failed Queries</div>
            </div>
            
            <!-- Negative Feedback -->
            <div class="bg-white p-6 border border-gray-200 rounded-xl shadow-sm">
                <div class="text-2xl font-bold text-gray-900">
                    {{ dashboardMetrics?.negative_feedback || 0 }}
                </div>
                <div class="text-sm font-medium text-gray-600 mt-1">Negative Feedback</div>
            </div>
            
            <!-- Instructions Effectiveness -->
            <div class="bg-white p-6 border border-gray-200 rounded-xl shadow-sm">
                <div class="text-2xl font-bold text-gray-900">
                    {{ isJudgeEnabled ? (getInstructionsEffectiveness() + '%') : 'N/A' }}
                </div>
                <div class="text-sm font-medium text-gray-600 mt-1 flex items-center">
                    Instructions Effectiveness
                    <UTooltip :text="isJudgeEnabled ? 'AI judge score for how well instructions guide responses (20-100 scale, average for period)' : 'LLM Judge agent is turned off'">
                        <UIcon name="i-heroicons-information-circle" class="w-4 h-4 ml-1 text-gray-400 cursor-help" />
                    </UTooltip>
                </div>
            </div>
            
            <!-- Total Items -->
            <div class="bg-white p-6 border border-gray-200 rounded-xl shadow-sm">
                <div class="text-2xl font-bold text-gray-900">
                    {{ dashboardMetrics?.total_items || 0 }}
                </div>
                <div class="text-sm font-medium text-gray-600 mt-1">Total Agent Runs</div>
            </div>
        </div>

        <!-- Filter Tabs -->
        <div class="mb-6">
            <div class="border-b border-gray-200">
                <nav class="-mb-px flex space-x-8">
                    <button
                        v-for="filter in filterOptions"
                        :key="filter.value"
                        @click="handleFilterChange(filter)"
                        :class="[
                            selectedFilter.value === filter.value
                                ? 'border-blue-500 text-blue-600'
                                : 'border-transparent text-gray-500 hover:text-gray-700 hover:border-gray-300',
                            'whitespace-nowrap py-2 px-1 border-b-2 font-medium text-sm'
                        ]"
                    >
                        {{ filter.label }}
                        <span
                            v-if="filter.count !== undefined && filter.count >= 0"
                            :class="[
                                selectedFilter.value === filter.value
                                    ? 'bg-blue-100 text-blue-600'
                                    : 'bg-gray-100 text-gray-600',
                                'ml-2 py-0.5 px-2 rounded-full text-xs font-medium'
                            ]"
                        >
                            {{ filter.count }}
                        </span>
                    </button>
                </nav>
            </div>
        </div>

        <!-- Loading state -->
        <div v-if="isLoading" class="flex items-center justify-center py-12">
            <div class="flex items-center space-x-2">
                <div class="animate-spin rounded-full h-6 w-6 border-b-2 border-blue-600"></div>
                <span class="text-gray-600">Loading diagnosis data...</span>
            </div>
        </div>

        <!-- Agent Executions Table -->
        <div v-else class="bg-white shadow-sm border border-gray-200 rounded-lg overflow-hidden">
            <div class="overflow-x-auto">
                <table class="min-w-full divide-y divide-gray-200">
                    <thead class="bg-gray-50">
                        <tr>
                            <th class="px-6 py-3 text-left text-xs font-medium text-gray-500 uppercase tracking-wider min-w-[320px] w-[320px]">Prompt</th>
                            <th class="px-6 py-3 text-left text-xs font-medium text-gray-500 uppercase tracking-wider">Status</th>
                            <th class="px-6 py-3 text-left text-xs font-medium text-gray-500 uppercase tracking-wider">Data</th>
                            <th class="px-6 py-3 text-left text-xs font-medium text-gray-500 uppercase tracking-wider">Tools</th>
                            <th class="px-6 py-3 text-left text-xs font-medium text-gray-500 uppercase tracking-wider">Feedback</th>
                            <th class="px-6 py-3 text-left text-xs font-medium text-gray-500 uppercase tracking-wider">Report</th>
                            <th class="px-6 py-3 text-left text-xs font-medium text-gray-500 uppercase tracking-wider">User</th>
                            <th class="px-6 py-3 text-left text-xs font-medium text-gray-500 uppercase tracking-wider">Date</th>
                        </tr>
                    </thead>
                    <tbody class="bg-white divide-y divide-gray-200 text-xs">
                        <tr v-for="item in executionItems" :key="item.agent_execution_id" class="hover:bg-gray-50 cursor-pointer" @click="openTraceFromAE(item)">
                            <td class="px-6 py-4">
                                <div class="text-xs text-gray-900">
                                    <div class="relative group max-w-[320px] w-[320px]">
                                        <p class="truncate">{{ truncate(item.prompt || '', 40) }}</p>
                                        <div class="pointer-events-none absolute left-0 top-full mt-1 z-10 hidden group-hover:block bg-white border border-gray-200 rounded-md shadow-sm p-2 text-xs whitespace-pre-wrap max-w-[520px] max-h-56 overflow-auto">
                                            {{ item.prompt || '—' }}
                                        </div>
                                    </div>
                                </div>
                            </td>
                            <td class="px-6 py-4 whitespace-nowrap">
                                <div class="relative inline-block group">
                                    <span class="inline-flex px-2 py-1 text-xs font-medium rounded-full"
                                          :class="item.agent_execution_status === 'error' ? 'bg-red-100 text-red-800' : (item.agent_execution_status === 'completed' || item.agent_execution_status === 'success') ? 'bg-green-100 text-green-800' : 'bg-gray-100 text-gray-800'">
                                        {{ item.agent_execution_status === 'error' ? 'error' : 'success' }}
                                    </span>
                                    <div v-if="item.agent_execution_status === 'error' && item.error_json?.message" class="pointer-events-none absolute left-0 top-full mt-1 z-10 hidden group-hover:block bg-white border border-gray-200 rounded-md shadow-sm p-2 text-xs text-red-700 whitespace-pre-wrap max-w-[520px] max-h-56 overflow-auto">
                                        {{ item.error_json.message }}
                                    </div>
                                </div>
                            </td>
                            <td class="px-6 py-4">
                                <div class="flex flex-wrap gap-1 max-w-md">
                                    <span v-for="(title, idx) in item.step_titles || []" :key="idx" class="px-2 py-0.5 bg-gray-100 text-gray-700 rounded text-[11px]"
                                          @click.stop="openTraceFromAE(item)">
                                        {{ title }}
                                    </span>
                                    <span v-if="(item.step_titles || []).length === 0" class="text-gray-400">None</span>
                                </div>
                            </td>
                            <td class="px-6 py-4">
                                <div class="text-xs text-gray-900">Total: {{ item.total_tools }}</div>
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
                                          :class="item.feedback_direction > 0 ? 'bg-green-100 text-green-800' : item.feedback_direction < 0 ? 'bg-red-100 text-red-800' : 'bg-gray-100 text-gray-800'">
                                        {{ item.feedback_direction > 0 ? 'Positive' : (item.feedback_direction < 0 ? 'Negative' : 'No Feedback') }}
                                    </span>
                                    <div v-if="item.feedback_direction < 0 && item.feedback_message" class="mt-1 text-xs text-gray-600 max-w-sm">
                                        <UTooltip :text="item.feedback_message">
                                            <span class="truncate cursor-help">{{ truncate(item.feedback_message, 120) }}</span>
                                        </UTooltip>
                                    </div>
                                </div>
                            </td>
                            <td class="px-6 py-4 whitespace-nowrap">
                                <a v-if="item.report_link" :href="item.report_link" class="text-blue-600 hover:underline" @click.stop>
                                    {{ item.report_name || item.report_id }}
                                </a>
                                <span v-else>{{ item.report_name || item.report_id }}</span>
                            </td>
                            <td class="px-2 py-1">
                                <div class="text-xs text-gray-900">{{ item.user_name || '—' }}</div>
                            </td>
                            <td class="px-3 py-1">
                                <span class="text-xs text-gray-500">{{ formatDate(item.created_at as any) }}</span>
                            </td>
                        </tr>
                    </tbody>
                </table>
            </div>

            <!-- Empty state -->
            <div v-if="executionItems.length === 0 && !isLoading" class="text-center py-12">
                <UIcon name="i-heroicons-clipboard-document-check" class="mx-auto h-12 w-12 text-gray-400" />
                <h3 class="mt-2 text-sm font-medium text-gray-900">No data found</h3>
                <p class="mt-1 text-sm text-gray-500">
                    No agent executions found for the selected period.
                </p>
                <div class="mt-2 text-xs text-gray-400">
                    Debug: {{ debugInfo }}
                </div>
            </div>
        </div>

        <!-- Pagination -->
        <div v-if="executionItems.length > 0" class="mt-6 flex items-center justify-between">
            <div class="text-sm text-gray-700">
                Showing {{ (currentPage - 1) * pageSize + 1 }} to {{ Math.min(currentPage * pageSize, totalItems) }} of {{ totalItems }} results
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
                    Previous
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
                    Next
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
const { isJudgeEnabled } = useOrgSettings()

definePageMeta({
    layout: 'monitoring'
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

// Filter state
const selectedFilter = ref({ label: 'All Agent Runs', value: 'all' })
const filterOptions = ref([
    { label: 'All Agent Runs', value: 'all', count: 0 },
    { label: 'Negative Feedback', value: 'negative_feedback', count: 0 },
    { label: 'Failed Queries', value: 'failed_queries', count: 0 }
])

// Add these to the state section
const showTraceModal = ref(false)
const selectedTraceItem = ref<any | null>(null)

// Date range state (same as ConsoleOverview)
const selectedPeriod = ref({ label: 'All Time', value: 'all_time' })
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
    selectedPeriod.value = { label: 'All Time', value: 'all_time' }
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
    // Refresh both overall metrics and diagnosis data when date range changes
    Promise.all([
        fetchOverallMetrics(),
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
        
        if (dateRange.value.start) {
            params.append('start_date', new Date(dateRange.value.start).toISOString())
        }
        if (dateRange.value.end) {
            params.append('end_date', new Date(dateRange.value.end).toISOString())
        }
        
        // Add filter parameter
        if (selectedFilter.value.value !== 'all') {
            params.append('filter', selectedFilter.value.value)
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
            return 'bg-red-100 text-red-800'
        case 'validation_error':
            return 'bg-yellow-100 text-yellow-800'
        case 'negative_feedback':
            return 'bg-orange-100 text-orange-800'
        case 'no_issue':
            return 'bg-green-100 text-green-800'
        default:
            return 'bg-gray-100 text-gray-800'
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

const formatDate = (dateString: string) => {
    if (!dateString) return ''
    const date = new Date(dateString)
    return date.toLocaleDateString()
}

// Add these methods to the existing script section

const fetchOverallMetrics = async () => {
    try {
        const params = new URLSearchParams()
        if (dateRange.value.start) {
            params.append('start_date', new Date(dateRange.value.start).toISOString())
        }
        if (dateRange.value.end) {
            params.append('end_date', new Date(dateRange.value.end).toISOString())
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
                { label: 'All Agent Runs', value: 'all', count: dashboardResponse.data.value.total_items },
                { label: 'Negative Feedback', value: 'negative_feedback', count: dashboardResponse.data.value.negative_feedback },
                { label: 'Failed Queries', value: 'failed_queries', count: dashboardResponse.data.value.failed_queries }
            ]
        }
        
        if (judgeResponse.data.value) {
            instructionsEffectiveness.value = judgeResponse.data.value.instructions_effectiveness
        }
    } catch (error) {
        console.error('Failed to fetch overall metrics:', error)
    }
}

const getInstructionsEffectiveness = () => {
    if (instructionsEffectiveness.value === null || instructionsEffectiveness.value === undefined) {
        return 'N/A'
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



// Watch for page changes
watch(currentPage, () => {
    fetchDiagnosisData()
})

// Initialize
onMounted(async () => {
    initializeDateRange()
    // Fetch dashboard metrics and diagnosis data on initial load
    await Promise.all([
        fetchOverallMetrics(),
        fetchDiagnosisData()
    ])
})
</script>