<template>
    <div class="flex-shrink-0 p-4 pb-8 bg-white">
        <!-- Instructions button (minimal) -->
        <div class="mb-2">
            <button
                class="text-gray-500 hover:text-gray-900 hover:bg-gray-50 rounded-md p-1 text-xs flex items-center"
                @click="openInstructions"
            >
                <Icon name="heroicons-cube" class="w-4 h-4 mr-1" />
                Instructions
            </button>
        </div>

        <!-- Minimalist prompt container -->
        <div class="border border-gray-200 rounded-xl bg-white focus-within:border-gray-300 transition-colors">
            <!-- Input -->
            <div class="p-3">
                <MentionInput
                    v-model="text"
                    @update:mentions="handleMentionsUpdate"
                    @submit="submit"
                    :placeholder="placeholder"
                    :rows="2"
                    :selectedDataSourceIds="selectedDataSources.map(ds => ds.id)"
                />
            </div>

            <!-- Bottom controls -->
            <div class="px-3 pb-3 flex items-center justify-between">
                <div class="flex items-center space-x-1 relative">
                    <!-- Data source selector -->
                    <DataSourceSelector v-model:selectedDataSources="selectedDataSources" :reportId="report_id" />

                    <!-- Mode selector -->
                    <UPopover :key="'mode-' + (props.popoverOffset || 0)" :popper="popperLegacy">
                        <UTooltip :text="isCompactPrompt ? (mode === 'chat' ? 'Chat' : 'Deep Analytics') : ''" :popper="{ strategy: 'fixed', placement: 'bottom-start' }">
                            <button class="text-gray-500 hover:text-gray-900 hover:bg-gray-50 rounded-md px-2 py-1 text-xs flex items-center">
                                <Icon :name="mode === 'chat' ? 'heroicons-chat-bubble-left-right' : 'heroicons-light-bulb'" class="w-4 h-4" />
                                <span v-if="!isCompactPrompt" class="ml-1">{{ mode === 'chat' ? 'Chat' : 'Deep Analytics' }}</span>
                            </button>
                        </UTooltip>
                        <template #panel="{ close }">
                            <div class="p-2 text-xs">
                                <div class="px-2 py-1 rounded hover:bg-gray-100 cursor-pointer flex items-center justify-between w-[180px]" @click="() => { selectMode('chat'); close(); }">
                                    <div class="flex items-center">
                                        <Icon name="heroicons-chat-bubble-left-right" class="w-4 h-4 mr-2" />
                                        Chat
                                    </div>
                                    <Icon v-if="mode === 'chat'" name="heroicons-check" class="w-4 h-4 text-blue-500" />
                                </div>
                                    <div class="px-2 py-1 rounded hover:bg-gray-100 cursor-pointer flex items-center justify-between" @click="() => { selectMode('deep'); close(); }">
                                    <div class="flex items-center">
                                        <Icon name="heroicons-light-bulb" class="w-4 h-4 mr-2" />
                                        Deep Analytics
                                    </div>
                                    <Icon v-if="mode === 'deep'" name="heroicons-check" class="w-4 h-4 text-blue-500" />
                                </div>
                            </div>
                        </template>
                    </UPopover>
                </div>

                <div class="flex items-center space-x-0.5">
                    <!-- File attach (open files modal) -->
                    <FileUploadComponent :report_id="report_id" @update:uploadedFiles="onFilesUploaded" />

                    <!-- Model selector -->
                    <UPopover :key="'model-' + (props.popoverOffset || 0)" :popper="popperLegacy">
                        <UTooltip :text="isCompactPrompt ? selectedModelLabel : ''" :popper="{ strategy: 'fixed', placement: 'bottom-start' }">
                            <button class="text-gray-500 hover:text-gray-900 hover:bg-gray-50 rounded-md px-2 py-1 text-xs flex items-center">
                                <Icon name="heroicons-cpu-chip" class="w-4 h-4" />
                                <span v-if="!isCompactPrompt" class="ml-1">{{ selectedModelLabel }}</span>
                            </button>
                        </UTooltip>
                        <template #panel="{ close }">
                            <div class="p-2 text-xs max-h-64 overflow-y-auto w-[200px]">
                                <div v-for="m in models" :key="m.id" class="px-2 py-1 rounded hover:bg-gray-100 cursor-pointer flex items-center" @click="() => { selectModel(m.id); close(); }">
                                    <div class="mr-2">
                                        <LLMProviderIcon :provider="m.provider?.provider_type || 'default'" :icon="true" class="w-4 h-4" />
                                    </div>
                                    <div class="flex flex-col flex-1 text-left min-w-0">
                                        <span class="font-medium truncate">{{ m.name }}</span>
                                        <span class="text-gray-500 text-[10px] truncate">{{ m.provider?.name }}</span>
                                    </div>
                                    <Icon v-if="selectedModel === m.id" name="heroicons-check" class="w-4 h-4 text-blue-500 ml-2 flex-shrink-0" />
                                </div>
                            </div>
                        </template>
                    </UPopover>

                    <!-- Send / Stop -->
                    <button
                        v-if="latestInProgressCompletion"
                        class="text-white bg-gray-500 hover:bg-gray-600 w-7 h-7 rounded-full flex items-center justify-center transition-colors ml-1"
                        :disabled="isStopping"
                        @click="$emit('stopGeneration')"
                    >
                        <Icon name="heroicons-stop-solid" class="w-3.5 h-3.5" />
                    </button>
                    <button
                        v-else
                        class="text-white bg-gray-700 hover:cursor-pointer hover:bg-black w-7 h-7 rounded-full flex items-center justify-center transition-colors ml-1"
                        :disabled="!canSubmit"
                        @click="submit"
                    >
                        <Icon name="heroicons-arrow-right" class="w-3.5 h-3.5" />
                    </button>
                </div>
            </div>
        </div>

        <!-- Modals -->
        <InstructionsListModalComponent ref="instructionsListModalRef" />
    </div>
</template>

<script setup lang="ts">
import { ref, computed, onMounted, nextTick, watch } from 'vue'
import { useRouter } from 'vue-router'

import DataSourceSelector from '@/components/prompt/DataSourceSelector.vue'
import InstructionsListModalComponent from '@/components/InstructionsListModalComponent.vue'
import LLMProviderIcon from '@/components/LLMProviderIcon.vue'
import FileUploadComponent from '@/components/FileUploadComponent.vue'
import MentionInput from '@/components/prompt/MentionInput.vue'

const props = defineProps({
    report_id: String,
    latestInProgressCompletion: Object,
    isStopping: Boolean,
    // Allow fine-tuning alignment if needed later
    popoverOffset: { type: Number, default: 16 },
    // Landing page prefill support
    textareaContent: { type: String, default: '' }
})

const emit = defineEmits(['submitCompletion','stopGeneration','update:modelValue'])

const text = ref('')
const placeholder = 'Ask for data, dashboard or a deep analysis'
const mode = ref<'chat' | 'deep'>('chat')
const selectedDataSources = ref<any[]>([])
const uploadedFiles = ref<any[]>([])
const isCompactPrompt = ref(false)
const inlineMentions = ref<any[]>([])

// Popover state
const showModeMenu = ref(false)
const showModelMenu = ref(false)

// Model selector state - fetch from backend
const models = ref<any[]>([])
const selectedModel = ref<string>('')
const selectedModelLabel = computed(() => {
    const model = models.value.find(m => m.id === selectedModel.value)
    return model?.name || 'Select Model'
})

// Legacy popper (for current Nuxt UI stable)
// Use a small fixed skid so content hugs the left edge of the chip
// Use absolute strategy so transforms from split-screen don't affect placement
const popperLegacy = computed(() => ({ strategy: 'absolute' as const, placement: 'bottom-start' as const, offset: [ 0, 8 ] }))


async function loadModels() {
    try {
        const { data } = await useMyFetch('/api/llm/models?is_enabled=true')
        if (data.value && Array.isArray(data.value)) {
            models.value = data.value
            // Set the default model as selected, or fall back to first enabled model
            if (!selectedModel.value && models.value.length > 0) {
                // First try to find the model marked as default
                const defaultModel = models.value.find(m => m.is_default)
                if (defaultModel) {
                    selectedModel.value = defaultModel.id
                } else {
                    // Fall back to first enabled model if no default is set
                    selectedModel.value = models.value[0].id
                }
            }
        }
    } catch (error) {
        console.error('Failed to load models:', error)
        // Fallback to hardcoded models
        models.value = [
            { id: 'default', name: 'Default Model', provider: { name: 'System' } }
        ]
        selectedModel.value = 'default'
    }
}

function selectModel(modelId: string) { 
    selectedModel.value = modelId 
}
function selectMode(m: 'chat' | 'deep') { mode.value = m }

// Functions to select and close popovers
function selectModeAndClose(m: 'chat' | 'deep') {
    selectMode(m)
    showModeMenu.value = false
}

function selectModelAndClose(modelId: string) {
    selectModel(modelId)
    showModelMenu.value = false
}

function handleMentionsUpdate(mentions: any[]) {
    inlineMentions.value = mentions
}

function onInput() {
    emit('update:modelValue', text.value)
}

const canSubmit = computed(() => text.value.trim().length > 0 && !props.latestInProgressCompletion)

function submit() {
    if (!canSubmit.value) return
    
    // Organize inline mentions by type
    const mentionsByType = {
        data_sources: inlineMentions.value.filter(m => m.type === 'data_source'),
        tables: inlineMentions.value.filter(m => m.type === 'datasource_table'),
        files: inlineMentions.value.filter(m => m.type === 'file'),
        entities: inlineMentions.value.filter(m => m.type === 'entity')
    }
    const payload = {
        text: text.value,
        mentions: [
            { name: 'DATA SOURCES', items: mentionsByType.data_sources },
            { name: 'TABLES', items: mentionsByType.tables },
            { name: 'FILES', items: mentionsByType.files },
            { name: 'ENTITIES', items: mentionsByType.entities }
        ],
        mode: mode.value,                 // 'chat' | 'deep'
        model_id: selectedModel.value     // backend model id from selector
    }
    if (props.report_id) {
        // In-report behavior: emit to parent stream
        emit('submitCompletion', payload)
        text.value = ''
    } else {
        // Landing page behavior: create a new report
        createReport()
    }
}

function onFilesUploaded(files: any[]) {
    uploadedFiles.value = files || []
}

const instructionsListModalRef = ref<any | null>(null)
function openInstructions() { instructionsListModalRef.value?.openModal?.() }

onMounted(async () => {
    await loadModels()
    // Hydrate selected data sources from report when in a report context
    try {
        if (props.report_id) {
            const res = await useMyFetch(`/reports/${props.report_id}`, { method: 'GET' })
            const report = (res as any)?.data?.value as any
            if (report && Array.isArray(report.data_sources)) {
                selectedDataSources.value = report.data_sources
            }
        }
    } catch (e) {
        console.error('Failed to hydrate data sources for report:', e)
    }
    // Compact mode: if container is narrow, hide labels
    const root = document.querySelector('.flex-shrink-0') as HTMLElement
    const ro = new ResizeObserver(() => {
        const w = root?.clientWidth || 0
        isCompactPrompt.value = w > 0 && w < 420
    })
    if (root) ro.observe(root)
})

// Keep local text in sync with parent-provided content (landing page)
watch(() => props.textareaContent, (newVal) => {
    if (typeof newVal === 'string' && newVal !== text.value) {
        text.value = newVal
    }
}, { immediate: true })

// Re-hydrate data sources if report_id changes
watch(() => props.report_id, async (newId) => {
    if (!newId) return
    try {
        const res = await useMyFetch(`/reports/${newId}`, { method: 'GET' })
        const report = (res as any)?.data?.value as any
        if (report && Array.isArray(report.data_sources)) {
            selectedDataSources.value = report.data_sources
        }
    } catch (e) {
        console.error('Failed to rehydrate data sources when report changed:', e)
    }
})

const router = useRouter()

async function createReport() {
    try {
        if (!text.value.trim()) return
        const response = await useMyFetch('/reports', {
            method: 'POST',
            body: JSON.stringify({
                title: 'untitled report',
                files: uploadedFiles.value?.map((file: any) => file.id) || [],
                new_message: text.value,
                data_sources: selectedDataSources.value?.map((ds: any) => ds.id) || []
            })
        })
        if ((response as any)?.error?.value) {
            throw new Error('Report creation failed')
        }
        const data = (response as any)?.data?.value as any
        if (data?.id) {
            // Build mentions from inlineMentions only (no automatic data sources)
            const mentionsByType = {
                data_sources: inlineMentions.value.filter((m: any) => m.type === 'data_source'),
                tables: inlineMentions.value.filter((m: any) => m.type === 'datasource_table'),
                files: inlineMentions.value.filter((m: any) => m.type === 'file'),
                entities: inlineMentions.value.filter((m: any) => m.type === 'entity')
            }
            const mentions = [
                { name: 'DATA SOURCES', items: mentionsByType.data_sources },
                { name: 'TABLES', items: mentionsByType.tables },
                { name: 'FILES', items: mentionsByType.files },
                { name: 'ENTITIES', items: mentionsByType.entities }
            ]

            router.push({ 
                path: `/reports/${data.id}`, 
                query: { 
                    new_message: text.value,
                    mode: mode.value,
                    model_id: selectedModel.value || '',
                    mentions: encodeURIComponent(JSON.stringify(mentions))
                }
            })
        }
        text.value = ''
    } catch (error) {
        console.error('Failed to create report:', error)
    }
}
</script>

<style scoped>
.placeholder-gray-400::placeholder { color: #9ca3af; }
</style>


