<template>
    <div class="bg-white rounded-2xl border border-gray-100 shadow-sm overflow-hidden">
        <div class="p-6 border-b border-gray-50">
            <h3 class="text-lg font-semibold text-gray-900">Recent Negative Feedback</h3>
            <p class="text-sm text-gray-500 mt-1">Latest user complaints - for more go to <nuxt-link to="/console/diagnosis" class="text-blue-600 hover:text-blue-800">diagnosis</nuxt-link> page</p>
        </div>
        <div class="p-0">
            <div v-if="isLoading" class="flex items-center justify-center h-40">
                <div class="flex items-center space-x-2">
                    <div class="animate-spin rounded-full h-6 w-6 border-b-2 border-blue-600"></div>
                    <span class="text-gray-600">Loading feedback...</span>
                </div>
            </div>
            <div v-else class="overflow-hidden">
                <table class="min-w-full">
                    <thead class="bg-gray-50">
                        <tr>
                            <th class="px-6 py-3 text-left text-xs font-medium text-gray-500 uppercase tracking-wider">Description</th>
                            <th class="px-6 py-3 text-left text-xs font-medium text-gray-500 uppercase tracking-wider">Trace</th>
                        </tr>
                    </thead>
                    <tbody class="bg-white divide-y divide-gray-200">
                        <tr v-for="(feedback, index) in negativeFeedbackData?.recent_feedbacks || []" :key="feedback.id" class="hover:bg-gray-50">
                            <td class="px-6 py-4">
                                <div class="text-sm text-gray-900 max-w-xs" :title="feedback.description">
                                    {{ feedback.description.length > 50 ? feedback.description.substring(0, 50) + '...' : feedback.description }}
                                </div>
                                <div class="text-xs text-gray-500 mt-1">
                                    {{ feedback.user_name }} | 
                                    {{ formatDate(feedback.created_at) }}
                                </div>
                            </td>
                            <td class="px-6 py-4 whitespace-nowrap">
                                <button 
                                    @click="openTrace(feedback.completion_id)"
                                    class="text-blue-600 hover:text-blue-800 text-sm font-medium"
                                >
                                    Trace
                                </button>
                            </td>
                        </tr>
                        <tr v-if="!negativeFeedbackData?.recent_feedbacks?.length" class="hover:bg-gray-50">
                            <td colspan="2" class="px-6 py-4 text-center text-gray-500">
                                No negative feedback for this period
                            </td>
                        </tr>
                    </tbody>
                </table>
            </div>
        </div>
    </div>
</template>

<script setup lang="ts">
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

interface DateRange {
    start: string
    end: string
}

interface RecentNegativeFeedbackMetrics {
    recent_feedbacks: RecentNegativeFeedbackData[]
    total_negative_feedbacks: number
    date_range: DateRange
}

interface Props {
    negativeFeedbackData: RecentNegativeFeedbackMetrics | null
    isLoading: boolean
}

defineProps<Props>()

const formatDate = (dateString: string) => {
    return new Date(dateString).toLocaleString()
}

const openTrace = (completionId: string) => {
    // Navigate to completion trace/diagnosis page
    window.open(`/r/${completionId}`, '_blank')
}
</script>