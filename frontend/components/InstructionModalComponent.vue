<template>
    <UModal v-model="instructionModalOpen">
        <div class="p-4 relative">
            <button @click="instructionModalOpen = false" class="absolute top-2 right-2 text-gray-500 hover:text-gray-700">
                <Icon name="heroicons:x-mark" class="w-5 h-5" />
            </button>
            <h1 class="text-lg font-semibold">{{ isEditing ? 'Edit Instruction' : 'Add New Instruction' }}</h1>
            <p class="text-sm text-gray-500">Create or modify instructions for AI agents</p>
            <hr class="my-4" />

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
                                {{ formatStatus(instructionForm.status) }}
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

                <!-- Form Actions -->
                <div class="flex justify-between items-center pt-4">
                    <!-- NEW: Delete button (only show when editing) -->
                    <UButton 
                        v-if="isEditing"
                        label="Delete Instruction" 
                        color="red" 
                        variant="soft" 
                        @click="confirmDelete"
                        :loading="isDeleting"
                    />
                    
                    <!-- Existing buttons moved to right side -->
                    <div class="flex space-x-2">
                        <UButton 
                            label="Cancel" 
                            color="gray" 
                            variant="soft" 
                            @click="closeModal" 
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
    </UModal>
</template>

<script setup lang="ts">
import DataSourceSelectorComponent from '~/components/DataSourceSelectorComponent.vue'

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
}

// Props and Emits
const props = defineProps<{
    modelValue: boolean
    instruction?: any // For editing existing instruction
}>()

const emit = defineEmits(['update:modelValue', 'instructionSaved'])

// Reactive state
const toast = useToast()
const isSubmitting = ref(false)
const isDeleting = ref(false)  // NEW
const selectedDataSources = ref<DataSource[]>([])

// Form data
const instructionForm = ref<InstructionForm>({
    text: '',
    status: 'draft',
    category: 'general'
})

// Computed properties
const instructionModalOpen = computed({
    get: () => props.modelValue,
    set: (value) => emit('update:modelValue', value)
})

const isEditing = computed(() => !!props.instruction)

// Options for dropdowns
const statusOptions = [
    { label: 'Draft', value: 'draft' },
    { label: 'Published', value: 'published' },
    { label: 'Archived', value: 'archived' }
]

const categoryOptions = [
    { label: 'General', value: 'general' },
    { label: 'Code Generation', value: 'code_gen' },
    { label: 'Data Modeling', value: 'data_modeling' }
]

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
const handleDataSourceChange = (dataSources: DataSource[]) => {
    selectedDataSources.value = dataSources
}

const resetForm = () => {
    instructionForm.value = {
        text: '',
        status: 'draft',
        category: 'general'
    }
    selectedDataSources.value = []
    isSubmitting.value = false
}

const closeModal = () => {
    resetForm()
    instructionModalOpen.value = false
}

const submitForm = async () => {
    if (isSubmitting.value) return
    
    isSubmitting.value = true
    
    try {
        // Prepare the payload
        const payload = {
            text: instructionForm.value.text,
            status: instructionForm.value.status,
            category: instructionForm.value.category,
            data_source_ids: selectedDataSources.value.map(ds => ds.id)
        }

        let response
        if (isEditing.value) {
            // Update existing instruction
            response = await useMyFetch(`/api/instructions/${props.instruction.id}`, {
                method: 'PUT',
                body: payload
            })
        } else {
            // Create new instruction
            response = await useMyFetch('/api/instructions', {
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
            closeModal()
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

// NEW: Delete confirmation and handler
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
            closeModal()
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
        // Populate form when editing
        instructionForm.value = {
            text: newInstruction.text || '',
            status: newInstruction.status || 'draft',
            category: newInstruction.category || 'general'
        }
        selectedDataSources.value = newInstruction.data_sources || []
    }
}, { immediate: true })

watch(instructionModalOpen, (newValue) => {
    if (!newValue) {
        resetForm()
    }
})
</script> 