<template>
    <div class="h-screen w-full bg-white flex flex-col overflow-hidden">
        <!-- Loading state -->
        <div v-if="isLoading" class="flex-1 flex items-center justify-center text-gray-500">
            <Spinner class="w-5 h-5 mr-2" />
            <span class="text-sm">Loading conversation…</span>
        </div>

        <!-- Error state -->
        <div v-else-if="error" class="flex-1 flex items-center justify-center">
            <div class="text-center">
                <Icon name="heroicons:exclamation-circle" class="w-12 h-12 text-gray-300 mx-auto mb-4" />
                <h1 class="text-lg font-medium text-gray-700">Conversation not found</h1>
                <p class="text-sm text-gray-400 mt-1">This conversation may have been unshared or doesn't exist.</p>
            </div>
        </div>

        <!-- Conversation content -->
        <template v-else>
            <!-- Minimal header -->
            <header class="flex-none border-b border-gray-100 py-5 px-4 bg-white z-10">
                <div class="max-w-2xl mx-auto">
                    <h1 class="text-base font-medium text-gray-900">{{ conversation.title || 'Untitled' }}</h1>
                    <p class="text-xs text-gray-400 mt-1">by {{ conversation.user_name }} · {{ formatDate(conversation.created_at) }}</p>
                </div>
            </header>

            <!-- Messages -->
            <div 
                ref="messagesContainer"
                class="flex-1 overflow-y-auto py-6 px-4"
                @scroll="handleScroll"
            >
                <!-- Loading more indicator -->
                <div v-if="isLoadingMore" class="flex justify-center py-3 mb-4">
                    <Spinner class="w-4 h-4 text-gray-400" />
                    <span class="text-xs text-gray-400 ml-2">Loading older messages…</span>
                </div>
                
                <!-- Load more button (shown when there's more to load) -->
                <div v-else-if="hasMore" class="flex justify-center py-3 mb-4">
                    <button 
                        @click="loadPreviousCompletions"
                        class="text-xs text-gray-400 hover:text-gray-600 transition-colors"
                    >
                        ↑ Load older messages
                    </button>
                </div>
                
                <ul class="max-w-2xl mx-auto space-y-4">
                    <li v-for="m in conversation.completions" :key="m.id" class="text-gray-700 text-sm">
                        <div class="flex rounded-lg p-1" :class="m.role === 'user' ? 'justify-end' : 'justify-start'">
                            <!-- User message (right-aligned bubble) -->
                            <template v-if="m.role === 'user'">
                                <div class="flex items-start gap-2 max-w-xl w-full mb-4">
                                    <div class="flex-1 flex justify-end">
                                        <div class="inline-block rounded-xl px-3 py-2 bg-gray-50 text-gray-900 text-left">
                                            <div v-if="m.prompt?.content" class="pt-1 markdown-wrapper">
                                                <MDC :value="m.prompt.content" class="markdown-content" />
                                            </div>
                                        </div>
                                    </div>
                                    <!-- User avatar -->
                                    <div class="w-[28px] flex-shrink-0">
                                        <div class="h-7 w-7 uppercase flex items-center justify-center text-xs border border-blue-200 bg-blue-100 rounded-full">
                                            {{ conversation.user_name?.charAt(0) || '?' }}
                                        </div>
                                    </div>
                                </div>
                            </template>

                            <!-- System message (left-aligned) -->
                            <template v-else>
                                <div class="w-[28px] mr-2 flex-shrink-0">
                                    <div class="h-7 w-7 flex font-bold items-center justify-center text-xs rounded-lg bg-contain bg-center bg-no-repeat" style="background-image: url('/assets/logo-128.png')">
                                    </div>
                                </div>
                                <div class="w-full ml-4 max-w-2xl">
                                    <div>
                                        <!-- Render each completion block -->
                                        <div v-for="block in m.completion_blocks" :key="block.id">
                                            <!-- 1. Thinking box (reasoning) -->
                                            <div v-if="block.plan_decision?.reasoning || block.reasoning || block.status === 'stopped'" class="thinking-box">
                                                <div class="thinking-header" @click="toggleReasoning(block.id)">
                                                    <Icon :name="isReasoningCollapsed(block.id) ? 'heroicons-chevron-right' : 'heroicons-chevron-down'" class="w-4 h-4 text-gray-400" />
                                                    <span v-if="hasCompletedContent(block) || block.tool_execution" class="ml-1">
                                                        {{ getThoughtProcessLabel(block) }}
                                                    </span>
                                                    <span v-else class="ml-1">
                                                        <div class="dots" />
                                                    </span>
                                                </div>
                                                <Transition name="fade">
                                                    <div v-if="!isReasoningCollapsed(block.id)" class="thinking-content">
                                                        <template v-if="block.plan_decision?.reasoning || block.reasoning">
                                                            <MDC :value="block.plan_decision?.reasoning || block.reasoning || ''" class="markdown-content" />
                                                        </template>
                                                        <template v-else-if="block.status === 'stopped'">
                                                            <div class="text-gray-400 italic">Generation was stopped before completion.</div>
                                                        </template>
                                                    </div>
                                                </Transition>
                                            </div>

                                            <!-- 2. Block content - assistant message -->
                                            <div v-if="(block.content || block.plan_decision?.assistant) && !block.plan_decision?.final_answer && block.status !== 'error'" class="block-content markdown-wrapper">
                                                <MDC :value="block.content || block.plan_decision?.assistant || ''" class="markdown-content" />
                                            </div>

                                            <!-- 3. Tool execution -->
                                            <div v-if="block.tool_execution" class="tool-execution-container">
                                                <component 
                                                    v-if="shouldUseToolComponent(block.tool_execution)"
                                                    :is="getToolComponent(block.tool_execution.tool_name)"
                                                    :key="`${block.id}:${block.tool_execution.id || 'noid'}`"
                                                    :tool-execution="block.tool_execution"
                                                    :readonly="true"
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
                                                        </div>
                                                    </div>
                                                </div>
                                            </div>

                                            <!-- Tool widget preview (readonly) -->
                                            <div class="mt-1" v-if="shouldShowToolWidgetPreview(block.tool_execution) && block.tool_execution">
                                                <ToolWidgetPreview :tool-execution="block.tool_execution" :readonly="true" />
                                            </div>

                                            <!-- 4. Final answer -->
                                            <div v-if="block.plan_decision?.analysis_complete && (block.plan_decision?.final_answer || (!block.content && !block.tool_execution))" class="mt-2 markdown-wrapper">
                                                <MDC :value="block.plan_decision?.final_answer || block.plan_decision?.assistant || block.content || ''" class="markdown-content" />
                                            </div>
                                        </div>
                                    </div>

                                    <!-- Status messages -->
                                    <div v-if="m.status === 'stopped'" class="text-xs text-gray-500 mt-2 italic">
                                        <Icon name="heroicons-stop-circle" class="w-4 h-4 inline mr-1" />
                                        Generation stopped
                                    </div>
                                    <div v-else-if="m.status === 'error'" class="text-xs text-gray-500">
                                        <Icon name="heroicons-x-mark" class="w-4 h-4 inline mr-1 text-red-500" />
                                        <span class="italic">An error occurred</span>
                                    </div>
                                </div>
                            </template>
                        </div>
                    </li>
                </ul>
            </div>

            <!-- Footer -->
            <footer class="flex-none border-t border-gray-100 py-2 text-center bg-white z-10">
                <a href="https://bagofwords.com" target="_blank" class="text-[10px] text-gray-300 hover:text-gray-400 transition-colors">
                    Powered by Bag of Words
                </a>
            </footer>
        </template>
    </div>
</template>

<script setup lang="ts">
import { ref, onMounted, onUnmounted, nextTick } from 'vue'
import Spinner from '~/components/Spinner.vue'
import CreateWidgetTool from '~/components/tools/CreateWidgetTool.vue'
import CreateDataTool from '~/components/tools/CreateDataTool.vue'
import DescribeTablesTool from '~/components/tools/DescribeTablesTool.vue'
import DescribeEntityTool from '~/components/tools/DescribeEntityTool.vue'
import ReadResourcesTool from '~/components/tools/ReadResourcesTool.vue'
import InspectDataTool from '~/components/tools/InspectDataTool.vue'
import ExecuteCodeTool from '~/components/tools/ExecuteCodeTool.vue'
import AnswerQuestionTool from '~/components/tools/AnswerQuestionTool.vue'
import ToolWidgetPreview from '~/components/tools/ToolWidgetPreview.vue'

const route = useRoute()
const token = route.params.token as string

const isLoading = ref(true)
const error = ref(false)
const conversation = ref<any>({
    title: '',
    user_name: '',
    completions: [],
    created_at: null,
})

// Pagination state
const hasMore = ref(false)
const nextBefore = ref<string | null>(null)
const isLoadingMore = ref(false)
const messagesContainer = ref<HTMLElement | null>(null)

// Collapsed reasoning tracking
const collapsedReasoning = ref<Set<string>>(new Set())
const expandedToolDetails = ref<Set<string>>(new Set())

definePageMeta({
    layout: false,
    auth: false,
})

function formatDate(dateString: string | null): string {
    if (!dateString) return ''
    try {
        const date = new Date(dateString)
        return date.toLocaleDateString('en-US', { 
            month: 'short', 
            day: 'numeric',
            year: 'numeric'
        })
    } catch {
        return ''
    }
}

function toggleReasoning(blockId: string) {
    if (collapsedReasoning.value.has(blockId)) {
        collapsedReasoning.value.delete(blockId)
    } else {
        collapsedReasoning.value.add(blockId)
    }
}

function isReasoningCollapsed(blockId: string): boolean {
    return collapsedReasoning.value.has(blockId)
}

function toggleToolDetails(toolId: string) {
    if (expandedToolDetails.value.has(toolId)) {
        expandedToolDetails.value.delete(toolId)
    } else {
        expandedToolDetails.value.add(toolId)
    }
}

function isToolDetailsExpanded(toolId: string): boolean {
    return expandedToolDetails.value.has(toolId)
}

function hasCompletedContent(block: any): boolean {
    return !!(block.content || block.tool_execution || block.status === 'completed' || block.status === 'stopped' || block.plan_decision?.analysis_complete || block.plan_decision?.final_answer)
}

function getThoughtProcessLabel(block: any): string {
    if (block.status === 'stopped') return 'Thought Process'
    
    if (block.started_at && block.completed_at) {
        const startTime = new Date(block.started_at).getTime()
        const endTime = new Date(block.completed_at).getTime()
        const durationMs = endTime - startTime
        const durationSeconds = Math.round(durationMs / 1000)
        if (durationSeconds > 1800) return 'Stopped'
        return `Thought for ${durationSeconds}s`
    }
    
    if (block.tool_execution?.duration_ms) {
        const durationSeconds = (block.tool_execution.duration_ms / 1000).toFixed(1)
        return `Thought for ${durationSeconds}s`
    }
    
    return 'Thought Process'
}

function getToolComponent(toolName: string) {
    switch (toolName) {
        case 'create_widget':
            return CreateWidgetTool
        case 'create_data':
            return CreateDataTool
        case 'describe_tables':
            return DescribeTablesTool
        case 'describe_entity':
            return DescribeEntityTool
        case 'read_resources':
            return ReadResourcesTool
        case 'inspect_data':
            return InspectDataTool
        case 'execute_code':
        case 'execute_sql':
        case 'create_and_execute_code':
            return ExecuteCodeTool
        case 'answer_question':
            return AnswerQuestionTool
        default:
            return null
    }
}

function shouldUseToolComponent(toolExecution: any): boolean {
    return getToolComponent(toolExecution?.tool_name) !== null
}

function shouldShowToolWidgetPreview(toolExecution: any): boolean {
    if (!toolExecution) return false
    const showForTools = ['create_and_execute_code', 'execute_code', 'execute_sql']
    return showForTools.includes(toolExecution.tool_name) && 
           toolExecution.status === 'success' &&
           (toolExecution.created_widget || toolExecution.created_step)
}

async function loadConversation() {
    try {
        const { data, error: fetchError } = await useMyFetch(`/api/c/${token}?limit=10`)
        
        if (fetchError.value || !data.value) {
            error.value = true
            return
        }
        
        conversation.value = data.value
        hasMore.value = (data.value as any).has_more || false
        nextBefore.value = (data.value as any).next_before || null
        
        // Auto-collapse reasoning for blocks that have content
        for (const completion of conversation.value.completions || []) {
            for (const block of completion.completion_blocks || []) {
                if (block.content || block.tool_execution || block.plan_decision?.final_answer) {
                    collapsedReasoning.value.add(block.id)
                }
            }
        }
    } catch (e) {
        console.error('Failed to load conversation:', e)
        error.value = true
    } finally {
        isLoading.value = false
        
        // Scroll to bottom after DOM renders (must be after isLoading = false)
        await nextTick()
        // Use a slightly longer timeout and requestAnimationFrame to ensure layout is stable
        setTimeout(() => {
            requestAnimationFrame(() => {
                scrollToBottom()
            })
        }, 100)
    }
}

function scrollToBottom() {
    const container = messagesContainer.value
    if (container) {
        container.scrollTop = container.scrollHeight
    }
}

async function loadPreviousCompletions() {
    if (isLoadingMore.value || !hasMore.value || !nextBefore.value) return
    
    isLoadingMore.value = true
    
    try {
        const { data, error: fetchError } = await useMyFetch(`/api/c/${token}?limit=10&before=${nextBefore.value}`)
        
        if (fetchError.value || !data.value) {
            return
        }
        
        // Get scroll height before prepending
        const container = messagesContainer.value
        const scrollHeightBefore = container?.scrollHeight || 0
        
        // Prepend older completions
        const olderCompletions = (data.value as any).completions || []
        conversation.value.completions = [...olderCompletions, ...conversation.value.completions]
        
        hasMore.value = (data.value as any).has_more || false
        nextBefore.value = (data.value as any).next_before || null
        
        // Auto-collapse reasoning for new blocks
        for (const completion of olderCompletions) {
            for (const block of completion.completion_blocks || []) {
                if (block.content || block.tool_execution || block.plan_decision?.final_answer) {
                    collapsedReasoning.value.add(block.id)
                }
            }
        }
        
        // Maintain scroll position after prepending
        await nextTick()
        if (container) {
            const scrollHeightAfter = container.scrollHeight
            container.scrollTop = scrollHeightAfter - scrollHeightBefore
        }
    } catch (e) {
        console.error('Failed to load more completions:', e)
    } finally {
        isLoadingMore.value = false
    }
}

function handleScroll(event: Event) {
    const target = event.target as HTMLElement
    // Load more when scrolled near the top (within 100px)
    if (target.scrollTop < 100 && hasMore.value && !isLoadingMore.value) {
        loadPreviousCompletions()
    }
}

onMounted(() => {
    loadConversation()
})

onUnmounted(() => {
    // Cleanup handled by Vue's event binding
})
</script>

<style scoped>
/* Thinking box - collapsible reasoning */
.thinking-box {
    margin-bottom: 4px;
}

.thinking-header {
    display: flex;
    align-items: center;
    cursor: pointer;
    font-size: 12px;
    font-weight: 400;
    color: #6b7280;
    user-select: none;
}

.thinking-header:hover {
    color: #374151;
}

.thinking-content {
    padding: 4px 0 4px 10px;
    margin-top: 2px;
    margin-bottom: 4px;
    border-left: 1px dashed #e5e7eb;
    font-size: 12px !important;
    line-height: 1.4;
    color: #6b7280;
}

.thinking-content :deep(*) {
    font-size: 12px !important;
    line-height: 1.4 !important;
}

.thinking-content :deep(.markdown-content) {
    font-size: 12px !important;
    line-height: 1.4 !important;
}

.thinking-content :deep(p) {
    font-size: 12px !important;
    margin: 0;
}

/* Tool execution container */
.tool-execution-container {
    margin: 8px 0;
}

/* Block content - assistant messages */
.block-content {
    margin-bottom: 4px;
    font-size: 13px;
}

/* Markdown styling */
.markdown-wrapper :deep(.markdown-content) {
    @apply leading-relaxed;
    font-size: 13px;

    p {
        margin-bottom: 1em;
    }
    p:last-child {
        margin-bottom: 0;
    }

    :where(h1, h2, h3, h4, h5, h6) {
        @apply font-bold mb-4 mt-6;
    }

    h1 { @apply text-2xl; }
    h2 { @apply text-xl; }
    h3 { @apply text-lg; }

    ul, ol { @apply pl-6 mb-4; }
    ul { @apply list-disc; }
    ol { @apply list-decimal; }
    li { @apply mb-1.5; }

    pre {
        @apply bg-gray-50 p-4 rounded-lg mb-4 overflow-x-auto;
        white-space: pre-wrap;
        word-wrap: break-word;
    }
    pre code {
        background: none;
        padding: 0;
        border-radius: 0;
        font-size: 13px;
        line-height: 1.5;
        display: block;
        white-space: pre-wrap;
        word-wrap: break-word;
    }
    code {
        @apply bg-gray-100 px-1.5 py-0.5 rounded font-mono;
        font-size: 12px;
        color: #374151;
    }
    a { 
        @apply text-gray-900 no-underline relative;
        transition: color 0.15s ease;
    }
    a:hover {
        @apply text-gray-700;
    }
    blockquote { @apply border-l-4 border-gray-200 pl-4 italic my-4; }
    table { @apply w-full border-collapse mb-4; }
    table th, table td { @apply border border-gray-200 p-2 text-xs bg-white; }
}

/* Fade transitions */
.fade-enter-active,
.fade-leave-active {
    transition: opacity 0.3s ease;
}

.fade-enter-from,
.fade-leave-to {
    opacity: 0;
}

/* Thinking dots animation */
@keyframes ellipsis {
    0% { content: 'Thinking.'; }
    33% { content: 'Thinking..'; }
    66% { content: 'Thinking...'; }
}

.dots::after {
    content: 'Thinking...';
    display: inline-block;
    background: linear-gradient(90deg, #888 0%, #999 25%, #ccc 50%, #999 75%, #888 100%);
    background-size: 200% 100%;
    -webkit-background-clip: text;
    background-clip: text;
    color: transparent;
    animation: shimmer 2s linear infinite, ellipsis 1s infinite;
    font-weight: 400;
    font-size: 12px;
    opacity: 1;
}

@keyframes shimmer {
    0% { background-position: -100% 0; }
    100% { background-position: 100% 0; }
}
</style>
