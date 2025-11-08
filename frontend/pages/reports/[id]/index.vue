<template>

	<!-- Loading until report is fetched (allow streaming UI to render while report loads) -->
	<div v-if="!reportLoaded && messages.length === 0" class="h-screen w-full flex items-center justify-center text-gray-500">
		<Spinner class="w-5 h-5 mr-2" />
		<span class="text-sm">Loading report…</span>
	</div>

	<SplitScreenLayout v-else
		:isSplitScreen="isSplitScreen" 
		:leftPanelWidth="leftPanelWidth"
		:isResizing="isResizing"
		@startResize="startResize"
	>
		<template #left>
	<div class="flex flex-col h-screen overflow-y-hidden bg-white relative">
		<ReportHeader 
			v-if="report"
			:report="report"
			:isSplitScreen="isSplitScreen"
			:isStreaming="isStreaming"
			@toggleSplitScreen="toggleSplitScreen"
			@rerun="rerunReport"
			@stop="abortStream"
		/>

		<!-- Messages -->
		<div class="flex-1 overflow-y-auto mt-4 pb-4" ref="scrollContainer">
			<div class="pl-4 pr-2 pb-[3px]" :class="isSplitScreen ? 'w-full' : 'md:w-1/2 w-full mx-auto'">
				<ul v-if="messages.length > 0" class="mx-auto w-full">
					<!-- Top loader for older pages -->
					<li v-if="hasMore && isLoadingMore" class="text-gray-500 mb-2 text-xs text-center">
						<Spinner class="w-4 h-4 inline mr-2" /> Loading older messages…
					</li>
					<li v-for="m in messages" :key="m.id" class="text-gray-700 mb-2 text-sm">
						<div class="flex rounded-lg p-1">
							<div class="w-[28px] mr-2">
								<div v-if="m.role === 'user'" class="h-7 w-7 uppercase flex items-center justify-center text-xs border border-blue-200 bg-blue-100 rounded-full inline-block">
									{{ report.user.name.charAt(0) }}
								</div>
								<div v-else class="h-7 w-7 flex font-bold items-center justify-center text-xs rounded-lg inline-block bg-contain bg-center bg-no-repeat" style="background-image: url('/assets/logo-128.png')">
								</div>
							</div>
							<div class="w-full ml-4">
								<!-- User message -->
								<div v-if="m.role === 'user' && m.prompt?.content" class="pt-1 markdown-wrapper">
									<MDC :value="m.prompt.content" class="markdown-content" />
								</div>

							<!-- System message -->
							<div v-else-if="m.role === 'system'">
								<!-- Render each completion block -->
									<div v-for="(block, blockIndex) in m.completion_blocks" :key="block.id">
										<!-- Research blocks: put reasoning, tool execution, and assistant in thinking toggle -->
										<div v-if="isResearchBlock(block)">
											<!-- Thinking toggle for research blocks -->
											<div v-if="block.plan_decision?.reasoning || block.reasoning || block.tool_execution || block.status === 'stopped'">
												<div class="flex justify-between items-center cursor-pointer" @click="toggleReasoning(block.id)">
													<div class="font-normal text-sm text-gray-400  mb-2">
														<div class="flex items-center">
															<Icon :name="isReasoningCollapsed(block.id) ? 'heroicons-chevron-right' : 'heroicons-chevron-down'" class="w-4 h-4 text-gray-400" />
														<span v-if="hasCompletedContent(block)" class="ml-1 font-normal">
															{{ getThoughtProcessLabel(block) }}
														</span>
														<span v-else class="ml-1">
															<div class="dots" />
														</span>
														</div>
													</div>
												</div>
												<Transition name="fade">
													<div v-if="!isReasoningCollapsed(block.id)" class="text-sm mt-2 leading-relaxed text-gray-500 mb-2 reasoning-content">
														<!-- Reasoning -->
														<div v-if="block.plan_decision?.reasoning || block.reasoning" class="markdown-wrapper mb-2">
															<template v-if="isBlockFinalized(block)">
																<MDC :value="block.plan_decision?.reasoning || block.reasoning || ''" class="markdown-content" />
															</template>
															<template v-else>
																<div class="streaming-text">{{ block.plan_decision?.reasoning || block.reasoning || '' }}</div>
															</template>
														</div>
														
														<!-- Fallback for stopped blocks with no reasoning -->
														<div v-else-if="block.status === 'stopped'" class="text-gray-400 italic mb-2">
															Generation was stopped before completion.
														</div>
														
														<!-- Tool execution details in thinking -->
														<div v-if="block.tool_execution" class="mb-4">
														<!-- Use specialized tool component if available -->
														<component 
															v-if="shouldUseToolComponent(block.tool_execution)"
															:is="getToolComponent(block.tool_execution.tool_name)"
															:key="`${block.id}:${(block.tool_execution && block.tool_execution.id) ? block.tool_execution.id : 'noid'}`"
															:tool-execution="block.tool_execution"
															@addWidget="handleAddWidgetFromPreview"
															@refreshDashboard="refreshDashboardFast"
															@toggleSplitScreen="toggleSplitScreen"
															@editQuery="handleEditQuery"
														/>


														<!-- Fallback to generic tool display -->
														<div v-else>
															<div class="text-xs text-gray-600 mb-1 font-medium" v-if="block.tool_execution.tool_name !== 'clarify' && block.tool_execution.tool_name !== 'answer_question' && block.tool_execution.tool_name !== 'suggest_instructions'">
																{{ block.tool_execution.tool_name }}{{ block.tool_execution.tool_action ? ` → ${block.tool_execution.tool_action}` : '' }} ({{ block.tool_execution.status }})
															</div>
															<div class="text-xs text-gray-500 bg-gray-50 p-2 rounded">
																<div v-if="block.tool_execution.result_summary">{{ block.tool_execution.result_summary }}</div>
																<div v-if="block.tool_execution.duration_ms">Duration: {{ block.tool_execution.duration_ms }}ms</div>
																<div v-if="block.tool_execution.created_widget_id" class="text-green-600">→ Widget: {{ block.tool_execution.created_widget_id }}</div>
																<div v-if="block.tool_execution.created_step_id" class="text-purple-600">→ Step: {{ block.tool_execution.created_step_id }}</div>
															</div>
														</div>
													</div>
													
													<!-- Assistant message in thinking for research -->
													<div v-if="block.content" class="markdown-wrapper">
														<MDC :key="block.id + ':' + (block.content?.length || 0)" :value="block.content || ''" class="markdown-content" />
													</div>
												</div>
											</Transition>
										</div>
									</div>
									
									<!-- Action blocks: render like before -->
									<div v-else>
										<!-- Block reasoning section -->
										<div v-if="block.plan_decision?.reasoning || block.reasoning || block.status === 'stopped'">
											<div class="flex justify-between items-center cursor-pointer" @click="toggleReasoning(block.id)">
												<div class="font-normal text-sm text-gray-500 mb-2">
													<div class="flex items-center">
														<Icon :name="isReasoningCollapsed(block.id) ? 'heroicons-chevron-right' : 'heroicons-chevron-down'" class="w-4 h-4 text-gray-500" />
														<span v-if="hasCompletedContent(block)" class="ml-1">
															{{ getThoughtProcessLabel(block) }}
														</span>
														<span v-else class="ml-1">
															<div class="dots" />
														</span>
													</div>
												</div>
										</div>
										<Transition name="fade">
											<div v-if="!isReasoningCollapsed(block.id)" class="text-sm mt-2 leading-relaxed text-gray-500 mb-2 reasoning-content markdown-wrapper">
												<template v-if="block.plan_decision?.reasoning || block.reasoning">
													<MDC :key="block.id + ':' + (block.content?.length || 0)" :value="block.plan_decision?.reasoning || block.reasoning || ''" class="markdown-content" />
												</template>
												<template v-else-if="block.status === 'stopped'">
													<div class="text-gray-400 italic">Generation was stopped before completion.</div>
												</template>
											</div>
										</Transition>
									</div>
									<!-- Block content -->
									<div v-if="block.content && !block.plan_decision?.final_answer && block.status !== 'error'" class="markdown-wrapper">
										<template v-if="isBlockFinalized(block)">
											<MDC :value="block.content || ''" class="markdown-content" />
										</template>
										<template v-else>
											<div class="streaming-text">{{ block.content || '' }}</div>
										</template>
									</div>
									
									<!-- Final answer (if this is the last block and analysis is complete) -->
									<div v-else-if="block.plan_decision?.final_answer && block.plan_decision?.analysis_complete" class="mt-2 markdown-wrapper">
										<MDC :value="block.plan_decision?.final_answer || ''" class="markdown-content" />
									</div>
									
									<!-- Tool execution details -->
									<div v-if="block.tool_execution" class="mt-3 mb-4">
										<!-- Use specialized tool component if available -->
										<component 
											v-if="shouldUseToolComponent(block.tool_execution)"
											:is="getToolComponent(block.tool_execution.tool_name)"
											:key="`${block.id}:${(block.tool_execution && block.tool_execution.id) ? block.tool_execution.id : 'noid'}`"
											:tool-execution="block.tool_execution"
											@addWidget="handleAddWidgetFromPreview"
											@toggleSplitScreen="toggleSplitScreen"
											@editQuery="handleEditQuery"
										/>
										<!-- Fallback to generic expandable tool display -->
										<div v-else>
											<div class="text-xs text-gray-500 mb-1">
												<span class="cursor-pointer hover:text-gray-700" @click="toggleToolDetails(block.tool_execution.id)" v-if="block.tool_execution.tool_name !== 'clarify' && block.tool_execution.tool_name !== 'answer_question' && block.tool_execution.tool_name !== 'suggest_instructions'">
													{{ block.tool_execution.tool_name }}{{ block.tool_execution.tool_action ? ` → ${block.tool_execution.tool_action}` : '' }} ({{ block.tool_execution.status }})
												</span>
												<div v-if="isToolDetailsExpanded(block.tool_execution.id)" class="ml-2 mt-1 text-xs text-gray-400 bg-gray-50 p-2 rounded">
													<div v-if="block.tool_execution.result_summary">{{ block.tool_execution.result_summary }}</div>
													<div v-if="block.tool_execution.duration_ms">Duration: {{ block.tool_execution.duration_ms }}ms</div>
													<div v-if="block.tool_execution.created_widget_id" class="text-green-600">→ Widget: {{ block.tool_execution.created_widget_id }}</div>
													<div v-if="block.tool_execution.created_step_id" class="text-purple-600">→ Step: {{ block.tool_execution.created_step_id }}</div>
												</div>
											</div>
										</div>
									</div>
									<div class="mt-1" v-if="shouldShowToolWidgetPreview(block.tool_execution) && block.tool_execution">
										<ToolWidgetPreview :tool-execution="block.tool_execution" @addWidget="handleAddWidgetFromPreview" @toggleSplitScreen="toggleSplitScreen" @editQuery="handleEditQuery" />
									</div>
								</div>
							
							<!-- Thinking dots when system is working but no visible progress - moved to end -->
							<div v-if="shouldShowWorkingDots(m)" class="mt-2">
								<div class="simple-dots"></div>
							</div>
							</div>
							
							<!-- Instruction Suggestions (outside blocks) -->
							<div v-if="m.instruction_suggestions && m.instruction_suggestions.length > 0" class="mt-4 mb-4">
								<InstructionSuggestions 
									:tool-execution="{ 
										id: `suggestions-${m.id}`, 
										tool_name: 'suggest_instructions',
										status: 'success',
										result_json: { drafts: m.instruction_suggestions }
									}"
								/>
							</div>
							
							<!-- Show status messages for stopped/error completions -->
							<div class="mt-2" v-if="isRealCompletion(m) && m.status === 'success'">
								<div class="flex items-center space-x-2">
									<CompletionItemFeedback :completion="{ id: (m.system_completion_id || m.id) }" :feedbackScore="m.feedback_score || 0" />
									
									<!-- Debug button -->
									<button
										v-if="canViewConsole"
										@click="openTraceModal(m.system_completion_id || m.id)"
										class="flex items-center justify-center w-6 h-6 hover:bg-gray-50 rounded-md transition-colors group"
										:title="'View Agent Trace'"
									>
										<Icon name="heroicons-bug-ant" class="w-4 h-4 text-gray-500 group-hover:text-gray-900" />
									</button>
								</div>
							</div>
							<div v-if="m.status === 'stopped'" class="text-xs text-gray-500 mt-2 italic">
								<Icon name="heroicons-stop-circle" class="w-4 h-4 inline mr-1" />
								Generation stopped
							</div>
							<div v-else-if="m.status === 'error'" class="text-xs text-gray-500">
								<Icon name="heroicons-x-mark" class="w-4 h-4 inline mr-1 text-red-500" />
								<span v-if="getMessageError(m)" class="pre-wrap">
									<Icon name="heroicons-x-mark" class="w-4 h-4 inline mr-1 text-red-500" />
									{{ getMessageError(m) }}</span>
								<span v-else class="italic">An error occurred</span>
							</div>
						</div>
					</div>
					</div>
				</li>
			</ul>
			<div v-else class="w-full mt-32 fade-in" :class="isSplitScreen ? 'w-full' : 'md:w-1/2'">
				<h1 class="text-4xl mb-4">🪴</h1>
				<h1 class="text-lg font-semibold">Ask a question to get started.</h1>

				<hr class="my-4">
				<p class="text-gray-500 text-sm"><span class="font-semibold">Tip:</span> <br />
					Not sure what to ask? You can ask the AI Analyst to suggest questions about a specific topic.
				</p>

			</div>
			</div>
		</div>

		<!-- Minimal reconnect banner while polling after refresh (bottom, above prompt) -->
		<div v-if="isPolling" class="mx-auto px-4 mt-2 mb-2" :class="isSplitScreen ? 'w-full' : 'md:w-1/2 w-full'">
			<div class="text-xs text-gray-500 flex items-center">
				<Spinner class="w-3 h-3 mr-2 text-gray-400" />
				<span class="poll-shimmer">Loading… showing recent progress</span>
			</div>
		</div>

		<!-- Prompt box (in normal flow at the bottom of the left column) -->
		<div class="shrink-0 bg-white">
			<div class="mx-auto px-4" :class="isSplitScreen ? 'w-full' : 'md:w-1/2 w-full'">
				<PromptBoxV2 
					:report_id="report_id"
					:latestInProgressCompletion="isStreaming ? {} : undefined"
					:isStopping="false"
					@submitCompletion="onSubmitCompletion"
					@stopGeneration="abortStream"
				/>
			</div>
		</div>
	</div>
		</template>
		<template #right>
			<div>
				<DashboardComponent 
					ref="dashboardRef"
					v-if="reportLoaded && (visualizations || []).length >= 0"
					:report="report" 
					:edit="true" 
					:visualizations="visualizations"
					:textWidgetsIds="textWidgetsIds"
					@toggleSplitScreen="toggleSplitScreen"
					@editVisualization="handleEditQuery"
				/>
				<div v-else-if="reportLoaded && !(visualizations || []).length" class="p-4 text-center text-gray-500">
					No dashboard items yet.
				</div>
			</div>
		</template>
	</SplitScreenLayout>

	<!-- Trace Modal -->
	<TraceModal
		v-model="showTraceModal"
		:report-id="report_id"
		:completion-id="selectedCompletionForTrace || ''"
	/>

	<!-- Query Code Editor Modal -->
	<QueryCodeEditorModal
		:visible="showQueryEditor"
		:query-id="queryEditorProps.queryId"
		:step-id="queryEditorProps.stepId"
		:initial-code="queryEditorProps.initialCode"
		:title="queryEditorProps.title"
		@close="closeQueryEditor"
		@stepCreated="onStepCreated"
	/>

</template>

<script setup lang="ts">
import { ref, nextTick, onMounted, onUnmounted, watch, computed } from 'vue'
import PromptBoxV2 from '~/components/prompt/PromptBoxV2.vue'
import CreateWidgetTool from '~/components/tools/CreateWidgetTool.vue'
import CreateDataTool from '~/components/tools/CreateDataTool.vue'
import CreateDashboardTool from '~/components/tools/CreateDashboardTool.vue'
import AnswerQuestionTool from '~/components/tools/AnswerQuestionTool.vue'
import DescribeTablesTool from '~/components/tools/DescribeTablesTool.vue'
import ReadResourcesTool from '~/components/tools/ReadResourcesTool.vue'
import InstructionSuggestions from '@/components/InstructionSuggestions.vue'
import DataSourceIcon from '~/components/DataSourceIcon.vue'
import ExecuteCodeTool from '~/components/tools/ExecuteCodeTool.vue'
import ToolWidgetPreview from '~/components/tools/ToolWidgetPreview.vue'
import SplitScreenLayout from '~/components/report/SplitScreenLayout.vue'
import ReportHeader from '~/components/report/ReportHeader.vue'
import DashboardComponent from '~/components/DashboardComponent.vue'
import CompletionItemFeedback from '~/components/CompletionItemFeedback.vue'
import TraceModal from '~/components/console/TraceModal.vue'
import QueryCodeEditorModal from '~/components/tools/QueryCodeEditorModal.vue'
import Spinner from '~/components/Spinner.vue'
import { useCan } from '~/composables/usePermissions'

// Types
type ChatRole = 'user' | 'system'
type ChatStatus = 'in_progress' | 'success' | 'error' | 'stopped'

interface ToolCall {
	id: string
	tool_name: string
	tool_action?: string
	status: string
	result_summary?: string
	result_json?: any
	duration_ms?: number
	created_widget_id?: string
	created_step_id?: string
    created_widget?: any
    created_step?: any
}

interface CompletionBlock {
	id: string
	seq?: number
	block_index: number
	status: string
	content?: string
	reasoning?: string
	title?: string
	icon?: string
	started_at?: string
	completed_at?: string
	plan_decision?: {
		reasoning?: string
		assistant?: string
		final_answer?: string
		analysis_complete?: boolean
		plan_type?: string
	}
	tool_execution?: ToolCall
}

interface ChatMessage {
	id: string
	role: ChatRole
	status?: ChatStatus
	prompt?: { content: string }
	completion_blocks?: CompletionBlock[]
	tool_calls?: ToolCall[]
	created_at?: string
	// Backend system completion id used for sigkill
	system_completion_id?: string
	sigkill?: string | null
	feedback_score?: number
	// Transient streaming error message (set from SSE completion.error)
	error_message?: string
	// Optional structured error
	error?: any
	// Instruction suggestions generated during this completion
	instruction_suggestions?: Array<{ text: string; category: string }>
}

const route = useRoute()
const report_id = (route.params.id as string) || ''

// Permissions
const canViewConsole = computed(() => useCan('view_console'))

const messages = ref<ChatMessage[]>([])
// Pagination state
const pageLimit = 10
const hasMore = ref<boolean>(true)
const isLoadingMore = ref<boolean>(false)
const cursorBefore = ref<string | null>(null)
const promptText = ref<string>('')
const isStreaming = ref<boolean>(false)
let currentController: AbortController | null = null
const scrollContainer = ref<HTMLElement | null>(null)
const scrollAnchor = ref<HTMLElement | null>(null)
// No absolute prompt box; no padding ref needed
// Scroll state tracking
const isUserAtBottom = ref<boolean>(true)
const suppressAutoScroll = ref<boolean>(false)
const lastScrollTop = ref<number>(0)
// Hysteresis thresholds
const NEAR_BOTTOM_PX = 96
const RETURN_TO_BOTTOM_PX = 12

// Trace modal state
const showTraceModal = ref(false)
const selectedCompletionForTrace = ref<string | null>(null)

// Report and Dashboard state
const reportLoaded = ref(false)
const report = ref<any | null>(null)
const visualizations = ref<any[]>([])
const dashboardRef = ref<any | null>(null)
const textWidgetsIds = ref<string[]>([])

// Split screen state
const isSplitScreen = ref(false)
const leftPanelWidth = ref(450)
const isResizing = ref(false)
const initialMouseX = ref(0)
const initialPanelWidth = ref(0)

// Toggle states
const collapsedReasoning = ref<Set<string>>(new Set())
const expandedToolDetails = ref<Set<string>>(new Set())
function isRealCompletion(m: ChatMessage): boolean {
    // During streaming we use a temporary client id like "system-<ts>".
    // Only allow feedback UI when we have a real backend id (UUID) either in
    // system_completion_id or in id.
    const cid = (m.system_completion_id || m.id) || ''
    // UUID v4 pattern (loose): 8-4-4-4-12 hex
    const uuidRe = /^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$/
    return uuidRe.test(cid)
}

function getMessageError(m: any): string | null {
  // Prefer a content message stored on the completion (backend persisted), else last error block content
  try {
    // Some backends put message into completion.completion.content
    const content = (m?.completion?.content) || (m?.prompt?.content && m.status==='error' ? null : null)
    if (typeof content === 'string' && content.trim()) return content.trim()
  } catch {}
  const blocks = m?.completion_blocks || []
  for (let i = blocks.length - 1; i >= 0; i--) {
    const b = blocks[i]
    if (b?.status === 'error' && typeof b?.content === 'string' && b.content.trim()) {
      return b.content.trim()
    }
  }
  return null
}


// Helper functions for block types
function isResearchBlock(block: CompletionBlock): boolean {
	return block.plan_decision?.plan_type === 'research' || (block.title?.includes('research') ?? false)
}

function isBlockFinalized(block: CompletionBlock): boolean {
	return !!(block.plan_decision?.analysis_complete || block.completed_at || block.status === 'stopped')
}

function hasCompletedContent(block: CompletionBlock): boolean {
	return !!(block.content || block.status === 'completed' || block.status === 'stopped')
}

function getToolComponent(toolName: string) {
	switch (toolName) {
    // 'create_data_model' removed
		case 'create_widget':
			return CreateWidgetTool
    case 'create_data':
      return CreateDataTool
			case 'describe_tables':
				return DescribeTablesTool
		case 'create_and_execute_code':
			return ExecuteCodeTool
		case 'create_dashboard':
			return CreateDashboardTool
		case 'answer_question':
			return AnswerQuestionTool
		case 'read_resources':
			return ReadResourcesTool
		case 'suggest_instructions':
			return InstructionSuggestions
		case 'execute_code':
		case 'execute_sql':
			return ExecuteCodeTool
		default:
			return null
	}
}

function shouldUseToolComponent(toolExecution: ToolCall): boolean {
	return getToolComponent(toolExecution.tool_name) !== null
}

function shouldShowToolWidgetPreview(toolExecution: ToolCall | undefined): boolean {
	if (!toolExecution) return false
	
  // Only show for generic code-execution tools with success status.
  // Tools with a specialized component (e.g., create_widget, create_data) handle their own preview.
  const showForTools = ['create_and_execute_code', 'execute_code', 'execute_sql']
	return showForTools.includes(toolExecution.tool_name) && 
	       toolExecution.status === 'success' &&
	       (toolExecution.created_widget || toolExecution.created_step)
}

function shouldShowWorkingDots(message: ChatMessage): boolean {
	// Only show for system messages that are in progress
	if (message.role !== 'system' || message.status !== 'in_progress') {
		return false
	}
	
	// Don't show dots if the message was killed (sigkill timestamp exists)
	if (message.sigkill) {
		return false
	}
	
	// CASE 1: No blocks yet - show dots (initial startup phase)
	if (!message.completion_blocks || message.completion_blocks.length === 0) {
		return true
	}
	
	// CASE 2: Blocks exist but no meaningful content yet (early startup)
	const hasAnyMeaningfulContent = message.completion_blocks.some(block => 
		block.plan_decision?.reasoning || 
		block.reasoning || 
		block.content ||
		block.tool_execution
	)
	
	// If no meaningful content yet, show dots
	if (!hasAnyMeaningfulContent) {
		return true
	}
	
	// CASE 3: Check if we're in a "gap" between blocks during streaming
	const lastBlock = message.completion_blocks[message.completion_blocks.length - 1]
	
	// If the last block has final_answer and analysis_complete, we're truly done
	if (lastBlock?.plan_decision?.analysis_complete === true) {
		return false
	}
	
	// Check if the last block has finished its main content but no tools are running
	const lastBlockHasContent = lastBlock && (
		lastBlock.content ||
		lastBlock.plan_decision?.final_answer
	)
	
	// Check if tools are actively running
	const hasActiveTools = message.completion_blocks.some(block => 
		block.tool_execution?.status === 'running' || 
		block.status === 'in_progress'
	)
	
	// Check if any block is actively streaming text (has reasoning but no assistant yet)
	const hasStreamingContent = message.completion_blocks.some(block => 
		(block.plan_decision?.reasoning && !block.content) ||
		(block.reasoning && !block.content)
	)
	
	// Show dots when:
	// 1. System is in progress AND
	// 2. No active tools/streaming AND
	// 3. Last block has content but system continues (preparing next block)
	return !hasActiveTools && !hasStreamingContent && (!!lastBlockHasContent && message.status === 'in_progress')
}

function getThoughtProcessLabel(block: CompletionBlock): string {
	// Handle stopped blocks
	if (block.status === 'stopped') {
		return 'Thought Process'
	}

	// Prefer planner-provided reasoning duration when available
	const metricsAny: any = (block.plan_decision as any)?.metrics || (block.plan_decision as any)?.metrics_json
	const thinkingMs: number | undefined = metricsAny?.thinking_ms
	if (typeof thinkingMs === 'number' && isFinite(thinkingMs) && thinkingMs >= 0) {
		const secs = Math.max(0, Math.round(thinkingMs / 1000))
		return `Thought for ${secs}s`
	}
	
	// Calculate duration from started_at to completed_at if available
	if (block.started_at && block.completed_at) {
		const startTime = new Date(block.started_at).getTime()
		const endTime = new Date(block.completed_at).getTime()
		const durationMs = endTime - startTime
		const durationSeconds = Math.round(durationMs / 1000)
		
		// Sanity check for unreasonable durations (over 30 minutes)
		if (durationSeconds > 1800) {
			return 'Stopped'
		}
		
		return `Thought for ${durationSeconds}s`
	}
	
	// Fallback to duration from tool execution if available
	if (block.tool_execution?.duration_ms) {
		const durationSeconds = (block.tool_execution.duration_ms / 1000).toFixed(1)
		return `Thought for ${durationSeconds}s`
	}
	
	// Default fallback
	return 'Thought Process'
}



// Auto-collapse reasoning when content becomes available
watch(() => messages.value, (newMessages) => {
	newMessages.forEach(message => {
		if (message.completion_blocks) {
			message.completion_blocks.forEach(block => {
				// Auto-collapse reasoning when assistant content becomes available
				if (hasCompletedContent(block) && !collapsedReasoning.value.has(block.id)) {
					collapsedReasoning.value.add(block.id)
				}
			})
		}
	})
}, { deep: true })

// Watch for split screen changes and scroll to bottom to maintain position
watch(() => isSplitScreen.value, () => {
    nextTick(() => setTimeout(safeScrollToBottom, 80))
})

function goBack() {
	if (history.length > 1) history.back()
}

function toggleReasoning(messageId: string) {
	if (collapsedReasoning.value.has(messageId)) {
		collapsedReasoning.value.delete(messageId)
	} else {
		collapsedReasoning.value.add(messageId)
	}
}

function isReasoningCollapsed(messageId: string) {
	return collapsedReasoning.value.has(messageId)
}

function toggleToolDetails(toolId: string) {
	if (expandedToolDetails.value.has(toolId)) {
		expandedToolDetails.value.delete(toolId)
	} else {
		expandedToolDetails.value.add(toolId)
	}
}

function isToolDetailsExpanded(toolId: string) {
	return expandedToolDetails.value.has(toolId)
}

function scrollToBottom() {
  // Single-pass scroll: go to max scroll position
  nextTick(() => {
    setTimeout(() => {
      const container = scrollContainer.value
      if (!container) return
      container.offsetHeight // force reflow
      container.scrollTop = container.scrollHeight
    }, 40)
  })
}

// Guarded scroll that respects user upward scrolling during streaming
function safeScrollToBottom() {
  if (isStreaming.value && suppressAutoScroll.value) return
  scrollToBottom()
}

// Only auto-scroll when the user is already near the bottom to avoid jumpiness
function autoScrollIfNearBottom() {
  const container = scrollContainer.value
  if (!container) return
  const threshold = NEAR_BOTTOM_PX
  const distanceFromBottom = container.scrollHeight - (container.scrollTop + container.clientHeight)
  if (suppressAutoScroll.value && isStreaming.value) return
  if (distanceFromBottom <= threshold) {
    scrollToBottom()
  }
}

function scheduleInitialScroll() {
    const delays = [0, 80, 160, 320, 640]
    for (const delay of delays) setTimeout(safeScrollToBottom, delay)
}

// Keep scrolling to bottom across successive layout passes until height stabilizes
function settleScrollToBottom(maxFrames = 24) {
    const container = scrollContainer.value
    if (!container) return
    let frames = 0
    let lastHeight = -1
    const tick = () => {
        if (!scrollContainer.value) return
        const h = scrollContainer.value.scrollHeight
        if (h !== lastHeight) {
            lastHeight = h
            scrollContainer.value.scrollTop = h
            frames = 0
        } else {
            frames++
        }
        if (frames < 3 && maxFrames-- > 0) {
            requestAnimationFrame(tick)
        }
    }
    requestAnimationFrame(tick)
}

async function handleStreamingEvent(eventType: string | null, payload: any, sysMessageIndex: number) {
	if (!eventType || sysMessageIndex === -1) return
	
	if (!messages.value[sysMessageIndex]) return

	const sysMessage = messages.value[sysMessageIndex]
	
	switch (eventType) {
		case 'completion.started':
			// Update system message status
			sysMessage.status = 'in_progress'
			// Stash backend system completion id for stop-generation (sigkill)
			if (payload && payload.system_completion_id) {
				sysMessage.system_completion_id = payload.system_completion_id
			}
			break

		case 'instructions.suggest.started':
			// Create a lightweight synthetic block to display drafts streaming
			if (!sysMessage.completion_blocks) sysMessage.completion_blocks = []
			// Avoid duplicating if already created in the same completion
			if (!sysMessage.completion_blocks.some(b => b.title === 'Instruction Suggestions')) {
				sysMessage.completion_blocks.push({
					id: `instr-${Date.now()}`,
					block_index: (sysMessage.completion_blocks.length || 0) + 1,
					status: 'in_progress',
					title: 'Instruction Suggestions',
					icon: '📝',
					tool_execution: {
						id: `instr-te-${Date.now()}`,
						tool_name: 'suggest_instructions',
						status: 'running',
						result_json: { drafts: [] }
					}
				} as any)
			}
			break

		case 'instructions.suggest.partial':
			// Append each streamed draft to the synthetic block (keep full object for actions)
			{
				const b = [...(sysMessage.completion_blocks || [])].reverse().find(x => x.tool_execution?.tool_name === 'suggest_instructions')
				if (b && b.tool_execution) {
					b.tool_execution.result_json = b.tool_execution.result_json || {}
					const rj: any = b.tool_execution.result_json
					rj.drafts = Array.isArray(rj.drafts) ? rj.drafts : []
					const instr = payload?.instruction
					if (instr && typeof instr.text === 'string') {
						// Push the full server-sent payload so it includes id/status/global_status
						rj.drafts.push({ ...instr })
						b.status = 'in_progress'
					}
				}
			}
			break

		case 'instructions.suggest.finished':
			// Mark the synthetic block done
			{
				const b = [...(sysMessage.completion_blocks || [])].reverse().find(x => x.tool_execution?.tool_name === 'suggest_instructions')
				if (b && b.tool_execution) {
					b.tool_execution.status = 'success'
					b.status = 'success'
				}
			}
			break

		case 'block.upsert':
			// Add or update a completion block
			if (payload.block) {
				const block = payload.block
				if (!sysMessage.completion_blocks) {
					sysMessage.completion_blocks = []
				}
				
				// Find existing block or insert in-order by block_index (avoid resorting array)
				const existingIndex = sysMessage.completion_blocks.findIndex(b => b.id === block.id)
				if (existingIndex >= 0) {
					// Update existing block in place
					Object.assign(sysMessage.completion_blocks[existingIndex], block)
				} else {
					let insertPos = sysMessage.completion_blocks.length
					for (let i = 0; i < sysMessage.completion_blocks.length; i++) {
						const bi = sysMessage.completion_blocks[i]
						if ((bi?.block_index ?? Number.MAX_SAFE_INTEGER) > (block?.block_index ?? Number.MAX_SAFE_INTEGER)) {
							insertPos = i
							break
						}
					}
					sysMessage.completion_blocks.splice(insertPos, 0, block)
				}
			}
			break

		case 'block.delta.text':
			// Update text snapshot for a specific block (full overwrite)
			if (payload.block_id && payload.field && payload.text) {
				const idx = sysMessage.completion_blocks?.findIndex(b => b.id === payload.block_id) ?? -1
				if (idx >= 0 && sysMessage.completion_blocks) {
					const updated = { ...sysMessage.completion_blocks[idx] }
					if (payload.field === 'content') {
						updated.content = payload.text
					} else if (payload.field === 'reasoning') {
						updated.reasoning = payload.text
						if (!updated.plan_decision) updated.plan_decision = {}
						updated.plan_decision.reasoning = payload.text
					}
					sysMessage.completion_blocks.splice(idx, 1, updated)
				}
			}
			break

		case 'block.delta.token':
			// Handle individual token streaming for real-time typing effect
			if (payload.block_id && payload.field && payload.token) {
				const idx = sysMessage.completion_blocks?.findIndex(b => b.id === payload.block_id) ?? -1
				if (idx >= 0 && sysMessage.completion_blocks) {
					const t = String(payload.token || '')
					const updated = { ...sysMessage.completion_blocks[idx] }
					if (payload.field === 'content') {
						updated.content = (updated.content || '') + t
					} else if (payload.field === 'reasoning') {
						updated.reasoning = (updated.reasoning || '') + t
						if (!updated.plan_decision) updated.plan_decision = {}
						updated.plan_decision.reasoning = (updated.plan_decision.reasoning || '') + t
					}
					sysMessage.completion_blocks.splice(idx, 1, updated)
				}
			}
			break

		case 'block.delta.text.complete':
			// Handle text completion finalization
			if (payload.block_id && payload.field && payload.is_final) {
				const block = sysMessage.completion_blocks?.find(b => b.id === payload.block_id)
				if (block) {
					// Mark field as complete - could be used for UI effects
				}
			}
			break

		case 'block.delta.artifact':
			// Handle artifact changes (for progressive updates)
			if (payload.change && payload.change.type === 'step') {
				const block = sysMessage.completion_blocks?.find(b => b.tool_execution?.created_step_id === payload.change.step_id)
				if (block && block.tool_execution) {
					block.status = 'in_progress'
					// Merge streamed data_model fields into tool_execution.result_json for live UI updates
					const fields = payload.change.fields || {}
					if (fields.data_model) {
						block.tool_execution.result_json = block.tool_execution.result_json || {}
						const rj: any = block.tool_execution.result_json
						rj.data_model = { ...(rj.data_model || {}), ...fields.data_model }
						if (Array.isArray(fields.data_model.columns)) {
							const existing = new Map<string, any>((rj.data_model.columns || []).map((c: any) => [c.generated_column_name, c]))
							for (const col of fields.data_model.columns) existing.set(col.generated_column_name, col)
							rj.data_model.columns = Array.from(existing.values())
						}
					}
				}
			}
			break

		case 'tool.started':
			// Update block to show tool execution started
			if (payload.tool_name) {
				// Find the most recent block and update it
				const lastBlock = sysMessage.completion_blocks?.[sysMessage.completion_blocks.length - 1]
				if (lastBlock) {
					if (!lastBlock.tool_execution) {
						lastBlock.tool_execution = {
							id: `temp-${Date.now()}`,
							tool_name: payload.tool_name,
							status: 'running'
						}
					}
					// Reset result_json for fresh run to avoid stale shared references
					lastBlock.tool_execution.result_json = {}
					// For describe_tables, stash the query so the UI can show it
					try {
						if (payload.tool_name === 'describe_tables' && payload.arguments) {
							const q = payload.arguments.query
							const qStr = Array.isArray(q) ? q.join(', ') : (typeof q === 'string' ? q : (q ? JSON.stringify(q) : 'tables'))
							;(lastBlock.tool_execution.result_json as any).search_query = q
							lastBlock.tool_execution.result_summary = `Searching ${qStr}…`
						}
						if (payload.tool_name === 'read_resources' && payload.arguments) {
							const q = payload.arguments.query
							const qStr = Array.isArray(q) ? q.join(', ') : (typeof q === 'string' ? q : (q ? JSON.stringify(q) : 'resources'))
							;(lastBlock.tool_execution.result_json as any).search_query = q
							lastBlock.tool_execution.result_summary = `Searching ${qStr}…`
						}
					} catch {}
					lastBlock.status = 'in_progress'
				}
			}
			break

		case 'tool.progress':
			// Update tool execution progress on the latest block (best-effort) and stream data model deltas
			if (payload.tool_name) {
				const lastBlock = sysMessage.completion_blocks?.[sysMessage.completion_blocks.length - 1]
				if (lastBlock) {
					if (!lastBlock.tool_execution) {
						lastBlock.tool_execution = {
							id: `temp-${Date.now()}`,
							tool_name: payload.tool_name,
							status: 'running'
						}
					} else {
						lastBlock.tool_execution.status = 'running'
					}

					// Record progress stage for tool-specific UIs
					if (payload.payload && lastBlock.tool_execution) {
						;(lastBlock.tool_execution as any).progress_stage = payload.payload.stage || null
						// Capture icon for read_resources submit_search stage if provided
						if (payload.tool_name === 'read_resources' && payload.payload.stage === 'submit_search' && payload.payload.icon) {
							lastBlock.tool_execution.result_json = lastBlock.tool_execution.result_json || {}
							;(lastBlock.tool_execution.result_json as any).icon = payload.payload.icon
						}
					}

          // Progressive data model updates for create_widget tool
          if ((payload.tool_name === 'create_widget') && payload.payload) {
						const p = payload.payload
						// Ensure result_json.data_model structure exists
						lastBlock.tool_execution.result_json = lastBlock.tool_execution.result_json || {}
						const rj = lastBlock.tool_execution.result_json as any
						rj.data_model = rj.data_model || { type: null, columns: [], series: [] }

						if (p.stage === 'data_model_type_determined' && p.data_model_type) {
							rj.data_model.type = p.data_model_type
						}
						if (p.stage === 'column_added' && p.column) {
							const exists = (rj.data_model.columns || []).some((c: any) => c.generated_column_name === p.column.generated_column_name)
							if (!exists) {
								rj.data_model.columns.push(p.column)
							}
						}
						if (p.stage === 'series_configured' && Array.isArray(p.series)) {
							rj.data_model.series = p.series
						}
						if (p.stage === 'widget_creation_needed' && p.data_model) {
							rj.data_model = { ...rj.data_model, ...p.data_model }
						}
					}

					// Progressive instruction drafts for suggest_instructions tool
					if (payload.tool_name === 'suggest_instructions' && payload.payload) {
						const p = payload.payload
						if (p.stage === 'instruction_added' && p.instruction) {
							lastBlock.tool_execution.result_json = lastBlock.tool_execution.result_json || {}
							const rj: any = lastBlock.tool_execution.result_json
							rj.drafts = Array.isArray(rj.drafts) ? rj.drafts : []
							const draft = { text: String(p.instruction.text || ''), category: p.instruction.category || null }
							if (draft.text) {
								rj.drafts.push(draft)
								lastBlock.status = 'in_progress'
							}
						}
					}

					// When create_dashboard streams a completed block, broadcast layout change so previews refresh membership
					if (payload.tool_name === 'create_dashboard' && payload.payload && payload.payload.stage === 'block.completed') {
						try {
							window.dispatchEvent(new CustomEvent('dashboard:layout_changed', { detail: { report_id: report_id, action: 'added' } }))
						} catch {}
					}

					lastBlock.status = 'in_progress'
				}
			}
			break

		case 'tool.partial':
			// Streamed partial output for tools (e.g., answer_question)
			if (payload.tool_name) {
				const lastBlock = sysMessage.completion_blocks?.[sysMessage.completion_blocks.length - 1]
				if (lastBlock) {
					if (!lastBlock.tool_execution) {
						lastBlock.tool_execution = {
							id: `temp-${Date.now()}`,
							tool_name: payload.tool_name,
							status: 'running'
						}
					}
					const fullAnswer = (payload.payload && typeof payload.payload.answer === 'string') ? payload.payload.answer : null
					const delta = (payload.payload && typeof payload.payload.delta === 'string') ? payload.payload.delta : null
					lastBlock.tool_execution.result_json = lastBlock.tool_execution.result_json || {}
					const rj: any = lastBlock.tool_execution.result_json
					if (fullAnswer !== null) {
						// Replace with accumulated answer (preferred)
						rj.answer = fullAnswer
						lastBlock.status = 'in_progress'
					} else if (delta) {
						// Backward-compatibility: append streaming delta
						rj.answer = (rj.answer || '') + delta
						lastBlock.status = 'in_progress'
					}
				}
			}
			break

		case 'widget.created':
			// No-op for now; this is displayed in the report UI elsewhere
			break

		case 'data_model.completed':
			// No-op; step/widget UIs will reflect final data model. Avoid logging unknown.
			break

		case 'tool.finished':
			// Update tool execution status
			if (payload.tool_name && payload.status) {
				// Prefer precise targeting when identifiers are available
				const blocks = sysMessage.completion_blocks || []
				let blockWithTool = blocks.find(b => (payload.block_id && b.id === payload.block_id)) 
					|| blocks.find(b => (payload.tool_execution_id && b.tool_execution?.id === payload.tool_execution_id))
					// Fallback: choose the most recent running/in-progress block for this tool
					|| [...blocks].reverse().find(b => 
						b.tool_execution?.tool_name === payload.tool_name && 
						(b.tool_execution?.status === 'running' || b.status === 'in_progress')
					)
					// Last fallback: most recent block with matching tool name
					|| [...blocks].reverse().find(b => b.tool_execution?.tool_name === payload.tool_name)

				if (blockWithTool?.tool_execution) {
					blockWithTool.tool_execution.status = payload.status
					blockWithTool.status = payload.status === 'success' ? 'success' : 'error'
					if (payload.result_summary) {
						blockWithTool.tool_execution.result_summary = payload.result_summary
					}
					if (payload.result_json) {
						blockWithTool.tool_execution.result_json = payload.result_json
					}
					if (payload.duration_ms !== undefined) {
						blockWithTool.tool_execution.duration_ms = payload.duration_ms
					}
					if (payload.created_widget_id) {
						blockWithTool.tool_execution.created_widget_id = payload.created_widget_id
					}
					if (payload.created_step_id) {
						blockWithTool.tool_execution.created_step_id = payload.created_step_id
					}
					// If the dashboard was created successfully, refresh widgets and open the dashboard pane
					if (payload.tool_name === 'create_dashboard' && payload.status === 'success') {
						try { await loadVisualizations() } catch (e) { /* noop */ }
						if (!isSplitScreen.value) toggleSplitScreen()
					}
				}
			}
			break

		case 'decision.partial':
		case 'decision.final':
			// Update plan decision information
			if (payload.reasoning || payload.assistant) {
				const lastBlock = sysMessage.completion_blocks?.[sysMessage.completion_blocks.length - 1]
				if (lastBlock) {
					if (!lastBlock.plan_decision) {
						lastBlock.plan_decision = {}
					}
					if (payload.reasoning) {
						lastBlock.plan_decision.reasoning = payload.reasoning
					}
					if (payload.assistant) {
						lastBlock.plan_decision.assistant = payload.assistant
					}
					if (payload.final_answer) {
						lastBlock.plan_decision.final_answer = payload.final_answer
					}
					if (eventType === 'decision.final') {
						lastBlock.plan_decision.analysis_complete = payload.analysis_complete ?? true
					}
				}
			}
			break

		case 'completion.finished':
			// Mark completion as finished with proper status if provided; don't default to success
			const completionStatus = (payload && typeof payload.status === 'string') ? payload.status : null
			if (completionStatus) {
				sysMessage.status = completionStatus as any
				if (completionStatus === 'error' && payload?.error?.message) {
					sysMessage.error_message = String(payload.error.message)
					// Ensure a single error block exists for history (won't render duplicate due to block suppression)
					if (!sysMessage.completion_blocks?.some((b: any) => b.status === 'error')) {
						sysMessage.completion_blocks = sysMessage.completion_blocks || []
						sysMessage.completion_blocks.push({ id: `error-${Date.now()}`, block_index: 999, status: 'error', content: sysMessage.error_message })
					}
				}
			}
			loadReport()
			break

		case 'completion.error':
			// Dedicated error event; ensure UI flips to error state and capture the message
			sysMessage.status = 'error'
			if (payload?.error) {
				const msg = typeof payload.error === 'string' ? payload.error : (payload.error.message || '')
				if (msg) sysMessage.error_message = String(msg)
				if (!sysMessage.completion_blocks?.some((b: any) => b.status === 'error')) {
					sysMessage.completion_blocks = sysMessage.completion_blocks || []
					sysMessage.completion_blocks.push({ id: `error-${Date.now()}`, block_index: 999, status: 'error', content: sysMessage.error_message })
				}
			}
			break

		default:
			// Handle unknown events gracefully
			break
	}
}

async function loadCompletions() {
	try {
		const { data } = await useMyFetch(`/reports/${report_id}/completions?limit=${pageLimit}`)
		const response = data.value as any
		const list = response?.completions || []
		messages.value = list.map((c: any) => {
			// Override status if sigkill timestamp exists - this means it was stopped
			let status = c.status as ChatStatus
			if (c.sigkill && status === 'in_progress') {
				status = 'stopped'
			}
			
			return {
				id: c.id,
				role: c.role as ChatRole,
				status: status,
				prompt: c.prompt,
				completion_blocks: c.completion_blocks?.map((b: any) => ({
					id: b.id,
					seq: b.seq,
					block_index: b.block_index,
					status: b.status,
					content: b.content,
					reasoning: b.reasoning,
					plan_decision: b.plan_decision,
					tool_execution: b.tool_execution ? {
						id: b.tool_execution.id,
						tool_name: b.tool_execution.tool_name,
						tool_action: b.tool_execution.tool_action,
						status: b.tool_execution.status,
						result_summary: b.tool_execution.result_summary,
						result_json: b.tool_execution.result_json,
						duration_ms: b.tool_execution.duration_ms,
						created_widget_id: b.tool_execution.created_widget_id,
						created_step_id: b.tool_execution.created_step_id,
						created_widget: b.tool_execution.created_widget,
						created_step: b.tool_execution.created_step
					} : undefined
				})) || [],
				created_at: c.created_at,
				sigkill: c.sigkill,
				feedback_score: c.feedback_score,
				instruction_suggestions: c.instruction_suggestions
			}
		})
		// Update cursors
		hasMore.value = !!response?.has_more
		cursorBefore.value = response?.next_before || null
        await nextTick()
        safeScrollToBottom()
	} catch (e) {
		console.error('Error loading completions:', e)
	}
}

// Load previous page (older completions) and prepend while preserving scroll anchor
async function loadPreviousCompletions() {
    if (isLoadingMore.value || !hasMore.value) return
    const container = scrollContainer.value
    if (!container) return
    isLoadingMore.value = true
    const prevHeight = container.scrollHeight
    try {
        const qs = cursorBefore.value ? `&before=${encodeURIComponent(cursorBefore.value)}` : ''
        const { data } = await useMyFetch(`/reports/${report_id}/completions?limit=${pageLimit}${qs}`)
        const response = data.value as any
        const list: any[] = response?.completions || []
        const newItems: ChatMessage[] = list.map((c: any) => {
            let status = c.status as ChatStatus
            if (c.sigkill && status === 'in_progress') status = 'stopped'
            return {
                id: c.id,
                role: c.role as ChatRole,
                status,
                prompt: c.prompt,
                completion_blocks: c.completion_blocks?.map((b: any) => ({
                    id: b.id,
                    seq: b.seq,
                    block_index: b.block_index,
                    status: b.status,
                    content: b.content,
                    reasoning: b.reasoning,
                    plan_decision: b.plan_decision,
                    tool_execution: b.tool_execution ? {
                        id: b.tool_execution.id,
                        tool_name: b.tool_execution.tool_name,
                        tool_action: b.tool_execution.tool_action,
                        status: b.tool_execution.status,
                        result_summary: b.tool_execution.result_summary,
                        result_json: b.tool_execution.result_json,
                        duration_ms: b.tool_execution.duration_ms,
                        created_widget_id: b.tool_execution.created_widget_id,
                        created_step_id: b.tool_execution.created_step_id,
                        created_widget: b.tool_execution.created_widget,
                        created_step: b.tool_execution.created_step
                    } : undefined
                })) || [],
                created_at: c.created_at,
                sigkill: c.sigkill,
                feedback_score: c.feedback_score,
                instruction_suggestions: c.instruction_suggestions
            }
        })
        // Dedupe by id and prepend
        const existingIds = new Set(messages.value.map(m => m.id))
        const toPrepend = newItems.filter(m => !existingIds.has(m.id))
        if (toPrepend.length > 0) {
            messages.value = [...toPrepend, ...messages.value]
            await nextTick()
            // Keep viewport anchored to previous items
            const newHeight = container.scrollHeight
            container.scrollTop = newHeight - prevHeight
        }
        hasMore.value = !!response?.has_more
        cursorBefore.value = response?.next_before || null
    } catch (e) {
        // keep hasMore as-is on error
    } finally {
        isLoadingMore.value = false
    }
}

function onScroll() {
    const container = scrollContainer.value
    if (!container) return
    // Infinite scroll trigger near top
    if (!isLoadingMore.value && hasMore.value) {
        const thresholdTop = 64
        if (container.scrollTop <= thresholdTop) {
            loadPreviousCompletions()
        }
    }

    // Update bottom proximity and user intent
    const distanceFromBottom = container.scrollHeight - (container.scrollTop + container.clientHeight)
    isUserAtBottom.value = distanceFromBottom <= RETURN_TO_BOTTOM_PX

    const isScrollingUp = container.scrollTop < lastScrollTop.value
    // Suppress auto-scroll on any upward scroll, regardless of proximity
    if (isScrollingUp) {
        suppressAutoScroll.value = true
    }
    // Re-enable only when the user returns to within tight bottom threshold
    if (!isScrollingUp && distanceFromBottom <= RETURN_TO_BOTTOM_PX) {
        suppressAutoScroll.value = false
    }
    lastScrollTop.value = container.scrollTop
}

async function loadReport() {
	const { data } = await useMyFetch(`/api/reports/${report_id}`)
	report.value = data.value
	reportLoaded.value = true
	
	// Check if we need to redirect to legacy UI based on app version
	if (report.value?.app_version === "0.0.189") {
		await navigateTo(`/reports/${report_id}/legacy`)
		return
	}
}

async function loadVisualizations() {
	try {
		const { data, error } = await useMyFetch(`/api/queries?report_id=${report_id}`, { method: 'GET' })
		if (error.value) throw error.value
		const queries = Array.isArray(data.value) ? data.value : []
		const list: any[] = []
		for (const q of queries) {
			for (const v of (q?.visualizations || [])) {
				if (v && v.id) list.push(v)
			}
		}
		visualizations.value = list
	} catch (e) {
		visualizations.value = []
	}
}

// Fast dashboard refresh triggered by editor save
async function refreshDashboardFast() {
    try {
        const dash = dashboardRef.value
        if (dash && typeof dash.refreshLayout === 'function') {
            await dash.refreshLayout()
        }
    } catch (e) {
        // noop
    }
}

// Ensure dashboard pane opens only when currently closed
onMounted(() => {
    window.addEventListener('dashboard:ensure_open', () => {
        if (!isSplitScreen.value) toggleSplitScreen()
    })
})

// When a tool finishes saving a new step, broadcast the default step change if we have enough info
watch(() => messages.value, () => {
    try {
        const last = [...messages.value].reverse().find(m => m.role === 'system')
        const lastBlock = last?.completion_blocks?.slice(-1)[0]
        const te: any = lastBlock?.tool_execution
        if (te && te.created_step && te.created_step.query_id) {
            try {
                window.dispatchEvent(new CustomEvent('query:default_step_changed', { detail: { query_id: te.created_step.query_id, step: te.created_step } }))
            } catch {}
        }
    } catch {}
}, { deep: true })

async function loadActiveLayoutHasBlocks(): Promise<boolean> {
    try {
        const { data } = await useMyFetch(`/api/reports/${report_id}/layouts`)
        const layouts = Array.isArray(data.value) ? (data.value as any[]) : []
        const active = layouts.find((l: any) => l.is_active)
        return !!(active && Array.isArray(active.blocks) && active.blocks.length > 0)
    } catch (e) {
        return false
    }
}

function toggleSplitScreen() {
	nextTick(() => {
		isSplitScreen.value = !isSplitScreen.value
		if (isSplitScreen.value) {
			leftPanelWidth.value = 460
		}
        safeScrollToBottom()
	})
}

function startResize(e: MouseEvent) {
	isResizing.value = true
	initialMouseX.value = e.clientX
	initialPanelWidth.value = leftPanelWidth.value
		document.addEventListener('mousemove', handleResize)
	document.addEventListener('mouseup', stopResize)
	document.body.style.userSelect = 'none'
}

function handleResize(e: MouseEvent) {
	if (!isResizing.value) return
	const minWidth = 280
	const maxWidth = window.innerWidth * 0.8
	const dx = e.clientX - initialMouseX.value
	const newWidth = initialPanelWidth.value + dx
	leftPanelWidth.value = Math.min(Math.max(newWidth, minWidth), maxWidth)
	// Trigger scroll to bottom during live resize to maintain scroll position
    safeScrollToBottom()
}

function stopResize() {
	isResizing.value = false
	document.removeEventListener('mousemove', handleResize)
	document.removeEventListener('mouseup', stopResize)
	document.body.style.userSelect = 'auto'
}

onUnmounted(() => {
	document.removeEventListener('mousemove', handleResize)
	document.removeEventListener('mouseup', stopResize)
	document.body.style.userSelect = 'auto'
    window.removeEventListener('resize', safeScrollToBottom)
	try { scrollContainer.value?.removeEventListener('scroll', onScroll) } catch {}
	// Stop any polling timers
	stopPollingInProgressCompletion()
})

function rerunReport() {
	useMyFetch(`/api/reports/${report_id}/rerun`, { method: 'POST' }).then(() => {
		loadVisualizations()
	})
}

// Handle Add to dashboard from ToolWidgetPreview
async function handleAddWidgetFromPreview(payload: { widget?: any, step?: any, visualization?: any }) {
    try {
        const viz = payload?.visualization
        const widget = payload?.widget
        if (viz?.id) {
            const block = { type: 'visualization', visualization_id: viz.id, x: 0, y: 0, width: 6, height: 7 }
            await useMyFetch(`/api/reports/${report_id}/layouts/active/blocks`, { method: 'PATCH', body: { blocks: [block] } })
        } else if (widget?.id) {
            const block = { type: 'widget', widget_id: widget.id, x: 0, y: 0, width: 6, height: 7 }
            await useMyFetch(`/api/reports/${report_id}/layouts/active/blocks`, { method: 'PATCH', body: { blocks: [block] } })
        } else {
            return
        }
        
        // Update the local widget status immediately to reflect the change in UI
        // Find the tool execution that contains this widget and update its status
        messages.value.forEach(message => {
            if (message.completion_blocks) {
                message.completion_blocks.forEach(block => {
                    if (viz?.id && (block.tool_execution as any)?.created_visualizations) {
                        const list = (block.tool_execution as any).created_visualizations as any[]
                        const found = list.find(v => v?.id === viz.id)
                        if (found) found.status = 'published'
                    }
                    if (widget?.id && block.tool_execution?.created_widget?.id === widget.id && block.tool_execution) {
                        block.tool_execution.created_widget.status = 'published'
                    }
                })
            }
        })
        
        		if (!isSplitScreen.value) toggleSplitScreen()
		await loadVisualizations()
        // Ask dashboard to refresh layout immediately so item appears
        try {
            const dash = dashboardRef.value
            if (dash && typeof dash.refreshLayout === 'function') await dash.refreshLayout()
        } catch {}
		// Scroll to bottom when dashboard opens after adding widget
		await nextTick()
        safeScrollToBottom()
    } catch (e) {
        console.error('Failed to add widget from preview:', e)
    }
}

function abortStream() {
	if (currentController) {
		currentController.abort()
		currentController = null
	}
	// Signal backend to stop the running agent loop if we know the server-side id
	try {
					const sysMsg = [...messages.value].reverse().find(m => m.role === 'system' && m.status === 'in_progress')
		const systemId = (sysMsg as any)?.system_completion_id
		if (systemId) {
			useMyFetch(`/api/completions/${systemId}/sigkill`, { method: 'POST' })
			// Mark locally as stopped for immediate UI feedback
			const msgIndex = messages.value.findIndex(m => m.id === sysMsg?.id)
			if (msgIndex !== -1) {
				// Force Vue reactivity by replacing the entire array
				const newMessages = [...messages.value]
				const updatedMessage = { ...newMessages[msgIndex], status: 'stopped' as ChatStatus }
				
				// Also update all completion blocks to stopped status
				if (updatedMessage.completion_blocks) {
					updatedMessage.completion_blocks = updatedMessage.completion_blocks.map(block => ({
						...block,
						status: block.status === 'in_progress' ? 'stopped' as ChatStatus : block.status,
						completed_at: block.completed_at || new Date().toISOString()
					}))
				}
				
				newMessages[msgIndex] = updatedMessage
				messages.value = newMessages
				
				// Force a nextTick update
				nextTick(() => {
				})
			}
		}
	} catch (e) {
		console.error('Failed to send sigkill:', e)
	}
	isStreaming.value = false
}

function openTraceModal(completionId: string) {
	selectedCompletionForTrace.value = completionId
	showTraceModal.value = true
}

function handleExampleClick(starter: string) {
	if (starter) {
		onSubmitCompletion({ text: starter, mentions: [] });
	}
}

// State for QueryCodeEditorModal
const showQueryEditor = ref(false)
const queryEditorProps = ref<{
	queryId: string | null
	stepId: string | null
	initialCode: string
	title: string
}>({
	queryId: null,
	stepId: null,
	initialCode: '',
	title: ''
})

function handleEditQuery(payload: { queryId: string; stepId: string | null; initialCode: string; title: string }) {
	queryEditorProps.value = {
		queryId: payload.queryId,
		stepId: payload.stepId,
		initialCode: payload.initialCode,
		title: payload.title
	}
	showQueryEditor.value = true
}

function closeQueryEditor() {
	showQueryEditor.value = false
}

function onStepCreated(step: any) {
	// Handle step creation - could refresh the current view or update state
	console.log('Step created:', step)
	// Optionally refresh the completion or update the UI
}

function onSubmitCompletion(data: { text: string, mentions: any[]; mode?: string; model_id?: string }) {
	const text = data.text.trim()
	if (!text) return

	// Append user message
	const userMsg: ChatMessage = {
		id: `user-${Date.now()}`,
		role: 'user',
		prompt: { content: text }
	}
	messages.value.push(userMsg)

	// Append placeholder system message for streaming
	const sysId = `system-${Date.now()}`
	const sysMsg: ChatMessage = {
		id: sysId,
		role: 'system',
		status: 'in_progress',
		completion_blocks: []
	}
	messages.value.push(sysMsg)
	scrollToBottom()

	// Stop any background polling and start streaming
	stopPollingInProgressCompletion()

	// Start streaming
	if (isStreaming.value) abortStream()
	currentController = new AbortController()
	isStreaming.value = true

	const requestBody = {
		prompt: {
			content: text,
			mentions: data.mentions || [],
			mode: data.mode || 'chat',
			model_id: data.model_id || null
		},
		stream: true
	}

	startStreaming(requestBody, sysId)
}

async function startStreaming(requestBody: any, sysId: string) {

	try {
		const options: any = {
			method: 'POST',
			headers: { 'Content-Type': 'application/json' },
			body: JSON.stringify(requestBody),
			signal: currentController?.signal,
			stream: true
		}
		const raw: any = await useMyFetch(`/reports/${report_id}/completions`, options as any)
		const res: Response = (raw?.data?.value ?? raw?.data) as unknown as Response

		if (!res?.ok || !res?.body) throw new Error(`Stream HTTP error: ${res?.status}`)

		const reader = res.body!.getReader()
		const decoder = new TextDecoder()
		let buffer = ''
		let currentEvent: string | null = null

		const ensureSys = () => messages.value.findIndex(m => m.id === sysId)

		while (true) {
			const { done, value } = await reader.read()
			if (done) {
				break
			}
			
			// Check if stream was aborted
			if (currentController?.signal.aborted) {
				break
			}
			
			buffer += decoder.decode(value, { stream: true })

			let nlIndex: number
			while ((nlIndex = buffer.indexOf('\n')) >= 0) {
				const line = buffer.slice(0, nlIndex).trimEnd()
				buffer = buffer.slice(nlIndex + 1)

				if (line.startsWith('event:')) {
					currentEvent = line.slice(6).trim()
				} else if (line.startsWith('data:')) {
					const dataStr = line.slice(5).trim()
					if (dataStr === '[DONE]') {
						isStreaming.value = false
						currentController = null
						return
					}
					try {
						const parsed = JSON.parse(dataStr)
						const payload = parsed.data ?? parsed
						const idx = ensureSys()
						if (idx !== -1) {
							await handleStreamingEvent(currentEvent, payload, idx)
							await nextTick()
							// Autoscroll only if user is near bottom (guarded inside scrollToBottom)
							autoScrollIfNearBottom()
						}
					} catch (e) {
						// ignore non-JSON data lines
					}
				}
			}
		}
	} catch (err) {
		console.error('Streaming error:', err)
		const idx = messages.value.findIndex(m => m.id === sysId)
		if (idx !== -1) {
			let errorMessage = 'An error occurred during streaming.'
			
			if (err instanceof Error) {
				if (err.name === 'AbortError') {
					// Check if this was a user-initiated stop (sigkill) vs connection abort
					const sysMsg = messages.value[idx]
					if (sysMsg && sysMsg.system_completion_id) {
						// This was likely a user stop, mark as stopped without error
						messages.value[idx] = { ...messages.value[idx], status: 'stopped' }
						return // Don't add error block for user stops
					} else {
						// Connection was aborted for other reasons
						errorMessage = 'Stream was cancelled.'
						messages.value[idx] = { ...messages.value[idx], status: 'stopped' }
					}
				} else if (err.message.includes('Stream HTTP error')) {
					errorMessage = `Connection error: ${err.message}`
					messages.value[idx] = { ...messages.value[idx], status: 'error' }
				} else {
					errorMessage = `Error: ${err.message}`
					messages.value[idx] = { ...messages.value[idx], status: 'error' }
				}
			} else {
				messages.value[idx] = { ...messages.value[idx], status: 'error' }
			}
			
			// Add error block if not already present
			if (!messages.value[idx].completion_blocks?.some(b => b.status === 'error')) {
				if (!messages.value[idx].completion_blocks) {
					messages.value[idx].completion_blocks = []
				}
				messages.value[idx].completion_blocks!.push({
					id: `error-${Date.now()}`,
					block_index: 999,
					status: 'error',
					content: errorMessage,
					title: 'Error',
					icon: '❌'
				})
			}
		}
	} finally {
		isStreaming.value = false
		currentController = null
	}
}

// === Minimal polling for refresh resume (no SSE resume) ===
const isPolling = ref<boolean>(false)
const pollIntervalMs = 1200
let pollHandle: number | null = null

function getLastInProgressSystem(): ChatMessage | undefined {
	return [...messages.value].reverse().find(m => m.role === 'system' && m.status === 'in_progress')
}

function stopPollingInProgressCompletion() {
	if (pollHandle !== null) {
		clearTimeout(pollHandle)
		pollHandle = null
	}
	isPolling.value = false
}

async function startPollingInProgressCompletion() {
	if (isStreaming.value || isPolling.value) return
	const sys = getLastInProgressSystem()
	if (!sys) return

	isPolling.value = true
	const startTs = Date.now()
	const maxDurationMs = 2 * 60 * 1000

	const tick = async () => {
		try {
			await loadCompletions()
			autoScrollIfNearBottom()
			const still = getLastInProgressSystem()
			if (!still) {
				stopPollingInProgressCompletion()
				return
			}
			if (Date.now() - startTs > maxDurationMs) {
				stopPollingInProgressCompletion()
				return
			}
		} catch (e) {
			// keep polling on transient errors
		} finally {
			pollHandle = window.setTimeout(tick, pollIntervalMs)
		}
	}

	pollHandle = window.setTimeout(tick, pollIntervalMs)
}

onMounted(async () => {
	await Promise.all([
		loadReport(),
		loadVisualizations(),
		loadCompletions(),
		
		loadActiveLayoutHasBlocks().then(hasBlocks => {
			if (hasBlocks) {
				isSplitScreen.value = true
			}
		})

	])
	
	// Handle new_message query parameter after everything is loaded
	if (route.query.new_message && messages.value.length == 0) {
		let mentions: any[] = []
		try {
			const raw = typeof route.query.mentions === 'string' ? decodeURIComponent(route.query.mentions) : ''
			if (raw) mentions = JSON.parse(raw)
		} catch {}
		const mode = typeof route.query.mode === 'string' ? route.query.mode : 'chat'
		const model_id = typeof route.query.model_id === 'string' ? route.query.model_id : null
		onSubmitCompletion({ text: route.query.new_message as string, mentions, mode, model_id: model_id || undefined })
	}

	// If a system message is still in progress (after refresh), begin polling until it finishes
	if (!isStreaming.value && getLastInProgressSystem()) {
		startPollingInProgressCompletion()
	}
	
	// Open dashboard pane if there are any published widgets
	if (visualizations.value.some(viz => viz.status === 'published')) {
		isSplitScreen.value = true
		// Scroll to bottom when automatically opening dashboard
    nextTick(() => setTimeout(safeScrollToBottom, 100))
	}
    // Aggressive initial scroll to handle async content mounting
	scheduleInitialScroll()
    window.addEventListener('resize', safeScrollToBottom)
	// Attach scroll listener for infinite scroll up
	try { scrollContainer.value?.addEventListener('scroll', onScroll) } catch {}
    // Initialize scroll position state
    try {
        const c = scrollContainer.value
        if (c) {
            lastScrollTop.value = c.scrollTop
            const dist = c.scrollHeight - (c.scrollTop + c.clientHeight)
            isUserAtBottom.value = dist <= RETURN_TO_BOTTOM_PX
            suppressAutoScroll.value = false
        }
    } catch {}
})

</script>

<style scoped>
.overflow-y-auto {
	overflow-y: auto !important;
}

/* Minimal typography akin to CompletionMessageComponent */
.markdown-wrapper :deep(.markdown-content) {
	@apply leading-relaxed;
	font-size: 14px;

	:where(h1, h2, h3, h4, h5, h6) {
		@apply font-bold mb-4 mt-6;
	}

	h1 { @apply text-3xl; }
	h2 { @apply text-2xl; }
	h3 { @apply text-xl; }

	ul, ol { @apply pl-6 mb-4; }
	ul { @apply list-disc; }
	ol { @apply list-decimal; }
	li { @apply mb-1.5; }

	pre { @apply bg-gray-50 p-4 rounded-lg mb-4 overflow-x-auto; }
	code { @apply bg-gray-50 px-1 py-0.5 rounded text-sm font-mono; }
	a { @apply text-blue-600 hover:text-blue-800 underline; }
	blockquote { @apply border-l-4 border-gray-200 pl-4 italic my-4; }
	table { @apply w-full border-collapse mb-4; }
	table th, table td { @apply border border-gray-200 p-2 text-xs bg-white; }
}

/* Streaming text (no re-mount, minimal styles, prevent flicker) */
.streaming-text {
    will-change: contents;
}

@keyframes simple-ellipsis { 0% { content: '.'; } 33% { content: '..'; } 66% { content: '...'; } }
.simple-dots::after { content: '.'; display: inline-block; margin-top: 5px; animation: simple-ellipsis 1.5s infinite; font-weight: 400; font-size: 14px; color: #888; }

@keyframes shimmer {
	0% { background-position: -100% 0; }
	100% { background-position: 100% 0; }
}

@keyframes ellipsis {
	0% { content: 'Thinking.'; }
	33% { content: 'Thinking..'; }
	66% { content: 'Thinking...'; }
}

.dots::after {
	content: 'Thinking...';
	display: inline-block;
	margin-top: 5px;
	background: linear-gradient(90deg, #888 0%, #999 25%, #ccc 50%, #999 75%, #888 100%);
	background-size: 200% 100%;
	-webkit-background-clip: text;
	background-clip: text;
	color: transparent;
	animation: shimmer 2s linear infinite, ellipsis 1s infinite;
	font-weight: 400;
	font-size: 14px;
	opacity: 1;
}

/* Add fade transitions */
.fade-enter-active,
.fade-leave-active {
	transition: opacity 0.3s ease;
}

.fade-enter-from,
.fade-leave-to {
	opacity: 0;
}

.fade-in {
    animation: fadeIn 0.6s ease-in;
}

@keyframes fadeIn {
    0% {
        opacity: 0;
        transform: translateY(10px);
    }
    100% {
        opacity: 1;
        transform: translateY(0);
    }
}

.reasoning-content { 
	opacity: 0.8; 
	transition: opacity 0.2s ease; 
}

.reasoning-content:hover { 
	opacity: 1; 
}

/* Minimal shimmer for reconnect banner */
.poll-shimmer {
	background: linear-gradient(90deg, #888 0%, #999 25%, #ccc 50%, #999 75%, #888 100%);
	background-size: 200% 100%;
	-webkit-background-clip: text;
	background-clip: text;
	color: transparent;
	animation: shimmer 2s linear infinite;
	font-weight: 400;
	opacity: 1;
}
</style>



