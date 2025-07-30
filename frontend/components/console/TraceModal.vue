<template>
    <UModal v-model="isOpen" :ui="{ width: 'sm:max-w-6xl'}">
        <UCard>
            <!-- Header -->
            <template #header>
                <div class="flex items-center justify-between">
                    <h3 class="text-lg font-semibold text-gray-900">
                        Trace: {{ traceData?.issue_type?.replace('_', ' ').toUpperCase() || 'Issue' }}
                    </h3>
                    <UButton
                        color="gray"
                        variant="ghost"
                        icon="i-heroicons-x-mark-20-solid"
                        @click="closeModal"
                    />
                </div>
                <div class="text-sm text-gray-500 mt-1">
                    Report ID: {{ reportId }} • User: {{ traceData?.user_name }}
                </div>
            </template>

            <!-- Content -->
            <div class="h-[500px] flex flex-col">
                <div class="grid grid-cols-5 gap-6 flex-1 min-h-0">
                    <!-- Left Pane: Tree View (2/5 width) -->
                    <div class="col-span-2 border-r border-gray-200 pr-4 flex flex-col min-h-0">
                        <h4 class="text-sm font-medium text-gray-900 mb-3 flex-shrink-0">Execution Flow</h4>
                        
                        <div class="flex-1 min-h-0 overflow-y-auto pr-2">
                            <div class="space-y-2">
                                <!-- Head Completion -->
                                <div v-if="traceData?.head_completion" class="relative">
                                    <div :class="[
                                        'p-3 rounded-lg border-2 cursor-pointer transition-colors',
                                        selectedItem?.id === traceData.head_completion.completion_id 
                                            ? 'border-blue-500 bg-blue-50' 
                                            : 'border-gray-200 hover:border-gray-300'
                                    ]" @click="selectItem(traceData.head_completion, 'completion')">
                                        <div class="flex items-center">
                                            <UIcon name="i-heroicons-user" class="w-4 h-4 text-blue-600 mr-2" />
                                            <div>
                                                <div class="text-xs font-medium text-gray-900">User Prompt</div>
                                                <div class="text-xs text-gray-600 truncate mt-1">
                                                    {{ traceData.head_completion.content?.substring(0, 60) }}...
                                                </div>
                                            </div>
                                        </div>
                                    </div>
                                </div>

                                <!-- Completions and Steps -->
                                <div v-for="completion in systemCompletions" :key="completion.completion_id" class="relative ml-4">
                                    <!-- Completion -->
                                    <div :class="[
                                        'p-3 rounded-lg border-2 cursor-pointer transition-colors mb-2',
                                        completion.has_issue 
                                            ? 'border-red-500 bg-red-50' 
                                            : selectedItem?.id === completion.completion_id 
                                                ? 'border-blue-500 bg-blue-50' 
                                                : 'border-gray-200 hover:border-gray-300'
                                    ]" @click="selectItem(completion, 'completion')">
                                        <div class="flex items-center">
                                            <UIcon :name="getCompletionIcon(completion)" 
                                                   :class="getCompletionIconClass(completion)" />
                                            <div class="ml-2 flex-1">
                                                <div class="text-xs font-medium text-gray-900">
                                                    {{ getCompletionLabel(completion) }}
                                                </div>
                                                <div class="text-xs text-gray-600 truncate mt-1">
                                                    {{ completion.content?.substring(0, 50) }}...
                                                </div>
                                                <div v-if="completion.has_issue" class="text-xs text-red-600 mt-1 font-medium">
                                                    {{ getIssueLabel(completion.issue_type) }}
                                                </div>
                                            </div>
                                        </div>
                                    </div>

                                    <!-- Related Steps -->
                                    <div v-for="step in getStepsForCompletion(completion.completion_id)" 
                                         :key="step.step_id" 
                                         class="ml-6 mb-2">
                                        <div :class="[
                                            'p-2 rounded border cursor-pointer transition-colors',
                                            step.has_issue 
                                                ? 'border-red-400 bg-red-25' 
                                                : selectedItem?.id === step.step_id 
                                                    ? 'border-blue-400 bg-blue-25' 
                                                    : 'border-gray-300 hover:border-gray-400'
                                        ]" @click="selectItem(step, 'step')">
                                            <div class="flex items-center">
                                                <UIcon :name="getStepIcon(step)" 
                                                       :class="getStepIconClass(step)" />
                                                <div class="ml-2">
                                                    <div class="text-xs font-medium text-gray-900">{{ step.title }}</div>
                                                    <div class="text-xs text-gray-500">Status: {{ step.status }}</div>
                                                </div>
                                            </div>
                                        </div>
                                    </div>

                                    <!-- Related Feedback -->
                                    <div v-for="feedback in getFeedbackForCompletion(completion.completion_id)" 
                                         :key="feedback.feedback_id" 
                                         class="ml-6 mb-2">
                                        <div :class="[
                                            'p-2 rounded border cursor-pointer transition-colors',
                                            feedback.direction === -1 
                                                ? 'border-orange-400 bg-orange-25' 
                                                : 'border-green-400 bg-green-25'
                                        ]" @click="selectItem(feedback, 'feedback')">
                                            <div class="flex items-center">
                                                <UIcon :name="feedback.direction === 1 ? 'i-heroicons-hand-thumb-up' : 'i-heroicons-hand-thumb-down'" 
                                                       :class="feedback.direction === 1 ? 'w-3 h-3 text-green-600' : 'w-3 h-3 text-orange-600'" />
                                                <div class="ml-2">
                                                    <div class="text-xs font-medium text-gray-900">
                                                        {{ feedback.direction === 1 ? 'Positive' : 'Negative' }} Feedback
                                                    </div>
                                                    <div class="text-xs text-gray-500 truncate">
                                                        {{ feedback.message || 'No message' }}
                                                    </div>
                                                </div>
                                            </div>
                                        </div>
                                    </div>
                                </div>
                            </div>
                        </div>
                    </div>

                    <!-- Right Pane: Details (3/5 width) -->
                    <div class="col-span-3 flex flex-col min-h-0">
                        <div v-if="!selectedItem" class="flex items-center justify-center h-full text-gray-500">
                            <div class="text-center">
                                <UIcon name="i-heroicons-cursor-arrow-rays" class="w-12 h-12 mx-auto mb-4 text-gray-400" />
                                <p class="text-xs">Select an item from the tree to view details</p>
                            </div>
                        </div>

                        <div v-else class="flex-1 min-h-0 overflow-y-auto pr-2">
                            <!-- Item Header -->
                            <div class="mb-4 flex-shrink-0">
                                <div class="flex items-center mb-2">
                                    <UIcon :name="getSelectedItemIcon()" class="w-4 h-4 mr-2 text-gray-600" />
                                    <h4 class="text-sm font-medium text-gray-900">{{ getSelectedItemTitle() }}</h4>
                                </div>
                                <div class="text-xs text-gray-500">
                                    {{ formatDate(selectedItem.created_at) }}
                                </div>
                            </div>

                            <!-- Completion Details -->
                            <div v-if="selectedItemType === 'completion'" class="space-y-4">
                                <div>
                                    <label class="block text-xs font-medium text-gray-700 mb-2">Content</label>
                                    <div class="p-3 bg-gray-50 rounded-lg border">
                                        <pre class="text-xs text-gray-900 whitespace-pre-wrap">{{ selectedItem.content || 'No content' }}</pre>
                                    </div>
                                </div>

                                <div v-if="selectedItem.reasoning">
                                    <label class="block text-xs font-medium text-gray-700 mb-2">Reasoning</label>
                                    <div class="p-3 bg-blue-50 rounded-lg border border-blue-200">
                                        <pre class="text-xs text-gray-900 whitespace-pre-wrap">{{ selectedItem.reasoning }}</pre>
                                    </div>
                                </div>

                                <!-- AI Scoring Section -->
                                <div v-if="selectedItem.role === 'user' && hasAnyScores(selectedItem)" class="bg-purple-50 border border-purple-200 rounded-lg p-3">
                                    <label class="block text-xs font-medium text-purple-800 mb-3">AI Quality Scores (1-5 scale)</label>
                                    <div class="grid grid-cols-3 gap-3">
                                        <div v-if="selectedItem.instructions_effectiveness" class="text-center">
                                            <div class="text-lg font-bold text-purple-700">{{ selectedItem.instructions_effectiveness }}</div>
                                            <div class="text-xs text-purple-600">Instructions</div>
                                        </div>
                                        <div v-if="selectedItem.context_effectiveness" class="text-center">
                                            <div class="text-lg font-bold text-purple-700">{{ selectedItem.context_effectiveness }}</div>
                                            <div class="text-xs text-purple-600">Context</div>
                                        </div>
                                        <div v-if="selectedItem.response_score" class="text-center">
                                            <div class="text-lg font-bold text-purple-700">{{ selectedItem.response_score }}</div>
                                            <div class="text-xs text-purple-600">Response</div>
                                        </div>
                                    </div>
                                </div>

                                <div class="grid grid-cols-2 gap-4">
                                    <div>
                                        <label class="block text-xs font-medium text-gray-700 mb-1">Role</label>
                                        <span class="inline-flex px-2 py-1 text-xs font-medium rounded-full bg-blue-100 text-blue-800">
                                            {{ selectedItem.role }}
                                        </span>
                                    </div>
                                    <div>
                                        <label class="block text-xs font-medium text-gray-700 mb-1">Status</label>
                                        <span :class="[
                                            'inline-flex px-2 py-1 text-xs font-medium rounded-full',
                                            selectedItem.status === 'success' ? 'bg-green-100 text-green-800' : 'bg-gray-100 text-gray-800'
                                        ]">
                                            {{ selectedItem.status || 'N/A' }}
                                        </span>
                                    </div>
                                </div>
                            </div>

                            <!-- Step Details -->
                            <div v-else-if="selectedItemType === 'step'" class="space-y-4">
                                <div class="grid grid-cols-2 gap-4">
                                    <div>
                                        <label class="block text-xs font-medium text-gray-700 mb-1">Title</label>
                                        <p class="text-xs text-gray-900">{{ selectedItem.title }}</p>
                                    </div>
                                    <div>
                                        <label class="block text-xs font-medium text-gray-700 mb-1">Status</label>
                                        <span :class="[
                                            'inline-flex px-2 py-1 text-xs font-medium rounded-full',
                                            selectedItem.status === 'success' ? 'bg-green-100 text-green-800' :
                                            selectedItem.status === 'error' ? 'bg-red-100 text-red-800' :
                                            'bg-gray-100 text-gray-800'
                                        ]">
                                            {{ selectedItem.status }}
                                        </span>
                                    </div>
                                </div>

                                <div v-if="selectedItem.data_model">
                                    <label class="block text-xs font-medium text-gray-700 mb-2">Data Model</label>
                                    <div class="p-3 bg-gray-50 rounded-lg border max-h-32 overflow-y-auto">
                                        <pre class="text-xs text-gray-900">{{ JSON.stringify(selectedItem.data_model, null, 2) }}</pre>
                                    </div>
                                </div>

                                <div v-if="selectedItem.code">
                                    <label class="block text-xs font-medium text-gray-700 mb-2">Generated Code</label>
                                    <div class="p-3 bg-gray-900 rounded-lg max-h-40 overflow-y-auto">
                                        <pre class="text-xs text-green-400 font-mono">{{ selectedItem.code }}</pre>
                                    </div>
                                </div>

                                <div v-if="selectedItem.data">
                                    <label class="block text-xs font-medium text-gray-700 mb-2">Data Output</label>
                                    <div class="border rounded-lg bg-white h-48">
                                        <RenderTable 
                                            v-if="selectedItem.data?.columns" 
                                            :widget="{ id: 'trace-widget' }" 
                                            :step="selectedItem" 
                                        />
                                        <div v-else class="p-3 bg-gray-50 rounded-lg border h-full overflow-y-auto">
                                            <pre class="text-xs text-gray-900">{{ JSON.stringify(selectedItem.data, null, 2) }}</pre>
                                        </div>
                                    </div>
                                </div>
                            </div>

                            <!-- Feedback Details -->
                            <div v-else-if="selectedItemType === 'feedback'" class="space-y-4">
                                <div class="grid grid-cols-2 gap-4">
                                    <div>
                                        <label class="block text-xs font-medium text-gray-700 mb-1">Direction</label>
                                        <span :class="[
                                            'inline-flex px-2 py-1 text-xs font-medium rounded-full',
                                            selectedItem.direction === 1 ? 'bg-green-100 text-green-800' : 'bg-red-100 text-red-800'
                                        ]">
                                            {{ selectedItem.direction === 1 ? 'Positive' : 'Negative' }}
                                        </span>
                                    </div>
                                    <div>
                                        <label class="block text-xs font-medium text-gray-700 mb-1">Feedback ID</label>
                                        <p class="text-xs text-gray-900">{{ selectedItem.feedback_id }}</p>
                                    </div>
                                </div>

                                <div v-if="selectedItem.message">
                                    <label class="block text-xs font-medium text-gray-700 mb-2">Message</label>
                                    <div class="p-3 bg-gray-50 rounded-lg border">
                                        <p class="text-xs text-gray-900">{{ selectedItem.message }}</p>
                                    </div>
                                </div>
                            </div>
                        </div>
                    </div>
                </div>
            </div>
        </UCard>
    </UModal>
</template>

<script setup lang="ts">
import RenderTable from '../RenderTable.vue'

interface TraceCompletionData {
    completion_id: string
    role: string
    content?: string
    reasoning?: string
    created_at: string
    status?: string
    has_issue: boolean
    issue_type?: string
    instructions_effectiveness?: number
    context_effectiveness?: number
    response_score?: number
}

interface TraceStepData {
    step_id: string
    title: string
    status: string
    code?: string
    data_model?: any
    data?: any
    created_at: string
    completion_id: string
    has_issue: boolean
}

interface TraceFeedbackData {
    feedback_id: string
    direction: number
    message?: string
    created_at: string
    completion_id: string
}

interface TraceData {
    report_id: string
    head_completion: TraceCompletionData
    completions: TraceCompletionData[]
    steps: TraceStepData[]
    feedbacks: TraceFeedbackData[]
    issue_completion_id: string
    issue_type: string
    user_name: string
    user_email?: string
}

interface Props {
    modelValue: boolean
    reportId: string
    completionId: string
}

const props = defineProps<Props>()
const emit = defineEmits<{
    'update:modelValue': [value: boolean]
}>()

// State
const isLoading = ref(false)
const traceData = ref<TraceData | null>(null)
const selectedItem = ref<any>(null)
const selectedItemType = ref<'completion' | 'step' | 'feedback'>('completion')

const isOpen = computed({
    get: () => props.modelValue,
    set: (value) => emit('update:modelValue', value)
})

const systemCompletions = computed(() => {
    return traceData.value?.completions.filter(c => c.role !== 'user') || []
})

// Methods
const fetchTraceData = async () => {
    if (!props.reportId || !props.completionId) return
    
    isLoading.value = true
    try {
        const response = await useMyFetch<TraceData>(`/api/console/trace/${props.reportId}/${props.completionId}`)
        
        if (response.error.value) {
            console.error('Error fetching trace data:', response.error.value)
        } else if (response.data.value) {
            traceData.value = response.data.value
            // Auto-select the issue completion
            const issueCompletion = traceData.value.completions.find(c => c.completion_id === props.completionId)
            if (issueCompletion) {
                selectItem(issueCompletion, 'completion')
            }
        }
    } catch (error) {
        console.error('Failed to fetch trace data:', error)
    } finally {
        isLoading.value = false
    }
}

const closeModal = () => {
    emit('update:modelValue', false)
    selectedItem.value = null
    traceData.value = null
}

const selectItem = (item: any, type: 'completion' | 'step' | 'feedback') => {
    selectedItem.value = { ...item, id: item.completion_id || item.step_id || item.feedback_id }
    selectedItemType.value = type
}

const getStepsForCompletion = (completionId: string) => {
    return traceData.value?.steps.filter(s => s.completion_id === completionId) || []
}

const getFeedbackForCompletion = (completionId: string) => {
    return traceData.value?.feedbacks.filter(f => f.completion_id === completionId) || []
}

const getCompletionIcon = (completion: TraceCompletionData) => {
    if (completion.has_issue) return 'i-heroicons-exclamation-triangle'
    return completion.role === 'user' ? 'i-heroicons-user' : 'i-heroicons-cpu-chip'
}

const getCompletionIconClass = (completion: TraceCompletionData) => {
    if (completion.has_issue) return 'w-4 h-4 text-red-600 mr-2'
    return completion.role === 'user' ? 'w-4 h-4 text-blue-600 mr-2' : 'w-4 h-4 text-gray-600 mr-2'
}

const getCompletionLabel = (completion: TraceCompletionData) => {
    if (completion.role === 'user') return 'User Input'
    return 'System Response'
}

const getStepIcon = (step: TraceStepData) => {
    if (step.has_issue) return 'i-heroicons-x-circle'
    return step.status === 'success' ? 'i-heroicons-check-circle' : 'i-heroicons-clock'
}

const getStepIconClass = (step: TraceStepData) => {
    if (step.has_issue) return 'w-3 h-3 text-red-600'
    return step.status === 'success' ? 'w-3 h-3 text-green-600' : 'w-3 h-3 text-yellow-600'
}

const getIssueLabel = (issueType?: string) => {
    switch (issueType) {
        case 'failed_step': return 'Failed Step'
        case 'negative_feedback': return 'Negative Feedback'
        case 'both': return 'Multiple Issues'
        default: return 'Issue'
    }
}

const getSelectedItemIcon = () => {
    if (selectedItemType.value === 'completion') return 'i-heroicons-chat-bubble-left-right'
    if (selectedItemType.value === 'step') return 'i-heroicons-cog-6-tooth'
    return 'i-heroicons-hand-thumb-up'
}

const getSelectedItemTitle = () => {
    if (!selectedItem.value) return ''
    
    if (selectedItemType.value === 'completion') {
        return selectedItem.value.role === 'user' ? 'User Prompt' : 'System Response'
    }
    if (selectedItemType.value === 'step') {
        return `Step: ${selectedItem.value.title}`
    }
    return `${selectedItem.value.direction === 1 ? 'Positive' : 'Negative'} Feedback`
}

const formatDate = (dateString: string) => {
    return new Date(dateString).toLocaleString()
}

const hasAnyScores = (item: any) => {
    return item.instructions_effectiveness || item.context_effectiveness || item.response_score
}

// Watch for modal opening
watch(() => props.modelValue, (newValue) => {
    if (newValue) {
        fetchTraceData()
    }
})
</script> 