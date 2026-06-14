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
                        maxWidth: isAnalyzing ? '1400px' : '960px',
                        maxHeight: 'min(85vh, 800px)',
                        transition: 'max-width 300ms cubic-bezier(0.4, 0, 0.2, 1)'
                    }"
                >
                    <!-- Header -->
                    <div class="flex items-center justify-between px-5 py-2.5 border-b shrink-0">
                        <span class="text-xs font-medium text-gray-400 uppercase tracking-wide">{{ modalTitle }}</span>
                        <button @click="closeModal" class="text-gray-300 hover:text-gray-500 transition-colors">
                            <Icon name="heroicons:x-mark" class="w-4 h-4" />
                        </button>
                    </div>

                    <!-- Body -->
                    <div class="flex-1 min-h-0">
                        <!-- GLOBAL: component owns its columns; analysis renders in the middle slot -->
                        <InstructionGlobalCreateComponent
                            v-if="selectedInstructionType === 'global'"
                            :instruction="instruction"
                            :analyzing="isAnalyzing"
                            :split-layout="true"
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
                        >
                            <template #analyze>
                                <InstructionAnalysisPanel
                                    :related="relatedForPanel"
                                    :is-loading-related="isLoadingRelated"
                                    :impacted-prompts="impactedPrompts"
                                    :is-loading-impact="isLoadingImpact"
                                    :impact-score="impactScore"
                                    :impact-matched-count="impactMatchedCount"
                                    :impact-total-count="impactTotalCount"
                                    :section-max-height="sectionMaxHeight"
                                    @refresh="refreshAnalysis"
                                />
                            </template>
                        </InstructionGlobalCreateComponent>

                        <!-- PRIVATE: single-column form with analysis panel on the right -->
                        <div
                            v-else
                            class="h-full grid transition-all duration-300 ease-out"
                            :style="{
                                gridTemplateColumns: isAnalyzing ? 'minmax(0, 1.75fr) minmax(0, 1fr)' : '1fr 0px'
                            }"
                        >
                            <div class="flex flex-col h-full overflow-y-auto min-w-0">
                                <InstructionPrivateCreateComponent
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
                            <div class="overflow-hidden">
                                <InstructionAnalysisPanel
                                    v-if="isAnalyzing"
                                    :related="relatedForPanel"
                                    :is-loading-related="isLoadingRelated"
                                    :impacted-prompts="impactedPrompts"
                                    :is-loading-impact="isLoadingImpact"
                                    :impact-score="impactScore"
                                    :impact-matched-count="impactMatchedCount"
                                    :impact-total-count="impactTotalCount"
                                    :section-max-height="sectionMaxHeight"
                                    @refresh="refreshAnalysis"
                                />
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
import InstructionAnalysisPanel from '~/components/InstructionAnalysisPanel.vue'
import InstructionLabelsManagerModal from '~/components/InstructionLabelsManagerModal.vue'
import { usePermissionsLoaded, useCan, useCanAny } from '~/composables/usePermissions'
import Spinner from '~/components/Spinner.vue'
import { onMounted, onUnmounted } from 'vue'

const { t } = useI18n()

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
const isReadOnly = computed(() => isEditing.value && !useCan('manage_instructions'))

// Modal title based on current state
const modalTitle = computed(() => {
    if (!isEditing.value) return t('instructionModal.newTitle')
    if (isReadOnly.value) return t('instructionModal.viewTitle')
    // When editing: show "Instruction" (label) in view mode, "Edit Instruction" in edit mode
    return isInViewMode.value ? t('instructionModal.label') : t('instructionModal.editTitle')
})
const isGitSourced = computed(() => props.instruction?.source_type === 'git')
// Use local form state for sync status so UI updates immediately
const isGitSynced = computed(() => isGitSourced.value && sharedForm.value.source_sync_enabled !== false)

const instructionModalOpen = computed({
    get: () => props.modelValue,
    set: (value) => emit('update:modelValue', value)
})

const isAnalyzing = ref(false)
const showManageLabelsModal = ref(false)

// Mock data for the analysis pane
interface PromptSample {
    content: string
    created_at?: string | Date | null
}
const impactScore = ref(0)
const impactedPrompts = ref<PromptSample[]>([])
const relatedInstructions = ref<Array<{ id: string; text: string; status: 'draft' | 'published' | 'archived'; createdByName: string }>>([])
const matchedTokens = ref<string[]>([])  // Keywords from backend for highlighting

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
                matchedTokens.value = res.related_instructions.tokens || []
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

// Highlight and trim text to show relevant snippets around matched keywords
const highlightAndTrimText = (text: string): string => {
    if (!text) return ''

    const tokens = matchedTokens.value
    if (tokens.length === 0) {
        // No tokens - just return truncated text
        return text.length > 150 ? escapeHtml(text.slice(0, 150)) + '...' : escapeHtml(text)
    }

    // Find positions of all token matches
    const matches: Array<{ start: number; end: number }> = []
    const lowerText = text.toLowerCase()

    for (const token of tokens) {
        let pos = 0
        while ((pos = lowerText.indexOf(token, pos)) !== -1) {
            matches.push({ start: pos, end: pos + token.length })
            pos += 1
        }
    }

    if (matches.length === 0) {
        // No matches found - return truncated text
        return text.length > 150 ? escapeHtml(text.slice(0, 150)) + '...' : escapeHtml(text)
    }

    // Sort matches by position
    matches.sort((a, b) => a.start - b.start)

    // Build snippets around matches (show context around each match)
    const contextChars = 40
    const maxTotalLength = 200
    const snippets: string[] = []
    let totalLength = 0
    let lastEnd = 0

    for (const match of matches) {
        if (totalLength >= maxTotalLength) break

        const snippetStart = Math.max(0, match.start - contextChars)
        const snippetEnd = Math.min(text.length, match.end + contextChars)

        // Skip if this overlaps with previous snippet
        if (snippetStart < lastEnd) continue

        let snippet = ''
        if (snippetStart > 0) snippet += '...'
        snippet += text.slice(snippetStart, snippetEnd)
        if (snippetEnd < text.length) snippet += '...'

        snippets.push(snippet)
        totalLength += snippet.length
        lastEnd = snippetEnd
    }

    // Join snippets and highlight tokens
    let result = snippets.join(' ')

    // Escape HTML first
    result = escapeHtml(result)

    // Highlight all tokens (case insensitive)
    for (const token of tokens) {
        const regex = new RegExp(`(${escapeRegex(token)})`, 'gi')
        result = result.replace(regex, '<mark class="bg-yellow-200 text-yellow-900 px-0.5 rounded">$1</mark>')
    }

    return result
}

const escapeHtml = (str: string): string => {
    return str
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;')
        .replace(/'/g, '&#039;')
}

const escapeRegex = (str: string): string => {
    return str.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
}

// Pre-render the highlighted snippet HTML so the analysis panel stays presentational.
const relatedForPanel = computed(() =>
    relatedInstructions.value.map(it => ({
        ...it,
        highlightedHtml: highlightAndTrimText(it.text),
    }))
)

// Each section's max height for the analysis panel
const sectionMaxHeight = 'calc((min(85vh, 800px) - 120px) / 2)'

const impactMatchedCount = ref(0)
const impactTotalCount = ref(0)

const isLoadingImpact = ref(false)
const isLoadingRelated = ref(false)

const canCreateInstructions = computed(() => useCanAny('manage_instructions', 'data_source'))

const selectedInstructionType = computed(() => {
    // Check permissions first - admins always use the global component for consistent UI
    const permissionsLoaded = usePermissionsLoaded()
    if (!permissionsLoaded.value) {
        // Default to private to avoid flashing the admin UI. It will correct itself once permissions load.
        return 'private'
    }

    // Users with manage_instructions (org-wide OR on any data source) use the global component
    if (canCreateInstructions.value) {
        return 'global'
    }

    // Users without create permission use the private component (for suggestions)
    return 'private'
})

// Users without manage_instructions default to suggestions when creating
const effectiveIsSuggestion = computed(() => {
    if (props.isSuggestion !== undefined) return props.isSuggestion
    return !canCreateInstructions.value
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
        if (useCan('manage_instructions')) {
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