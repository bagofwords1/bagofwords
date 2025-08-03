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
                    {{ overallMetrics?.failed_steps_count || 0 }}
                </div>
                <div class="text-sm font-medium text-gray-600 mt-1">Failed Queries</div>
            </div>
            
            <!-- Negative Feedback -->
            <div class="bg-white p-6 border border-gray-200 rounded-xl shadow-sm">
                <div class="text-2xl font-bold text-gray-900">
                    {{ overallMetrics?.negative_feedback_count || 0 }}
                </div>
                <div class="text-sm font-medium text-gray-600 mt-1">Negative Feedback</div>
            </div>
            
            <!-- Instructions Effectiveness -->
            <div class="bg-white p-6 border border-gray-200 rounded-xl shadow-sm">
                <div class="text-2xl font-bold text-gray-900">
                    {{ getInstructionsEffectiveness() }}%
                </div>
                <div class="text-sm font-medium text-gray-600 mt-1 flex items-center">
                    Instructions Effectiveness
                    <UTooltip text="AI judge score for how well instructions guide responses (20-100 scale, average for period)">
                        <UIcon name="i-heroicons-information-circle" class="w-4 h-4 ml-1 text-gray-400 cursor-help" />
                    </UTooltip>
                </div>
            </div>
            
            <!-- Total Issues -->
            <div class="bg-white p-6 border border-gray-200 rounded-xl shadow-sm">
                <div class="text-2xl font-bold text-gray-900">
                    {{ overallMetrics?.total_items || 0 }}
                </div>
                <div class="text-sm font-medium text-gray-600 mt-1">Total Issues</div>
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

        <!-- Diagnosis Table -->
        <div v-else class="bg-white shadow-sm border border-gray-200 rounded-lg overflow-hidden">
            <div class="overflow-x-auto">
                <table class="min-w-full divide-y divide-gray-200">
                    <thead class="bg-gray-50">
                        <tr>
                            <th class="px-6 py-3 text-left text-xs font-medium text-gray-500 uppercase tracking-wider">
                                Status
                            </th>

                            <th class="px-6 py-3 text-left text-xs font-medium text-gray-500 uppercase tracking-wider">
                                Head Prompt
                            </th>
                            <th class="px-6 py-3 text-left text-xs font-medium text-gray-500 uppercase tracking-wider">
                                Query
                            </th>
                            <th class="px-6 py-3 text-left text-xs font-medium text-gray-500 uppercase tracking-wider">
                                Feedback
                            </th>
                            <th class="px-6 py-3 text-left text-xs font-medium text-gray-500 uppercase tracking-wider">
                                User
                            </th>
                            <th class="px-6 py-3 text-left text-xs font-medium text-gray-500 uppercase tracking-wider">
                                Created
                            </th>
                            <th class="px-6 py-3 text-left text-xs font-medium text-gray-500 uppercase tracking-wider">
                                Actions
                            </th>
                        </tr>
                    </thead>
                    <tbody class="bg-white divide-y divide-gray-200 text-xs">
                        <tr v-for="item in diagnosisItems" :key="item.id" class="hover:bg-gray-50 cursor-pointer" @click="openTrace(item)">
                            <!-- Status -->
                            <td class="px-6 py-4 whitespace-nowrap">
                                <span class="inline-flex px-1 py-0.5 text-xs font-medium rounded-full"
                                      :class="getIssueTypeClass(item.issue_type)">
                                    {{ getIssueTypeLabel(item.issue_type) }}
                                </span>
                            </td>
                            
                            <!-- Head Completion -->
                            <td class="px-6 py-4">
                                <div class="text-xs text-gray-900 max-w-md">
                                    <p class="truncate" :title="item.head_completion_prompt">
                                        {{ item.head_completion_prompt || 'No prompt available' }}
                                    </p>
                                </div>
                            </td>
                            
                            <!-- Step Info -->
                            <td class="px-3 py-1">
                                <div v-if="item.step_info" class="text-xs">
                                    <div class="text-gray-900">{{ item.step_info.step_title }}</div>
                                    <div class="text-gray-500">Status: {{ item.step_info.step_status }}</div>
                                </div>
                                <div v-else class="text-xs text-gray-400">N/A</div>
                            </td>
                            
                            <!-- Feedback -->
                            <td class="px-3 py-1">
                                <div v-if="item.feedback_info" class="text-xs">
                                    <div class="flex items-center">
                                        <UIcon name="i-heroicons-hand-thumb-down" class="w-4 h-4 text-red-500 mr-2" />
            <div class="text-gray-500 max-w-xs truncate" :title="item.feedback_info.message">
                                        {{ item.feedback_info.message || 'No message' }}
                                    </div>
                                    </div>
                        
                                </div>
                                <div v-else class="text-xs text-gray-400">No feedback</div>
                            </td>
                            
                            <!-- User -->
                            <td class="px-2 py-1">
                                <div class="text-xs text-gray-900">{{ item.user_name }}</div>
                            </td>
                            
                            <!-- Time -->
                            <td class="px-3 py-1">
                                <span class="text-xs text-gray-500">
                                    {{ formatDate(item.created_at) }}
                                </span>
                            </td>
                            
                            <!-- Actions -->
                            <td class="px-6 py-4 whitespace-nowrap text-xs font-medium">
                                <UButton
                                    variant="outline"
                                    size="xs"
                                    color="blue"
                                    @click="openTrace(item)"
                                >
                                    Trace
                                </UButton>
                            </td>
                        </tr>
                    </tbody>
                </table>
            </div>

            <!-- Empty state -->
            <div v-if="diagnosisItems.length === 0 && !isLoading" class="text-center py-12">
                <UIcon name="i-heroicons-clipboard-document-check" class="mx-auto h-12 w-12 text-gray-400" />
                <h3 class="mt-2 text-sm font-medium text-gray-900">No data found</h3>
                <p class="mt-1 text-sm text-gray-500">
                    No {{ selectedFilter.label.toLowerCase() }} found for the selected period.
                </p>
                <div class="mt-2 text-xs text-gray-400">
                    Debug: {{ debugInfo }}
                </div>
            </div>
        </div>

        <!-- Pagination -->
        <div v-if="diagnosisItems.length > 0" class="mt-6 flex items-center justify-between">
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
            :completion-id="selectedTraceItem?.id || ''"
        />
    </div>
</template>

<script setup lang="ts">
import DateRangePicker from '~/components/console/DateRangePicker.vue'
import TraceModal from '~/components/console/TraceModal.vue'

definePageMeta({
    layout: 'console'
})

// Types
interface DiagnosisStepData {
    step_id: string
    step_title: string
    step_status: string
    step_code?: string
    step_data_model?: any
    created_at: string
}

interface DiagnosisFeedbackData {
    feedback_id: string
    direction: number
    message?: string
    created_at: string
}

interface DiagnosisItemData {
    id: string
    head_completion_id: string
    head_completion_prompt: string
    problematic_completion_id: string
    problematic_completion_content?: string
    user_id: string
    user_name: string
    user_email?: string
    report_id: string
    issue_type: string
    step_info?: DiagnosisStepData
    feedback_info?: DiagnosisFeedbackData
    created_at: string
    trace_url?: string
}

interface DiagnosisMetrics {
    diagnosis_items: DiagnosisItemData[]
    total_items: number
    total_queries_count: number
    failed_steps_count: number
    negative_feedback_count: number
    code_errors_count: number
    validation_errors_count: number
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
const metrics = ref<DiagnosisMetrics | null>(null)
const overallMetrics = ref<DiagnosisMetrics | null>(null) // Static metrics for top cards
const diagnosisItems = ref<DiagnosisItemData[]>([])
const currentPage = ref(1)
const pageSize = ref(10)
const totalItems = ref(0)
const debugInfo = ref('')
const instructionsEffectiveness = ref<number | null>(null)

// Filter state
const selectedFilter = ref({ label: 'All Issues', value: 'all' })
const filterOptions = ref([
    { label: 'All Issues', value: 'all', count: 0 },
    { label: 'All Queries', value: 'all_queries', count: 0 },
    { label: 'Negative Feedback', value: 'negative_feedback', count: 0 },
    { label: 'Code Errors', value: 'code_errors', count: 0 },
    { label: 'Validation Errors', value: 'validation_errors', count: 0 }
])

// Add these to the state section
const showTraceModal = ref(false)
const selectedTraceItem = ref<DiagnosisItemData | null>(null)

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
        
        // Fetch diagnosis data
        const diagnosisResponse = await useMyFetch<DiagnosisMetrics>(`/api/console/metrics/diagnosis?${params}`)
        
        if (diagnosisResponse.error.value) {
            console.error('Error fetching diagnosis data:', diagnosisResponse.error.value)
            debugInfo.value = `Error: ${diagnosisResponse.error.value}`
            metrics.value = null
            diagnosisItems.value = []
            totalItems.value = 0
        } else if (diagnosisResponse.data.value) {
            metrics.value = diagnosisResponse.data.value
            diagnosisItems.value = diagnosisResponse.data.value.diagnosis_items || []
            totalItems.value = diagnosisResponse.data.value.total_items || 0
            debugInfo.value = `Loaded ${diagnosisItems.value.length} items, total: ${totalItems.value}`
            
            // Update filter counts
            updateFilterCounts(diagnosisResponse.data.value)
        }
    } catch (error) {
        console.error('Failed to fetch diagnosis data:', error)
        debugInfo.value = `Exception: ${error}`
        metrics.value = null
        diagnosisItems.value = []
        totalItems.value = 0
    } finally {
        isLoading.value = false
    }
}

const getIssueTypeClass = (issueType: string) => {
    switch (issueType) {
        case 'failed_step':
        case 'code_error':
            return 'bg-red-100 text-red-800'
        case 'validation_error':
            return 'bg-yellow-100 text-yellow-800'
        case 'negative_feedback':
            return 'bg-orange-100 text-orange-800'
        case 'both':
            return 'bg-purple-100 text-purple-800'
        case 'no_issue':
            return 'bg-green-100 text-green-800'
        default:
            return 'bg-gray-100 text-gray-800'
    }
}

const getIssueTypeLabel = (issueType: string) => {
    switch (issueType) {
        case 'failed_step':
            return 'Failed Step'
        case 'code_error':
            return 'Code Error'
        case 'validation_error':
            return 'Validation Error'
        case 'negative_feedback':
            return 'Negative Feedback'
        case 'both':
            return 'Both Issues'
        case 'no_issue':
            return 'Success'
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
        
        // Fetch overall metrics (without any filter) for the top cards
        const [metricsResponse, judgeResponse] = await Promise.all([
            useMyFetch<DiagnosisMetrics>(`/api/console/metrics/diagnosis?${params}`),
            useMyFetch<any>(`/api/console/metrics?${params}`)
        ])
        
        if (metricsResponse.data.value) {
            overallMetrics.value = metricsResponse.data.value
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
const openTrace = (item: DiagnosisItemData) => {
    selectedTraceItem.value = item
    showTraceModal.value = true
}

// Filter methods
const handleFilterChange = (filter: { label: string, value: string }) => {
    selectedFilter.value = filter
    currentPage.value = 1
    fetchDiagnosisData()
}

const updateFilterCounts = (data: DiagnosisMetrics) => {
    filterOptions.value = [
        { label: 'All Issues', value: 'all', count: data.total_items },
        { label: 'All Queries', value: 'all_queries', count: data.total_queries_count },
        { label: 'Negative Feedback', value: 'negative_feedback', count: data.negative_feedback_count },
        { label: 'Code Errors', value: 'code_errors', count: data.code_errors_count },
        { label: 'Validation Errors', value: 'validation_errors', count: data.validation_errors_count }
    ]
}

// Watch for page changes
watch(currentPage, () => {
    fetchDiagnosisData()
})

// Initialize
onMounted(async () => {
    initializeDateRange()
    // Fetch both overall metrics and diagnosis data on initial load
    await Promise.all([
        fetchOverallMetrics(),
        fetchDiagnosisData()
    ])
})
</script>