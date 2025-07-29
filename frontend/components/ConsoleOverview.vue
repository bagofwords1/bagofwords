<template>
    <div class="mt-6">
        <!-- Date Range Picker -->
        <DateRangePicker
            :selected-period="selectedPeriod"
            :custom-date-range="customDateRange"
            :date-range="dateRange"
            @period-change="handlePeriodChange"
            @custom-range-change="handleCustomDateChange"
            @apply-custom-range="applyCustomRange"
        />
            
        <!-- Metrics Cards -->
        <MetricsCards :metrics-comparison="metricsComparison" />

        <!-- Main Charts Section -->
        <div class="grid grid-cols-1 lg:grid-cols-2 gap-6 mb-8">
            <!-- Activity Chart -->
            <ActivityChart
                :activity-metrics="timeSeriesData?.activity_metrics || null"
                :is-loading="isLoadingCharts"
            />

            <!-- Performance Chart -->
            <PerformanceChart
                :performance-metrics="timeSeriesData?.performance_metrics || null"
                :is-loading="isLoadingCharts"
            />
        </div>

        <!-- Secondary Charts Section -->
        <div class="space-y-6 mb-8">
            <!-- First Row: Table Usage (Bar Chart) + Table Joins Heatmap -->
            <div class="grid grid-cols-1 lg:grid-cols-2 gap-6">
                <!-- Table Usage Chart -->
                <TableUsageChart
                    :table-usage-data="tableUsageData"
                    :is-loading="isLoadingTableCharts"
                />
                
                <!-- Table Joins Heatmap -->
                <TableJoinsHeatmap
                    :table-joins-data="tableJoinsData"
                    :is-loading="isLoadingTableCharts"
                />
            </div>

            <!-- Second Row: Failed Queries + Negative Feedback -->
            <div class="grid grid-cols-1 lg:grid-cols-2 gap-6 mb-6">
                <!-- Recently Failed Queries -->
                <div class="bg-white rounded-2xl border border-gray-100 shadow-sm overflow-hidden">
                    <div class="p-6 border-b border-gray-50">
                        <h3 class="text-lg font-semibold text-gray-900">Recently Failed Queries</h3>
                        <p class="text-sm text-gray-500 mt-1">Latest query failures - for more go to <nuxt-link to="/console/diagnosis" class="text-blue-600 hover:text-blue-800">diagnosis</nuxt-link> page</p>
                    </div>
                    <div class="p-0">
                        <div class="overflow-hidden">
                            <table class="min-w-full">
                                <thead class="bg-gray-50">
                                    <tr>
                                        <th class="px-6 py-3 text-left text-xs font-medium text-gray-500 uppercase tracking-wider">Prompt</th>
                                        <th class="px-6 py-3 text-left text-xs font-medium text-gray-500 uppercase tracking-wider">Failure Reason</th>
                                    </tr>
                                </thead>
                                <tbody class="bg-white divide-y divide-gray-200">
                                    <tr v-for="(query, index) in mockFailedQueries" :key="index" class="hover:bg-gray-50">
                                        <td class="px-6 py-4">
                                            <div class="text-sm text-gray-900 max-w-xs truncate" :title="query.prompt">
                                                {{ query.prompt }}
                                            </div>
                                            <div class="text-xs text-gray-500">
                                                {{ new Date(query.timestamp).toLocaleString() }}
                                            </div>
                                        </td>
                                        <td class="px-6 py-4 whitespace-nowrap">
                                            <div class="flex items-center">
                                                <Icon 
                                                    :name="query.failure_reason === 'validation' ? 'heroicons-exclamation-triangle' : 'heroicons-x-circle'"
                                                    :class="[
                                                        'w-4 h-4 mr-2',
                                                        query.failure_reason === 'validation' ? 'text-yellow-500' : 'text-red-500'
                                                    ]"
                                                />
                                                <span :class="[
                                                    'inline-flex items-center px-2.5 py-0.5 rounded-full text-xs font-medium',
                                                    query.failure_reason === 'validation' ? 'bg-yellow-100 text-yellow-800' : 'bg-red-100 text-red-800'
                                                ]">
                                                    {{ query.failure_reason }}
                                                </span>
                                            </div>
                                        </td>
                                    </tr>
                                </tbody>
                            </table>
                        </div>
                    </div>
                </div>

                <!-- Recent Negative Feedback -->
                <RecentNegativeFeedbackTable
                    :negative-feedback-data="negativeFeedbackData"
                    :is-loading="isLoadingWidgets"
                />
            </div>

            <!-- Third Row: Top Users + Top Prompt Types (50%-50%) -->
            <div class="grid grid-cols-1 lg:grid-cols-2 gap-6">
                <!-- Top Users -->
                <TopUsersTable
                    :top-users-data="topUsersData"
                    :is-loading="isLoadingWidgets"
                />
                
                <!-- Top Prompt Types -->
                <TopPromptTypesChart
                    :prompt-types-data="mockPromptTypesData"
                />
            </div>
        </div>
    </div>
</template>

<script setup lang="ts">
// Import components
import DateRangePicker from '~/components/console/DateRangePicker.vue'
import MetricsCards from '~/components/console/MetricsCards.vue'
import ActivityChart from '~/components/console/ActivityChart.vue'
import PerformanceChart from '~/components/console/PerformanceChart.vue'
import TableUsageChart from '~/components/console/TableUsageChart.vue'
import TableJoinsHeatmap from '~/components/console/TableJoinsHeatmap.vue'
import TopPromptTypesChart from '~/components/console/TopPromptTypesChart.vue'
import TopUsersTable from '~/components/console/TopUsersTable.vue'
import RecentNegativeFeedbackTable from '~/components/console/RecentNegativeFeedbackTable.vue'

// Interfaces
interface SimpleMetrics {
    total_messages: number
    total_queries: number
    total_feedbacks: number
    accuracy: string
    instructions_coverage: string
    active_users: number
    // Removed instructions_efficiency and feedback_efficiency
}

interface MetricChange {
    absolute: number
    percentage: number
}

interface MetricsComparison {
    current: SimpleMetrics
    previous: SimpleMetrics
    changes: Record<string, MetricChange>
    period_days: number
}

interface TimeSeriesPoint {
    date: string
    value: number
}

interface TimeSeriesPointFloat {
    date: string
    value: number
}

interface DateRange {
    start: string
    end: string
}

interface ActivityMetrics {
    messages: TimeSeriesPoint[]
    queries: TimeSeriesPoint[]
}

interface PerformanceMetrics {
    accuracy: TimeSeriesPointFloat[]
    instructions_efficiency: TimeSeriesPointFloat[]
    positive_feedback_rate: TimeSeriesPointFloat[]
}

interface TimeSeriesMetrics {
    date_range: DateRange
    activity_metrics: ActivityMetrics
    performance_metrics: PerformanceMetrics
}

// Add these interfaces after the existing ones
interface TableUsageData {
    table_name: string
    usage_count: number
    database_name?: string
}

interface TableUsageMetrics {
    top_tables: TableUsageData[]
    total_queries_analyzed: number
    date_range: DateRange
}

interface TableJoinData {
    table1: string
    table2: string
    join_count: number
}

interface TableJoinsHeatmap {
    table_pairs: TableJoinData[]
    unique_tables: string[]
    total_queries_analyzed: number
    date_range: DateRange
}

interface TopUserData {
    user_id: string
    name: string
    email?: string
    role?: string
    messages_count: number
    queries_count: number
    trend_percentage: number
}

interface TopUsersMetrics {
    top_users: TopUserData[]
    total_users_analyzed: number
    date_range: DateRange
}

interface RecentNegativeFeedbackData {
    id: string
    description: string
    user_name: string
    user_id: string
    completion_id: string
    prompt?: string
    created_at: string
    trace?: string
}

interface RecentNegativeFeedbackMetrics {
    recent_feedbacks: RecentNegativeFeedbackData[]
    total_negative_feedbacks: number
    date_range: DateRange
}

// State
const metricsComparison = ref<MetricsComparison | null>(null)
const dateRange = ref({
    start: '',
    end: ''
})
const isLoadingCharts = ref(false)
const timeSeriesData = ref<TimeSeriesMetrics | null>(null)

// Add these missing state definitions:
const selectedPeriod = ref({ label: 'All Time', value: 'all_time' })
const customDateRange = ref()

const periodOptions = [
    { label: 'All Time', value: 'all_time' },
    { label: 'Last 30 Days', value: '30_days' },
    { label: 'Last 90 Days', value: '90_days' },
    { label: 'Custom Range', value: 'custom' }
]

// Replace the mock data state with real data state
const tableUsageData = ref<TableUsageMetrics | null>(null)
const tableJoinsData = ref<TableJoinsHeatmap | null>(null)
const isLoadingTableCharts = ref(false)

const topUsersData = ref<TopUsersMetrics | null>(null)
const negativeFeedbackData = ref<RecentNegativeFeedbackMetrics | null>(null)
const isLoadingWidgets = ref(false)

// Keep the other mock data for widgets that don't have backend yet
const mockLeaderboardData = ref([
    { name: 'Alice Johnson', role: 'Data Analyst', messages: 156, queries: 89, trend: 12 },
    { name: 'Bob Smith', role: 'Business Analyst', messages: 134, queries: 76, trend: 8 },
    { name: 'Carol Davis', role: 'Product Manager', messages: 98, queries: 54, trend: -3 },
    { name: 'David Wilson', role: 'Data Scientist', messages: 87, queries: 62, trend: 15 },
    { name: 'Eva Brown', role: 'Marketing', messages: 73, queries: 41, trend: 6 },
])

const mockPromptTypesData = ref([
    { type: 'Data Analysis', count: 245 },
    { type: 'Report Generation', count: 189 },
    { type: 'Chart Creation', count: 156 },
    { type: 'Data Exploration', count: 134 },
    { type: 'KPI Tracking', count: 98 },
    { type: 'Trend Analysis', count: 87 },
    { type: 'Comparison', count: 76 }
].sort((a, b) => b.count - a.count))

const mockFailedQueries = ref([
    { 
        prompt: 'Show me sales data for Q4 2024', 
        failure_reason: 'validation', 
        error_detail: 'Date range not found in database',
        timestamp: '2024-01-15 14:30:22'
    },
    { 
        prompt: 'Generate revenue chart by region', 
        failure_reason: 'code error', 
        error_detail: 'Column "region_id" does not exist',
        timestamp: '2024-01-15 13:45:18'
    },
    { 
        prompt: 'Calculate customer churn rate', 
        failure_reason: 'validation', 
        error_detail: 'Insufficient data permissions',
        timestamp: '2024-01-15 12:20:15'
    },
    { 
        prompt: 'Export user analytics to Excel', 
        failure_reason: 'code error', 
        error_detail: 'Memory limit exceeded',
        timestamp: '2024-01-15 11:55:42'
    },
    { 
        prompt: 'Show top performing products', 
        failure_reason: 'validation', 
        error_detail: 'Product table access denied',
        timestamp: '2024-01-15 10:30:08'
    }
])

const mockNegativeFeedback = ref([
    {
        description: 'Chart colors are too similar, hard to distinguish',
        trace: 'dashboard_designer.create_chart',
        user: 'Alice Johnson',
        timestamp: '2024-01-15 15:20:30'
    },
    {
        description: 'Query took too long to execute',
        trace: 'data_source.execute_query',
        user: 'Bob Smith',
        timestamp: '2024-01-15 14:45:12'
    },
    {
        description: 'Incorrect data aggregation in report',
        trace: 'reporter.generate_report',
        user: 'Carol Davis',
        timestamp: '2024-01-15 13:30:45'
    },
    {
        description: 'Missing data validation in form',
        trace: 'excel.process_upload',
        user: 'David Wilson',
        timestamp: '2024-01-15 12:15:20'
    },
    {
        description: 'Export functionality not working',
        trace: 'completion.export_results',
        user: 'Eva Brown',
        timestamp: '2024-01-15 11:40:55'
    }
])

// Helper functions for date range formatting
const formatDateRange = () => {
    if (!dateRange.value.start || selectedPeriod.value.value === 'all_time') {
        return ''
    }
    
    const start = new Date(dateRange.value.start)
    const end = new Date(dateRange.value.end)
    
    return `${start.toLocaleDateString()} - ${end.toLocaleDateString()}`
}

const initializeDateRange = () => {
    // Default to all time
    selectedPeriod.value = { label: 'All Time', value: 'all_time' }
    dateRange.value = {
        start: '',
        end: new Date().toISOString().split('T')[0]
    }
}

const handlePeriodChange = (period: { label: string, value: string }) => {
    if (period.value === 'custom') {
        // Don't change dateRange yet, wait for custom selection
        return
    }
    
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
    
    refreshData()
}

const fetchTimeSeriesData = async () => {
    isLoadingCharts.value = true
    try {
        const params = new URLSearchParams()
        if (dateRange.value.start) {
            params.append('start_date', new Date(dateRange.value.start).toISOString())
        }
        if (dateRange.value.end) {
            params.append('end_date', new Date(dateRange.value.end).toISOString())
        }
        
        const { data, error } = await useMyFetch<TimeSeriesMetrics>(`/api/console/metrics/timeseries?${params}`, {
            method: 'GET'
        })
        
        if (error.value) {
            console.error('Failed to fetch timeseries data:', error.value)
        } else if (data.value) {
            timeSeriesData.value = data.value
        }
    } catch (err) {
        console.error('Error fetching timeseries data:', err)
    } finally {
        isLoadingCharts.value = false
    }
}

const fetchTableUsageData = async () => {
    isLoadingTableCharts.value = true
    try {
        const params = new URLSearchParams()
        if (dateRange.value.start) {
            params.append('start_date', new Date(dateRange.value.start).toISOString())
        }
        if (dateRange.value.end) {
            params.append('end_date', new Date(dateRange.value.end).toISOString())
        }
        
        const { data, error } = await useMyFetch<TableUsageMetrics>(`/api/console/metrics/table-usage?${params}`, {
            method: 'GET'
        })
        
        if (error.value) {
            console.error('Failed to fetch table usage data:', error.value)
        } else if (data.value) {
            tableUsageData.value = data.value
        }
    } catch (err) {
        console.error('Error fetching table usage data:', err)
    } finally {
        isLoadingTableCharts.value = false
    }
}

const fetchTableJoinsData = async () => {
    try {
        const params = new URLSearchParams()
        if (dateRange.value.start) {
            params.append('start_date', new Date(dateRange.value.start).toISOString())
        }
        if (dateRange.value.end) {
            params.append('end_date', new Date(dateRange.value.end).toISOString())
        }
        
        const { data, error } = await useMyFetch<TableJoinsHeatmap>(`/api/console/metrics/table-joins-heatmap?${params}`, {
            method: 'GET'
        })
        
        if (error.value) {
            console.error('Failed to fetch table joins data:', error.value)
        } else if (data.value) {
            tableJoinsData.value = data.value
        }
    } catch (err) {
        console.error('Error fetching table joins data:', err)
    }
}

const fetchTopUsers = async () => {
    isLoadingWidgets.value = true
    try {
        const params = new URLSearchParams()
        if (dateRange.value.start) {
            params.append('start_date', new Date(dateRange.value.start).toISOString())
        }
        if (dateRange.value.end) {
            params.append('end_date', new Date(dateRange.value.end).toISOString())
        }
        
        const { data, error } = await useMyFetch<TopUsersMetrics>(`/api/console/metrics/top-users?${params}`, {
            method: 'GET'
        })
        
        if (error.value) {
            console.error('Failed to fetch top users:', error.value)
        } else if (data.value) {
            topUsersData.value = data.value
        }
    } catch (err) {
        console.error('Error fetching top users:', err)
    } finally {
        isLoadingWidgets.value = false
    }
}

const fetchNegativeFeedback = async () => {
    try {
        const params = new URLSearchParams()
        if (dateRange.value.start) {
            params.append('start_date', new Date(dateRange.value.start).toISOString())
        }
        if (dateRange.value.end) {
            params.append('end_date', new Date(dateRange.value.end).toISOString())
        }
        
        const { data, error } = await useMyFetch<RecentNegativeFeedbackMetrics>(`/api/console/metrics/recent-negative-feedback?${params}`, {
            method: 'GET'
        })
        
        if (error.value) {
            console.error('Failed to fetch negative feedback:', error.value)
        } else if (data.value) {
            negativeFeedbackData.value = data.value
        }
    } catch (err) {
        console.error('Error fetching negative feedback:', err)
    }
}

const refreshData = async () => {
    await Promise.all([
        fetchTimeSeriesData(),
        fetchTableUsageData(),
        fetchTableJoinsData(),
        fetchTopUsers(),
        fetchNegativeFeedback()
    ])
}

// Add custom date functions
const handleCustomDateChange = (dates: Date[]) => {
    customDateRange.value = dates
}

const applyCustomRange = (newDateRange: DateRange) => {
    dateRange.value = newDateRange
    refreshData()
}

onMounted(async () => {
    initializeDateRange()
    try {
        const { data: metricsData, error: metricsError } = await useMyFetch<MetricsComparison>('/api/console/metrics/comparison', {
            method: 'GET'
        })
        
        if (metricsError.value) {
            console.error('Failed to fetch metrics:', metricsError.value)
        } else if (metricsData.value) {
            metricsComparison.value = metricsData.value
        }
        
        // Fetch all data in parallel
        await Promise.all([
            fetchTimeSeriesData(),
            fetchTableUsageData(),
            fetchTableJoinsData(),
            fetchTopUsers(),
            fetchNegativeFeedback()
        ])
    } catch (err) {
        console.error('Error fetching data:', err)
    }
})
</script>

<style scoped>
.chart {
    width: 100%;
    height: 100%;
}
</style> 