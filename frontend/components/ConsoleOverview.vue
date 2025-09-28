<template>
    <div class="mt-6">
        <!-- Date Range Picker -->
        <DateRangePicker
            :selected-period="selectedPeriod"
            :date-range="dateRange"
            @period-change="handlePeriodChange"
        />
            
        <!-- Metrics Cards -->
        <MetricsCards :metrics-comparison="metricsComparison" :org-settings="orgSettings" />

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
                :org-settings="orgSettings"
            />
        </div>

        <!-- Secondary Charts Section -->
        <div class="space-y-6 mb-8">
            <!-- First Row: Table Usage (Bar Chart) + Tool Usage (Bar Chart) -->
            <div class="grid grid-cols-1 lg:grid-cols-2 gap-6">
                <!-- Table Usage Chart -->
                <TableUsageChart
                    :table-usage-data="tableUsageData"
                    :is-loading="isLoadingTableCharts"
                />
                
                <!-- Tool Usage Chart -->
                <ToolUsageChart
                    :tool-usage-data="toolUsageData"
                    :is-loading="isLoadingTableCharts"
                />
            </div>

            <!-- Second Row: Failed Queries + Recent Instructions -->
            <div class="grid grid-cols-1 lg:grid-cols-2 gap-6 mb-6">
                <!-- Recently Failed Queries -->
                <RecentQueries
                    :date-range="dateRange"
                />

                <!-- Recent Instructions -->
                <RecentInstructions
                    :date-range="dateRange"
                />
            </div>

            <!-- Bottom Row: Top Users -->
            <TopUsersTable
                :top-users-data="topUsersData"
                :is-loading="isLoadingWidgets"
            />

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
import ToolUsageChart from '~/components/console/ToolUsageChart.vue'
import TableJoinsHeatmap from '~/components/console/TableJoinsHeatmap.vue'
import TopPromptTypesChart from '~/components/console/TopPromptTypesChart.vue'
import TopUsersTable from '~/components/console/TopUsersTable.vue'
import RecentInstructions from '~/components/console/RecentInstructions.vue'
import RecentQueries from '~/components/console/RecentQueries.vue'

// Interfaces
interface SimpleMetrics {
    total_messages: number
    total_queries: number
    total_feedbacks: number
    accuracy: string
    instructions_coverage: string
    active_users: number
    instructions_effectiveness: number
    context_effectiveness: number
    response_quality: number
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
    instructions_coverage: TimeSeriesPointFloat[]
    instructions_effectiveness: TimeSeriesPointFloat[]
    context_effectiveness: TimeSeriesPointFloat[]
    response_quality: TimeSeriesPointFloat[]
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

interface ToolUsageItem {
    tool_name: string
    label: string
    count: number
}

interface ToolUsageMetrics {
    items: ToolUsageItem[]
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

const orgSettings = useOrgSettings()

const periodOptions = [
    { label: 'All Time', value: 'all_time' },
    { label: 'Last 30 Days', value: '30_days' },
    { label: 'Last 90 Days', value: '90_days' }
]

// Replace the mock data state with real data state
const tableUsageData = ref<TableUsageMetrics | null>(null)
const tableJoinsData = ref<TableJoinsHeatmap | null>(null)
const isLoadingTableCharts = ref(false)

const topUsersData = ref<TopUsersMetrics | null>(null)
const isLoadingWidgets = ref(false)

const toolUsageData = ref<ToolUsageMetrics | null>(null)

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

// Remove mockFailedQueries since we now use real data from RecentQueries component

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

const fetchToolUsageData = async () => {
    isLoadingTableCharts.value = true
    try {
        const params = new URLSearchParams()
        if (dateRange.value.start) {
            params.append('start_date', new Date(dateRange.value.start).toISOString())
        }
        if (dateRange.value.end) {
            params.append('end_date', new Date(dateRange.value.end).toISOString())
        }
        const { data, error } = await useMyFetch<ToolUsageMetrics>(`/api/console/metrics/tool-usage?${params}`, { method: 'GET' })
        if (error.value) {
            console.error('Failed to fetch tool usage data:', error.value)
        } else if (data.value) {
            toolUsageData.value = data.value
        }
    } catch (err) {
        console.error('Error fetching tool usage data:', err)
    } finally {
        isLoadingTableCharts.value = false
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

const refreshData = async () => {
    await Promise.all([
        fetchTimeSeriesData(),
        fetchTableUsageData(),
        fetchTableJoinsData(),
        fetchTopUsers(),
        fetchToolUsageData()
    ])
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
            fetchToolUsageData()
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