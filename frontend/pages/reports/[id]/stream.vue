<template>

    <div class="flex flex-col h-screen overflow-y-hidden bg-white">
        <header class="sticky top-0 bg-white z-10 flex flex-row pt-1 h-[40px] border-gray-200 pb-1 pr-2">
            <GoBackChevron />
            <h1 class="text-sm md:text-left text-center mt-1">
                <span class="font-semibold text-sm">Streaming Test</span>
            </h1>
        </header>

        <div class="flex-1 overflow-y-auto mt-4 pb-20" ref="scrollContainer">
            <div class="mx-auto w-full md:w-2/3 px-4">
                <div class="mb-4">
                    <PromptBoxExcel 
                        ref="promptBoxRef"
                        :widgets="widgets"
                        :selectedWidgetId="selectedWidgetId"
                        :excelData="excelData"
                        :latestInProgressCompletion="latestInProgressCompletion"
                        :isStopping="isStoppingGeneration"
                        @submitCompletion="submitCompletionStream"
                        :report_id="report_id"
                        @update:selectedWidgetId="handleSelectedWidgetId"
                        @stopGeneration="() => abortStream()"
                    />
                </div>

                <!-- Completions v2 Section -->
                <div class="mb-4 border rounded-md overflow-hidden">
                    <div class="p-3 border-b bg-gray-50 flex items-center justify-between">
                        <div>
                            <span class="text-sm font-semibold">Completions v2</span>
                            <span class="ml-2 text-xs text-gray-500" v-if="isLoadingCompletions">(loading...)</span>
                        </div>
                        <div class="space-x-2">
                            <button class="px-2 py-1 text-xs border rounded" @click="loadCompletions" :disabled="isLoadingCompletions">
                                {{ isLoadingCompletions ? 'Loading...' : 'Refresh' }}
                            </button>
                        </div>
                    </div>
                    <div class="overflow-auto p-3" style="max-height: 40vh;">
                        <div v-if="completions.length === 0" class="text-sm text-gray-500">
                            No completions yet. Click "Refresh" to load.
                        </div>
                        <div v-for="completion in completions" :key="completion.id" class="mb-4 border rounded p-3">
                            <div class="flex items-center justify-between mb-2">
                                <span class="text-sm font-medium">{{ completion.role }} - {{ completion.status }}</span>
                                <span class="text-xs text-gray-500">{{ completion.created_at }}</span>
                            </div>
                            
                            <!-- Completion Blocks -->
                            <div v-if="completion.completion_blocks && completion.completion_blocks.length > 0" class="ml-2">
                                <div class="text-xs font-medium text-gray-600 mb-1">Blocks ({{ completion.completion_blocks.length }}):</div>
                                <div v-for="block in completion.completion_blocks" :key="block.id" class="mb-2 p-2 bg-gray-50 rounded text-xs">
                                    <div class="flex items-center justify-between">
                                        <span class="font-mono">{{ block.seq ?? '-' }} | {{ block.title }}</span>
                                        <span class="text-gray-500">{{ block.status }}</span>
                                    </div>
                                    <div v-if="block.reasoning" class="text-gray-600 mt-1">{{ block.reasoning }}</div>
                                    <div v-if="block.tool_execution" class="mt-1 text-blue-600">
                                        Tool: {{ block.tool_execution.tool_name }} ({{ block.tool_execution.status }})
                                        <span v-if="block.tool_execution.created_widget_id" class="ml-2 text-green-600">
                                            → Widget: {{ block.tool_execution.created_widget_id }}
                                        </span>
                                        <span v-if="block.tool_execution.created_step_id" class="ml-2 text-purple-600">
                                            → Step: {{ block.tool_execution.created_step_id }}
                                        </span>
                                    </div>
                                </div>
                            </div>
                            
                            <!-- Summary -->
                            <div v-if="completion.summary" class="mt-2 text-xs text-gray-500">
                                Summary: {{ JSON.stringify(completion.summary) }}
                            </div>
                        </div>
                    </div>
                </div>

                <!-- Stream Events Section -->
                <div class="border rounded-md overflow-hidden">
                    <div class="p-3 border-b bg-gray-50 flex items-center justify-between">
                        <div>
                            <span class="text-sm font-semibold">Stream Events</span>
                            <span class="ml-2 text-xs text-gray-500" v-if="isStreaming">(streaming...)</span>
                        </div>
                        <div class="space-x-2">
                            <button class="px-2 py-1 text-xs border rounded" @click="clearEvents">Clear</button>
                            <button class="px-2 py-1 text-xs border rounded" @click="abortStream" :disabled="!isStreaming">Abort</button>
                        </div>
                    </div>
                    <div class="overflow-auto" style="max-height: 60vh;">
                        <table class="min-w-full text-left text-xs">
                            <thead class="bg-gray-100 sticky top-0">
                                <tr>
                                    <th class="px-3 py-2 w-20">Seq</th>
                                    <th class="px-3 py-2 w-56">Event</th>
                                    <th class="px-3 py-2 w-56">Timestamp</th>
                                    <th class="px-3 py-2">Data (raw)</th>
                                </tr>
                            </thead>
                            <tbody>
                                <tr v-for="(e, idx) in streamEvents" :key="idx" class="border-t">
                                    <td class="px-3 py-2 align-top">{{ e.seq ?? '-' }}</td>
                                    <td class="px-3 py-2 align-top font-mono">{{ e.event }}</td>
                                    <td class="px-3 py-2 align-top">{{ e.timestamp }}</td>
                                    <td class="px-3 py-2 align-top font-mono whitespace-pre-wrap">{{ e.data }}</td>
                                </tr>
                            </tbody>
                        </table>
                    </div>
                </div>
            </div>
        </div>
    </div>

</template>

<script setup lang="ts">
import { ref, computed, nextTick } from 'vue'
import PromptBoxExcel from '@/components/excel/PromptBoxExcel.vue'
import GoBackChevron from '@/components/excel/GoBackChevron.vue'

const route = useRoute()
const report_id = route.params.id as string
const config = useRuntimeConfig()
const { token } = useAuth()

// Minimal props for PromptBoxExcel
const widgets = ref<any[]>([])
const selectedWidgetId = ref<{ widgetId: string | null; stepId: string | null; widgetTitle: string | null }>({ widgetId: null, stepId: null, widgetTitle: null })
const excelData = ref<any>({ sheetName: '', address: '', sheetData: [] })
const latestInProgressCompletion = computed<Record<string, any> | undefined>(() => undefined)
const isStoppingGeneration = ref(false)

// Streaming state
const isStreaming = ref(false)
const streamEvents = ref<Array<{ event: string; seq?: number; timestamp?: string; data: string }>>([])
let currentController: AbortController | null = null

// Completions v2 state
const completions = ref<any[]>([])
const isLoadingCompletions = ref(false)

function handleSelectedWidgetId(widgetId: any, stepId: any, widgetTitle: any) {
    selectedWidgetId.value = { widgetId, stepId, widgetTitle }
}

function clearEvents() {
    streamEvents.value = []
}

async function loadCompletions() {
    isLoadingCompletions.value = true
    try {
        const { data } = await useMyFetch(`/api/reports/${report_id}/completions.v2`)
        if (data.value) {
            completions.value = data.value.completions || []
            console.log('Loaded completions v2:', data.value)
        }
    } catch (error) {
        console.error('Error loading completions v2:', error)
    } finally {
        isLoadingCompletions.value = false
    }
}

function abortStream() {
    if (currentController) {
        currentController.abort()
        currentController = null
    }
    isStreaming.value = false
}

async function submitCompletionStream(promptValue: any) {
    if (isStreaming.value) abortStream()
    clearEvents()

    const requestBody = {
        prompt: {
            content: promptValue.text,
            mentions: promptValue.mentions,
            widget_id: selectedWidgetId.value.widgetId,
            step_id: selectedWidgetId.value.stepId
        }
    }

    currentController = new AbortController()
    isStreaming.value = true

    try {
        const { data: res } = await useMyFetch(`/reports/${report_id}/completions/stream`, {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json'
            },
            body: JSON.stringify(requestBody),
            signal: currentController.signal,
            stream: true 
        }) as { data: Response }

        if (!res.ok || !res.body) {
            throw new Error(`Stream HTTP error: ${res.status}`)
        }

        const reader = res.body.getReader()
        const decoder = new TextDecoder()
        let buffer = ''
        let currentEvent: string | null = null

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
                        // Refresh completions after streaming completes
                        loadCompletions()
                        return
                    }
                    try {
                        const parsed = JSON.parse(dataStr)
                        const ev = {
                            event: currentEvent || parsed.event || 'message',
                            seq: parsed.seq || parsed.data?.seq,
                            timestamp: parsed.timestamp || undefined,
                            data: JSON.stringify(parsed.data ?? parsed)
                        }
                        streamEvents.value.push(ev)
                        await nextTick()
                    } catch (e) {
                        streamEvents.value.push({ event: currentEvent || 'message', data: dataStr })
                    }
                } else if (line === '') {
                    // blank line separates events; noop
                }
            }
        }
    } catch (err) {
        if ((err as any)?.name !== 'AbortError') {
            streamEvents.value.push({ event: 'error', data: String(err) })
        }
    } finally {
        isStreaming.value = false
        currentController = null
    }
}

const promptBoxRef = ref<any>(null)
const scrollContainer = ref<any>(null)

// Load completions on mount
import { onMounted } from 'vue'
onMounted(() => {
    loadCompletions()
})

</script>

<style scoped>
.overflow-y-auto {
    overflow-y: auto !important;
}
</style>


