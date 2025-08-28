<template>
    <div class="bg-white rounded-2xl border border-gray-100 shadow-sm overflow-hidden">
        <div class="p-6 border-b border-gray-50">
            <h3 class="text-lg font-semibold text-gray-900">Recently Failed Queries</h3>
            <p class="text-sm text-gray-500 mt-1">Latest query failures - for more go to <nuxt-link to="/console/diagnosis" class="text-blue-600 hover:text-blue-800">diagnosis</nuxt-link> page</p>
        </div>
        <div class="p-0">
            <div v-if="isLoading" class="flex items-center justify-center py-8">
                <div class="flex items-center space-x-2">
                    <div class="animate-spin rounded-full h-4 w-4 border-b-2 border-blue-600"></div>
                    <span class="text-sm text-gray-600">Loading...</span>
                </div>
            </div>
            <div v-else-if="recentQueries.length === 0" class="text-center py-8">
                <UIcon name="i-heroicons-check-circle" class="mx-auto h-8 w-8 text-green-400" />
                <p class="text-sm text-gray-500 mt-2">No recent failures</p>
            </div>
            <div v-else class="overflow-hidden">
                <table class="min-w-full table-fixed">
                    <thead class="bg-gray-50">
                        <tr>
                            <th class="w-3/4 px-6 py-3 text-left text-xs font-medium text-gray-500 uppercase tracking-wider">Content</th>
                            <th class="w-1/4 px-6 py-3 text-left text-xs font-medium text-gray-500 uppercase tracking-wider">Issue Type</th>
                        </tr>
                    </thead>
                    <tbody class="bg-white divide-y divide-gray-200">
                        <tr v-for="query in recentQueries" :key="query.id" class="hover:bg-gray-50 cursor-pointer" @click="openTrace(query)">
                            <td class="w-3/4 px-6 py-4">
                                <div class="text-sm text-gray-900 truncate" :title="getContentText(query)">
                                    {{ getContentText(query) }}
                                </div>
                                <div class="text-xs text-gray-500">
                                    {{ formatDate(query.created_at) }}
                                </div>
                            </td>
                            <td class="w-1/4 px-6 py-4">
                                <div class="flex items-center">
                                    <UIcon 
                                        :name="getIssueIcon(query)"
                                        :class="getIssueIconClass(query)"
                                    />
                                    <span :class="getIssueTypeClass(query)" class="whitespace-nowrap">
                                        {{ getIssueTypeLabel(query) }}
                                    </span>
                                </div>
                            </td>
                        </tr>
                    </tbody>
                </table>
            </div>
        </div>
        
        <!-- Trace Modal -->
        <TraceModal
            v-model="showTraceModal"
            :report-id="selectedQuery?.report_id || ''"
            :completion-id="selectedQuery?.completion_id || ''"
        />
    </div>
</template>

<script setup lang="ts">
import TraceModal from './TraceModal.vue'

// Types
interface AgentExecutionSummaryItem {
    agent_execution_id: string
    created_at: string
    completion_id?: string
    prompt: string
    agent_execution_status: string
    error_json?: any
    total_tools: number
    total_failed_tools: number
    total_successful_tools: number
    feedback_status: string
    feedback_direction: number
    feedback_message?: string
    step_titles: string[]
    user_name: string
    user_email: string
    report_id: string
    report_name: string
    report_link?: string
}

interface AgentExecutionSummariesResponse {
    items: AgentExecutionSummaryItem[]
    total_items: number
    date_range: {
        start: string
        end: string
    }
}

interface Props {
    dateRange?: {
        start: string
        end: string
    }
}

const props = withDefaults(defineProps<Props>(), {
    dateRange: () => ({
        start: '',
        end: ''
    })
})

// State
const isLoading = ref(false)
const recentQueries = ref<AgentExecutionSummaryItem[]>([])
const showTraceModal = ref(false)
const selectedQuery = ref<AgentExecutionSummaryItem | null>(null)

// Methods
const fetchRecentQueries = async () => {
    isLoading.value = true
    try {
        const params = new URLSearchParams({
            page: '1',
            page_size: '5' // Only fetch 5 items
        })
        
        if (props.dateRange.start) {
            params.append('start_date', new Date(props.dateRange.start).toISOString())
        }
        if (props.dateRange.end) {
            params.append('end_date', new Date(props.dateRange.end).toISOString())
        }
        
        const response = await useMyFetch<AgentExecutionSummariesResponse>(`/api/console/agent_executions/summaries?${params}`)
        
        if (response.error.value) {
            console.error('Error fetching recent queries:', response.error.value)
            recentQueries.value = []
        } else if (response.data.value) {
            // Filter to only show executions with issues (errors or negative feedback)
            const itemsWithIssues = response.data.value.items.filter(item => 
                item.agent_execution_status === 'error' || 
                item.feedback_direction < 0 ||
                item.total_failed_tools > 0
            )
            recentQueries.value = itemsWithIssues.slice(0, 5) || []
        }
    } catch (error) {
        console.error('Failed to fetch recent queries:', error)
        recentQueries.value = []
    } finally {
        isLoading.value = false
    }
}

const openTrace = (query: AgentExecutionSummaryItem) => {
    selectedQuery.value = query
    showTraceModal.value = true
}

const getIssueIcon = (query: AgentExecutionSummaryItem) => {
    if (query.agent_execution_status === 'error') {
        return 'i-heroicons-x-circle'
    } else if (query.feedback_direction < 0) {
        return 'i-heroicons-exclamation-triangle'
    } else if (query.total_failed_tools > 0) {
        return 'i-heroicons-x-circle'
    } else {
        return 'i-heroicons-question-mark-circle'
    }
}

const getIssueIconClass = (query: AgentExecutionSummaryItem) => {
    if (query.agent_execution_status === 'error') {
        return 'w-4 h-4 mr-2 text-red-500'
    } else if (query.feedback_direction < 0) {
        return 'w-4 h-4 mr-2 text-yellow-500'
    } else if (query.total_failed_tools > 0) {
        return 'w-4 h-4 mr-2 text-red-500'
    } else {
        return 'w-4 h-4 mr-2 text-gray-500'
    }
}

const getIssueTypeClass = (query: AgentExecutionSummaryItem) => {
    if (query.agent_execution_status === 'error') {
        return 'inline-flex items-center px-2.5 py-0.5 rounded-full text-xs font-medium bg-red-100 text-red-800'
    } else if (query.feedback_direction < 0) {
        return 'inline-flex items-center px-2.5 py-0.5 rounded-full text-xs font-medium bg-yellow-100 text-yellow-800'
    } else if (query.total_failed_tools > 0) {
        return 'inline-flex items-center px-2.5 py-0.5 rounded-full text-xs font-medium bg-red-100 text-red-800'
    } else {
        return 'inline-flex items-center px-2.5 py-0.5 rounded-full text-xs font-medium bg-gray-100 text-gray-800'
    }
}

const getIssueTypeLabel = (query: AgentExecutionSummaryItem) => {
    if (query.agent_execution_status === 'error') {
        return 'Execution Error'
    } else if (query.feedback_direction < 0) {
        return 'Negative Feedback'
    } else if (query.total_failed_tools > 0) {
        return 'Failed Tools'
    } else {
        return 'Unknown'
    }
}

const getContentText = (query: AgentExecutionSummaryItem) => {
    // For negative feedback, show the feedback message if available
    if (query.feedback_direction < 0 && query.feedback_message) {
        return query.feedback_message
    }
    
    // For execution errors, show error message if available
    if (query.agent_execution_status === 'error' && query.error_json?.message) {
        return `Error: ${query.error_json.message}`
    }
    
    // Fallback to prompt
    return query.prompt || 'No content available'
}

const formatDate = (dateString: string) => {
    if (!dateString) return ''
    const date = new Date(dateString)
    return date.toLocaleString()
}

// Watch for dateRange changes
watch(() => props.dateRange, () => {
    fetchRecentQueries()
}, { deep: true })

// Initialize
onMounted(() => {
    fetchRecentQueries()
})
</script> 