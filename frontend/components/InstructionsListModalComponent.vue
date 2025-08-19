<template>
    <UModal v-model="instructionsListModal" :ui="{ width: 'sm:max-w-4xl' }">
        <div class="p-6 relative">
            <button @click="instructionsListModal = false"
                class="absolute top-2 right-2 text-gray-500 hover:text-gray-700 outline-none">
                <Icon name="heroicons:x-mark" class="w-5 h-5" />
            </button>
            
            <!-- Header -->
            <div class="mb-6">
                <h1 class="text-lg font-semibold">Global Instructions</h1>
                <p class="mt-1 text-sm text-gray-500">Manage global AI instructions</p>
            </div>

            <!-- Filter buttons and Add button -->
            <div class="mb-4 flex justify-between items-center">
                <div class="flex space-x-1 bg-gray-100 p-1 rounded-lg w-fit">
                    <button 
                        @click="setActiveFilter('all')" 
                        :class="[
                            activeFilter === 'all' 
                                ? 'bg-white text-gray-900 shadow-sm' 
                                : 'text-gray-500 hover:text-gray-900',
                            'px-3 py-1.5 text-xs font-medium rounded-md transition-all duration-200'
                        ]"
                    >
                        All Instructions
                    </button>
                    <button 
                        @click="setActiveFilter('my')" 
                        :class="[
                            activeFilter === 'my' 
                                ? 'bg-white text-gray-900 shadow-sm' 
                                : 'text-gray-500 hover:text-gray-900',
                            'px-3 py-1.5 text-xs font-medium rounded-md transition-all duration-200'
                        ]"
                    >
                        {{ myInstructionsButtonText }}
                    </button>
                </div>
                
                <UButton
                    :icon="buttonIcon"
                    color="blue"
                    size="xs"
                    variant="solid"
                    @click="addInstruction"
                >
                    {{ buttonText }}
                </UButton>
            </div>

            <!-- Instructions List -->
            <div v-if="isLoading" class="flex items-center justify-center py-12">
                <USpinner size="lg" />
            </div>
            <div v-else-if="instructions.length > 0">
                <div class="bg-white shadow-sm border border-gray-200 rounded-lg overflow-hidden">
                    <div class="overflow-x-auto">
                        <table class="min-w-full divide-y divide-gray-200">
                            <thead class="bg-gray-50">
                                <tr>
                                    <th class="px-3 py-2 text-left text-xs font-medium text-gray-500 uppercase tracking-wider">
                                        Instruction
                                    </th>
                                    <th class="px-3 py-2 text-center text-xs font-medium text-gray-500 uppercase tracking-wider w-24">
                                        <!-- References -->
                                    </th>
                                    <th class="px-3 py-2 text-center text-xs font-medium text-gray-500 uppercase tracking-wider w-24">
                                        <!-- Icons column -->
                                    </th>
                                </tr>
                            </thead>
                            <tbody class="bg-white divide-y divide-gray-200">
                                <tr v-for="instruction in instructions" 
                                    :key="instruction.id" 
                                    :class="[
                                        'hover:bg-gray-50 cursor-pointer'
                                    ]"
                                    @click="handleInstructionClick(instruction)"
                                >
                                    <td class="px-3 py-2 text-sm">
                                        <div class="max-w-md">
                                            <p class="text-gray-900 leading-tight">{{ instruction.text }}</p>
                                        </div>
                                    </td>
                                    <td class="px-3 py-2 text-sm">
                                        <div class="flex items-center justify-center gap-2">
                                            <template v-if="(instruction as any).references && (instruction as any).references.length">
                                                <UTooltip v-for="ref in (instruction as any).references" :key="ref.id" :text="getRefDisplayName(ref)">
                                                    <UIcon :name="getRefIcon(ref.object_type)" class="w-3 h-3 text-gray-600" />
                                                </UTooltip>
                                            </template>
                                            <span v-else class="text-xs text-gray-400">None</span>
                                        </div>
                                    </td>
                                    <td class="px-3 py-2 text-sm">
                                        <div class="flex items-center justify-center gap-2">
                                            <!-- Status Icon -->
                                            <UTooltip :text="getStatusTooltip(instruction)">
                                                <div :class="getStatusIconClass(instruction)" class="w-3 h-3 rounded-full flex-shrink-0"></div>
                                            </UTooltip>
                                            
                                            <!-- Category Icon -->
                                            <UTooltip :text="'Category: ' + formatCategory(instruction.category)">
                                                <Icon :name="getCategoryIcon(instruction.category)" class="w-4 h-4 text-gray-500 flex-shrink-0" />
                                            </UTooltip>
                                            
                                            <!-- Data Source Icon -->
                                            <UTooltip :text="getDataSourceTooltip(instruction)">
                                                <div class="flex -space-x-1">
                                                    <Icon v-if="instruction.data_sources.length === 0" 
                                                          name="heroicons:globe-alt" 
                                                          class="w-4 h-4 text-gray-500 flex-shrink-0" />
                                                    <DataSourceIcon v-else-if="instruction.data_sources.length === 1"
                                                                    :type="instruction.data_sources[0].type" 
                                                                    class="w-4 h-4 flex-shrink-0" />
                                                    <div v-else class="flex -space-x-1">
                                                        <DataSourceIcon v-for="(dataSource, index) in instruction.data_sources.slice(0, 2)" 
                                                                        :key="dataSource.id"
                                                                        :type="dataSource.type" 
                                                                        class="w-4 h-4 border border-white rounded flex-shrink-0" />
                                                        <div v-if="instruction.data_sources.length > 2" 
                                                             class="w-4 h-4 bg-gray-400 text-white text-xs rounded flex items-center justify-center border border-white flex-shrink-0">
                                                            +{{ instruction.data_sources.length - 2 }}
                                                        </div>
                                                    </div>
                                                </div>
                                            </UTooltip>
                                        </div>
                                    </td>

                                </tr>
                            </tbody>
                        </table>
                    </div>
                </div>
            </div>
            <div v-else class="flex items-center justify-center py-12">
                <div class="flex flex-col items-center justify-center gap-2">
                    <Icon name="heroicons:document-text" class="mx-auto h-12 w-12 text-gray-400" />
                    <h3 class="mt-2 text-sm font-medium text-gray-900">No instructions found</h3>
                    <p class="mt-1 text-sm text-gray-500">{{ getEmptyStateMessage() }}</p>
                </div>
            </div>
        </div>

        <!-- Instruction Modal -->
        <InstructionModalComponent
            v-model="showInstructionModal"
            :instruction="editingInstruction"
            :initial-type="initialInstructionType"
            :is-suggestion="!canCreateGlobalInstructions"
            @instructionSaved="handleInstructionSaved"
        />

        <!-- Instruction Details Modal (Read-only) -->
        <InstructionDetailsModal
            v-model="showDetailsModal"
            :instruction="viewingInstruction"
        />
    </UModal>
</template>

<script setup lang="ts">
import InstructionModalComponent from '~/components/InstructionModalComponent.vue';
import InstructionDetailsModal from '~/components/InstructionDetailsModal.vue';
import DataSourceIcon from '~/components/DataSourceIcon.vue';

// Define interfaces - same as before
interface DataSource {
    id: string
    name: string
    type: string
}

interface User {
    id: string
    name: string
    email: string
}

interface Instruction {
    id: string
    text: string
    thumbs_up: number
    status: 'draft' | 'published' | 'archived'
    category: 'code_gen' | 'data_modeling' | 'general'
    user_id: string
    organization_id: string
    user: User
    data_sources: DataSource[]
    created_at: string
    updated_at: string
    
    // Dual-status lifecycle fields
    private_status: string | null
    global_status: string | null
    is_seen: boolean
    can_user_toggle: boolean
    reviewed_by_user_id: string | null
    reviewed_by?: User
}

const toast = useToast()
const instructionsListModal = ref(false)
const activeFilter = ref('all') // Fix: start with 'all' not 'all-published'
const instructions = ref<Instruction[]>([])
const isLoading = ref(false)
const showInstructionModal = ref(false)
const editingInstruction = ref<Instruction | null>(null)
const showDetailsModal = ref(false)
const viewingInstruction = ref<Instruction | null>(null)

// Check if user can create global instructions
const canCreateGlobalInstructions = computed(() => {
    return useCan('create_instructions')
})

// Dynamic button text based on permissions
const myInstructionsButtonText = computed(() => {
    return canCreateGlobalInstructions.value 
        ? 'My Contributed Instructions' 
        : 'My Suggested Instructions'
})

const initialInstructionType = computed(() => {
    return canCreateGlobalInstructions.value ? 'global' : 'private'
})

// Computed properties for button
const buttonText = computed(() => {
    return canCreateGlobalInstructions.value ? 'Add Instruction' : 'Suggest Instruction'
})

const buttonIcon = computed(() => {
    return canCreateGlobalInstructions.value ? 'i-heroicons-plus' : 'i-heroicons-plus'
})

// Color is set directly in template to avoid TS type mismatch

// Methods
// Get current user ID helper  
const nuxtApp = useNuxtApp() as any
const getCurrentUserId = () => {
    return nuxtApp?.$auth?.user?.id || null
}

// Clean fetch method using the new permission-based parameters
const fetchInstructions = async () => {
    isLoading.value = true
    try {
        const params: any = {
            limit: 1000,
            include_own: true,
        }
        
        if (activeFilter.value === 'my') {
            const currentUserId = getCurrentUserId()
            if (currentUserId) {
                params.user_id = currentUserId
                params.include_drafts = true
                params.include_archived = true // Also fetch archived instructions
            }
        } else {
            params.status = 'published'
            if (useCan('view_hidden_instructions')) {
                params.include_hidden = true
            }
        }
        
        //console.log('Fetching instructions with params:', params)
        
        const { data, error } = await useMyFetch<Instruction[]>('/api/instructions', {
            method: 'GET',
            query: params
        })
        
        if (error.value) {
            console.error('Failed to fetch instructions:', error.value)
            toast.add({
                title: 'Error',
                description: 'Failed to fetch instructions',
                color: 'red'
            })
        } else if (data.value) {
            console.log('Fetched instructions:', data.value.length)
            
            if (activeFilter.value === 'my') {
                // Filter to show instructions that are either suggested, rejected, or private drafts.
                instructions.value = data.value.filter(instruction => {
                    const isSuggested = instruction.global_status === 'suggested'
                    const isRejected = instruction.global_status === 'rejected'
                    const isPrivateDraft = instruction.private_status === 'draft' && !instruction.global_status
                    
                    return isSuggested || isRejected || isPrivateDraft
                })
            } else {
                instructions.value = data.value
            }
        }
    } catch (err) {
        console.error('Error:', err)
        toast.add({
            title: 'Error',
            description: 'Failed to fetch instructions',
            color: 'red'
        })
    } finally {
        isLoading.value = false
    }
}

const setActiveFilter = async (filter: string) => {
    console.log('Setting active filter to:', filter)
    activeFilter.value = filter
    await fetchInstructions()
}

const openModal = () => {
    instructionsListModal.value = true
    fetchInstructions()
}

const handleInstructionClick = (instruction: Instruction) => {
    if (canCreateGlobalInstructions.value) {
        editingInstruction.value = instruction
        showInstructionModal.value = true
    } else {
        // Show read-only details modal for users without global instruction permissions
        viewingInstruction.value = instruction
        showDetailsModal.value = true
    }
}

const handleInstructionSaved = (savedInstruction: Instruction) => {
    fetchInstructions()
    showInstructionModal.value = false
    editingInstruction.value = null
    
    toast.add({
        title: 'Success',
        description: 'Instruction saved successfully',
        color: 'green'
    })
}

const addInstruction = () => {
    editingInstruction.value = null
    showInstructionModal.value = true
}

const getEmptyStateMessage = () => {
    if (activeFilter.value === 'my') {
        return canCreateGlobalInstructions.value
            ? 'You haven\'t contributed any instructions yet.'
            : 'You haven\'t suggested any instructions yet.'
    }
    return canCreateGlobalInstructions.value 
        ? 'No global instructions available yet.' 
        : 'No instructions to review yet.'
}

// Status display functions - same as ConsoleInstructions
const getDisplayStatus = (instruction: Instruction) => {
    return formatStatus(instruction.status) // Draft/Published/Archived
}

const getSubStatus = (instruction: Instruction) => {
    if (instruction.global_status === 'suggested') {
        return 'Pending Review'
    } else if (instruction.reviewed_by_user_id && instruction.global_status) {
        const reviewerName = instruction.reviewed_by?.name || 'Admin'
        
        if (instruction.global_status === 'approved') {
            return `Approved by ${reviewerName}`
        } else if (instruction.global_status === 'rejected') {
            return `Rejected by ${reviewerName}`
        }
    }
    
    return null
}

const getStatusClass = (instruction: Instruction) => {
    if (instruction.global_status === 'suggested') {
        return 'bg-yellow-100 text-yellow-800'
    } else if (instruction.global_status === 'approved') {
        return 'bg-green-100 text-green-800'
    } else if (instruction.global_status === 'rejected') {
        return 'bg-red-100 text-red-800'
    } else {
        const statusClasses = {
            draft: 'bg-yellow-100 text-yellow-800',
            published: 'bg-green-100 text-green-800',
            archived: 'bg-gray-100 text-gray-800'
        }
        return statusClasses[instruction.status as keyof typeof statusClasses] || 'bg-gray-100 text-gray-800'
    }
}

const formatStatus = (status: string) => {
    const statusMap = {
        draft: 'Draft',
        published: 'Published',
        archived: 'Archived'
    }
    return statusMap[status as keyof typeof statusMap] || status
}

// Status icon functions
const getStatusIconClass = (instruction: Instruction) => {
    if (instruction.global_status === 'suggested') {
        return 'bg-yellow-400'
    } else if (instruction.global_status === 'approved') {
        return 'bg-green-400'
    } else if (instruction.global_status === 'rejected') {
        return 'bg-red-400'
    } else {
        const statusClasses = {
            draft: 'bg-yellow-400',
            published: 'bg-green-400',
            archived: 'bg-gray-400'
        }
        return statusClasses[instruction.status as keyof typeof statusClasses] || 'bg-gray-400'
    }
}

const getStatusTooltip = (instruction: Instruction) => {
    const baseStatus = formatStatus(instruction.status)
    const subStatus = getSubStatus(instruction)
    return subStatus ? `${baseStatus} - ${subStatus}` : baseStatus
}

const getCategoryIcon = (category: string) => {
    const categoryIcons = {
        code_gen: 'heroicons:code-bracket',
        data_modeling: 'heroicons:cube',
        general: 'heroicons:document-text'
    }
    return categoryIcons[category as keyof typeof categoryIcons] || 'heroicons:document-text'
}

const getDataSourceTooltip = (instruction: Instruction) => {
    if (instruction.data_sources.length === 0) {
        return 'All Data Sources'
    } else if (instruction.data_sources.length === 1) {
        return instruction.data_sources[0].name
    } else {
        return `${instruction.data_sources.length} Data Sources: ${instruction.data_sources.map(ds => ds.name).join(', ')}`
    }
}

const getRefIcon = (type: string) => {
  if (type === 'metadata_resource') return 'i-heroicons-rectangle-stack'
  if (type === 'datasource_table') return 'i-heroicons-table-cells'
  if (type === 'memory') return 'i-heroicons-book-open'
  return 'i-heroicons-circle'
}

const getRefDisplayName = (ref: any) => {
  const objectType = ref.object_type
  const dataSourceName = ref.data_source_name || 'Unknown'
  
  if (ref.display_text) return `${dataSourceName} - ${objectType}: ${ref.display_text}`
  if (ref.object?.name) return `${dataSourceName} - ${objectType}: ${ref.object.name}`
  if (ref.object?.title) return `${dataSourceName} - ${objectType}: ${ref.object.title}`
  return `${dataSourceName} - ${objectType}`
}

const formatCategory = (category: string) => {
    const categoryMap = {
        code_gen: 'Code Generation',
        data_modeling: 'Data Modeling',
        general: 'General'
    }
    return categoryMap[category as keyof typeof categoryMap] || category
}

onMounted(() => {
    fetchInstructions()
})

defineExpose({ openModal })
</script>