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
                                        :name="getIssueIcon(query.issue_type)"
                                        :class="getIssueIconClass(query.issue_type)"
                                    />
                                    <span :class="getIssueTypeClass(query.issue_type)" class="whitespace-nowrap">
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
            :completion-id="selectedQuery?.id || ''"
        />
    </div>
</template>

<script setup lang="ts">
import TraceModal from './TraceModal.vue'

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
    failed_steps_count: number
    negative_feedback_count: number
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
const recentQueries = ref<DiagnosisItemData[]>([])
const showTraceModal = ref(false)
const selectedQuery = ref<DiagnosisItemData | null>(null)

// Methods
const fetchRecentQueries = async () => {
    isLoading.value = true
    try {
        const params = new URLSearchParams({
            page: '1',
            page_size: '7' // Only fetch 5 items
        })
        
        if (props.dateRange.start) {
            params.append('start_date', new Date(props.dateRange.start).toISOString())
        }
        if (props.dateRange.end) {
            params.append('end_date', new Date(props.dateRange.end).toISOString())
        }
        
        const response = await useMyFetch<DiagnosisMetrics>(`/api/console/metrics/diagnosis?${params}`)
        
        if (response.error.value) {
            console.error('Error fetching recent queries:', response.error.value)
            recentQueries.value = []
        } else if (response.data.value) {
            // Show failed steps and negative feedback (both are query-related issues)
            recentQueries.value = response.data.value.diagnosis_items.slice(0, 5) || []
        }
    } catch (error) {
        console.error('Failed to fetch recent queries:', error)
        recentQueries.value = []
    } finally {
        isLoading.value = false
    }
}

const openTrace = (query: DiagnosisItemData) => {
    selectedQuery.value = query
    showTraceModal.value = true
}

const getIssueIcon = (issueType: string) => {
    switch (issueType) {
        case 'failed_step':
            return 'i-heroicons-x-circle'
        case 'negative_feedback':
            return 'i-heroicons-exclamation-triangle'
        case 'both':
            return 'i-heroicons-exclamation-triangle'
        default:
            return 'i-heroicons-question-mark-circle'
    }
}

const getIssueIconClass = (issueType: string) => {
    switch (issueType) {
        case 'failed_step':
            return 'w-4 h-4 mr-2 text-red-500'
        case 'negative_feedback':
            return 'w-4 h-4 mr-2 text-yellow-500'
        case 'both':
            return 'w-4 h-4 mr-2 text-purple-500'
        default:
            return 'w-4 h-4 mr-2 text-gray-500'
    }
}

const getIssueTypeClass = (issueType: string) => {
    switch (issueType) {
        case 'failed_step':
            return 'inline-flex items-center px-2.5 py-0.5 rounded-full text-xs font-medium bg-red-100 text-red-800'
        case 'negative_feedback':
            return 'inline-flex items-center px-2.5 py-0.5 rounded-full text-xs font-medium bg-yellow-100 text-yellow-800'
        case 'both':
            return 'inline-flex items-center px-2.5 py-0.5 rounded-full text-xs font-medium bg-purple-100 text-purple-800'
        default:
            return 'inline-flex items-center px-2.5 py-0.5 rounded-full text-xs font-medium bg-gray-100 text-gray-800'
    }
}

const getIssueTypeLabel = (query: DiagnosisItemData) => {
    if (query.issue_type === 'negative_feedback') {
        return 'Negative Feedback'
    } else if (query.step_info?.step_status === 'error') {
        return 'Code Error'
    } else if (query.issue_type === 'failed_step') {
        return 'Failed Query'
    } else if (query.issue_type === 'both') {
        return 'Multiple Issues'
    } else {
        return 'Unknown'
    }
}

const getContentText = (query: DiagnosisItemData) => {
    // For negative feedback, show the feedback message
    if (query.issue_type === 'negative_feedback' && query.feedback_info?.message) {
        return query.feedback_info.message
    }
    
    // For failed steps, show the original prompt
    if (query.issue_type === 'failed_step' || query.issue_type === 'both') {
        return query.head_completion_prompt || 'No prompt available'
    }
    
    // Fallback to prompt
    return query.head_completion_prompt || 'No content available'
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