<template>
    <!-- Floating chat for the shared artifact page /r/{id}. Small on purpose:
         a bubble that opens a single-thread panel. The thread is private to
         this viewer (backend keeps it on a hidden per-viewer chat report). -->
    <!-- `raised` clears the "Made with Bag of words" badge pinned bottom-end. -->
    <div :class="['fixed end-4 z-[1001] flex flex-col items-end', raised ? 'bottom-16' : 'bottom-4']" data-testid="artifact-chat">
        <!-- Panel -->
        <div v-if="open"
            class="mb-3 w-[360px] max-w-[calc(100vw-2rem)] h-[480px] max-h-[calc(100dvh-7rem)] bg-white dark:bg-gray-900 border border-gray-200 dark:border-gray-700 rounded-xl shadow-xl flex flex-col overflow-hidden"
            data-testid="artifact-chat-panel">
            <!-- Header -->
            <div class="flex items-center justify-between px-3 py-2 border-b border-gray-100 dark:border-gray-800 flex-shrink-0">
                <div class="flex items-center gap-2 min-w-0">
                    <Icon name="heroicons:chat-bubble-left-right" class="w-4 h-4 text-blue-600 flex-shrink-0" />
                    <span class="text-xs font-medium text-gray-700 dark:text-gray-300 truncate">Ask about this dashboard</span>
                </div>
                <button @click="open = false" class="text-gray-400 hover:text-gray-600 dark:hover:text-gray-300">
                    <Icon name="heroicons:x-mark" class="w-4 h-4" />
                </button>
            </div>

            <!-- Sign-in required -->
            <div v-if="status && !status.available && status.reason === 'auth_required'"
                class="flex-1 flex flex-col items-center justify-center px-6 text-center" data-testid="chat-signin-prompt">
                <Icon name="heroicons:lock-closed" class="w-8 h-8 text-gray-300 mb-3" />
                <p class="text-xs text-gray-500 dark:text-gray-400 mb-4">Sign in to ask questions about this dashboard.</p>
                <a :href="`/users/sign-in?redirect=/r/${reportId}`"
                    class="px-3 py-1.5 text-xs font-medium text-white bg-blue-600 rounded-lg hover:bg-blue-700">Sign in</a>
            </div>

            <!-- Not available (non-member etc.) -->
            <div v-else-if="status && !status.available"
                class="flex-1 flex flex-col items-center justify-center px-6 text-center" data-testid="chat-unavailable">
                <Icon name="heroicons:no-symbol" class="w-8 h-8 text-gray-300 mb-3" />
                <p class="text-xs text-gray-500 dark:text-gray-400">Chat is available to members of this workspace only.</p>
            </div>

            <!-- Thread -->
            <template v-else>
                <div ref="scrollEl" class="flex-1 overflow-y-auto px-3 py-3 space-y-3" data-testid="chat-messages">
                    <div v-if="messages.length === 0 && !isStreaming" class="text-center mt-10 px-4">
                        <Icon name="heroicons:sparkles" class="w-6 h-6 text-gray-300 mx-auto mb-2" />
                        <p class="text-xs text-gray-400">
                            {{ status?.scope === 'data_only'
                                ? "Ask about the data on this dashboard."
                                : "Ask about this dashboard — I can also run live queries to dig deeper." }}
                        </p>
                    </div>
                    <template v-for="msg in messages" :key="msg.id">
                        <!-- User message -->
                        <div v-if="msg.role === 'user'" class="flex justify-end">
                            <div class="bg-blue-600 text-white text-xs rounded-2xl rounded-br-md px-3 py-2 max-w-[85%] whitespace-pre-wrap break-words">
                                {{ msg.prompt?.content }}
                            </div>
                        </div>
                        <!-- Assistant message: tool activity lines + markdown content blocks -->
                        <div v-else class="flex flex-col gap-1.5 items-start" :data-completion-id="msg.id">
                            <!-- A block can carry BOTH the agent's narration and a tool
                                 call ("I'll search the sales files" + search_files), so
                                 content and the tool line render together, in that order —
                                 same as the report page. -->
                            <template v-for="block in orderedBlocks(msg)" :key="block.id">
                                <div v-if="block.content" class="bg-gray-100 dark:bg-gray-800 rounded-2xl rounded-bl-md px-3 py-2 max-w-[95%] text-xs text-gray-800 dark:text-gray-200 chat-md">
                                    <MDC :value="block.content" />
                                </div>
                                <!-- Tool activity: the collapsed-header look of the report
                                     page's tool components ("Created data — Revenue by
                                     region"), but never expandable. This is a text-only
                                     surface (Teams-style) — the answer itself carries the
                                     data, so tool results need no UI of their own here. -->
                                <div v-if="block.tool_execution" class="flex items-center text-[11px] text-gray-500 dark:text-gray-400 ps-1 min-w-0 max-w-full">
                                    <Spinner v-if="block.status === 'in_progress'" class="w-3 h-3 me-1.5 text-gray-400 flex-shrink-0" />
                                    <Icon v-else-if="block.status === 'error'" name="heroicons-exclamation-circle" class="w-3 h-3 me-1.5 text-amber-500 flex-shrink-0" />
                                    <Icon v-else name="heroicons-check" class="w-3 h-3 me-1.5 text-green-500 flex-shrink-0" />
                                    <span class="flex-shrink-0" :class="block.status === 'in_progress' ? 'tool-shimmer' : 'text-gray-700 dark:text-gray-300'">
                                        {{ toolLabel(block) }}
                                    </span>
                                    <span v-if="toolDetail(block)" class="ms-1 text-gray-400 truncate">— {{ toolDetail(block) }}</span>
                                    <span v-if="toolDuration(block)" class="ms-1.5 text-gray-400 flex-shrink-0">{{ toolDuration(block) }}</span>
                                </div>
                                <div v-else-if="!block.content && block.title" class="flex items-center gap-1.5 text-[11px] text-gray-400 ps-1">
                                    <Spinner v-if="block.status === 'in_progress'" class="w-3 h-3" />
                                    <Icon v-else :name="block.status === 'error' ? 'heroicons:exclamation-triangle' : 'heroicons:check'" class="w-3 h-3" />
                                    <span class="truncate max-w-[280px]">{{ block.title }}</span>
                                </div>
                            </template>
                            <div v-if="msg.status === 'error' && !hasContent(msg)" class="text-[11px] text-red-400 ps-1">
                                Something went wrong. Try again.
                            </div>
                        </div>
                    </template>
                    <div v-if="isStreaming && !streamHasBlocks" class="flex items-center ps-1">
                        <Spinner class="w-4 h-4" />
                    </div>
                </div>

                <!-- Composer -->
                <div class="border-t border-gray-100 dark:border-gray-800 p-2 flex-shrink-0">
                    <div class="flex items-end gap-1.5">
                        <textarea ref="inputEl" v-model="draft" rows="1"
                            class="flex-1 resize-none text-xs border border-gray-200 dark:border-gray-700 rounded-lg px-2.5 py-2 bg-white dark:bg-gray-900 text-gray-800 dark:text-gray-200 focus:outline-none focus:ring-1 focus:ring-blue-500 max-h-24"
                            :placeholder="isStreaming ? 'Answering...' : 'Ask a question...'"
                            :disabled="isStreaming"
                            data-testid="chat-input"
                            @keydown.enter.exact.prevent="send" />
                        <button @click="send" :disabled="isStreaming || !draft.trim()"
                            class="flex-shrink-0 w-8 h-8 rounded-full bg-blue-600 text-white flex items-center justify-center hover:bg-blue-700 disabled:opacity-40"
                            data-testid="chat-send">
                            <Icon name="heroicons:paper-airplane" class="w-3.5 h-3.5" />
                        </button>
                    </div>
                    <p v-if="status?.scope === 'agents' && status?.agents?.length" class="text-[10px] text-gray-300 dark:text-gray-600 mt-1 ps-1 truncate">
                        Can query: {{ status.agents.map((a: any) => a.name).join(', ') }}
                    </p>
                    <p v-else-if="status?.scope === 'data_only'" class="text-[10px] text-gray-300 dark:text-gray-600 mt-1 ps-1">
                        Answers from dashboard data only
                    </p>
                </div>
            </template>
        </div>

        <!-- Bubble button -->
        <button @click="toggle"
            class="w-12 h-12 rounded-full bg-blue-600 text-white shadow-lg hover:bg-blue-700 flex items-center justify-center transition-transform hover:scale-105"
            data-testid="artifact-chat-bubble" aria-label="Chat about this dashboard">
            <Icon :name="open ? 'heroicons:chevron-down' : 'heroicons:chat-bubble-oval-left-ellipsis'" class="w-6 h-6" />
        </button>
    </div>
</template>

<script setup lang="ts">
import { ref, nextTick } from 'vue'
import Spinner from '~/components/Spinner.vue'

const props = defineProps<{ reportId: string; raised?: boolean }>()

const open = ref(false)
const status = ref<any>(null)
const messages = ref<any[]>([])
const draft = ref('')
const isStreaming = ref(false)
const streamHasBlocks = ref(false)
const scrollEl = ref<HTMLElement | null>(null)
const inputEl = ref<HTMLTextAreaElement | null>(null)
let initialized = false

const toggle = async () => {
    open.value = !open.value
    if (open.value && !initialized) {
        initialized = true
        await Promise.all([loadStatus(), loadHistory()])
    }
    if (open.value) nextTick(() => inputEl.value?.focus())
}

async function loadStatus() {
    try {
        const { data, error } = await useMyFetch(`/r/${props.reportId}/chat`)
        if (error.value) {
            status.value = { available: false, reason: 'error' }
            return
        }
        status.value = data.value
    } catch {
        status.value = { available: false, reason: 'error' }
    }
}

async function loadHistory() {
    try {
        const { data, error } = await useMyFetch(`/r/${props.reportId}/chat/completions?limit=30`)
        if (error.value || !data.value) return
        const list = (data.value as any).completions || []
        messages.value = list
            .slice()
            .sort((a: any, b: any) => new Date(a.created_at).getTime() - new Date(b.created_at).getTime())
        scrollToBottom()
    } catch { /* thread just renders empty */ }
}

// Verb labels matching the report page's tool headers, for the tools in the
// artifact-chat allowlist (ARTIFACT_CHAT_TOOL_ALLOWLIST, backend). Unknown
// tools fall back to a prettified tool_name so a new tool never renders blank.
const TOOL_LABELS: Record<string, { running: string; done: string }> = {
    create_data: { running: 'Creating data', done: 'Created data' },
    describe_tables: { running: 'Describing tables', done: 'Described tables' },
    describe_entity: { running: 'Reading entity', done: 'Read entity' },
    read_query: { running: 'Reading query', done: 'Read query' },
    read_artifact: { running: 'Reading dashboard', done: 'Read dashboard' },
    inspect_data: { running: 'Inspecting data', done: 'Inspected data' },
    search_files: { running: 'Searching files', done: 'Searched files' },
    grep_files: { running: 'Searching files', done: 'Searched files' },
    list_files: { running: 'Listing files', done: 'Listed files' },
    read_file: { running: 'Reading file', done: 'Read file' },
    clarify: { running: 'Asking a question', done: 'Asked a question' },
}

function toolLabel(block: any): string {
    const name = block.tool_execution?.tool_name || ''
    const entry = TOOL_LABELS[name]
    if (entry) return block.status === 'in_progress' ? entry.running : entry.done
    return name.replace(/_/g, ' ').replace(/^./, (c: string) => c.toUpperCase())
}

// The tool's own human title ("Finding sales.csv", "Revenue by region").
// Deliberately NOT block.title — that is planner bookkeeping
// ("Planning (research) → search_files") and never reads well here.
function toolDetail(block: any): string {
    const detail = block.tool_execution?.arguments_json?.title || ''
    return detail && detail.toLowerCase() !== toolLabel(block).toLowerCase() ? detail : ''
}

// Sub-second calls are the norm; a "0.1s" on every line is just noise.
function toolDuration(block: any): string {
    const ms = block.tool_execution?.duration_ms ?? block.duration_ms
    if (!ms || ms < 1000 || block.status === 'in_progress') return ''
    return `${(ms / 1000).toFixed(1)}s`
}

function orderedBlocks(msg: any) {
    return (msg.completion_blocks || [])
        .slice()
        // Planner bookkeeping blocks ("Planning (action)" etc.) are noise in a
        // compact bubble — the spinner placeholder already covers "working".
        .filter((b: any) => b.content || b.tool_execution || (b.title && !/^planning/i.test(b.title)))
        .sort((a: any, b: any) => (a.block_index ?? 0) - (b.block_index ?? 0))
}

function hasContent(msg: any) {
    return (msg.completion_blocks || []).some((b: any) => b.content)
}

function scrollToBottom() {
    nextTick(() => {
        if (scrollEl.value) scrollEl.value.scrollTop = scrollEl.value.scrollHeight
    })
}

async function send() {
    const content = draft.value.trim()
    if (!content || isStreaming.value) return
    draft.value = ''
    isStreaming.value = true
    streamHasBlocks.value = false

    // Optimistic user message + assistant placeholder the SSE events fill in.
    const userMsg = { id: `local-user-${Date.now()}`, role: 'user', status: 'success', prompt: { content }, created_at: new Date().toISOString() }
    const assistantMsg = ref<any>({ id: `local-sys-${Date.now()}`, role: 'system', status: 'in_progress', completion_blocks: [], created_at: new Date().toISOString() })
    messages.value.push(userMsg, assistantMsg.value)
    scrollToBottom()

    try {
        const res: any = await useMyFetch(`/r/${props.reportId}/chat/completions`, {
            method: 'POST',
            stream: true,
            body: JSON.stringify({ prompt: { content }, stream: true }),
            headers: { 'Content-Type': 'application/json' },
        } as any)
        const response: Response = res.data
        const reader = response.body!.getReader()
        const decoder = new TextDecoder()
        let buffer = ''
        while (true) {
            const { done, value } = await reader.read()
            if (done) break
            buffer += decoder.decode(value, { stream: true })
            // SSE frames are separated by a blank line.
            const frames = buffer.split('\n\n')
            buffer = frames.pop() || ''
            for (const frame of frames) {
                const dataLine = frame.split('\n').find(l => l.startsWith('data: '))
                if (!dataLine) continue
                let evt: any
                try { evt = JSON.parse(dataLine.slice(6)) } catch { continue }
                handleEvent(evt, assistantMsg.value)
            }
        }
    } catch (e) {
        assistantMsg.value.status = 'error'
        console.error('Artifact chat stream failed:', e)
    } finally {
        isStreaming.value = false
        // Reconcile with the server's persisted thread (final block content,
        // real ids) — the stream is progressive, history is authoritative.
        await loadHistory()
    }
}

// The trailing spinner shows whenever the agent is working with nothing
// visibly in progress: before the first visible block, and between rounds
// (a tool finished, the next planner call hasn't produced anything yet).
// Planner bookkeeping blocks are invisible (orderedBlocks filters them), so
// they must not count as "in progress" here either.
function refreshWaitSpinner(assistantMsg: any) {
    const visible = orderedBlocks(assistantMsg)
    streamHasBlocks.value = visible.some((b: any) => b.status === 'in_progress')
}

function handleEvent(evt: any, assistantMsg: any) {
    const kind = evt.event
    if (kind === 'block.upsert' && evt.data?.block) {
        const block = evt.data.block
        const blocks = assistantMsg.completion_blocks
        const idx = blocks.findIndex((b: any) => b.id === block.id)
        if (idx >= 0) blocks[idx] = block
        else blocks.push(block)
        refreshWaitSpinner(assistantMsg)
        scrollToBottom()
    } else if (kind === 'block.delta.text' && evt.data?.block_id) {
        // Full overwrite snapshot for a block's text field.
        const b = assistantMsg.completion_blocks.find((x: any) => x.id === evt.data.block_id)
        if (b && evt.data.field === 'content' && evt.data.text) {
            b.content = evt.data.text
            refreshWaitSpinner(assistantMsg)
            scrollToBottom()
        }
    } else if (kind === 'block.delta.token' && evt.data?.block_id) {
        // Per-token append for the typing effect.
        const b = assistantMsg.completion_blocks.find((x: any) => x.id === evt.data.block_id)
        if (b && evt.data.field === 'content' && evt.data.token) {
            b.content = (b.content || '') + String(evt.data.token)
            refreshWaitSpinner(assistantMsg)
            scrollToBottom()
        }
    } else if (kind === 'completion.finished') {
        assistantMsg.status = evt.data?.status || 'success'
    } else if (kind === 'completion.error') {
        assistantMsg.status = 'error'
    }
}
</script>

<style scoped>
.chat-md :deep(p) { margin: 0 0 0.4rem 0; }
.chat-md :deep(p:last-child) { margin-bottom: 0; }
.chat-md :deep(ul), .chat-md :deep(ol) { margin: 0.2rem 0 0.4rem 1rem; list-style: disc; }
.chat-md :deep(ol) { list-style: decimal; }
.chat-md :deep(table) { font-size: 11px; border-collapse: collapse; margin: 0.3rem 0; }
.chat-md :deep(th), .chat-md :deep(td) { border: 1px solid rgb(209 213 219 / 0.6); padding: 2px 6px; }
.chat-md :deep(code) { background: rgb(0 0 0 / 0.06); border-radius: 3px; padding: 0 3px; font-size: 11px; }
.chat-md :deep(pre) { overflow-x: auto; background: rgb(0 0 0 / 0.05); border-radius: 6px; padding: 6px; margin: 0.3rem 0; }

/* Same shimmer as the report page's tool headers (see CreateDataTool.vue). */
.tool-shimmer {
    background: linear-gradient(90deg, #888 0%, #999 25%, #ccc 50%, #999 75%, #888 100%);
    background-size: 200% 100%;
    -webkit-background-clip: text;
    background-clip: text;
    color: transparent;
    animation: chat-shimmer 2s linear infinite;
}
@keyframes chat-shimmer {
    0% { background-position: 200% 0; }
    100% { background-position: -200% 0; }
}
</style>
