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
		<div class="flex-1 overflow-y-auto mt-4 pb-24" ref="scrollContainer">
			<div class="mx-auto w-full md:w-1/2 px-4">
				<ul v-if="messages.length > 0" class="mx-auto w-full">
					<li v-for="m in messages" :key="m.id" class="text-gray-700 mb-3 text-sm">
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
									<!-- Thinking dots when empty -->
									<div v-if="!m.completion?.reasoning && !m.completion?.content && m.status === 'in_progress'">
										<div class="simple-dots"></div>
									</div>
									
									<!-- Collapsible reasoning section -->
									<div v-if="m.completion?.reasoning && m.completion.reasoning.length > 0">
										<div class="flex justify-between items-center cursor-pointer" @click="toggleReasoning(m.id)">
											<div class="font-medium text-sm text-gray-400 mb-2">
												<div class="flex items-center">
													<span class="text-xs" :class="{ 'transform rotate-90': isReasoningCollapsed(m.id) }">⌄</span>
													<span v-if="(m.completion?.content && m.completion?.content.length > 0) || m.status === 'stopped' || m.status === 'error'" class="ml-1">
														Thought Process
													</span>
													<span v-else class="ml-1">
														<div class="dots" />
													</span>
												</div>
											</div>
										</div>
										<div v-if="!isReasoningCollapsed(m.id)" class="text-sm mt-2 leading-relaxed text-gray-500 mb-3 reasoning-content markdown-wrapper">
											<MDC :value="m.completion.reasoning" class="markdown-content" />
										</div>
									</div>
									
									<!-- Content -->
									<div v-if="m.completion?.content" class="markdown-wrapper">
										<MDC :value="m.completion.content" class="markdown-content" />
									</div>
									
									<!-- Tool calls (minimalistic) -->
									<div v-if="m.tool_calls && m.tool_calls.length > 0" class="mt-2">
										<div v-for="tool in m.tool_calls" :key="tool.id" class="text-xs text-gray-500 mb-1">
											<span class="cursor-pointer hover:text-gray-700" @click="toggleToolDetails(tool.id)">
												{{ tool.tool_name }}{{ tool.tool_action ? ` → ${tool.tool_action}` : '' }} ({{ tool.status }})
											</span>
											<div v-if="isToolDetailsExpanded(tool.id)" class="ml-2 mt-1 text-xs text-gray-400 bg-gray-50 p-2 rounded">
												<div v-if="tool.result_summary">{{ tool.result_summary }}</div>
												<div v-if="tool.duration_ms">Duration: {{ tool.duration_ms }}ms</div>
												<div v-if="tool.created_widget_id" class="text-green-600">→ Widget: {{ tool.created_widget_id }}</div>
												<div v-if="tool.created_step_id" class="text-purple-600">→ Step: {{ tool.created_step_id }}</div>
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
		<div class="absolute bottom-4 left-0 right-0">
			<div class="mx-auto w-full md:w-1/2 px-4">
				<form @submit.prevent="onSubmit" class="bg-white border border-gray-200 rounded-lg p-2 shadow-sm">
					<textarea v-model="promptText" :disabled="isStreaming" rows="2" placeholder="Message..." class="w-full text-sm outline-none resize-none p-2 rounded-md"></textarea>
					<div class="flex items-center justify-between mt-2">
						<div class="text-xs text-gray-500" v-if="isStreaming">Streaming...</div>
						<div class="space-x-2">
							<button type="button" class="px-3 py-1 text-xs border rounded" @click="abortStream" v-if="isStreaming">Stop</button>
							<button type="submit" class="px-3 py-1 text-xs border rounded bg-gray-50 hover:bg-gray-100" :disabled="!promptText.trim() || isStreaming">Send</button>
						</div>
					</div>
				</form>
			</div>
		</div>
	</div>

</template>

<script setup lang="ts">
import { ref, nextTick, onMounted } from 'vue'

// Types
type ChatRole = 'user' | 'system'
type ChatStatus = 'in_progress' | 'success' | 'error' | 'stopped'

interface ToolCall {
	id: string
	tool_name: string
	tool_action?: string
	status: string
	result_summary?: string
	duration_ms?: number
	created_widget_id?: string
	created_step_id?: string
}

interface ChatMessage {
	id: string
	role: ChatRole
	status?: ChatStatus
	prompt?: { content: string }
	completion?: { content?: string; reasoning?: string }
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
	nextTick(() => {
		setTimeout(() => {
			const container = scrollContainer.value as any
			if (container) {
				// Force layout and scroll
				// eslint-disable-next-line @typescript-eslint/no-unused-expressions
				container.offsetHeight
				const h = (container as any).scrollHeight
				;(container as any).scrollTop = h + 1000
			}
		}, 50)
	})
}

async function loadCompletions() {
	try {
		const { data } = await useMyFetch(`/api/reports/${report_id}/completions.v2`)
		const response = data.value as any
		const list = response?.completions || []
		messages.value = list.map((c: any) => ({
			id: c.id,
			role: c.role as ChatRole,
			status: c.status as ChatStatus,
			prompt: c.prompt,
			completion: c.completion,
			tool_calls: c.tool_calls || c.completion_blocks?.filter((b: any) => b.tool_execution)?.map((b: any) => ({
				id: b.tool_execution.id,
				tool_name: b.tool_execution.tool_name,
				tool_action: b.tool_execution.tool_action,
				status: b.tool_execution.status,
				result_summary: b.tool_execution.result_summary,
				duration_ms: b.tool_execution.duration_ms,
				created_widget_id: b.tool_execution.created_widget_id,
				created_step_id: b.tool_execution.created_step_id
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

async function onSubmit() {
	const text = promptText.value.trim()
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
		completion: { content: '', reasoning: '' }
	}
	messages.value.push(sysMsg)
	promptText.value = ''
	scrollToBottom()

	// Start streaming
	if (isStreaming.value) abortStream()
	currentController = new AbortController()
	isStreaming.value = true

	const requestBody = {
		prompt: {
			content: text,
			mentions: [] as any[]
		}
	}

	try {
		const options: any = {
			method: 'POST',
			headers: { 'Content-Type': 'application/json' },
			body: JSON.stringify(requestBody),
			signal: currentController.signal,
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
			if (done) break
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
						await loadCompletions()
						return
					}
					try {
						const parsed = JSON.parse(dataStr)
						const payload = parsed.data ?? parsed
						const idx = ensureSys()
						if (idx !== -1) {
							const msg = messages.value[idx]
							const contentDelta = payload.content_delta || payload.delta || payload.text || ''
							const reasoningDelta = payload.reasoning_delta || ''
							if (contentDelta) msg.completion = { ...(msg.completion || {}), content: (msg.completion?.content || '') + contentDelta }
							if (reasoningDelta) msg.completion = { ...(msg.completion || {}), reasoning: (msg.completion?.reasoning || '') + reasoningDelta }
							messages.value.splice(idx, 1, { ...msg })
							await nextTick()
							scrollToBottom()
						}
					} catch (e) {
						// ignore non-JSON data lines
					}
				}
			}
		}
	} catch (err) {
		const idx = messages.value.findIndex(m => m.id === sysId)
		if (idx !== -1) {
			messages.value[idx] = { ...messages.value[idx], status: 'error' }
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
	@apply text-gray-700 leading-relaxed;
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

.reasoning-content { opacity: 0.75; transition: opacity 0.2s ease; }
.reasoning-content:hover { opacity: 1; }
</style>


