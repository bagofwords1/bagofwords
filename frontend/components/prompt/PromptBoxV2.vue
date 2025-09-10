<template>
    <div class="flex-shrink-0 p-4 pb-8 bg-white">
        <!-- Instructions button (minimal) -->
        <div class="mb-2">
            <button
                class="text-gray-500 hover:text-gray-700 text-xs flex items-center"
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
                <textarea
                    ref="textareaRef"
                    v-model="text"
                    class="w-full resize-none bg-transparent outline-none text-sm text-gray-900 placeholder-gray-400"
                    rows="2"
                    :placeholder="placeholder"
                    @keydown.enter.exact.prevent="handleEnter"
                    @keydown.enter.shift.stop
                    @input="autoGrow"
                ></textarea>
            </div>

            <!-- Bottom controls -->
            <div class="px-3 pb-3 flex items-center justify-between">
                <div class="flex items-center space-x-1 relative">
                    <!-- Data source selector -->
                    <DataSourceSelector v-model:selectedDataSources="selectedDataSources" :reportId="report_id" />

                    <!-- Mode selector -->
                    <UPopover :key="'mode-' + (props.popoverOffset || 0)" :popper="popperLegacy">
                        <UTooltip :text="isCompactPrompt ? (mode === 'chat' ? 'Chat' : 'Deep Research') : ''" :popper="{ strategy: 'fixed', placement: 'bottom-start' }">
                            <button class="text-gray-500 text-gray-500 hover:text-gray-900 hover:bg-gray-50 rounded-md p-2 hover:text-gray-700 text-xs px-2 py-1 rounded flex items-center">
                                <Icon :name="mode === 'chat' ? 'heroicons-chat-bubble-left-right' : 'heroicons-light-bulb'" class="w-4 h-4" />
                                <span v-if="!isCompactPrompt" class="ml-1">{{ mode === 'chat' ? 'Chat' : 'Deep Research' }}</span>
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
                                    <div class="px-2 py-1 rounded hover:bg-gray-100 cursor-pointer flex items-center justify-between" @click="() => { selectMode('research'); close(); }">
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
                            <button class="text-gray-500 text-gray-500 hover:text-gray-900 hover:bg-gray-50 rounded-md p-2 hover:text-gray-700 text-xs px-2 py-1 rounded flex items-center">
                                <Icon name="heroicons-cpu-chip" class="w-4 h-4" />
                                <span v-if="!isCompactPrompt" class="ml-1">{{ selectedModelLabel }}</span>
                            </button>
                        </UTooltip>
                        <template #panel="{ close }">
                            <div class="p-2 text-xs max-h-64 overflow-y-auto w-[200px]">
                                <div v-for="m in models" :key="m.id" class="px-2 py-1 rounded hover:bg-gray-100 cursor-pointer flex items-center justify-between" @click="() => { selectModel(m.id); close(); }">
                                    <div class="flex items-start">
                                        <div class="mr-2 mt-0.5">
                                            <LLMProviderIcon :provider="m.provider?.provider_type || 'default'" :icon="true" class="w-4 h-4" />
                                        </div>
                                        <div class="flex flex-col">
                                            <span class="font-medium">{{ m.name }}</span>
                                            <span class="text-gray-500 text-[10px]">{{ m.provider?.name }}</span>
                                        </div>
                                    </div>
                                    <Icon v-if="selectedModel === m.id" name="heroicons-check" class="w-4 h-4 text-blue-500 ml-2" />
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
import { ref, computed, onMounted, nextTick } from 'vue'

import DataSourceSelector from '@/components/prompt/DataSourceSelector.vue'
import InstructionsListModalComponent from '@/components/InstructionsListModalComponent.vue'
import LLMProviderIcon from '@/components/LLMProviderIcon.vue'
import FileUploadComponent from '@/components/FileUploadComponent.vue'

const props = defineProps({
    report_id: String,
    latestInProgressCompletion: Object,
    isStopping: Boolean,
    // Allow fine-tuning alignment if needed later
    popoverOffset: { type: Number, default: 16 }
})

const emit = defineEmits(['submitCompletion','stopGeneration'])

const text = ref('')
const placeholder = 'Ask for data, dashboard or a deep research'
const mode = ref<'chat' | 'deep'>('chat')
const selectedDataSources = ref<any[]>([])
const isCompactPrompt = ref(false)

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
            // Set first enabled model as default if none selected
            if (!selectedModel.value && models.value.length > 0) {
                selectedModel.value = models.value[0].id
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

const textareaRef = ref<HTMLTextAreaElement | null>(null)
function autoGrow() {
    const ta = textareaRef.value
    if (!ta) return
    ta.style.height = 'auto'
    ta.style.height = Math.min(200, ta.scrollHeight) + 'px'
}

const canSubmit = computed(() => text.value.trim().length > 0 && !props.latestInProgressCompletion)

function handleEnter() {
    if (canSubmit.value) submit()
}

function submit() {
    const payload = {
        text: text.value,
        mentions: [
            { name: 'DATA SOURCES', items: selectedDataSources.value },
        ],
        mode: mode.value,                 // 'chat' | 'deep'
        model_id: selectedModel.value     // backend model id from selector
    }
    emit('submitCompletion', payload)
    text.value = ''
    nextTick(autoGrow)
}

function onFilesUploaded(files: any[]) {
    // Hook for future: could display attachments or include in mentions
}

const instructionsListModalRef = ref<any | null>(null)
function openInstructions() { instructionsListModalRef.value?.openModal?.() }

const helperText = computed(() => mode.value === 'deep' ? 'Deep Research may take longer' : 'Enter to send • Shift+Enter for new line')

onMounted(async () => {
    await loadModels()
    nextTick(autoGrow)
    // Compact mode: if container is narrow, hide labels
    const root = document.querySelector('.flex-shrink-0') as HTMLElement
    const ro = new ResizeObserver(() => {
        const w = root?.clientWidth || 0
        isCompactPrompt.value = w > 0 && w < 420
    })
    if (root) ro.observe(root)
})
</script>

<style scoped>
.placeholder-gray-400::placeholder { color: #9ca3af; }
</style>


