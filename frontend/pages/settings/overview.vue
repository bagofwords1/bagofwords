<template>
    <div class="mt-6">
        <!-- Metrics -->
        <div class="grid grid-cols-1 md:grid-cols-5 gap-4 mb-8">
            <div class="text-left p-4 border rounded">
                <div class="text-2xl font-semibold text-gray-900">{{ metrics.total_messages || 0 }}</div>
                <div class="text-sm text-gray-600">Messages</div>
            </div>
            
            <div class="text-left p-4 border rounded">
                <div class="text-2xl font-semibold text-gray-900">{{ metrics.total_queries || 0 }}</div>
                <div class="text-sm text-gray-600">Queries</div>
            </div>
            
            <div class="text-left p-4 border rounded">
                <div class="text-2xl font-semibold text-gray-900">{{ metrics.total_thumbs || 0 }}</div>
                <div class="text-sm text-gray-600">Thumbs</div>
            </div>
        </div>
        
        <!-- Recent Widgets Table -->
        <div class="mt-6">
            <h2 class="text-lg font-medium mb-4">Recent Data</h2>
            <div class="overflow-x-auto">
                <table class="min-w-full divide-y divide-gray-200">
                    <thead class="bg-gray-50">
                        <tr>
                            <th class="px-3 py-3 text-left text-xs font-medium text-gray-500 uppercase tracking-wider w-1/6">Widget</th>
                            <th class="px-3 py-3 text-left text-xs font-medium text-gray-500 uppercase tracking-wider w-1/12">User</th>
                            <th class="px-3 py-3 text-left text-xs font-medium text-gray-500 uppercase tracking-wider w-1/12">Time</th>
                            <th class="px-3 py-3 text-left text-xs font-medium text-gray-500 uppercase tracking-wider w-1/6">Completion ID</th>
                            <th class="px-3 py-3 text-left text-xs font-medium text-gray-500 uppercase tracking-wider w-1/6">Row Count</th>
                            <th class="px-3 py-3 text-left text-xs font-medium text-gray-500 uppercase tracking-wider w-1/12">Revisions</th>
                            <th class="px-3 py-3 text-left text-xs font-medium text-gray-500 uppercase tracking-wider w-1/12">Feedback</th>
                        </tr>
                    </thead>
                    <tbody class="bg-white divide-y divide-gray-200">
                        <tr v-for="widget in recentWidgets" :key="widget.id" class="hover:bg-gray-50">
                            <td class="px-3 py-4 cursor-pointer" @click="openModal(widget)">
                                <div class="text-sm font-medium text-gray-900 truncate" :title="widget.title">
                                    {{ widget.title }}
                                </div>
                            </td>
                            <td class="px-3 py-4 text-sm text-gray-500 truncate cursor-pointer" :title="widget.user_name" @click="openModal(widget)">
                                {{ widget.user_name }}
                            </td>
                            <td class="px-3 py-4 text-sm text-gray-500 cursor-pointer" @click="openModal(widget)">
                                {{ formatDate(widget.created_at) }}
                            </td>
                            <td class="px-3 py-4 text-sm text-gray-500 cursor-pointer" @click="openModal(widget)">
                                <div class="truncate max-w-32">
                                    {{ widget.completion_id || 'N/A' }}
                                </div>
                            </td>
                            <td class="px-3 py-4 text-sm text-gray-500 cursor-pointer" @click="openModal(widget)">
                                {{ widget.row_count || 0 }}
                            </td>
                            <td class="px-3 py-4 text-sm text-gray-500 text-center cursor-pointer" @click="openModal(widget)">
                                {{ widget.steps_count }}
                            </td>
                            <td class="px-3 py-4 text-sm text-gray-500">
                                <div class="flex items-center" @click.stop>
                                    <span class="text-center min-w-[20px]">{{ widget.thumbs_count }}</span>
                                    <UButton
                                        icon="i-heroicons-hand-thumb-up"
                                        color="green"
                                        variant="ghost"
                                        size="xs"
                                        @click="sendFeedback(widget.completion_id, 1)"
                                        :disabled="!widget.completion_id"
                                    />
                                    <UButton
                                        icon="i-heroicons-hand-thumb-down"
                                        color="red"
                                        variant="ghost"
                                        size="xs"
                                        @click="sendFeedback(widget.completion_id, -1)"
                                        :disabled="!widget.completion_id"
                                    />
                                </div>
                            </td>
                        </tr>
                    </tbody>
                </table>
            </div>
        </div>

        <!-- Modal -->
        <UModal v-model="isModalOpen" :ui="{ width: 'w-[95vw] max-w-none' }">
            <UCard v-if="selectedWidget" class="max-h-[95vh] overflow-hidden">
                <template #header>
                    <div class="flex items-center justify-between">
                        <h3 class="text-lg font-semibold">{{ selectedWidget.title }}</h3>
                        <UButton icon="i-heroicons-x-mark" color="gray" variant="ghost" @click="isModalOpen = false" />
                    </div>
                </template>

                <div class="overflow-y-auto max-h-[calc(95vh-120px)]">
                    <!-- Widget Info -->
                    <div class="grid grid-cols-2 md:grid-cols-3 gap-4 mb-6 p-4 bg-gray-50 rounded">
                        <div>
                            <div class="text-sm font-medium text-gray-500">Completion ID</div>
                            <div class="text-sm text-gray-900">{{ selectedWidget.completion_id || 'N/A' }}</div>
                        </div>
                        <div>
                            <div class="text-sm font-medium text-gray-500">User</div>
                            <div class="text-sm text-gray-900">{{ selectedWidget.user_name }}</div>
                        </div>
                        <div>
                            <div class="text-sm font-medium text-gray-500">Date</div>
                            <div class="text-sm text-gray-900">{{ formatDate(selectedWidget.created_at) }}</div>
                        </div>
                        <div>
                            <div class="text-sm font-medium text-gray-500">Widget ID</div>
                            <div class="text-sm text-gray-900">{{ selectedWidget.id }}</div>
                        </div>
                        <div>
                            <div class="text-sm font-medium text-gray-500">Total Steps</div>
                            <div class="text-sm text-gray-900">{{ selectedWidget.steps_count }}</div>
                        </div>
                        <div>
                            <div class="text-sm font-medium text-gray-500">Row Count</div>
                            <div class="text-sm text-gray-900">{{ selectedWidget.row_count || 0 }}</div>
                        </div>
                        <div>
                            <div class="text-sm font-medium text-gray-500">Thumbs</div>
                            <div class="text-sm text-gray-900">{{ selectedWidget.thumbs_count }}</div>
                        </div>
                        <div>
                            <div class="text-sm font-medium text-gray-500">Feedback</div>
                            <div class="flex items-center gap-1">
                                <UButton
                                    icon="i-heroicons-hand-thumb-up"
                                    color="gray"
                                    variant="ghost"
                                    size="xs"
                                    @click="sendFeedback(selectedWidget.completion_id, 1)"
                                    :disabled="!selectedWidget.completion_id"
                                />
                                <UButton
                                    icon="i-heroicons-hand-thumb-down"
                                    color="gray"
                                    variant="ghost"
                                    size="xs"
                                    @click="sendFeedback(selectedWidget.completion_id, -1)"
                                    :disabled="!selectedWidget.completion_id"
                                />
                            </div>
                        </div>
                    </div>

                    <!-- Completion Prompt -->
                    <div v-if="selectedWidget.completion_prompt" class="mb-4">
                        <h4 class="text-sm font-medium text-gray-700 mb-2">Completion Prompt</h4>
                        <div class="p-3 bg-gray-50 rounded border text-sm">
                            {{ selectedWidget.completion_prompt?.content || selectedWidget.completion_prompt }}
                        </div>
                    </div>

                    <!-- Collapsible Code -->
                    <div class="mb-4">
                        <UButton
                            :icon="codeExpanded ? 'i-heroicons-chevron-down' : 'i-heroicons-chevron-right'"
                            color="gray"
                            variant="ghost"
                            @click="codeExpanded = !codeExpanded"
                            class="mb-2"
                        >
                            Code
                        </UButton>
                        <div v-if="codeExpanded" class="p-4 bg-gray-50 rounded border">
                            <pre class="text-sm text-gray-900 whitespace-pre-wrap overflow-x-auto">{{ selectedWidget.code || 'No code available' }}</pre>
                        </div>
                    </div>

                    <!-- Collapsible Data -->
                    <div>
                        <UButton
                            :icon="dataExpanded ? 'i-heroicons-chevron-down' : 'i-heroicons-chevron-right'"
                            color="gray"
                            variant="ghost"
                            @click="toggleData"
                            class="mb-2"
                        >
                            Data Sample
                        </UButton>
                        <div v-if="dataExpanded" class="h-[500px]">
                            <!-- Loading Spinner -->
                            <div v-if="isDataLoading" class="flex items-center justify-center h-full">
                                <div class="flex items-center space-x-2">
                                    <div class="animate-spin rounded-full h-6 w-6 border-b-2 border-blue-600"></div>
                                    <span class="text-gray-600">Loading data...</span>
                                </div>
                            </div>
                            
                            <!-- Data Table -->
                            <RenderTable 
                                v-else-if="parsedStepData"
                                :widget="selectedWidget" 
                                :step="parsedStepData" 
                            />
                            
                            <!-- No Data Message -->
                            <div v-else class="p-4 bg-gray-50 rounded border text-sm text-gray-500 flex items-center justify-center h-full">
                                No data available or data format not supported
                            </div>
                        </div>
                    </div>
                </div>
            </UCard>
        </UModal>
    </div>
</template>

<script setup lang="ts">
import RenderTable from '~/components/RenderTable.vue'

const { organization } = useOrganization()


definePageMeta({ auth: true, permissions: ['modify_settings'], layout: 'settings' })

const metrics = ref({
    total_messages: 0,
    total_queries: 0,
    total_thumbs: 0
})

const recentWidgets = ref([])
const isModalOpen = ref(false)
const selectedWidget = ref(null)
const codeExpanded = ref(false)
const dataExpanded = ref(false)
const isDataLoading = ref(false)

// Format date helper
const formatDate = (dateString: string) => {
    if (!dateString) return ''
    const date = new Date(dateString)
    return date.toLocaleDateString()
}

// Send feedback function
const sendFeedback = async (completionId: string, vote: number) => {
    if (!completionId) {
        const toast = useToast()
        toast.add({
            title: 'Error',
            description: 'No completion ID available for feedback',
            color: 'red',
            timeout: 3000
        })
        return
    }

    try {
        const response = await useMyFetch(`/api/completions/${completionId}/feedback?vote=${vote}`, {
            method: 'POST',
        })

        if (response.status.value !== 'success') throw new Error('Failed to submit feedback')

        const toast = useToast()
        toast.add({
            title: 'Success',
            description: vote > 0 ? 'Successfully upvoted AI response' : 'Successfully downvoted AI response',
            color: 'green',
            timeout: 3000
        })
    } catch (err) {
        const toast = useToast()
        toast.add({
            title: 'Error',
            description: 'Failed to submit feedback',
            color: 'red',
            timeout: 5000,
            icon: 'i-heroicons-exclamation-circle'
        })
    }
}

// Parse data for RenderTable component
const parsedStepData = computed(() => {
    if (!selectedWidget.value?.output_sample) {
        return null
    }
    
    // Check if output_sample is already an object with the right structure
    if (selectedWidget.value.output_sample && 
        typeof selectedWidget.value.output_sample === 'object' &&
        selectedWidget.value.output_sample.columns &&
        selectedWidget.value.output_sample.rows) {
        
        return {
            data: selectedWidget.value.output_sample
        }
    }
    
    // Fallback: try to parse as JSON string
    try {
        const data = JSON.parse(selectedWidget.value.output_sample)
        
        if (data && data.columns && data.rows) {
            return {
                data: data
            }
        }
        
        return null
    } catch (e) {
        console.log('Failed to parse output_sample:', e)
        return null
    }
})

// Open modal with widget data
const openModal = async (widget: any) => {
    selectedWidget.value = widget
    isModalOpen.value = true
    codeExpanded.value = false
    dataExpanded.value = false
    isDataLoading.value = false
}

// Handle data expansion with loading
const toggleData = async () => {
    if (!dataExpanded.value) {
        isDataLoading.value = true
        // Simulate a small delay to show loading state
        await new Promise(resolve => setTimeout(resolve, 100))
    }
    dataExpanded.value = !dataExpanded.value
    isDataLoading.value = false
}

onMounted(async () => {
    try {
        // Fetch metrics
        const { data: metricsData, error: metricsError } = await useMyFetch('/api/organizations/metrics', {
            method: 'GET'
        })
        
        if (metricsError.value) {
            console.error('Failed to fetch metrics:', metricsError.value)
        } else if (metricsData.value) {
            metrics.value = metricsData.value
        }
        
        // Fetch recent widgets
        const { data: widgetsData, error: widgetsError } = await useMyFetch('/api/organizations/recent-widgets?limit=10', {
            method: 'GET'
        })
        
        if (widgetsError.value) {
            console.error('Failed to fetch recent widgets:', widgetsError.value)
        } else if (widgetsData.value) {
            recentWidgets.value = widgetsData.value
        }
    } catch (err) {
        console.error('Error fetching data:', err)
    }
})

definePageMeta({ auth: true, permissions: ['modify_settings'], layout: 'settings' })
</script> 