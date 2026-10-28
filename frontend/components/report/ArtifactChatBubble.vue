<template>
    <!-- Floating chat for the shared artifact page /r/{id}. Small on purpose:
         a bubble that opens a single-thread panel. The thread is private to
         this viewer (backend keeps it on a hidden per-viewer chat report).
         The transcript and composer are the shared compact-chat components
         (components/chat/*), the same ones embedded chat renders. -->
    <!-- `raised` clears the "Made with BOW" badge pinned bottom-end;
         `lift` (px) clears anything stacked under both, e.g. Intercom's launcher. -->
    <div class="fixed end-4 z-[1001] flex flex-col items-end" :style="{ bottom: `${bottomPx}px` }" data-testid="artifact-chat">
        <!-- Panel -->
        <div v-if="open"
            class="mb-3 w-[360px] max-w-[calc(100vw-2rem)] h-[480px] bg-white dark:bg-gray-900 border border-gray-200 dark:border-gray-700 rounded-xl shadow-xl flex flex-col overflow-hidden"
            :style="{ maxHeight: `calc(100dvh - ${bottomPx + 96}px)` }"
            data-testid="artifact-chat-panel">
            <!-- Header -->
            <div class="flex items-center justify-between px-3 py-2 border-b border-gray-100 dark:border-gray-800 flex-shrink-0">
                <div class="flex items-center gap-2 min-w-0">
                    <Icon name="heroicons:chat-bubble-left-right" class="w-4 h-4 text-blue-600 flex-shrink-0" />
                    <span class="text-xs font-medium text-gray-700 dark:text-gray-300 truncate">Ask about this dashboard</span>
                </div>
                <button @click="open = false" class="text-ink-muted hover:text-gray-600 dark:hover:text-gray-300">
                    <Icon name="heroicons:x-mark" class="w-4 h-4" />
                </button>
            </div>

            <!-- Sign-in required -->
            <div v-if="status && !status.available && status.reason === 'auth_required'"
                class="flex-1 flex flex-col items-center justify-center px-6 text-center" data-testid="chat-signin-prompt">
                <Icon name="heroicons:lock-closed" class="w-8 h-8 text-gray-300 mb-3" />
                <p class="text-xs text-gray-500 dark:text-gray-400 mb-4">Sign in to ask questions about this dashboard.</p>
                <a :href="`/users/sign-in?redirect=${chatBase}`"
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
                <CompactChatThread
                    :messages="messages"
                    :is-streaming="isStreaming"
                    :empty-text="status?.scope === 'data_only'
                        ? 'Ask about the data on this dashboard.'
                        : 'Ask about this dashboard — I can also run live queries to dig deeper.'" />

                <!-- Composer -->
                <div class="border-t border-gray-100 dark:border-gray-800 p-2 flex-shrink-0">
                    <CompactChatComposer ref="composerEl" :is-streaming="isStreaming" @send="send" />
                    <p v-if="status?.scope === 'agents' && status?.agents?.length" class="text-2xs text-gray-300 dark:text-gray-600 mt-1 ps-1 truncate">
                        Can query: {{ status.agents.map((a: any) => a.name).join(', ') }}
                    </p>
                    <p v-else-if="status?.scope === 'data_only'" class="text-2xs text-gray-300 dark:text-gray-600 mt-1 ps-1">
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
import { ref, computed, nextTick } from 'vue'
import CompactChatThread from '~/components/chat/CompactChatThread.vue'
import CompactChatComposer from '~/components/chat/CompactChatComposer.vue'
import {
    applyCompactEvent, newAssistantMessage, newUserMessage, readSseStream, sortByCreated,
} from '~/composables/useCompactChat'

// base: the page the chat belongs to -- the conversation's /r/{id}, or a
// dashboard's /d/{id}, whose chat API mirrors it.
const props = defineProps<{ reportId: string; base?: string; raised?: boolean; lift?: number }>()
const chatBase = computed(() => props.base || `/r/${props.reportId}`)

const bottomPx = computed(() => (props.raised ? 64 : 16) + (props.lift || 0))

const open = ref(false)
const status = ref<any>(null)
const messages = ref<any[]>([])
const isStreaming = ref(false)
const composerEl = ref<any>(null)
let initialized = false

const toggle = async () => {
    open.value = !open.value
    if (open.value && !initialized) {
        initialized = true
        await Promise.all([loadStatus(), loadHistory()])
    }
    if (open.value) nextTick(() => composerEl.value?.focus?.())
}

async function loadStatus() {
    try {
        const { data, error } = await useMyFetch(`${chatBase.value}/chat`)
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
        const { data, error } = await useMyFetch(`${chatBase.value}/chat/completions?limit=30`)
        if (error.value || !data.value) return
        messages.value = sortByCreated((data.value as any).completions || [])
    } catch { /* thread just renders empty */ }
}

async function send(content: string) {
    if (!content || isStreaming.value) return
    isStreaming.value = true

    // Optimistic user message + assistant placeholder the SSE events fill in.
    const userMsg = newUserMessage(content)
    const assistantMsg = reactive(newAssistantMessage())
    messages.value.push(userMsg, assistantMsg)

    try {
        const res: any = await useMyFetch(`${chatBase.value}/chat/completions`, {
            method: 'POST',
            stream: true,
            body: JSON.stringify({ prompt: { content }, stream: true }),
            headers: { 'Content-Type': 'application/json' },
        } as any)
        await readSseStream(res.data as Response, (evt) => applyCompactEvent(evt, assistantMsg, userMsg))
    } catch (e) {
        assistantMsg.status = 'error'
        console.error('Artifact chat stream failed:', e)
    } finally {
        isStreaming.value = false
        // Reconcile with the server's persisted thread (final block content,
        // real ids) — the stream is progressive, history is authoritative.
        await loadHistory()
    }
}
</script>
