<template>
    <!-- The compact transcript shared by the artifact-page bubble and embedded
         chat: user bubbles, assistant markdown blocks, one-line tool activity,
         an inline approval card when a run is paused on a tool, and a trailing
         spinner while the agent is working. Presentational: the owner feeds
         messages and handles sending/approvals. -->
    <div ref="scrollEl" class="flex-1 overflow-y-auto px-3 py-3 space-y-3" data-testid="chat-messages">
        <div v-if="messages.length === 0 && !isStreaming" class="text-center mt-8 px-4">
            <slot name="empty">
                <Icon name="heroicons:sparkles" class="w-6 h-6 text-gray-300 mx-auto mb-2" />
                <p class="text-xs text-ink-muted">{{ emptyText }}</p>
            </slot>
        </div>
        <template v-for="msg in messages" :key="msg.id">
            <!-- User message -->
            <div v-if="msg.role === 'user'" class="flex justify-end">
                <!-- dir=auto: each message follows its own script, so an English
                     question inside a Hebrew (RTL) chat still reads left to right. -->
                <div class="text-white text-xs rounded-2xl rounded-br-md px-3 py-2 max-w-[85%] whitespace-pre-wrap break-words compact-user-bubble"
                    :style="accentStyle" dir="auto" data-testid="chat-user-message">
                    {{ msg.prompt?.content }}
                </div>
            </div>
            <!-- Assistant message: tool activity lines + markdown content blocks -->
            <div v-else class="flex flex-col gap-1.5 items-start" :data-completion-id="msg.id" data-testid="chat-assistant-message">
                <!-- A block can carry BOTH the agent's narration and a tool call,
                     so content and the tool line render together, in that order —
                     same as the report page. -->
                <template v-for="block in orderedCompactBlocks(msg)" :key="block.id">
                    <!-- cache-key per block: MDC keys its parse cache by a hash of
                         the INITIAL value, and two streamed answers that open with
                         the same chunk would share one entry and overwrite each
                         other's rendered text (seen in the embedded-chat loop). -->
                    <div v-if="block.content" class="bg-gray-100 dark:bg-gray-800 rounded-2xl rounded-bl-md px-3 py-2 max-w-[95%] text-xs text-gray-800 dark:text-gray-200 chat-md" dir="auto">
                        <MDC :value="block.content" :cache-key="`compact-chat-${msg.id}-${block.id}`" />
                    </div>
                    <!-- Tool activity: the collapsed-header look of the report page's
                         tool components, never expandable. The answer itself carries
                         the data, so tool results need no UI of their own here. -->
                    <div v-if="block.tool_execution" class="flex items-center text-2xs text-gray-500 dark:text-gray-400 ps-1 min-w-0 max-w-full">
                        <Spinner v-if="block.status === 'in_progress'" class="w-3 h-3 me-1.5 text-ink-muted flex-shrink-0" />
                        <Icon v-else-if="block.status === 'error'" name="heroicons-exclamation-circle" class="w-3 h-3 me-1.5 text-amber-500 flex-shrink-0" />
                        <Icon v-else name="heroicons-check" class="w-3 h-3 me-1.5 text-green-500 flex-shrink-0" />
                        <span class="flex-shrink-0" :class="block.status === 'in_progress' ? 'tool-shimmer' : 'text-gray-700 dark:text-gray-300'">
                            {{ compactToolLabel(block) }}
                        </span>
                        <span v-if="compactToolDetail(block)" class="ms-1 text-ink-muted truncate">— {{ compactToolDetail(block) }}</span>
                        <span v-if="compactToolDuration(block)" class="ms-1.5 text-ink-muted flex-shrink-0">{{ compactToolDuration(block) }}</span>
                    </div>
                    <!-- Approval ('ask' policy): the run is paused on this tool call. -->
                    <div v-if="canApprove && pendingApproval(block) && !answered.has(pendingApproval(block).confirmation_id)"
                        class="ms-1 border-s-2 border-amber-300/70 dark:border-amber-600/50 ps-2.5 py-0.5 space-y-1.5 max-w-[95%]"
                        data-testid="chat-approval-card">
                        <div class="text-2xs text-gray-600 dark:text-gray-400">
                            {{ approvalText }}
                            <span class="font-mono">{{ pendingApproval(block).tool_name || block.tool_execution?.tool_name }}</span>
                        </div>
                        <pre v-if="approvalArgs(block)" class="max-h-24 overflow-auto rounded bg-gray-50 dark:bg-gray-900 text-2xs leading-tight text-gray-600 dark:text-gray-400 p-2 m-0 whitespace-pre-wrap break-words font-mono">{{ approvalArgs(block) }}</pre>
                        <div class="flex items-center gap-1.5 text-xs">
                            <button type="button" class="px-2 py-0.5 rounded font-medium text-white disabled:opacity-50" :style="accentStyle"
                                :disabled="responding" data-testid="chat-approve" @click="respond(msg, block, true)">{{ allowText }}</button>
                            <button type="button" class="px-2 py-0.5 rounded text-gray-500 dark:text-gray-400 hover:bg-gray-100 dark:hover:bg-gray-800 disabled:opacity-50"
                                :disabled="responding" data-testid="chat-deny" @click="respond(msg, block, false)">{{ denyText }}</button>
                        </div>
                    </div>
                    <div v-else-if="!block.content && !block.tool_execution && block.title" class="flex items-center gap-1.5 text-2xs text-ink-muted ps-1">
                        <Spinner v-if="block.status === 'in_progress'" class="w-3 h-3" />
                        <Icon v-else :name="block.status === 'error' ? 'heroicons:exclamation-triangle' : 'heroicons:check'" class="w-3 h-3" />
                        <span class="truncate max-w-[280px]">{{ block.title }}</span>
                    </div>
                </template>
                <div v-if="msg.status === 'error' && !messageHasContent(msg)" class="text-2xs text-red-400 ps-1" data-testid="chat-error-line">
                    {{ errorText }}
                </div>
                <div v-else-if="msg.status === 'stopped' && !messageHasContent(msg)" class="text-2xs text-ink-muted ps-1">
                    {{ stoppedText }}
                </div>
            </div>
        </template>
        <div v-if="isStreaming && !streamHasVisibleProgress" class="flex items-center ps-1" data-testid="chat-wait-spinner">
            <Spinner class="w-4 h-4" />
        </div>
    </div>
</template>

<script setup lang="ts">
import { ref, computed, nextTick, watch } from 'vue'
import Spinner from '~/components/Spinner.vue'
import {
    orderedCompactBlocks, compactToolLabel, compactToolDetail, compactToolDuration,
    messageHasContent, pendingApproval,
} from '~/composables/useCompactChat'

const props = withDefaults(defineProps<{
    messages: any[]
    isStreaming?: boolean
    emptyText?: string
    errorText?: string
    stoppedText?: string
    approvalText?: string
    allowText?: string
    denyText?: string
    accentColor?: string
    // Set when the owner can resolve tool approvals (an `approve` listener).
    canApprove?: boolean
}>(), {
    isStreaming: false,
    emptyText: 'Ask a question.',
    errorText: 'Something went wrong. Try again.',
    stoppedText: 'Stopped.',
    approvalText: 'Allow this tool call?',
    allowText: 'Allow',
    denyText: 'Deny',
    accentColor: '',
    canApprove: false,
})

const emit = defineEmits<{
    (e: 'approve', payload: { message: any; block: any; confirmationId: string; approved: boolean }): void
}>()

const scrollEl = ref<HTMLElement | null>(null)
const responding = ref(false)
const answered = ref(new Set<string>())

const accentStyle = computed(() => props.accentColor ? { backgroundColor: props.accentColor } : {})

// The trailing spinner shows whenever the agent is working with nothing
// visibly in progress: before the first visible block, and between rounds.
const streamHasVisibleProgress = computed(() => {
    const last = props.messages[props.messages.length - 1]
    if (!last || last.role === 'user') return false
    return orderedCompactBlocks(last).some((b: any) => b.status === 'in_progress')
})

function approvalArgs(block: any): string {
    const a = pendingApproval(block)?.arguments
    if (!a || !Object.keys(a).length) return ''
    try { return JSON.stringify(a, null, 2) } catch { return String(a) }
}

function respond(message: any, block: any, approved: boolean) {
    const c = pendingApproval(block)
    if (!c) return
    responding.value = true
    answered.value.add(c.confirmation_id)
    emit('approve', { message, block, confirmationId: c.confirmation_id, approved })
    setTimeout(() => { responding.value = false }, 400)
}

function scrollToBottom() {
    nextTick(() => {
        if (scrollEl.value) scrollEl.value.scrollTop = scrollEl.value.scrollHeight
    })
}

// Any change to the transcript (new message, streamed token, block upsert)
// keeps the latest content in view.
watch(() => props.messages, scrollToBottom, { deep: true })
watch(() => props.isStreaming, scrollToBottom)

defineExpose({ scrollToBottom })
</script>

<style scoped>
.chat-md :deep(p) { margin: 0 0 0.4rem 0; }
.chat-md :deep(p:last-child) { margin-bottom: 0; }
.chat-md :deep(ul), .chat-md :deep(ol) { margin: 0.2rem 0 0.4rem 1rem; list-style: disc; }
.chat-md :deep(ol) { list-style: decimal; }
.chat-md :deep(table) { font-size: 11px; border-collapse: collapse; margin: 0.3rem 0; display: block; max-width: 100%; overflow-x: auto; }
.chat-md :deep(th), .chat-md :deep(td) { border: 1px solid rgb(209 213 219 / 0.6); padding: 2px 6px; }
.chat-md :deep(code) { background: rgb(0 0 0 / 0.06); border-radius: 3px; padding: 0 3px; font-size: 11px; }
.chat-md :deep(pre) { overflow-x: auto; background: rgb(0 0 0 / 0.05); border-radius: 6px; padding: 6px; margin: 0.3rem 0; }
.chat-md :deep(a) { text-decoration: underline; }
.compact-user-bubble { background-color: rgb(37 99 235); }

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
