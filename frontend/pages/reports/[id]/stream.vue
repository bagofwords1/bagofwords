<template>

	<div class="flex flex-col h-screen overflow-y-hidden bg-white">
		<header class="sticky top-0 bg-white z-10 flex flex-row pt-1 h-[40px] border-gray-200 pb-1 pr-2 items-center">
			<button class="p-1.5 rounded text-lg hover:bg-gray-100 mr-2" @click="goBack" aria-label="Back">
				<span>←</span>
			</button>
			<h1 class="text-sm md:text-left text-center mt-1">
				<span class="font-semibold text-sm">Chat</span>
			</h1>
			<div class="ml-auto">
				<button v-if="isStreaming" class="px-2 py-1 text-xs border rounded" @click="abortStream">Stop</button>
			</div>
		</header>

		<!-- Messages -->
		<div class="flex-1 overflow-y-auto mt-4 pb-32" ref="scrollContainer">
			<div class="mx-auto w-full md:w-1/2 px-4">
				<ul v-if="messages.length > 0" class="mx-auto w-full">
					<li v-for="m in messages" :key="m.id" class="text-gray-700 mb-2 text-sm">
						<div class="flex rounded-lg p-1" :class="{ 'bg-red-50 border border-red-200': m.status === 'error' }">
							<div class="w-[28px] mr-2">
								<div v-if="m.role === 'user'" class="h-7 w-7 flex items-center justify-center text-xs border border-blue-200 bg-blue-100 rounded-full inline-block">
									Y
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
									<!-- Thinking dots when system is working but no visible progress -->
									<div v-if="shouldShowWorkingDots(m)">
										<div class="simple-dots"></div>
									</div>
									
									<!-- Render each completion block -->
									<div v-for="(block, blockIndex) in m.completion_blocks" :key="block.id" class="mb-5">
										<!-- Research blocks: put reasoning, tool execution, and assistant in thinking toggle -->
										<div v-if="isResearchBlock(block)">
											<!-- Thinking toggle for research blocks -->
											<div v-if="block.plan_decision?.reasoning || block.reasoning || block.tool_execution">
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
															<MDC :value="block.plan_decision?.reasoning || block.reasoning || ''" class="markdown-content" />
														</div>
														
														<!-- Tool execution details in thinking -->
														<div v-if="block.tool_execution" class="mb-4">
															<!-- Use specialized tool component if available -->
															<component 
																v-if="shouldUseToolComponent(block.tool_execution)"
																:is="getToolComponent(block.tool_execution.tool_name)"
																:tool-execution="block.tool_execution"
															/>


															<!-- Fallback to generic tool display -->
															<div v-else>
																<div class="text-xs text-gray-600 mb-1 font-medium">
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
														<div v-if="block.plan_decision?.assistant || block.content" class="markdown-wrapper">
															<MDC :value="block.plan_decision?.assistant || block.content || ''" class="markdown-content" />
														</div>
													</div>
												</Transition>
											</div>
										</div>
										
										<!-- Action blocks: render like before -->
										<div v-else>
											<!-- Block reasoning section -->
											<div v-if="block.plan_decision?.reasoning || block.reasoning">
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
														<MDC :value="block.plan_decision?.reasoning || block.reasoning || ''" class="markdown-content" />
													</div>
												</Transition>
											</div>
											
											<!-- Block content -->
											<div v-if="(block.plan_decision?.assistant || block.content) && !block.plan_decision?.final_answer" class="markdown-wrapper">
												<MDC :value="block.plan_decision?.assistant || block.content || ''" class="markdown-content" />
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
													:tool-execution="block.tool_execution"
												/>
												<!-- Fallback to generic expandable tool display -->
												<div v-else>
													<div class="text-xs text-gray-500 mb-1">
														<span class="cursor-pointer hover:text-gray-700" @click="toggleToolDetails(block.tool_execution.id)">
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
                                            <ToolWidgetPreview :tool-execution="block.tool_execution" />
                                        </div>
										</div>
									</div>
									
									<div v-if="m.status === 'stopped'" class="text-xs text-gray-500 mt-1">Stopped generating</div>
								</div>
							</div>
						</div>
					</li>
				</ul>
				<div v-else class="mx-auto w-full text-center text-gray-500 text-sm mt-24">
					Ask a question to get started.
				</div>
			</div>
		</div>

		<!-- Prompt box (bottom-fixed) -->
		<div class="fixed bottom-0 left-0 right-0 bg-white border-t border-gray-200">
			<div class="mx-auto w-full md:w-1/2">
				<PromptBoxExcel 
					:report_id="report_id"
					:excelData="{}"
					:selectedWidgetId="{ widgetId: null, stepId: null, widgetTitle: null }"
					:latestInProgressCompletion="isStreaming ? {} : undefined"
					:isStopping="false"
					@submitCompletion="onSubmitCompletion"
					@stopGeneration="abortStream"
				/>
			</div>
		</div>
	</div>

</template>

<script setup lang="ts">
import { ref, nextTick, onMounted, watch } from 'vue'
import PromptBoxExcel from '~/components/excel/PromptBoxExcel.vue'
import CreateDataModelTool from '~/components/tools/CreateDataModelTool.vue'
import ExecuteCodeTool from '~/components/tools/ExecuteCodeTool.vue'
import ToolWidgetPreview from '~/components/tools/ToolWidgetPreview.vue'

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
}

const route = useRoute()
const report_id = (route.params.id as string) || ''

const messages = ref<ChatMessage[]>([])
const promptText = ref<string>('')
const isStreaming = ref<boolean>(false)
let currentController: AbortController | null = null
const scrollContainer = ref<HTMLElement | null>(null)

// Toggle states
const collapsedReasoning = ref<Set<string>>(new Set())
const expandedToolDetails = ref<Set<string>>(new Set())

// Helper functions for block types
function isResearchBlock(block: CompletionBlock): boolean {
	return block.plan_decision?.plan_type === 'research' || (block.title?.includes('research') ?? false)
}

function hasCompletedContent(block: CompletionBlock): boolean {
	return !!(block.plan_decision?.assistant || block.content || block.status === 'completed')
}

function getToolComponent(toolName: string) {
	switch (toolName) {
		case 'create_data_model':
			return CreateDataModelTool
		case 'create_and_execute_code':
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
	
	// Only show for create_and_execute_code or execute_code tools with success status
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
	
	// CASE 1: No blocks yet - show dots (initial startup phase)
	if (!message.completion_blocks || message.completion_blocks.length === 0) {
		return true
	}
	
	// CASE 2: Blocks exist but no meaningful content yet (early startup)
	const hasAnyMeaningfulContent = message.completion_blocks.some(block => 
		block.plan_decision?.reasoning || 
		block.reasoning || 
		block.plan_decision?.assistant || 
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
		lastBlock.plan_decision?.assistant || 
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
		(block.plan_decision?.reasoning && !block.plan_decision?.assistant) ||
		(block.reasoning && !block.content)
	)
	
	// Show dots when:
	// 1. System is in progress AND
	// 2. No active tools/streaming AND
	// 3. Last block has content but system continues (preparing next block)
	return !hasActiveTools && !hasStreamingContent && (!!lastBlockHasContent && message.status === 'in_progress')
}

function getThoughtProcessLabel(block: CompletionBlock): string {
	// Calculate duration from started_at to completed_at if available
	if (block.started_at && block.completed_at) {
		const startTime = new Date(block.started_at).getTime()
		const endTime = new Date(block.completed_at).getTime()
		const durationMs = endTime - startTime
		const durationSeconds = (durationMs / 1000).toFixed(1)
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
  // Only autoscroll if user is already near the bottom to prevent jumpiness
  const maybeAutoScroll = () => {
    const container = scrollContainer.value as any
    if (!container) return
    const threshold = 120 // px from bottom to still autoscroll
    const distanceFromBottom = container.scrollHeight - (container.scrollTop + container.clientHeight)
    if (distanceFromBottom <= threshold) {
      // Force layout and scroll
      // eslint-disable-next-line @typescript-eslint/no-unused-expressions
      container.offsetHeight
      container.scrollTop = container.scrollHeight + 1000
    }
  }
  nextTick(() => setTimeout(maybeAutoScroll, 40))
}

async function handleStreamingEvent(eventType: string | null, payload: any, sysMessageIndex: number) {
	if (!eventType || sysMessageIndex === -1) return
	
	if (!messages.value[sysMessageIndex]) return

	const sysMessage = messages.value[sysMessageIndex]
	
	switch (eventType) {
		case 'completion.started':
			// Update system message status
			sysMessage.status = 'in_progress'
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
			// Update text content in a specific block (legacy)
			if (payload.block_id && payload.field && payload.text) {
				const block = sysMessage.completion_blocks?.find(b => b.id === payload.block_id)
				if (block) {
					if (payload.field === 'content') {
						block.content = payload.text
					} else if (payload.field === 'reasoning') {
						block.reasoning = payload.text
					}
				}
			}
			break

		case 'block.delta.token':
			// Handle individual token streaming for real-time typing effect
			if (payload.block_id && payload.field && payload.token) {
				const block = sysMessage.completion_blocks?.find(b => b.id === payload.block_id)
				if (block) {
					if (payload.field === 'content') {
						block.content = (block.content || '') + payload.token
					} else if (payload.field === 'reasoning') {
						block.reasoning = (block.reasoning || '') + payload.token
					}
				}
			}
			break

		case 'block.delta.text.complete':
			// Handle text completion finalization
			if (payload.block_id && payload.field && payload.is_final) {
				const block = sysMessage.completion_blocks?.find(b => b.id === payload.block_id)
				if (block) {
					// Mark field as complete - could be used for UI effects
					console.log(`Text streaming complete for ${payload.field} in block ${payload.block_id}`)
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

					// Progressive data model updates for create_data_model tool
					if (payload.tool_name === 'create_data_model' && payload.payload) {
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

					lastBlock.status = 'in_progress'
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
				const blockWithTool = sysMessage.completion_blocks?.find(b => 
					b.tool_execution?.tool_name === payload.tool_name
				)
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
			// Mark completion as finished
			sysMessage.status = 'success'
			break

		default:
			// Handle unknown events gracefully
			console.log('Unknown streaming event:', eventType, payload)
			break
	}
}

async function loadCompletions() {
	try {
		const { data } = await useMyFetch(`/reports/${report_id}/completions.v2`)
		const response = data.value as any
		const list = response?.completions || []
		messages.value = list.map((c: any) => ({
			id: c.id,
			role: c.role as ChatRole,
			status: c.status as ChatStatus,
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
			created_at: c.created_at
		}))
		await nextTick()
		scrollToBottom()
	} catch (e) {
		console.error('Error loading completions:', e)
	}
}

function abortStream() {
	if (currentController) {
		currentController.abort()
		currentController = null
	}
	isStreaming.value = false
}

function onSubmitCompletion(data: { text: string, mentions: any[] }) {
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

	// Start streaming
	if (isStreaming.value) abortStream()
	currentController = new AbortController()
	isStreaming.value = true

	const requestBody = {
		prompt: {
			content: text,
			mentions: data.mentions || []
		}
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
		const raw: any = await useMyFetch(`/reports/${report_id}/completions/stream`, options as any)
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
				console.log('Stream finished normally')
				break
			}
			
			// Check if stream was aborted
			if (currentController?.signal.aborted) {
				console.log('Stream was aborted')
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
							scrollToBottom()
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
					errorMessage = 'Stream was cancelled.'
					messages.value[idx] = { ...messages.value[idx], status: 'stopped' }
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

onMounted(async () => {
	await loadCompletions()
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

.reasoning-content { 
	opacity: 0.8; 
	transition: opacity 0.2s ease; 
}

.reasoning-content:hover { 
	opacity: 1; 
}
</style>



