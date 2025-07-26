<template>
    <div>
        <form @submit.prevent="submitForm" class="space-y-4">
            <!-- Instruction Text -->
            <div class="flex flex-col">
                <label class="text-sm font-medium text-gray-700 mb-2">
                    Instruction <span class="text-red-500">*</span>
                </label>
                <UTextarea 
                    v-model="instructionForm.text"
                    :rows="4"
                    placeholder="Enter the instruction text..."
                    class="w-full"
                    required
                />
            </div>

            <!-- Data Sources -->
            <div class="flex flex-col">
                <label class="text-sm font-medium text-gray-700 mb-2">
                    Data Sources
                </label>
                <p class="text-xs text-gray-500 mb-2">Select which data sources this instruction applies to. If none selected, applies to all.</p>
                
                <USelectMenu 
                    v-model="selectedDataSources" 
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
                                class="flex-shrink-0 ml-2"
                            />
                        </div>
                    </template>
                </USelectMenu>
            </div>

            <!-- Status -->
            <div class="flex flex-col">
                <label class="text-sm font-medium text-gray-700 mb-2">
                    Status <span class="text-red-500">*</span>
                </label>
                <USelectMenu 
                    v-model="instructionForm.status" 
                    :options="statusOptions" 
                    option-attribute="label"
                    value-attribute="value"
                    class="w-full"
                    required
                >
                    <UButton color="gray" class="flex-1 justify-start">
                        <span :class="getStatusClass(instructionForm.status)" class="inline-flex px-2 py-1 text-xs font-medium rounded-full mr-2">
                            {{ getCurrentStatusDisplayText() }}
                        </span>
                    </UButton>

                    <template #option="{ option }">
                        <div class="flex items-center gap-2">
                            <span :class="getStatusClass(option.value)" class="inline-flex px-2 py-1 text-xs font-medium rounded-full">
                                {{ option.label }}
                            </span>
                        </div>
                    </template>
                </USelectMenu>
            </div>

            <!-- Category -->
            <div class="flex flex-col">
                <label class="text-sm font-medium text-gray-700 mb-2">
                    Category <span class="text-red-500">*</span>
                </label>
                <USelectMenu 
                    v-model="instructionForm.category" 
                    :options="categoryOptions" 
                    option-attribute="label"
                    value-attribute="value"
                    class="w-full"
                    required
                >
                    <UButton color="gray" class="flex-1 justify-start">
                        <Icon :name="getCategoryIcon(instructionForm.category)" class="w-4 h-4 mr-2" />
                        {{ formatCategory(instructionForm.category) }}
                    </UButton>

                    <template #option="{ option }">
                        <div class="flex items-center gap-2">
                            <Icon :name="getCategoryIcon(option.value)" class="w-4 h-4" />
                            <span>{{ option.label }}</span>
                        </div>
                    </template>
                </USelectMenu>
            </div>

            <!-- Advanced Section -->
            <div class="border border-gray-200 rounded-lg p-4">
                <div class="flex items-center cursor-pointer" @click="showAdvanced = !showAdvanced">
                    <Icon 
                        :name="showAdvanced ? 'heroicons:chevron-down' : 'heroicons:chevron-right'" 
                        class="w-4 h-4 mr-2 transition-transform duration-200"
                    />
                    <h3 class="text-sm font-medium text-gray-700">Advanced Settings</h3>
                </div>
                
                <div v-show="showAdvanced" class="mt-4 space-y-4">
                    <div class="flex items-center justify-between">
                        <div>
                            <label class="text-sm font-medium text-gray-700">Visible in Instruction List</label>
                            <p class="text-xs text-gray-500">Users can see this instruction in the instructions list</p>
                        </div>
                        <UToggle v-model="instructionForm.is_seen" />
                    </div>

                </div>
            </div>

            <!-- Form Actions -->
            <div class="flex justify-between items-center pt-4">
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
                        :label="isEditing ? 'Update Instruction' : 'Create Instruction'"  
                        class="!bg-blue-500 !text-white"
                        :loading="isSubmitting"
                    />
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
const availableDataSources = ref<DataSource[]>([])
const selectedDataSources = ref<string[]>([])

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
            
            // Add flag to indicate this is an admin approval (for suggested instructions)
            is_admin_approval: isEditing.value && props.instruction?.global_status === 'suggested'
        }

        let response
        if (isEditing.value) {
            response = await useMyFetch(`/api/instructions/${props.instruction.id}`, {
                method: 'PUT',
                body: payload
            })
        } else {
            // Use the global endpoint for new instructions
            response = await useMyFetch('/api/instructions/global', {
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

// Watchers
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
    }
}, { immediate: true })

// Lifecycle
onMounted(() => {
    fetchDataSources()
})
</script>