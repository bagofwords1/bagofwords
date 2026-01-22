<template>
    <Teleport to="body">
        <div v-if="instructionModalOpen" class="fixed inset-0 z-50">
            <!-- Backdrop -->
            <div class="absolute inset-0 bg-black/50" @click="closeModal"></div>
            <!-- Modal container -->
            <div class="absolute inset-0 flex items-center justify-center p-4" @click.self="closeModal">
                <div 
                    class="relative bg-white rounded-xl shadow-2xl w-[94vw] overflow-hidden z-10 overscroll-contain flex flex-col"
                    :style="{
                        maxWidth: isAnalyzing ? '1200px' : '680px',
                        maxHeight: 'min(85vh, 800px)',
                        transition: 'max-width 300ms cubic-bezier(0.4, 0, 0.2, 1)'
                    }"
                >
                    <!-- Header -->
                    <div class="flex items-center justify-between px-5 py-4 border-b shrink-0">
                        <div>
                            <h1 class="text-lg font-semibold text-gray-900">{{ modalTitle }}</h1>
                            <p class="text-sm text-gray-500">Define rules for AI agents</p>
                        </div>
                        <button @click="closeModal" class="text-gray-400 hover:text-gray-600 transition-colors">
                            <Icon name="heroicons:x-mark" class="w-5 h-5" />
                        </button>
                    </div>

                    <!-- Body -->
                    <div 
                        class="flex-1 min-h-0 grid transition-all duration-300 ease-out"
                        :style="{
                            gridTemplateColumns: isAnalyzing ? '1fr 1fr' : '1fr 0px'
                        }"
                    >
                        <!-- Left: Form -->
                        <div class="flex flex-col h-full overflow-y-auto min-w-0">
                            <!-- Conditional rendering based on the computed selectedInstructionType -->
                            <InstructionGlobalCreateComponent
                                v-if="selectedInstructionType === 'global' && useCan('create_instructions')"
                                :instruction="instruction"
                                :analyzing="isAnalyzing"
                                :shared-form="sharedForm"
                                :selected-data-sources="selectedDataSources"
                                :is-git-sourced="isGitSourced"
                                :is-git-synced="isGitSynced"
                                :target-build-id="targetBuildId || undefined"
                                @instruction-saved="handleInstructionSaved"
                                @cancel="closeModal"
                                @update-form="updateSharedForm"
                                @update-data-sources="updateSelectedDataSources"
                                @toggle-analyze="toggleAnalyze"
                                @unlink-from-git="unlinkFromGit"
                                @relink-to-git="relinkToGit"
                                @view-mode-changed="handleViewModeChanged"
                            />
                            <InstructionPrivateCreateComponent 
                                v-else
                                :instruction="instruction"
                                :shared-form="sharedForm"
                                :selected-data-sources="selectedDataSources"
                                :is-suggestion="effectiveIsSuggestion"
                                :is-git-sourced="isGitSourced"
                                :is-git-synced="isGitSynced"
                                @instruction-saved="handleInstructionSaved"
                                @cancel="closeModal"
                                @update-form="updateSharedForm"
                                @update-data-sources="updateSelectedDataSources"
                                @toggle-analyze="toggleAnalyze"
                                @unlink-from-git="unlinkFromGit"
                                @relink-to-git="relinkToGit"
                            />
                        </div>

                        <!-- Right: Analysis panel -->
                        <div class="overflow-hidden">
                            <div 
                                v-if="isAnalyzing" 
                                class="h-full border-l border-gray-200 bg-gradient-to-b from-gray-50 to-white flex flex-col"
                            >
                                <!-- Panel header -->
                                <div class="px-4 py-3 border-b border-gray-100 flex items-center justify-between shrink-0">
                                    <h3 class="text-sm font-semibold text-gray-800">Analysis</h3>
                                    <UButton size="xs" variant="ghost" color="blue" @click="refreshAnalysis">
                                        <Icon name="heroicons:arrow-path" class="w-3.5 h-3.5 mr-1" />
                                        Refresh
                                    </UButton>
                                </div>
                                <div class="flex-1 overflow-y-auto p-4 space-y-4">
                                <!-- Impact Estimation -->
                                <div class="rounded-lg border border-gray-200 bg-white shadow-sm">
                                    <div class="flex items-center justify-between p-3 cursor-pointer hover:bg-gray-50 transition-colors" @click="showImpact = !showImpact">
                                        <div class="flex items-center gap-2">
                                            <h3 class="text-sm font-medium text-gray-900">Impact</h3>
                                            <UTooltip :text="impactTotalCount ? `${impactMatchedCount} of ${impactTotalCount} prompts relevant` : 'No prompts analyzed'">
                                                <span class="inline-flex items-center px-2 py-0.5 rounded-full text-xs font-medium bg-blue-100 text-blue-700">
                                                    {{ Math.round(impactScore * 100) }}%
                                                </span>
                                            </UTooltip>
                                        </div>
                                        <Icon :name="showImpact ? 'heroicons:chevron-down' : 'heroicons:chevron-right'" class="w-4 h-4 text-gray-400 transition-transform" />
                                    </div>
                                    <div v-show="showImpact" class="border-t border-gray-100 p-3 overflow-y-auto" :style="{ maxHeight: sectionMaxHeight }">
                                        <p class="text-xs text-gray-500 mb-2">Sample impacted prompts</p>
                                        <div v-if="isLoadingImpact" class="py-6 flex items-center justify-center text-gray-500">
                                            <Spinner class="w-4 h-4 mr-2" /> <span class="text-xs">Loading...</span>
                                        </div>
                                        <div v-else-if="impactedPrompts.length === 0" class="text-xs text-gray-500 py-2">No relevant prompts</div>
                                        <ul v-else class="divide-y divide-gray-100">
                                            <li v-for="(prompt, idx) in impactedPrompts" :key="idx" class="py-2">
                                                <div class="flex items-start justify-between gap-3">
                                                    <p class="text-xs text-gray-900 flex-1">{{ prompt.content }}</p>
                                                    <span v-if="prompt.created_at" class="text-[10px] text-gray-500 whitespace-nowrap">{{ formatDate(prompt.created_at) }}</span>
                                                </div>
                                            </li>
                                        </ul>
                                    </div>
                                </div>

                                <!-- Related Instructions -->
                                <div class="rounded-lg border border-gray-200 bg-white shadow-sm">
                                    <div class="flex items-center justify-between p-3 cursor-pointer hover:bg-gray-50 transition-colors" @click="showRelated = !showRelated">
                                        <div class="flex items-center gap-2">
                                            <h3 class="text-sm font-medium text-gray-900">Related</h3>
                                            <span class="inline-flex items-center px-2 py-0.5 rounded-full text-xs font-medium bg-gray-100 text-gray-600">{{ relatedInstructions.length }}</span>
                                        </div>
                                        <Icon :name="showRelated ? 'heroicons:chevron-down' : 'heroicons:chevron-right'" class="w-4 h-4 text-gray-400 transition-transform" />
                                    </div>
                                    <div v-show="showRelated" class="border-t border-gray-100 p-3 overflow-y-auto" :style="{ maxHeight: sectionMaxHeight }">
                                        <div v-if="isLoadingRelated" class="py-6 flex items-center justify-center text-gray-500">
                                            <Spinner class="w-4 h-4 mr-2" /> <span class="text-xs">Loading...</span>
                                        </div>
                                        <div v-else-if="relatedInstructions.length === 0" class="text-xs text-gray-500 py-2">No related instructions</div>
                                        <ul v-else class="divide-y divide-gray-100">
                                            <li v-for="inst in relatedInstructions" :key="inst.id" class="py-2">
                                                <div class="flex-1">
                                                    <p class="text-xs text-gray-900">{{ inst.text }}</p>
                                                    <div class="mt-1 flex items-center gap-2">
                                                        <span class="inline-flex px-1.5 py-0.5 rounded-full text-[10px]"
                                                              :class="inst.status === 'published' ? 'bg-green-100 text-green-800' : inst.status === 'draft' ? 'bg-yellow-100 text-yellow-800' : 'bg-gray-100 text-gray-800'">
                                                            {{ inst.status }}
                                                        </span>
                                                        <span class="text-[10px] text-gray-500">by {{ inst.createdByName }}</span>
                                                    </div>
                                                </div>
                                            </li>
                                        </ul>
                                    </div>
                                </div>

                                </div>
                            </div>
                        </div>
                    </div>
                </div>
            </div>

            <!-- Labels Manager Modal -->
            <InstructionLabelsManagerModal
                v-model="showManageLabelsModal"
                :instructions="[]"
                @labels-updated="handleLabelsUpdated"
            />
        </div>
    </Teleport>
</template>

<script setup lang="ts">
import InstructionGlobalCreateComponent from '~/components/InstructionGlobalCreateComponent.vue'
import InstructionPrivateCreateComponent from '~/components/InstructionPrivateCreateComponent.vue'
import InstructionLabelsManagerModal from '~/components/InstructionLabelsManagerModal.vue'
import { usePermissionsLoaded, useCan } from '~/composables/usePermissions'
import Spinner from '~/components/Spinner.vue'
import { onMounted, onUnmounted } from 'vue'

// Define interfaces
interface DataSource {
    id: string
    name: string
    type: string
}

interface SharedForm {
    text: string
    status: 'draft' | 'published' | 'archived'
    category: 'code_gen' | 'data_modeling' | 'general' | 'system' | 'visualizations' | 'dashboard'
    is_seen: boolean
    can_user_toggle: boolean
    private_status: string | null
    global_status: string | null
    label_ids: string[]
    // Unified Instructions System fields
    load_mode: 'always' | 'intelligent' | 'disabled'
    source_type?: 'user' | 'ai' | 'git'
    source_sync_enabled?: boolean
    title?: string | null
}

// Props and Emits
const props = defineProps<{
    modelValue: boolean
    instruction?: any
    initialType?: 'global' | 'private'
    isSuggestion?: boolean
    targetBuildId?: string | null  // If set, update instruction within this existing build
}>()

const emit = defineEmits(['update:modelValue', 'instructionSaved'])

// Reactive state
const selectedDataSources = ref<string[]>([])
const sharedForm = ref<SharedForm>({
    text: '',
    status: 'draft',
    category: 'general',
    is_seen: true,
    can_user_toggle: true,
    private_status: null,
    global_status: 'approved',
    label_ids: [],
    load_mode: 'always',
    source_type: 'user',
    source_sync_enabled: true,
    title: null
})

// View mode state (controlled by child component)
const isInViewMode = ref(true)

// Computed properties
const isEditing = computed(() => !!props.instruction)
const isReadOnly = computed(() => isEditing.value && !useCan('create_instructions'))

// Modal title based on current state
const modalTitle = computed(() => {
    if (!isEditing.value) return 'New Instruction'
    if (isReadOnly.value) return 'View Instruction'
    // When editing: show "View Instruction" in view mode, "Edit Instruction" in edit mode
    return isInViewMode.value ? 'Instruction' : 'Edit Instruction'
})
const isGitSourced = computed(() => props.instruction?.source_type === 'git')
// Use local form state for sync status so UI updates immediately
const isGitSynced = computed(() => isGitSourced.value && sharedForm.value.source_sync_enabled !== false)

const instructionModalOpen = computed({
    get: () => props.modelValue,
    set: (value) => emit('update:modelValue', value)
})

const isAnalyzing = ref(false)
const showImpact = ref(true)
const showRelated = ref(true)
const showManageLabelsModal = ref(false)

// Mock data for the analysis pane
interface PromptSample {
    content: string
    created_at?: string | Date | null
}
const impactScore = ref(0)
const impactedPrompts = ref<PromptSample[]>([])
const relatedInstructions = ref<Array<{ id: string; text: string; status: 'draft' | 'published' | 'archived'; createdByName: string }>>([])

const refreshAnalysis = async () => {
    const text = sharedForm.value?.text || (props.instruction?.text || '')
    if (!text || text.trim().length === 0) {
        // keep mock data if no text
        return
    }
    try {
        isLoadingImpact.value = true
        isLoadingRelated.value = true
        const body = {
            text,
            include: ['impact', 'related_instructions'],
            instruction_id: props.instruction?.id || undefined,
            limits: { prompts: 5, instructions: 5 }
        }
        const { data, error } = await useMyFetch('/instructions/analysis', {
            method: 'POST',
            body
        })
        if (!error.value && data.value) {
            const res = data.value as any
            if (res.impact) {
                impactScore.value = res.impact.score ?? 0
                impactedPrompts.value = Array.isArray(res.impact.prompts) ? res.impact.prompts : []
                impactMatchedCount.value = res.impact.matched_count ?? 0
                impactTotalCount.value = res.impact.total_count ?? 0
            }
            if (res.related_instructions) {
                relatedInstructions.value = (res.related_instructions.items || []).map((it: any) => ({
                    id: it.id,
                    text: it.text,
                    status: it.status,
                    createdByName: it.createdByName || 'unknown'
                }))
            }
        }
    } catch (e) {
        // swallow errors; keep mock data
        console.error('Failed to analyze instruction', e)
    } finally {
        isLoadingImpact.value = false
        isLoadingRelated.value = false
    }
}

// When enabling analysis, fetch live data once
watch(isAnalyzing, (val) => {
    if (val) {
        refreshAnalysis()
    }
})

const formatDate = (d: string | Date | null | undefined) => {
    if (!d) return ''
    const dt = typeof d === 'string' ? new Date(d) : d
    if (!(dt instanceof Date) || isNaN(dt.getTime())) return ''
    return dt.toLocaleString(undefined, { year: 'numeric', month: 'short', day: 'numeric' })
}

// Each section's max height for the analysis panel
const sectionMaxHeight = 'calc((min(85vh, 800px) - 120px) / 2)'

const impactMatchedCount = ref(0)
const impactTotalCount = ref(0)

const isLoadingImpact = ref(false)
const isLoadingRelated = ref(false)

const selectedInstructionType = computed(() => {
    // Check permissions first - admins always use the global component for consistent UI
    const permissionsLoaded = usePermissionsLoaded()
    if (!permissionsLoaded.value) {
        // Default to private to avoid flashing the admin UI. It will correct itself once permissions load.
        return 'private' 
    }
    
    // Admins always use the global component for all instruction types (git, user, AI)
    // This ensures consistent edit UI regardless of instruction source
    if (useCan('create_instructions')) {
        return 'global'
    }
    
    // Non-admins use the private component (for suggestions)
    return 'private'
})

// Non-admins default to suggestions when creating
const effectiveIsSuggestion = computed(() => {
    if (props.isSuggestion !== undefined) return props.isSuggestion
    return !useCan('create_instructions')
})

// Event handlers
const closeModal = () => {
    instructionModalOpen.value = false
    // resetForm is now called by the watcher below
    isAnalyzing.value = false
}

const toggleAnalyze = () => {
    isAnalyzing.value = !isAnalyzing.value
}

const resetForm = () => {
    sharedForm.value = {
        text: '',
        status: 'draft',
        category: 'general',
        is_seen: true,
        can_user_toggle: true,
        private_status: null,
        global_status: 'approved',
        label_ids: [],
        load_mode: 'always',
        source_type: 'user',
        source_sync_enabled: true,
        title: null
    }
    selectedDataSources.value = []
}

const updateSharedForm = (formData: Partial<SharedForm>) => {
    Object.assign(sharedForm.value, formData)
}

const updateSelectedDataSources = (dataSources: string[]) => {
    selectedDataSources.value = dataSources
}

const handleInstructionSaved = (data: any) => {
    emit('instructionSaved', data)
    closeModal()
}

const handleLabelsUpdated = () => {
    // Labels were updated - could emit event or refresh if needed
    // For now, the modal handles its own refresh
}

const handleViewModeChanged = (isViewMode: boolean) => {
    isInViewMode.value = isViewMode
}

const unlinkFromGit = async () => {
    if (!props.instruction?.id) return
    
    try {
        const { data, error } = await useMyFetch(`/api/instructions/${props.instruction.id}`, {
            method: 'PUT',
            body: {
                source_sync_enabled: false
            }
        })
        
        if (error.value) {
            console.error('Failed to unlink from git:', error.value)
            return
        }
        
        if (data.value) {
            // Update the local form state immediately
            // DON'T emit instructionSaved here - let the subsequent save do that
            // This prevents the modal from closing before the save completes
            sharedForm.value.source_sync_enabled = false
        }
    } catch (err) {
        console.error('Error unlinking from git:', err)
    }
}

const relinkToGit = async () => {
    if (!props.instruction?.id) return
    
    try {
        const { data, error } = await useMyFetch(`/api/instructions/${props.instruction.id}`, {
            method: 'PUT',
            body: {
                source_sync_enabled: true
            }
        })
        
        if (error.value) {
            console.error('Failed to relink to git:', error.value)
            return
        }
        
        if (data.value) {
            // Update the local form state immediately
            sharedForm.value.source_sync_enabled = true
            emit('instructionSaved', data.value)
        }
    } catch (err) {
        console.error('Error relinking to git:', err)
    }
}

// Watchers
watch(() => props.instruction, (newInstruction) => {
    if (newInstruction) {
        // Populate the form when an instruction to edit is passed in.
        sharedForm.value = {
            text: newInstruction.text || '',
            status: newInstruction.status || 'draft',
            category: newInstruction.category || 'general',
            is_seen: newInstruction.is_seen !== undefined ? newInstruction.is_seen : true,
            can_user_toggle: newInstruction.can_user_toggle !== undefined ? newInstruction.can_user_toggle : true,
            private_status: newInstruction.private_status || null,
            global_status: newInstruction.global_status || 'approved',
            label_ids: newInstruction.labels?.map((label: any) => label.id) || [],
            load_mode: newInstruction.load_mode || 'always',
            source_type: newInstruction.source_type || 'user',
            source_sync_enabled: newInstruction.source_sync_enabled !== false,
            title: newInstruction.title || null
        }
        selectedDataSources.value = newInstruction.data_sources?.map((ds: DataSource) => ds.id) || []
    } else {
        // If the instruction prop is cleared, reset the form for a clean 'create' state.
        resetForm()
    }
}, { immediate: true })

// Reset the form state only when the modal is closed.
watch(instructionModalOpen, (isOpen) => {
    if (isOpen) {
        // Reset view mode state when modal opens
        isInViewMode.value = true
        if (useCan('create_instructions')) {
            //isAnalyzing.value = true
            //refreshAnalysis()
        }
    } else {
        resetForm()
        isAnalyzing.value = false
        isInViewMode.value = true
    }
})

// Close on ESC key
let escHandler: ((e: KeyboardEvent) => void) | null = null
onMounted(() => {
    escHandler = (e: KeyboardEvent) => {
        if (e.key !== 'Escape') return

        // If any secondary modal (like Manage Labels) is open, let it handle ESC
        // and do not close the main instruction modal.
        if (showManageLabelsModal?.value) {
            return
        }

        if (instructionModalOpen.value) {
            closeModal()
        }
    }
    window.addEventListener('keydown', escHandler)
})
onUnmounted(() => {
    if (escHandler) window.removeEventListener('keydown', escHandler)
})

// Lock body scroll when modal is open
watch(instructionModalOpen, (isOpen) => {
    if (isOpen) {
        document.body.style.overflow = 'hidden'
    } else {
        document.body.style.overflow = ''
    }
}, { immediate: true })

onUnmounted(() => {
    // Ensure body scroll is restored if component unmounts while modal is open
    document.body.style.overflow = ''
})
</script> 