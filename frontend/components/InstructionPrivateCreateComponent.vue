<template>
    <div class="h-full flex flex-col overflow-hidden">
        <form @submit.prevent="submitForm" class="flex-1 flex flex-col overflow-hidden">
            <div class="p-4 flex-1 overflow-y-auto space-y-4">
            <!-- Git Source Info (above form) -->
            <div v-if="props.isGitSourced" class="mb-3">
                <div class="flex items-center justify-between p-2 bg-gray-50 rounded-lg border border-gray-200">
                    <div class="flex items-center gap-2 text-sm min-w-0">
                        <Icon name="heroicons:code-bracket" class="w-4 h-4 text-gray-500 shrink-0" />
                        <span class="text-gray-600 truncate font-mono text-xs">
                            {{ instruction?.structured_data?.path || instruction?.title || 'Git Repository' }}
                        </span>
                    </div>
                    <div class="flex items-center gap-2 shrink-0 ml-2">
                        <UTooltip 
                            v-if="props.isGitSynced"
                            text="Stop syncing from git. You'll be able to edit manually, but changes from the repository won't update this instruction."
                            :popper="{ placement: 'bottom' }"
                        >
                            <UButton 
                                size="xs" 
                                variant="ghost" 
                                color="orange"
                                @click="$emit('unlink-from-git')"
                            >
                                <Icon name="heroicons:link-slash" class="w-3 h-3 mr-1" />
                                Unlink
                            </UButton>
                        </UTooltip>
                        <template v-else>
                            <span class="text-xs text-gray-500">Unlinked</span>
                            <UTooltip 
                                text="Resume syncing from git. Your manual edits will be overwritten on next repository sync."
                                :popper="{ placement: 'bottom' }"
                            >
                                <UButton 
                                    size="xs" 
                                    variant="ghost" 
                                    color="blue"
                                    @click="$emit('relink-to-git')"
                                >
                                    <Icon name="heroicons:link" class="w-3 h-3 mr-1" />
                                    Relink
                                </UButton>
                            </UTooltip>
                        </template>
                    </div>
                </div>
            </div>

            <!-- Show suggestion notice -->
            <div v-if="isSuggestionEffective" class="bg-yellow-50 border border-yellow-200 rounded-lg p-3 mb-4">
                <div class="flex items-center">
                    <Icon name="heroicons:light-bulb" class="w-5 h-5 text-yellow-600 mr-2" />
                    <span class="text-sm font-medium text-yellow-800">Suggestion for Global Instruction</span>
                </div>
                <p class="text-xs text-yellow-600 mt-1">This will be submitted as a suggestion for administrators to review and potentially make available globally.</p>
            </div>

            <!-- Instruction Text -->
            <div class="flex flex-col">
                <div class="flex items-center justify-between mb-2">
                    <label class="text-sm font-medium text-gray-700">
                        Instruction <span class="text-red-500">*</span>
                    </label>
                    <button 
                        type="button"
                        @click="codeView = !codeView"
                        class="text-gray-400 hover:text-gray-600 p-1 rounded transition-colors"
                        :title="codeView ? 'Switch to text editor' : 'Switch to code editor'"
                    >
                        <Icon :name="codeView ? 'heroicons:document-text' : 'heroicons:code-bracket'" class="w-4 h-4" />
                    </button>
                </div>
                
                <!-- Normal textarea -->
                <UTextarea 
                    v-if="!codeView"
                    :model-value="sharedForm.text"
                    @update:model-value="updateForm({ text: $event })"
                    :rows="4"
                    placeholder="Enter the instruction text..."
                    class="w-full"
                    required
                />
                
                <!-- Code editor (Monaco with white background) -->
                <ClientOnly v-else>
                    <div class="border border-gray-300 rounded-md overflow-hidden">
                        <MonacoEditor
                            :model-value="sharedForm.text"
                            @update:model-value="updateForm({ text: $event })"
                            lang="sql"
                            :options="{ 
                                theme: 'vs', 
                                automaticLayout: true, 
                                minimap: { enabled: false }, 
                                wordWrap: 'on',
                                lineNumbers: 'on',
                                fontSize: 12,
                                scrollBeyondLastLine: false
                            }"
                            style="height: 150px"
                        />
                    </div>
                </ClientOnly>
            </div>

            <!-- Data Sources -->
            <div class="flex flex-col">
                <label class="text-sm font-medium text-gray-700 mb-2">
                    Data Sources
                </label>
                <p class="text-xs text-gray-500 mb-2">Select which data sources this instruction applies to. If none selected, applies to all.</p>
                
                <USelectMenu 
                    :model-value="selectedDataSources"
                    @update:model-value="updateDataSources"
                    :options="dataSourceOptions" 
                    option-attribute="name"
                    value-attribute="id"
                    multiple
                    class="w-full"
                    :ui="{
                        base: 'border border-gray-300 rounded-md',
                    }"
                    :uiMenu="{
                        base: 'w-full max-h-60 overflow-y-auto',
                    }"
                >
                    <template #label>
                        <div class="flex items-center flex-wrap gap-1">
                            <span v-if="isAllDataSourcesSelected" class="flex items-center">
                                <div class="flex -space-x-1 mr-2">
                                    <DataSourceIcon 
                                        v-for="ds in availableDataSources.slice(0, 3)" 
                                        :key="ds.id" 
                                        :type="ds.type" 
                                        class="h-4 w-4 border border-white rounded" 
                                    />
                                    <div v-if="availableDataSources.length > 3" 
                                         class="h-4 w-4 bg-gray-400 text-white text-xs rounded flex items-center justify-center border border-white">
                                        +{{ availableDataSources.length - 3 }}
                                    </div>
                                </div>
                                All Data Sources
                            </span>
                            <span v-else-if="selectedDataSources.length === 0" class="text-gray-500">
                                Select data sources (default: all)
                            </span>
                            <div v-else class="flex items-center flex-wrap gap-1">
                                <span v-for="ds in getSelectedDataSourceObjects" :key="ds.id" 
                                      class="flex items-center bg-blue-100 text-blue-800 text-xs px-2 py-1 rounded">
                                    <DataSourceIcon :type="ds.type" class="h-3 w-3 mr-1" />
                                    {{ ds.name }}
                                </span>
                            </div>
                        </div>
                    </template>

                    <template #option="{ option }">
                        <div class="flex items-center justify-between w-full py-2 pr-2">
                            <div class="flex items-center">
                                <div v-if="option.id === 'all'" class="flex -space-x-1 mr-2">
                                    <DataSourceIcon 
                                        v-for="ds in availableDataSources.slice(0, 3)" 
                                        :key="ds.id" 
                                        :type="ds.type" 
                                        class="h-4 w-4 border border-white rounded" 
                                    />
                                    <div v-if="availableDataSources.length > 3" 
                                         class="h-4 w-4 bg-gray-400 text-white text-xs rounded flex items-center justify-center border border-white">
                                        +{{ availableDataSources.length - 3 }}
                                    </div>
                                </div>
                                <DataSourceIcon v-else :type="option.type" class="w-4 h-4 mr-2" />
                                <span>{{ option.name }}</span>
                            </div>
                            <UCheckbox 
                                :model-value="option.id === 'all' ? isAllDataSourcesSelected : selectedDataSources.includes(option.id)"
                                @update:model-value="handleDataSourceToggle(option.id)"
                                class="flex-shrink-0"
                            />
                        </div>
                    </template>
                </USelectMenu>
            </div>

            <!-- References -->
            <div class="flex flex-col">
                <label class="text-sm font-medium text-gray-700 mb-2">
                    References
                </label>
                <p class="text-xs text-gray-500 mb-2">Select metadata resources, data source tables, or memories this instruction targets.</p>
                <USelectMenu
                    :options="filteredMentionableOptions"
                    option-attribute="name"
                    value-attribute="id"
                    multiple
                    searchable
                    searchable-placeholder="Search references..."
                    :model-value="selectedReferenceIds"
                    @update:model-value="handleReferencesChange"
                    class="w-full"
                    :ui="{ base: 'border border-gray-300 rounded-md' }"
                    :uiMenu="{ base: 'w-full max-h-60 overflow-y-auto' }"
                >
                    <template #label>
                        <div class="flex items-center flex-wrap gap-1">
                            <span v-if="selectedReferences.length === 0" class="text-gray-500">Select references</span>
                            <span v-for="ref in selectedReferences" :key="ref.id" class="flex items-center bg-gray-100 text-gray-800 text-xs px-2 py-1 rounded">
                                <UIcon :name="getRefIcon(ref.type)" class="w-3 h-3 mr-1" />
                                {{ ref.name }}
                            </span>
                        </div>
                    </template>
                    <template #option="{ option }">
                        <div class="flex items-center justify-between w-full py-2 pr-2">
                            <div class="flex items-center gap-2">
                                <UIcon :name="getRefIcon(option.type)" class="w-4 h-4" />
                                <div class="flex flex-col">
                                    <span class="text-sm">{{ option.name }}</span>
                                    <span class="text-xs text-gray-500">{{ option.type }}</span>
                                </div>
                            </div>
                            <UCheckbox :model-value="selectedReferenceIds.includes(option.id)" />
                        </div>
                    </template>
                </USelectMenu>
            </div>

            <!-- Load Mode -->
            <div class="flex flex-col">
                <label class="text-sm font-medium text-gray-700 mb-2">
                    AI Context Loading
                </label>
                <div class="flex items-center gap-2">
                    <UButton 
                        size="sm" 
                        :variant="sharedForm.load_mode === 'always' ? 'solid' : 'outline'" 
                        :color="sharedForm.load_mode === 'always' ? 'blue' : 'gray'"
                        @click="updateForm({ load_mode: 'always' })"
                    >
                        Always
                    </UButton>
                    <UButton 
                        size="sm" 
                        :variant="sharedForm.load_mode === 'intelligent' ? 'solid' : 'outline'" 
                        :color="sharedForm.load_mode === 'intelligent' ? 'purple' : 'gray'"
                        @click="updateForm({ load_mode: 'intelligent' })"
                    >
                        Smart
                    </UButton>
                    <UButton 
                        size="sm" 
                        :variant="sharedForm.load_mode === 'disabled' ? 'solid' : 'outline'" 
                        :color="sharedForm.load_mode === 'disabled' ? 'gray' : 'gray'"
                        @click="updateForm({ load_mode: 'disabled' })"
                    >
                        Disabled
                    </UButton>
                </div>
                <p class="text-xs text-gray-500 mt-1">
                    <span v-if="sharedForm.load_mode === 'always'">Always included in AI context</span>
                    <span v-else-if="sharedForm.load_mode === 'intelligent'">Included when relevant to the query</span>
                    <span v-else>Never included in AI context</span>
                </p>
            </div>

            <!-- Category -->
            <div class="flex flex-col">
                <label class="text-sm font-medium text-gray-700 mb-2">
                    Category <span class="text-red-500">*</span>
                </label>
                <USelectMenu 
                    :model-value="sharedForm.category"
                    @update:model-value="updateForm({ category: $event })"
                    :options="categoryOptions" 
                    option-attribute="label"
                    value-attribute="value"
                    class="w-full"
                    required
                >
                    <UButton color="gray" class="flex-1 justify-start">
                        <Icon :name="getCategoryIcon(sharedForm.category)" class="w-4 h-4 mr-2" />
                        {{ formatCategory(sharedForm.category) }}
                    </UButton>

                    <template #option="{ option }">
                        <div class="flex items-center gap-2">
                            <Icon :name="getCategoryIcon(option.value)" class="w-4 h-4" />
                            <span>{{ option.label }}</span>
                        </div>
                    </template>
                </USelectMenu>
            </div>

            <!-- Review Section for Suggestions -->
            <div v-if="isSuggestionEffective" class="border border-gray-200 rounded-lg p-4 hidden">
                <h3 class="text-sm font-medium text-gray-700 mb-3">Review Settings</h3>
                
                <div class="flex items-center justify-between">
                    <div>
                        <label class="text-sm font-medium text-gray-700">Request Admin Review</label>
                        <p class="text-xs text-gray-500">Submit this suggestion for administrator review to potentially make it globally available</p>
                    </div>
                    <UToggle 
                        v-model="needsReview"
                    />
                </div>
            </div>

            </div>

            <!-- Form Actions (fixed at bottom) -->
            <div class="shrink-0 bg-white border-t p-4">
                <div class="flex justify-between items-center">
                    <!-- Delete button (only show when editing) -->
                    <UButton 
                        v-if="isEditing"
                        label="Delete Instruction" 
                        color="red" 
                        variant="soft" 
                        @click="confirmDelete"
                        :loading="isDeleting"
                    />
                    
                    <div class="flex space-x-2" :class="{ 'ml-auto': !isEditing }">
                        <UButton 
                            label="Cancel" 
                            color="gray" 
                            variant="soft" 
                            @click="$emit('cancel')" 
                        />
                        <UButton 
                            type="submit" 
                            :label="isEditing ? 'Update Instruction' : (isSuggestionEffective ? 'Submit Suggestion' : 'Create Instruction')"  
                            color="blue"
                            class="!text-white"
                            :loading="isSubmitting"
                        />
                    </div>
                </div>
            </div>
        </form>
    </div>
</template>

<script setup lang="ts">
import DataSourceIcon from '~/components/DataSourceIcon.vue'
import { useCan } from '~/composables/usePermissions'

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
    
    // Dual-status lifecycle fields
    private_status: string | null
    global_status: string | null
    is_seen: boolean
    can_user_toggle: boolean
    
    // Unified Instructions System fields
    load_mode: 'always' | 'intelligent' | 'disabled'
}

interface MentionableItem {
    id: string
    type: 'metadata_resource' | 'datasource_table' | 'memory'
    name: string
    data_source_id?: string
    column_name?: string | null
}

// Props and Emits
const props = defineProps<{
    instruction?: any
    sharedForm: SharedForm
    selectedDataSources: string[]
    isSuggestion?: boolean
    isGitSourced?: boolean
    isGitSynced?: boolean
}>()

const emit = defineEmits(['instructionSaved', 'cancel', 'updateForm', 'updateDataSources', 'unlink-from-git', 'relink-to-git'])

// Reactive state
const toast = useToast()
const isSubmitting = ref(false)
const isDeleting = ref(false)
const availableDataSources = ref<DataSource[]>([])
const mentionableOptions = ref<MentionableItem[]>([])
const selectedReferences = ref<MentionableItem[]>([])
const codeView = ref(false)

// Determine if this should be treated as a suggestion (non-admins can only suggest)
const isSuggestionEffective = computed(() => props.isSuggestion || !useCan('create_instructions'))

// Review toggle for suggestions - defaults to true for suggestions
const needsReview = ref(isSuggestionEffective.value || false)

// Computed properties
const isEditing = computed(() => !!props.instruction)

const dataSourceOptions = computed(() => {
    const allOption = {
        id: 'all',
        name: 'All Data Sources',
        type: 'all'
    }
    return [allOption, ...availableDataSources.value]
})
const selectedReferenceIds = computed(() => selectedReferences.value.map(r => r.id))

// Filter mentionable options based on selected data sources
const filteredMentionableOptions = computed(() => {
    // If all data sources are selected (or none selected), show all references
    if (isAllDataSourcesSelected.value) {
        return mentionableOptions.value
    }
    
    // Otherwise, filter by selected data sources
    return mentionableOptions.value.filter(option => {
        // Memory type references are not tied to data sources
        if (option.type === 'memory') {
            return true
        }
        
        // For metadata_resource and datasource_table, check data_source_id
        if (option.data_source_id) {
            return selectedDataSources.value.includes(option.data_source_id)
        }
        
        // If no data_source_id, include it (fallback)
        return true
    })
})

const getRefIcon = (type: string) => {
    if (type === 'metadata_resource') return 'i-heroicons-rectangle-stack'
    if (type === 'datasource_table') return 'i-heroicons-table-cells'
    if (type === 'memory') return 'i-heroicons-book-open'
    return 'i-heroicons-circle'
}

const handleReferencesChange = (ids: string[]) => {
    const idSet = new Set(ids)
    selectedReferences.value = filteredMentionableOptions.value.filter(m => idSet.has(m.id))
}

// Validate references when data sources change
const validateSelectedReferences = () => {
    const validReferenceIds = new Set(filteredMentionableOptions.value.map(m => m.id))
    selectedReferences.value = selectedReferences.value.filter(ref => validReferenceIds.has(ref.id))
}

const isAllDataSourcesSelected = computed(() => {
    return props.selectedDataSources.includes('all') || props.selectedDataSources.length === 0
})

const getSelectedDataSourceObjects = computed(() => {
    return availableDataSources.value.filter(ds => props.selectedDataSources.includes(ds.id))
})

// Options for dropdowns
const categoryOptions = [
    { label: 'General', value: 'general' },
    { label: 'Code Generation', value: 'code_gen' },
    { label: 'Data Modeling', value: 'data_modeling' },
    { label: 'System', value: 'system' },
    { label: 'Visualizations', value: 'visualizations' },
    { label: 'Dashboard', value: 'dashboard' }
]

// Methods
const updateForm = (updates: Partial<SharedForm>) => {
    emit('updateForm', updates)
}

const updateDataSources = (dataSources: string[]) => {
    emit('updateDataSources', dataSources)
}

// Data source methods
const fetchDataSources = async () => {
    try {
        const { data, error } = await useMyFetch<DataSource[]>('/data_sources/active', {
            method: 'GET'
        })
        
        if (error.value) {
            console.error('Failed to fetch data sources:', error.value)
        } else if (data.value) {
            availableDataSources.value = data.value
        }
    } catch (err) {
        console.error('Error fetching data sources:', err)
    }
}

const fetchAvailableReferences = async () => {
    try {
        const { data, error } = await useMyFetch<MentionableItem[]>('/instructions/available-references', { method: 'GET' })
        if (!error.value && data.value) {
            mentionableOptions.value = data.value
        }
    } catch (err) {
        console.error('Error fetching available references:', err)
    }
}

const initReferencesFromInstruction = () => {
    if (props.instruction && Array.isArray(props.instruction.references)) {
        const map: Record<string, MentionableItem> = {}
        for (const m of mentionableOptions.value) map[m.id] = m
        
        // Use a Set to deduplicate by object_id
        const seenObjectIds = new Set<string>()
        const preselected: MentionableItem[] = []
        
        for (const r of props.instruction.references) {
            // Skip duplicates
            if (seenObjectIds.has(r.object_id)) continue
            seenObjectIds.add(r.object_id)
            
            const existing = map[r.object_id]
            if (existing) {
                preselected.push({ ...existing, column_name: r.column_name || null })
            } else {
                // Fallback if not in options yet
                preselected.push({ id: r.object_id, type: r.object_type, name: r.display_text || r.object_type, column_name: r.column_name || null })
            }
        }
        selectedReferences.value = preselected
    }
}

const handleDataSourceToggle = (dataSourceId: string) => {
    let newSelectedDataSources = [...props.selectedDataSources]
    
    if (dataSourceId === 'all') {
        if (isAllDataSourcesSelected.value) {
            newSelectedDataSources = newSelectedDataSources.filter(id => id !== 'all')
        } else {
            newSelectedDataSources = ['all']
        }
    } else {
        newSelectedDataSources = newSelectedDataSources.filter(id => id !== 'all')
        
        if (newSelectedDataSources.includes(dataSourceId)) {
            newSelectedDataSources = newSelectedDataSources.filter(id => id !== dataSourceId)
        } else {
            newSelectedDataSources.push(dataSourceId)
        }
    }
    
    updateDataSources(newSelectedDataSources)
}

// Helper functions
const formatCategory = (category: string) => {
    const categoryMap = {
        code_gen: 'Code Generation',
        data_modeling: 'Data Modeling',
        general: 'General',
        system: 'System',
        visualizations: 'Visualizations',
        dashboard: 'Dashboard'
    }
    return categoryMap[category as keyof typeof categoryMap] || category
}

const getCategoryIcon = (category: string) => {
    const categoryIcons = {
        code_gen: 'heroicons:code-bracket',
        data_modeling: 'heroicons:cube',
        general: 'heroicons:document-text',
        system: 'heroicons:cog-6-tooth',
        visualizations: 'heroicons:chart-bar',
        dashboard: 'heroicons:squares-2x2'
    }
    return categoryIcons[category as keyof typeof categoryIcons] || 'heroicons:document-text'
}

// Event handlers
const submitForm = async () => {
    if (isSubmitting.value) return
    
    isSubmitting.value = true
    
    try {
        console.log('Submitting form with shared form:', props.sharedForm)
        console.log('Selected data sources:', props.selectedDataSources)
        
        const payload = {
            text: props.sharedForm.text,
            status: isSuggestionEffective.value ? 'draft' : 'published',
            category: props.sharedForm.category,

            // Dual-status approach
            private_status: isSuggestionEffective.value ? 'draft' : 'published',
            global_status: isSuggestionEffective.value ? 'suggested' : null,

            is_seen: true,
            can_user_toggle: true,
            load_mode: props.sharedForm.load_mode || 'always',
            data_source_ids: isAllDataSourcesSelected.value ? [] : props.selectedDataSources,
            references: selectedReferences.value.map(r => ({
                object_type: r.type,
                object_id: r.id,
                column_name: r.column_name || null,
                relation_type: 'scope'
            }))
        }

        console.log('Payload:', payload)

        let response
        if (isEditing.value) {
            response = await useMyFetch(`/api/instructions/${props.instruction.id}`, {
                method: 'PUT',
                body: payload
            })
        } else {
            response = await useMyFetch('/api/instructions', {
                method: 'POST',
                body: payload
            })
        }

        console.log('Response:', response)

        if (response.status.value === 'success') {
            toast.add({
                title: 'Success',
                description: `${isSuggestionEffective.value ? 'Suggestion' : 'Instruction'} ${isEditing.value ? 'updated' : 'submitted'} successfully`,
                color: 'green'
            })
            
            emit('instructionSaved', response.data.value)
        } else {
            throw new Error('Failed to save instruction')
        }
    } catch (error) {
        console.error('Error saving instruction:', error)
        toast.add({
            title: 'Error',
            description: `Failed to ${isEditing.value ? 'update' : 'submit'} ${props.isSuggestion ? 'suggestion' : 'instruction'}`,
            color: 'red'
        })
    } finally {
        isSubmitting.value = false
    }
}

const confirmDelete = async () => {
    if (!props.instruction?.id) return
    
    const confirmed = window.confirm(
        `Are you sure you want to delete the instruction "${props.instruction.text.substring(0, 50)}${props.instruction.text.length > 50 ? '...' : ''}"?`
    )
    
    if (!confirmed) return
    
    isDeleting.value = true
    
    try {
        const response = await useMyFetch(`/api/instructions/${props.instruction.id}`, {
            method: 'DELETE'
        })
        
        if (response.status.value === 'success') {
            toast.add({
                title: 'Success',
                description: 'Instruction deleted successfully',
                color: 'green'
            })
            
            emit('instructionSaved', { deleted: true, id: props.instruction.id })
        } else {
            throw new Error('Failed to delete instruction')
        }
    } catch (error) {
        console.error('Error deleting instruction:', error)
        toast.add({
            title: 'Error',
            description: 'Failed to delete instruction',
            color: 'red'
        })
    } finally {
        isDeleting.value = false
    }
}

// Watch for suggestion status changes
watch(isSuggestionEffective, (newValue) => {
    if (newValue) {
        needsReview.value = true
    }
}, { immediate: true })

// Lifecycle
onMounted(() => {
    fetchDataSources()
    fetchAvailableReferences().then(() => initReferencesFromInstruction())
})

watch(() => props.instruction, () => {
    initReferencesFromInstruction()
})

// Validate references when data sources change
watch(() => props.selectedDataSources, () => {
    validateSelectedReferences()
}, { deep: true })
</script>