<template>
    <div>
        <form @submit.prevent="submitForm" class="p-4">
            <!-- Instruction Text -->
            <div class="flex flex-col mx-auto max-w-2xl py-2">
                <label class="text-md font-medium text-gray-800 mb-4">
                    Create Instruction
                </label>
                <textarea 
                    v-model="instructionForm.text"
                    :rows="7"
                    placeholder="Enter the instruction, rule, code, or any other text..."
                    class="w-full text-sm p-3 min-h-[160px] border border-gray-200 rounded-md focus:ring-0 focus:outline-none focus:border-gray-300"
                    required
                />
            </div>

            <!-- Scope (Data Sources + References) -->
            <div class="p-2 border border-gray-200 rounded-md mx-auto max-w-2xl mt-2">
                <div class="flex items-center cursor-pointer  hover:bg-gray-50 p-1" @click="showScope = !showScope">
                    <Icon 
                        :name="showScope ? 'heroicons:chevron-down' : 'heroicons:chevron-right'" 
                        class="w-4 h-4 mr-2 transition-transform duration-200"
                    />
                    <h3 class="text-xs font-medium text-gray-700">Scope</h3>
                </div>

                <div v-show="showScope" class="mt-4 space-y-4">
                    <div class="flex flex-col">
                        <p class="text-xs font-normal text-gray-700">Select data sources and/or references (tables, etc)</p>
                    </div>
                    <!-- Data Sources -->
                    <div class="flex flex-col">
                        <USelectMenu 
                            v-model="selectedDataSources" 
                            :options="dataSourceOptions" 
                            option-attribute="name"
                            value-attribute="id"
                            size="xs"
                            multiple
                            class="w-full text-xs shadow-none"
                        >
                            <template #label>
                                <div class="flex items-center flex-wrap gap-1">
                                    <span v-if="isAllDataSourcesSelected" class="flex items-center">
                                        <div class="flex -space-x-1 mr-2">
                                            <DataSourceIcon v-for="ds in availableDataSources.slice(0, 3)" :key="ds.id" :type="ds.type" class="h-3 border border-white rounded" />
                                            <div v-if="availableDataSources.length > 3" class="h-3 w-3 bg-gray-400 text-white text-[10px] rounded flex items-center justify-center border border-white">+{{ availableDataSources.length - 3 }}</div>
                                        </div>
                                        All Data Sources
                                    </span>
                                    <span v-else-if="selectedDataSources.length === 0" class="text-gray-500">Select data sources</span>
                                    <div v-else class="flex items-center flex-wrap gap-1">
                                        <span v-for="ds in getSelectedDataSourceObjects" :key="ds.id" class="flex items-center bg-blue-100 text-blue-800 text-[10px] px-1.5 py-0.5 rounded">
                                            <DataSourceIcon :type="ds.type" class="h-3 w-3 mr-1" />
                                            {{ ds.name }}
                                        </span>
                                    </div>
                                </div>
                            </template>
                            <template #option="{ option }">
                                <div class="flex items-center justify-between w-full py-1 pr-2">
                                    <div class="flex items-center">
                                        <div v-if="option.id === 'all'" class="flex -space-x-1 mr-2">
                                            <DataSourceIcon v-for="ds in availableDataSources.slice(0, 3)" :key="ds.id" :type="ds.type" class="h-3 w-3 border border-white rounded" />
                                            <div v-if="availableDataSources.length > 3" class="h-3 w-3 bg-gray-400 text-white text-[10px] rounded flex items-center justify-center border border-white">+{{ availableDataSources.length - 3 }}</div>
                                        </div>
                                        <DataSourceIcon v-else :type="option.type" class="w-3 h-3 mr-2" />
                                        <span class="text-xs">{{ option.name }}</span>
                                    </div>
                                    <UCheckbox :model-value="option.id === 'all' ? isAllDataSourcesSelected : selectedDataSources.includes(String(option.id))" @update:model-value="handleDataSourceToggle(String(option.id))" @click.stop class="flex-shrink-0 ml-2" />
                                </div>
                            </template>
                        </USelectMenu>
                    </div>

                    <!-- References -->
                    <div class="flex flex-col">
                        <USelectMenu
                            :options="filteredMentionableOptions"
                            option-attribute="name"
                            value-attribute="id"
                            size="xs"
                            multiple
                            searchable
                            searchable-placeholder="Search references..."
                            :model-value="selectedReferenceIds"
                            @update:model-value="handleReferencesChange"
                            class="w-full text-xs shadow-none border-none" 
                        >
                            <template #label>
                                <div class="flex items-center flex-wrap gap-1">
                                    <span v-if="selectedReferences.length === 0" class="text-gray-500">Select references</span>
                                    <span v-for="ref in selectedReferences" :key="ref.id" class="flex items-center bg-gray-100 text-gray-800 text-[10px] px-1.5 py-0.5 rounded">
                                        <UIcon :name="getRefIcon(ref.type)" class="w-3 h-3 mr-1" />
                                        {{ ref.name }}
                                    </span>
                                </div>
                            </template>
                            <template #option="{ option }">
                                <div class="w-full py-1 px-0.5 ">
                                    <div class="flex items-center gap-2 mb-1">
                                        <UCheckbox :model-value="selectedReferenceIds.includes(String(option.id))" @update:model-value="toggleReference(String(option.id))" @click.stop @mousedown.stop class="flex-shrink-0" />
                                        <UIcon :name="getRefIcon(option.type)" class="w-3 h-3 text-gray-600 flex-shrink-0" />
                                        <span class="text-xs font-medium text-gray-900 truncate">{{ option.name }}</span>
                                    </div>
                                    <div class="flex items-center gap-2 ml-6">
                                        <DataSourceIcon :type="option.data_source_type" class="w-3 h-3 flex-shrink-0" />
                                        <span class="text-[11px] text-gray-500 truncate">{{ option.data_source_name }}</span>
                                    </div>
                                </div>
                            </template>
                        </USelectMenu>
                    </div>
                </div>
            </div>

            <!-- Meta Row: Status & Category -->
            <div class="mx-auto max-w-2xl grid grid-cols-1 sm:grid-cols-2 gap-2 mt-4">
                <div class="flex flex-col">
                    <label class="text-xs font-medium text-gray-600 mb-1.5">Status</label>
                    <USelectMenu size="xs" v-model="instructionForm.status" :options="statusOptions" option-attribute="label" value-attribute="value" class="w-full text-xs" required >
                        <template #label>
                            <div class="inline-flex items-center text-xs">
                                <span :class="getStatusClass(instructionForm.status)" class="inline-flex px-2 py-0.5 text-[11px] font-medium rounded-full">{{ getCurrentStatusDisplayText() }}</span>
                            </div>
                        </template>
                        <template #option="{ option }">
                            <div class="flex items-center gap-2 text-xs">
                                <span :class="getStatusClass(option.value)" class="inline-flex px-2 py-1 !text-xs font-medium rounded-full">{{ option.label }}</span>
                            </div>
                        </template>
                    </USelectMenu>
                </div>
                <div class="flex flex-col">
                    <label class="text-xs font-medium text-gray-600 mb-1.5">Category</label>
                    <USelectMenu size="xs" v-model="instructionForm.category" :options="categoryOptions" option-attribute="label" value-attribute="value" class="w-full text-xs">
                        <template #label>
                            <div class="inline-flex items-center text-xs text-gray-700">
                                <Icon :name="getCategoryIcon(instructionForm.category)" class="w-3 h-3 mr-1.5" />
                                <span class="text-xs py-0.5 px-2">{{ formatCategory(instructionForm.category) }}</span>
                            </div>
                        </template>
                        <template #option="{ option }">
                            <div class="flex items-center gap-2">
                                <Icon :name="getCategoryIcon(option.value)" class="w-3 h-3" />
                                <span class="text-xs py-1 px-2">{{ option.label }}</span>
                            </div>
                        </template>
                    </USelectMenu>
                </div>
            </div>

            <!-- Advanced Section -->
            <div class="p-2 border border-gray-200 rounded-md mx-auto max-w-2xl mt-4">
                <div class="flex items-center cursor-pointer hover:bg-gray-50 p-1" @click="showAdvanced = !showAdvanced">
                    <Icon 
                        :name="showAdvanced ? 'heroicons:chevron-down' : 'heroicons:chevron-right'" 
                        class="w-4 h-4 mr-2 transition-transform duration-200"
                    />
                    <h3 class="text-xs font-medium text-gray-700">Advanced Settings</h3>
                </div>
                
                <div v-show="showAdvanced" class="mt-4 space-y-4">
                    <div class="flex items-center justify-between">
                        <div>
                            <label class="text-xs font-medium text-gray-700">Visible in Instruction List</label>
                            <p class="text-[11px] text-gray-500">Users can see this instruction in the instructions list</p>
                        </div>
                        <UToggle v-model="instructionForm.is_seen" />
                    </div>

                </div>
            </div>

            <!-- Form Actions -->
            <div class="flex justify-between items-center pt-3 mx-auto max-w-2xl">
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
                    <UButton label="Cancel" color="gray" variant="soft" size="xs" @click="$emit('cancel')" />
                    <UButton type="submit" :label="isEditing ? 'Update Instruction' : 'Create Instruction'"  size="xs" class="!bg-blue-500 !text-white" :loading="isSubmitting" />
                </div>
            </div>
        </form>
    </div>
</template>

<script setup lang="ts">
import DataSourceIcon from '~/components/DataSourceIcon.vue'

// Define interfaces
interface DataSource {
    id: string
    name: string
    type: string
}

interface InstructionForm {
    text: string
    status: 'draft' | 'published' | 'archived'
    category: 'code_gen' | 'data_modeling' | 'general'
    
    // Dual-status lifecycle fields
    private_status: string | null
    global_status: string | null
    is_seen: boolean
    can_user_toggle: boolean
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
}>()

const emit = defineEmits(['instructionSaved', 'cancel'])

// Reactive state
const toast = useToast()
const isSubmitting = ref(false)
const isDeleting = ref(false)
const showAdvanced = ref(false)
const showScope = ref(false)
const availableDataSources = ref<DataSource[]>([])
const selectedDataSources = ref<string[]>([])
const mentionableOptions = ref<MentionableItem[]>([])
const selectedReferences = ref<MentionableItem[]>([])

// Form data
const instructionForm = ref<InstructionForm>({
    text: '',
    status: 'draft',
    category: 'general',
    
    // Global instructions: null, approved, draft/published
    private_status: null,
    global_status: 'approved',
    is_seen: true,
    can_user_toggle: true
})

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

const isAllDataSourcesSelected = computed(() => {
    return selectedDataSources.value.includes('all') || selectedDataSources.value.length === 0
})

const getSelectedDataSourceObjects = computed(() => {
    return availableDataSources.value.filter(ds => selectedDataSources.value.includes(ds.id))
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

// Make status options dynamic based on instruction state
const statusOptions = computed(() => {
    const isEditingSuggested = props.instruction && props.instruction.global_status === 'suggested'
    
    if (isEditingSuggested) {
        // For suggested instructions being reviewed by admin
        return [
            { label: 'Draft - Pending Approval', value: 'draft' },
            { label: 'Published - Approve Suggestion', value: 'published' },
            { label: 'Archived - Reject Suggestion', value: 'archived' }
        ]
    } else {
        // For regular global instructions
        return [
            { label: 'Draft', value: 'draft' },
            { label: 'Published', value: 'published' },
            { label: 'Archived', value: 'archived' }
        ]
    }
})

// Helper function to get the right display text for the currently selected status
const getCurrentStatusDisplayText = () => {
    const currentStatus = instructionForm.value.status
    const isEditingSuggested = props.instruction && props.instruction.global_status === 'suggested'
    
    if (isEditingSuggested) {
        const suggestedStatusMap = {
            draft: 'Draft - Pending Approval',
            published: 'Published - Approve Suggestion', 
            archived: 'Archived - Reject Suggestion'
        }
        return suggestedStatusMap[currentStatus as keyof typeof suggestedStatusMap] || currentStatus
    } else {
        return formatStatus(currentStatus)
    }
}

// Options for dropdowns
const categoryOptions = [
    { label: 'General', value: 'general' },
    { label: 'Code Generation', value: 'code_gen' },
    { label: 'Data Modeling', value: 'data_modeling' }
]

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

const handleDataSourceToggle = (dataSourceId: string) => {
    if (dataSourceId === 'all') {
        if (isAllDataSourcesSelected.value) {
            selectedDataSources.value = selectedDataSources.value.filter(id => id !== 'all')
        } else {
            selectedDataSources.value = ['all']
        }
    } else {
        selectedDataSources.value = selectedDataSources.value.filter(id => id !== 'all')
        
        if (selectedDataSources.value.includes(dataSourceId)) {
            selectedDataSources.value = selectedDataSources.value.filter(id => id !== dataSourceId)
        } else {
            selectedDataSources.value.push(dataSourceId)
        }
    }
}

// Helper functions
const formatStatus = (status: string) => {
    const statusMap = {
        draft: 'Draft',
        published: 'Published',
        archived: 'Archived'
    }
    return statusMap[status as keyof typeof statusMap] || status
}

const formatCategory = (category: string) => {
    const categoryMap = {
        code_gen: 'Code Generation',
        data_modeling: 'Data Modeling',
        general: 'General'
    }
    return categoryMap[category as keyof typeof categoryMap] || category
}

const getStatusClass = (status: string) => {
    const statusClasses = {
        draft: 'bg-yellow-100 text-yellow-800',
        published: 'bg-green-100 text-green-800',
        archived: 'bg-gray-100 text-gray-800'
    }
    return statusClasses[status as keyof typeof statusClasses] || 'bg-gray-100 text-gray-800'
}

const getCategoryIcon = (category: string) => {
    const categoryIcons = {
        code_gen: 'heroicons:code-bracket',
        data_modeling: 'heroicons:cube',
        general: 'heroicons:document-text'
    }
    return categoryIcons[category as keyof typeof categoryIcons] || 'heroicons:document-text'
}

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

// Toggle a single reference id from checkbox interaction
const toggleReference = (id: string) => {
    const currentIds = new Set(selectedReferenceIds.value.map(String))
    if (currentIds.has(id)) {
        currentIds.delete(id)
    } else {
        currentIds.add(id)
    }
    handleReferencesChange(Array.from(currentIds))
}

// Validate references when data sources change
const validateSelectedReferences = () => {
    const validReferenceIds = new Set(filteredMentionableOptions.value.map(m => m.id))
    selectedReferences.value = selectedReferences.value.filter(ref => validReferenceIds.has(ref.id))
}

// Event handlers
const resetForm = () => {
    instructionForm.value = {
        text: '',
        status: 'draft',
        category: 'general',
        
        // Global instructions: null, approved, draft/published
        private_status: null,
        global_status: 'approved',
        is_seen: true,
        can_user_toggle: true
    }
    selectedDataSources.value = []
    selectedReferences.value = []
    isSubmitting.value = false
    showAdvanced.value = false
}

const submitForm = async () => {
    if (isSubmitting.value) return
    
    isSubmitting.value = true
    
    try {
        const payload = {
            text: instructionForm.value.text,
            status: instructionForm.value.status,
            category: instructionForm.value.category,
            
            // Dual-status approach for global instructions
            private_status: null,
            global_status: 'approved',
            
            is_seen: instructionForm.value.is_seen,
            can_user_toggle: instructionForm.value.can_user_toggle,
            data_source_ids: isAllDataSourcesSelected.value ? [] : selectedDataSources.value,
            references: selectedReferences.value.map(r => ({
                object_type: r.type,
                object_id: r.id,
                column_name: r.column_name || null,
                relation_type: 'scope'
            })),
            
            // Add flag to indicate this is an admin approval (for suggested instructions)
            is_admin_approval: isEditing.value && props.instruction?.global_status === 'suggested'
        }

        let response
        if (isEditing.value) {
            response = await useMyFetch(`/instructions/${props.instruction.id}`, {
                method: 'PUT',
                body: payload
            })
        } else {
            // Use the global endpoint for new instructions
            response = await useMyFetch('/instructions/global', {
                method: 'POST',
                body: payload
            })
        }

        if (response.status.value === 'success') {
            toast.add({
                title: 'Success',
                description: `Instruction ${isEditing.value ? 'updated' : 'created'} successfully`,
                color: 'green'
            })
            
            emit('instructionSaved', response.data.value)
            resetForm()
        } else {
            throw new Error('Failed to save instruction')
        }
    } catch (error) {
        console.error('Error saving instruction:', error)
        toast.add({
            title: 'Error',
            description: `Failed to ${isEditing.value ? 'update' : 'create'} instruction`,
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
        const response = await useMyFetch(`/instructions/${props.instruction.id}`, {
            method: 'DELETE'
        })
        
        if (response.status.value === 'success') {
            toast.add({
                title: 'Success',
                description: 'Instruction deleted successfully',
                color: 'green'
            })
            
            emit('instructionSaved', { deleted: true, id: props.instruction.id })
            resetForm()
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

// Lifecycle
onMounted(() => {
    fetchDataSources()
    fetchAvailableReferences().then(() => initReferencesFromInstruction())
})

watch(() => props.instruction, (newInstruction) => {
    if (newInstruction) {
        instructionForm.value = {
            text: newInstruction.text || '',
            status: newInstruction.status || 'draft',
            category: newInstruction.category || 'general',
            
            // Global instructions: null, approved, draft/published
            private_status: newInstruction.private_status || null,
            global_status: newInstruction.global_status || 'approved',
            is_seen: newInstruction.is_seen !== undefined ? newInstruction.is_seen : true,
            can_user_toggle: newInstruction.can_user_toggle !== undefined ? newInstruction.can_user_toggle : true
        }
        selectedDataSources.value = newInstruction.data_sources?.map((ds: DataSource) => ds.id) || []
        initReferencesFromInstruction()
    }
}, { immediate: true })

// Validate references when data sources change
watch(() => selectedDataSources.value, () => {
    validateSelectedReferences()
}, { deep: true })
</script>