<template>
    <UModal v-model="isOpen" :ui="{ width: 'sm:max-w-6xl'}">
        <UCard>
            <!-- Header -->
            <template #header>
                <div class="flex items-center justify-between">
                    <h3 class="text-lg font-semibold text-gray-900">Agent Execution</h3>
                    <UButton
                        color="gray"
                        variant="ghost"
                        icon="i-heroicons-x-mark-20-solid"
                        @click="closeModal"
                    />
                </div>
                <div class="flex items-start justify-between mt-1">
                    <div class="text-sm text-gray-500">Report ID: {{ reportId }}</div>
                    <!-- Header AI scoring (pastel badges) -->
                    <div
                        v-if="isJudgeEnabled && traceData?.agent_execution && hasAnyCompletionScores(traceData.agent_execution)"
                        class="flex items-center gap-2"
                    >
                        <div class="text-[11px] uppercase tracking-wide text-gray-500 mr-1">AI Scoring</div>
                        <div
                            v-if="traceData.agent_execution.instructions_effectiveness !== null"
                            class="inline-flex items-center px-2 py-1 rounded-full border text-xs bg-blue-50 text-blue-700 border-blue-200"
                        >
                            <span class="mr-1">Instructions</span>
                            <span class="font-semibold">{{ traceData.agent_execution.instructions_effectiveness }}/5</span>
                        </div>
                        <div
                            v-if="traceData.agent_execution.context_effectiveness !== null"
                            class="inline-flex items-center px-2 py-1 rounded-full border text-xs bg-purple-50 text-purple-700 border-purple-200"
                        >
                            <span class="mr-1">Context</span>
                            <span class="font-semibold">{{ traceData.agent_execution.context_effectiveness }}/5</span>
                        </div>
                        <div
                            v-if="traceData.agent_execution.response_score !== null"
                            class="inline-flex items-center px-2 py-1 rounded-full border text-xs bg-green-50 text-green-700 border-green-200"
                        >
                            <span class="mr-1">Response</span>
                            <span class="font-semibold">{{ traceData.agent_execution.response_score }}/5</span>
                        </div>
                    </div>
                </div>
            </template>

            <!-- Content -->
            <div class="h-[500px] flex flex-col">
                <!-- Loading State -->
                <div v-if="isLoading" class="flex-1 flex items-center justify-center">
                    <div class="text-center">
                        <Spinner class="w-8 h-8 mx-auto mb-4 text-gray-400" />
                        <p class="text-sm text-gray-500">Loading execution trace...</p>
                    </div>
                </div>

                <!-- Main Content -->
                <div v-else class="grid grid-cols-5 gap-6 flex-1 min-h-0">
                    <!-- Left Pane: Minimal Block List (2/5 width) -->
                    <div class="col-span-2 border-r border-gray-200 pr-4 flex flex-col min-h-0">
                        <div class="text-xs text-gray-600 mb-2">Execution Blocks</div>
                        <div class="flex-1 min-h-0 overflow-y-auto pr-2">
                            <div v-for="(item, index) in leftItems" :key="item.id" class="mb-2">
                                <div :class="[
                                'px-3 py-2 rounded border cursor-pointer text-xs',
                                selectedItem?.id === item.id ? 'border-blue-500 bg-blue-50' : 'border-gray-200 hover:border-gray-300'
                            ]" @click="selectLeftItem(item)">
                                    <div class="flex items-center justify-between">
                                        <div class="font-medium text-gray-900 truncate">{{ item.title }}</div>
                                        <UIcon :name="getLeftItemIcon(item)" :class="getLeftItemIconClass(item)" />
                                    </div>
                                    <div v-if="item.subtitle" class="text-gray-500 truncate mt-0.5">{{ item.subtitle }}</div>
                                    <div v-if="getItemDurationMs(item) !== null" class="mt-1 flex items-center justify-end text-[10px] text-gray-500">
                                        <UIcon name="i-heroicons-bolt" class="w-3 h-3 mr-1 text-gray-400" />
                                        <span>{{ formatDuration(getItemDurationMs(item) || 0) }}</span>
                                    </div>
                                </div>
                                <!-- Arrow between blocks -->
                                <div v-if="index < leftItems.length - 1" class="flex justify-center my-1">
                                    <UIcon name="i-heroicons-arrow-long-down-20-solid" class="w-5 h-5 text-gray-400" />
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
                            <!-- Block Details (minimal) -->
                            <div class="space-y-4">

                                <!-- User prompt + context (minimal) -->
                                <template v-if="selectedItem.id === 'user_prompt'">
                                    <div class="text-[11px] uppercase tracking-wide text-gray-500 mb-1">User Prompt</div>
                                    <pre class="text-xs text-gray-900 font-sans">{{ traceData?.head_prompt_snippet || '—' }}</pre>
                                    
                                    <div v-if="traceData?.head_context_snapshot" class="mt-4">
                                        <div class="text-[11px] uppercase tracking-wide text-gray-500 mb-2">Context</div>
                                        <ContextBrowser 
                                            :context-data="traceData.head_context_snapshot.context_view_json || {}" 
                                            :build="traceData?.build"
                                        />
                                    </div>
                                </template>

                                <!-- Decision details (minimal) -->
                                <template v-else>
                                    <!-- Feedback details -->
                                    <div v-if="selectedItem.kind === 'feedback'">
                                        <div class="text-[11px] uppercase tracking-wide text-gray-500 mb-1">Feedback</div>
                                        <div class="flex items-center space-x-2 mb-2">
                                            <span class="inline-flex px-2 py-1 text-xs font-medium rounded-full"
                                                  :class="(selectedItem.direction || 0) > 0 ? 'bg-green-100 text-green-800' : (selectedItem.direction || 0) < 0 ? 'bg-red-100 text-red-800' : 'bg-gray-100 text-gray-800'">
                                                {{ (selectedItem.direction || 0) > 0 ? 'Positive' : (selectedItem.direction || 0) < 0 ? 'Negative' : 'Neutral' }}
                                            </span>
                                            <span class="text-xs text-gray-500">{{ formatDate(selectedItem.created_at) }}</span>
                                        </div>
                                        <div v-if="selectedItem.message">
                                            <div class="text-[11px] uppercase tracking-wide text-gray-500 mb-1">Message</div>
                                            <pre class="text-xs text-gray-900 whitespace-pre-wrap font-sans leading-relaxed">{{ selectedItem.message }}</pre>
                                        </div>
                                    </div>
                                    <!-- Non-feedback details -->
                                    <div v-else>
                                        <div v-if="selectedItem.reasoning || selectedItem.plan_decision?.reasoning">
                                            <div class="text-[11px] uppercase tracking-wide text-gray-500 mb-1">Reasoning</div>
                                            <pre class="text-xs text-gray-900 whitespace-pre-wrap font-sans leading-relaxed">{{ selectedItem.reasoning || selectedItem.plan_decision?.reasoning }}</pre>
                                        </div>
                                        <div>
                                            <div class="text-[11px] uppercase tracking-wide text-gray-500 mb-1">Content</div>
                                            <pre class="text-xs text-gray-900 whitespace-pre-wrap font-sans leading-relaxed">{{ selectedItem.content || selectedItem.plan_decision?.assistant || 'No content' }}</pre>
                                        </div>

                                        <!-- Tool execution with specialized rendering -->
                                        <div v-if="selectedItem.tool_execution" class="mt-4">
                                            <div class="text-[11px] uppercase tracking-wide text-gray-500 mb-2">Tool Execution</div>
                                            <!-- Use specialized tool component if available -->
                                            <component 
                                                v-if="shouldUseToolComponent(selectedItem.tool_execution)"
                                                :is="getToolComponent(selectedItem.tool_execution.tool_name)"
                                                :tool-execution="selectedItem.tool_execution"
                                            />
                                            <!-- Fallback to generic tool display -->
                                            <GenericTool 
                                                v-else
                                                :tool-execution="selectedItem.tool_execution"
                                            />
                                        </div>
                                    </div>
                                </template>
                            </div>

                            <!-- Step Details (unused in compact UI) -->
                            <div v-if="false" class="space-y-4">
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

                            <!-- Feedback Details (unused in compact UI) -->
                            <div v-if="false" class="space-y-4">
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
import ContextBrowser from './ContextBrowser.vue'
import GenericTool from '../tools/GenericTool.vue'
import ExecuteCodeTool from '../tools/ExecuteCodeTool.vue'
import CreateWidgetTool from '../tools/CreateWidgetTool.vue'
import CreateDataTool from '../tools/CreateDataTool.vue'
import InspectDataTool from '../tools/InspectDataTool.vue'
import Spinner from '../Spinner.vue'
const { isJudgeEnabled } = useOrgSettings()

interface ToolExecutionUI {
    tool_name: string
    tool_action?: string
    result_json?: any
    duration_ms?: number
    status?: string
}

interface CompletionFeedbackUI {
    id: string
    direction: number
    message?: string
    created_at: string
}

interface InstructionBuild {
    id: string
    build_number: number
    title?: string
    is_main: boolean
    status: string
}

interface CompletionBlockV2 {
    id: string
    completion_id: string
    agent_execution_id?: string
    block_index: number
    title: string
    status: string
    content?: string
    reasoning?: string
    tool_execution?: ToolExecutionUI
    created_at: string
}

interface AgentExecutionTraceResponse {
    agent_execution: any
    completion_blocks: CompletionBlockV2[]
    head_prompt_snippet?: string
    head_context_snapshot?: any
    latest_feedback?: CompletionFeedbackUI | null
    build?: InstructionBuild
}

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
const traceData = ref<AgentExecutionTraceResponse | null>(null)
const selectedItem = ref<any>(null)
const selectedItemType = ref<'block'>('block')
const blocks = computed(() => traceData.value?.completion_blocks || [])

const isOpen = computed({
    get: () => props.modelValue,
    set: (value) => emit('update:modelValue', value)
})

const systemCompletions = computed(() => [])
const leftItems = computed(() => {
    const items: any[] = []
    // 1) User prompt
    if (traceData.value?.head_prompt_snippet) {
        items.push({ id: 'user_prompt', kind: 'prompt', title: 'User Prompt', subtitle: traceData.value.head_prompt_snippet })
    }
    // 2) Decisions (blocks)
    for (const b of blocks.value) {
        const te = (b as any).tool_execution
        const action = te?.tool_action ? te.tool_action : undefined
        const tool_call_name = action ? `${te.tool_name}.${action}` : te?.tool_name
        if (tool_call_name) {
            items.push({ id: b.id, kind: 'decision', title: `Decision: ${tool_call_name}`, subtitle: undefined, ref: b })
        } else {
            // Non-tool decision, show title
            items.push({ id: b.id, kind: 'decision', title: b.title || 'Decision', subtitle: undefined, ref: b })
        }
    }
    // 2b) Latest feedback (if exists)
    if (traceData.value?.latest_feedback) {
        const fb = traceData.value.latest_feedback
        const label = fb.direction > 0 ? 'Positive' : (fb.direction < 0 ? 'Negative' : 'Neutral')
        const subtitle = fb.message ? (fb.message.length > 140 ? fb.message.slice(0, 140) + '…' : fb.message) : undefined
        items.push({ id: 'latest_feedback', kind: 'feedback', title: `Feedback: ${label}`, subtitle, ref: fb })
    }
    // 3) Analysis completed marker (if any block has analysis_complete)
    const hasFinal = blocks.value.some((b: any) => b?.plan_decision?.analysis_complete)
    if (hasFinal) {
        items.push({ id: 'analysis_completed', kind: 'final', title: 'Decision: analysis_completed' })
    }
    return items
})

// Methods
const fetchTraceData = async () => {
    if (!props.reportId || !props.completionId) return
    
    isLoading.value = true
    try {
        const response = await useMyFetch<AgentExecutionTraceResponse>(`/api/console/agent_executions/by-completion/${props.completionId}`)
        
        if (response.error.value) {
            console.error('Error fetching trace data:', response.error.value)
        } else if (response.data.value) {
            traceData.value = response.data.value
            // Always open on the prompt block
            selectedItem.value = { id: 'user_prompt', title: 'User Prompt', content: traceData.value?.head_prompt_snippet, created_at: traceData.value?.agent_execution?.started_at }
            selectedItemType.value = 'block'
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

const selectItem = (item: any) => {
    selectedItem.value = { ...item, id: item.completion_id || item.step_id || item.feedback_id }
}

const selectBlock = (block: any) => {
    selectedItem.value = { ...block, id: block.id }
    selectedItemType.value = 'block'
}

const selectLeftItem = (item: any) => {
    if (item.kind === 'decision' && item.ref) {
        selectBlock(item.ref)
    } else if (item.kind === 'prompt') {
        selectedItem.value = { id: 'user_prompt', title: 'User Prompt', content: traceData.value?.head_prompt_snippet, created_at: traceData.value?.agent_execution?.started_at }
        selectedItemType.value = 'block'
    } else if (item.kind === 'feedback' && item.ref) {
        const fb = item.ref as CompletionFeedbackUI
        selectedItem.value = { id: 'latest_feedback', kind: 'feedback', title: 'Feedback', direction: fb.direction, message: fb.message, created_at: fb.created_at }
        selectedItemType.value = 'block'
    } else if (item.kind === 'final') {
        selectedItem.value = { id: 'analysis_completed', title: 'Analysis Completed', content: 'Analysis marked complete.', created_at: traceData.value?.agent_execution?.completed_at }
        selectedItemType.value = 'block'
    }
}

function getItemDurationMs(item: any): number | null {
    const block = item?.ref || item
    if (!block) return null
    const te = block.tool_execution
    if (te && typeof te.duration_ms === 'number') return te.duration_ms
    if (typeof block.duration_ms === 'number') return block.duration_ms
    return null
}

function formatDuration(ms: number): string {
    if (ms < 1000) return `${Math.round(ms)} ms`
    const seconds = ms / 1000
    if (seconds < 60) return `${seconds < 10 ? seconds.toFixed(1) : Math.round(seconds)} s`
    const minutes = seconds / 60
    return `${minutes.toFixed(1)} m`
}

const getStepsForCompletion = (_completionId: string) => []
const getFeedbackForCompletion = (_completionId: string) => []

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

const getSelectedItemIcon = () => 'i-heroicons-cog-6-tooth'

const getSelectedItemTitle = () => selectedItem.value?.title || 'Block'

const getStatusIcon = (status: string) => {
    if (status === 'error') return 'i-heroicons-x-circle'
    if (status === 'success' || status === 'completed') return 'i-heroicons-check-circle'
    return 'i-heroicons-clock'
}

const getStatusIconClass = (status: string) => {
    if (status === 'error') return 'w-3 h-3 text-red-600'
    if (status === 'success' || status === 'completed') return 'w-3 h-3 text-green-600'
    return 'w-3 h-3 text-gray-500'
}

const getBlockTitle = (block: CompletionBlockV2) => {
    if (block.title) return block.title
    if ((block as any)?.tool_execution) {
        const te = (block as any).tool_execution
        return `${te.tool_name}${te.tool_action ? ' → ' + te.tool_action : ''}`
    }
    return 'Block'
}

const getLeftItemIcon = (item: any) => {
    if (item.kind === 'prompt') return 'i-heroicons-user'
    if (item.kind === 'final') return 'i-heroicons-check-circle'
    if (item.kind === 'feedback') return (item?.ref?.direction || 0) > 0 ? 'i-heroicons-hand-thumb-up' : 'i-heroicons-hand-thumb-down'
    const status = item?.ref?.status
    return getStatusIcon(status || '')
}

const getLeftItemIconClass = (item: any) => {
    if (item.kind === 'prompt') return 'w-3 h-3 text-blue-600'
    if (item.kind === 'final') return 'w-3 h-3 text-green-600'
    if (item.kind === 'feedback') return (item?.ref?.direction || 0) > 0 ? 'w-3 h-3 text-green-600' : 'w-3 h-3 text-red-600'
    const status = item?.ref?.status
    return getStatusIconClass(status || '')
}

const formatDate = (dateString: string) => {
    return new Date(dateString).toLocaleString()
}

const hasAnyScores = (item: any) => {
    return item.instructions_effectiveness || item.context_effectiveness || item.response_score
}

const hasAnyCompletionScores = (completion: any) => {
    return completion.instructions_effectiveness !== null || 
           completion.context_effectiveness !== null || 
           completion.response_score !== null
}

// Tool component helpers (matching index.vue)
function getToolComponent(toolName: string) {
    switch (toolName) {
        case 'create_widget':
            return CreateWidgetTool
        case 'create_data':
            return CreateDataTool
        case 'inspect_data':
            return InspectDataTool
        case 'create_and_execute_code':
        case 'execute_code':
        case 'execute_sql':
            return ExecuteCodeTool
        default:
            return null
    }
}

function shouldUseToolComponent(toolExecution: any): boolean {
    return getToolComponent(toolExecution.tool_name) !== null
}

// Watch for modal opening
watch(() => props.modelValue, (newValue) => {
    if (newValue) {
        fetchTraceData()
    }
})
</script> 