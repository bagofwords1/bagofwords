<template>
    <div>
        <form @submit.prevent="submitForm" class="space-y-4">
            <!-- Show suggestion notice -->
            <div v-if="isSuggestion" class="bg-yellow-50 border border-yellow-200 rounded-lg p-3 mb-4">
                <div class="flex items-center">
                    <Icon name="heroicons:light-bulb" class="w-5 h-5 text-yellow-600 mr-2" />
                    <span class="text-sm font-medium text-yellow-800">Suggestion for Global Instruction</span>
                </div>
                <p class="text-xs text-yellow-600 mt-1">This will be submitted as a suggestion for administrators to review and potentially make available globally.</p>
            </div>

            <!-- Instruction Text -->
            <div class="flex flex-col">
                <label class="text-sm font-medium text-gray-700 mb-2">
                    Instruction <span class="text-red-500">*</span>
                </label>
                <UTextarea 
                    :model-value="sharedForm.text"
                    @update:model-value="updateForm({ text: $event })"
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
                    :options="mentionableOptions"
                    option-attribute="name"
                    value-attribute="id"
                    multiple
                    searchable
                    searchable-placeholder="Search mentionables..."
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
            <div v-if="isSuggestion" class="border border-gray-200 rounded-lg p-4 hidden">
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
                        :label="isEditing ? 'Update Instruction' : (isSuggestion ? 'Submit Suggestion' : 'Create Instruction')"  
                        color="blue"
                        class="!text-white"
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

interface SharedForm {
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
    sharedForm: SharedForm
    selectedDataSources: string[]
    isSuggestion?: boolean
}>()

const emit = defineEmits(['instructionSaved', 'cancel', 'updateForm', 'updateDataSources'])

// Reactive state
const toast = useToast()
const isSubmitting = ref(false)
const isDeleting = ref(false)
const availableDataSources = ref<DataSource[]>([])
const mentionableOptions = ref<MentionableItem[]>([])
const selectedReferences = ref<MentionableItem[]>([])

// Review toggle for suggestions - defaults to true for suggestions
const needsReview = ref(props.isSuggestion || false)

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

const getRefIcon = (type: string) => {
    if (type === 'metadata_resource') return 'i-heroicons-rectangle-stack'
    if (type === 'datasource_table') return 'i-heroicons-table-cells'
    if (type === 'memory') return 'i-heroicons-book-open'
    return 'i-heroicons-circle'
}

const handleReferencesChange = (ids: string[]) => {
    const idSet = new Set(ids)
    selectedReferences.value = mentionableOptions.value.filter(m => idSet.has(m.id))
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
    { label: 'Data Modeling', value: 'data_modeling' }
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

const fetchMentionables = async () => {
    try {
        const { data, error } = await useMyFetch<MentionableItem[]>('/api/mentionables', { method: 'GET' })
        if (!error.value && data.value) {
            mentionableOptions.value = data.value
        }
    } catch (err) {
        console.error('Error fetching mentionables:', err)
    }
}

const initReferencesFromInstruction = () => {
    if (props.instruction && Array.isArray(props.instruction.references)) {
        const map: Record<string, MentionableItem> = {}
        for (const m of mentionableOptions.value) map[m.id] = m
        const preselected: MentionableItem[] = []
        for (const r of props.instruction.references) {
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
        general: 'General'
    }
    return categoryMap[category as keyof typeof categoryMap] || category
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
const submitForm = async () => {
    if (isSubmitting.value) return
    
    isSubmitting.value = true
    
    try {
        console.log('Submitting form with shared form:', props.sharedForm)
        console.log('Selected data sources:', props.selectedDataSources)
        
        const payload = {
            text: props.sharedForm.text,
            status: 'published', // Always published for private instructions
            category: props.sharedForm.category,
            
            // Dual-status approach
            private_status: 'published',
            global_status: props.isSuggestion ? 'suggested' : null,
            
            is_seen: true,
            can_user_toggle: true,
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
                description: `${props.isSuggestion ? 'Suggestion' : 'Instruction'} ${isEditing.value ? 'updated' : 'submitted'} successfully`,
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

// Watch for suggestion prop changes
watch(() => props.isSuggestion, (newValue) => {
    if (newValue) {
        needsReview.value = true
    }
}, { immediate: true })

// Lifecycle
onMounted(() => {
    fetchDataSources()
    fetchMentionables().then(() => initReferencesFromInstruction())
})

watch(() => props.instruction, () => {
    initReferencesFromInstruction()
})
</script>