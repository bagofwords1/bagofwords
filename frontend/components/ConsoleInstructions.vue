<template>
    <div class="mt-6">
        <!-- Header with search and add button -->
        <div class="flex justify-between items-center mb-6">
            <div class="flex-1 max-w-md">
                <div class="relative">
                    <input
                        v-model="searchQuery"
                        type="text"
                        placeholder="Search instructions..."
                        class="w-full pl-10 pr-4 py-2 border border-gray-300 rounded-md focus:outline-none focus:ring-2 focus:ring-blue-500 focus:border-blue-500"
                    />
                    <UIcon name="i-heroicons-magnifying-glass" class="absolute left-3 top-2.5 h-4 w-4 text-gray-400" />
                </div>
            </div>
            
            <UButton
                icon="i-heroicons-plus"
                color="blue"
                variant="solid"
                @click="addInstruction"
                class="ml-4"
            >
                Add Instruction
            </UButton>
        </div>

        <!-- Loading state -->
        <div v-if="isLoading" class="flex items-center justify-center py-12">
            <div class="flex items-center space-x-2">
                <div class="animate-spin rounded-full h-6 w-6 border-b-2 border-blue-600"></div>
                <span class="text-gray-600">Loading instructions...</span>
            </div>
        </div>

        <!-- Instructions Table -->
        <div v-else class="bg-white shadow-sm border border-gray-200 rounded-lg overflow-hidden">
            <div class="overflow-x-auto">
                <table class="min-w-full divide-y divide-gray-200">
                    <thead class="bg-gray-50">
                        <tr>
                            <th class="px-6 py-3 text-left text-xs font-medium text-gray-500 uppercase tracking-wider">
                                Instruction
                            </th>
                            <th class="px-6 py-3 text-left text-xs font-medium text-gray-500 uppercase tracking-wider">
                                Data Sources
                            </th>
                            <th class="px-6 py-3 text-left text-xs font-medium text-gray-500 uppercase tracking-wider">
                                References
                            </th>
                            <th class="px-6 py-3 text-left text-xs font-medium text-gray-500 uppercase tracking-wider">
                                User
                            </th>
                            <th class="px-6 py-3 text-left text-xs font-medium text-gray-500 uppercase tracking-wider">
                                Status
                            </th>
                            <th class="px-6 py-3 text-left text-xs font-medium text-gray-500 uppercase tracking-wider">
                                Category
                            </th>
                            <th class="px-6 py-3 text-left text-xs font-medium text-gray-500 uppercase tracking-wider">
                                Created
                            </th>
                        </tr>
                    </thead>
                    <tbody class="bg-white divide-y divide-gray-200">
                        <tr v-for="instruction in paginatedInstructions" :key="instruction.id" class="hover:bg-gray-50 cursor-pointer" @click="editInstruction(instruction)">
                            <td class="px-6 py-4">
                                <div class="text-sm text-gray-900 max-w-md">
                                    <p class="truncate" :title="instruction.text">
                                        {{ instruction.text }}
                                    </p>
                                </div>
                            </td>
                            <td class="px-6 py-4">
                                <div class="flex items-center space-x-1">
                                    <div v-if="instruction.data_sources && instruction.data_sources.length > 0" class="flex items-center space-x-1">
                                        <DataSourceIcon
                                            v-for="dataSource in instruction.data_sources.slice(0, 3)"
                                            :key="dataSource.id"
                                            :type="dataSource.type"
                                            class="w-5 h-5"
                                            :title="dataSource.name"
                                        />
                                        <span v-if="instruction.data_sources.length > 3" class="text-xs text-gray-500">
                                            +{{ instruction.data_sources.length - 3 }}
                                        </span>
                                    </div>
                                    <div v-else class="flex items-center text-xs text-gray-500">
                                        <UIcon name="i-heroicons-globe-alt" class="w-4 h-4 mr-1" />
                                        <span>Global</span>
                                    </div>
                                </div>
                            </td>
                            <td class="px-6 py-4">
                                <div class="flex items-center space-x-1">
                                    <template v-if="(instruction as any).references && (instruction as any).references.length">
                                        <UTooltip v-for="ref in (instruction as any).references.slice(0,3)" :key="ref.id" :text="getRefDisplayName(ref)">
                                            <UIcon :name="getRefIcon(ref.object_type)" class="w-4 h-4 text-gray-600" />
                                        </UTooltip>
                                        <span v-if="(instruction as any).references.length > 3" class="text-xs text-gray-500">+{{ (instruction as any).references.length - 3 }}</span>
                                    </template>
                                    <span v-else class="text-xs text-gray-400">None</span>
                                </div>
                            </td>
                            <td class="px-6 py-4">
                                <div class="text-sm text-gray-900">
                                    {{ instruction.user?.name || 'AI Generated' }}
                                </div>
                            </td>
                            <td class="px-6 py-4">
                                <div class="flex flex-col">
                                    <span :class="getStatusClass(instruction)" class="inline-flex px-2 py-1 text-xs font-medium rounded-full w-fit">
                                        {{ getDisplayStatus(instruction) }}
                                    </span>
                                    <span v-if="getSubStatus(instruction)" class="text-xs text-gray-500 mt-1">
                                        {{ getSubStatus(instruction) }}
                                    </span>
                                </div>
                            </td>
                            <td class="px-6 py-4">
                                <span class="text-sm text-gray-600">
                                    {{ formatCategory(instruction.category) }}
                                </span>
                            </td>
                            <td class="px-6 py-4">
                                <span class="text-sm text-gray-500">
                                    {{ formatDate(instruction.created_at) }}
                                </span>
                            </td>
                        </tr>
                    </tbody>
                </table>
            </div>

            <!-- Empty state -->
            <div v-if="filteredInstructions.length === 0" class="text-center py-12">
                <UIcon name="i-heroicons-document-text" class="mx-auto h-12 w-12 text-gray-400" />
                <h3 class="mt-2 text-sm font-medium text-gray-900">No instructions found</h3>
                <p class="mt-1 text-sm text-gray-500">
                    {{ searchQuery ? 'Try adjusting your search query.' : 'Get started by creating your first instruction.' }}
                </p>
            </div>
        </div>

        <!-- Pagination -->
        <div v-if="filteredInstructions.length > pageSize" class="mt-6 flex items-center justify-between">
            <div class="text-sm text-gray-700">
                Showing {{ (currentPage - 1) * pageSize + 1 }} to {{ Math.min(currentPage * pageSize, filteredInstructions.length) }} of {{ filteredInstructions.length }} instructions
            </div>
            
            <div class="flex items-center space-x-2">
                <UButton
                    icon="i-heroicons-chevron-left"
                    color="gray"
                    variant="ghost"
                    size="sm"
                    @click="currentPage--"
                    :disabled="currentPage === 1"
                >
                    Previous
                </UButton>
                
                <div class="flex items-center space-x-1">
                    <UButton
                        v-for="page in visiblePages"
                        :key="page"
                        :color="page === currentPage ? 'blue' : 'gray'"
                        :variant="page === currentPage ? 'solid' : 'ghost'"
                        size="sm"
                        @click="currentPage = page"
                        class="min-w-[32px]"
                    >
                        {{ page }}
                    </UButton>
                </div>
                
                <UButton
                    icon="i-heroicons-chevron-right"
                    color="gray"
                    variant="ghost"
                    size="sm"
                    @click="currentPage++"
                    :disabled="currentPage === totalPages"
                >
                    Next
                </UButton>
            </div>
        </div>
    </div>

    <!-- Instruction Modal -->
    <InstructionModalComponent
        v-model="showInstructionModal"
        :instruction="editingInstruction"
        @instructionSaved="handleInstructionSaved"
    />
</template>

<script setup lang="ts">
import DataSourceIcon from '~/components/DataSourceIcon.vue'
import InstructionModalComponent from '~/components/InstructionModalComponent.vue'

// Define interfaces based on the backend schema
interface DataSource {
    id: string
    name: string
    type: string
    organization_id: string
    created_at: string
    updated_at: string
    context?: string
    description?: string
    summary?: string
    conversation_starters?: any[]
    is_active: boolean
    config: Record<string, any>
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
    private_status: string | null  // draft, published, archived (null for global-only)
    global_status: string | null   // null, suggested, approved, rejected
    is_seen: boolean
    can_user_toggle: boolean
    reviewed_by_user_id: string | null
    reviewed_by?: User  // Add this to get
}

const getRefIcon = (type: string) => {
  if (type === 'metadata_resource') return 'i-heroicons-rectangle-stack'
  if (type === 'datasource_table') return 'i-heroicons-table-cells'
  if (type === 'memory') return 'i-heroicons-book-open'
  return 'i-heroicons-circle'
}

const getRefDisplayName = (ref: any) => {
  // First try display_text, then object name/title, then fallback
  const objectType = ref.object_type
  const dataSourceName = ref.data_source_name
  if (ref.display_text) return dataSourceName + ' - ' + objectType + ': ' + ref.display_text
  if (ref.object?.name) return dataSourceName + ' - ' + objectType + ': ' + ref.object.name
  if (ref.object?.title) return dataSourceName + ' - ' + objectType + ': ' + ref.object.title
  return dataSourceName + ' - ' + objectType
}

// Reactive state
const instructions = ref<Instruction[]>([])
const isLoading = ref(false)
const searchQuery = ref('')
const currentPage = ref(1)
const pageSize = ref(10)

// Modal state
const showInstructionModal = ref(false)
const editingInstruction = ref<Instruction | null>(null)

// Computed properties
const filteredInstructions = computed(() => {
    if (!searchQuery.value) return instructions.value
    
    const query = searchQuery.value.toLowerCase()
    return instructions.value.filter(instruction => 
        instruction.text.toLowerCase().includes(query) ||
        instruction.user?.name?.toLowerCase().includes(query) ||
        instruction.status.toLowerCase().includes(query) ||
        instruction.category.toLowerCase().includes(query) ||
        instruction.data_sources.some(ds => ds.name.toLowerCase().includes(query))
    )
})

const paginatedInstructions = computed(() => {
    const start = (currentPage.value - 1) * pageSize.value
    const end = start + pageSize.value
    return filteredInstructions.value.slice(start, end)
})

const totalPages = computed(() => Math.ceil(filteredInstructions.value.length / pageSize.value))

const visiblePages = computed(() => {
    const pages = []
    const total = totalPages.value
    const current = currentPage.value
    
    // Show maximum 5 pages
    let start = Math.max(1, current - 2)
    let end = Math.min(total, start + 4)
    
    // Adjust start if we're near the end
    if (end - start < 4) {
        start = Math.max(1, end - 4)
    }
    
    for (let i = start; i <= end; i++) {
        pages.push(i)
    }
    
    return pages
})

// Methods
// Simple admin view using the new clean parameters
const fetchInstructions = async () => {
    // Check if organization is available before making API call
    const { organization, ensureOrganization } = useOrganization()
    
    try {
        await ensureOrganization()
        
        if (!organization.value?.id) {
            console.warn('ConsoleInstructions: Organization not available, skipping API call')
            return
        }
    } catch (error) {
        console.error('ConsoleInstructions: Error ensuring organization:', error)
        return
    }
    
    isLoading.value = true
    try {
        const response = await useMyFetch<Instruction[]>('/api/instructions', {
            method: 'GET',
            query: {
                limit: 1000,
                include_own: true,
                include_drafts: true,   // Admins see drafts
                include_hidden: true    // Admins see hidden
            }
        })
        
        if (response.error.value) {
            console.error('Error fetching instructions:', response.error.value)
        } else if (response.data.value) {
            //console.log('Instructions found:', response.data.value.length)
            instructions.value = response.data.value
        }
        
    } catch (err) {
        console.error('Error fetching instructions:', err)
    } finally {
        isLoading.value = false
    }
}

const addInstruction = () => {
    editingInstruction.value = null
    showInstructionModal.value = true
}

const editInstruction = (instruction: Instruction) => {
    editingInstruction.value = instruction
    showInstructionModal.value = true
}

const handleInstructionSaved = (savedInstruction: Instruction) => {
    if (editingInstruction.value) {
        // Update existing instruction
        const index = instructions.value.findIndex(i => i.id === savedInstruction.id)
        if (index !== -1) {
            instructions.value[index] = savedInstruction
        }
    } else {
        // Add new instruction
        instructions.value.unshift(savedInstruction)
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

const formatCategory = (category: string) => {
    const categoryMap = {
        code_gen: 'Code Generation',
        data_modeling: 'Data Modeling',
        general: 'General'
    }
    return categoryMap[category as keyof typeof categoryMap] || category
}

const getStatusClass = (instruction: Instruction) => {

        // Regular status colors
        const statusClasses = {
            draft: 'bg-yellow-100 text-yellow-800',
            published: 'bg-green-100 text-green-800',
            archived: 'bg-gray-100 text-gray-800'
        }
        return statusClasses[instruction.status as keyof typeof statusClasses] || 'bg-gray-100 text-gray-800'
}

const getDisplayStatus = (instruction: Instruction) => {
    // Just return the main status without brackets
    return formatStatus(instruction.status) // Draft/Published/Archived
}

const getSubStatus = (instruction: Instruction) => {
    // Show sub-status based on global_status and reviewer
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
    
    return null // No sub-status to show
}

const getInstructionType = (instruction: Instruction): string => {
    if (instruction.private_status && !instruction.global_status) {
        return 'private'
    } else if (instruction.private_status && instruction.global_status === 'suggested') {
        return 'suggested'
    } else if (!instruction.private_status && instruction.global_status === 'approved') {
        return 'global'
    } else {
        return 'unknown'
    }
}

const formatDate = (dateString: string) => {
    if (!dateString) return ''
    const date = new Date(dateString)
    return date.toLocaleDateString()
}

// Watch for search query changes and reset pagination
watch(searchQuery, () => {
    currentPage.value = 1
})

// Fetch instructions on component mount
onMounted(async () => {
    // Wait a bit for organization to be loaded by parent components
    await nextTick()
    fetchInstructions()
})
</script> 