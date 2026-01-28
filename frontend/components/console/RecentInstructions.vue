<template>
    <div class="bg-white rounded-2xl border border-gray-100 shadow-sm overflow-hidden">
        <div class="p-6 border-b border-gray-50">
            <h3 class="text-lg font-semibold text-gray-900">Recent Instructions</h3>
            <p class="text-sm text-gray-500 mt-1">Latest instruction updates - for more go to <nuxt-link to="/console/instructions" class="text-blue-600 hover:text-blue-800">instructions</nuxt-link> page</p>
        </div>
        <div class="p-0">
            <div v-if="isLoading" class="flex items-center justify-center h-40">
                <div class="flex items-center space-x-2">
                    <div class="animate-spin rounded-full h-6 w-6 border-b-2 border-blue-600"></div>
                    <span class="text-gray-600">Loading instructions...</span>
                </div>
            </div>
            <div v-else-if="recentInstructions.length === 0" class="text-center py-8">
                <UIcon name="i-heroicons-document-text" class="mx-auto h-8 w-8 text-gray-400" />
                <p class="text-sm text-gray-500 mt-2">No recent instructions</p>
            </div>
            <div v-else class="space-y-3 p-4 pt-0">
                <div v-for="instruction in recentInstructions" :key="instruction.id" 
                     class="flex items-start space-x-4 p-4 bg-gray-50 rounded-lg hover:bg-gray-100 transition-colors cursor-pointer"
                     @click="openInstruction(instruction)">
                    
                    <!-- Content -->
                    <div class="flex-1 min-w-0">
                        <!-- Instruction Text -->
                        <div class="text-sm text-gray-900 line-clamp-2" :title="instruction.text">
                            {{ getDisplayText(instruction) }}
                        </div>
                        
                        <!-- Data Sources -->
                        <div class="flex items-center space-x-2 mt-2">
                            <div class="flex items-center space-x-1">
                                <div v-if="instruction.data_sources && instruction.data_sources.length > 0" class="flex items-center space-x-1">
                                    <DataSourceIcon
                                        v-for="dataSource in instruction.data_sources.slice(0, 3)"
                                        :key="dataSource.id"
                                        :type="dataSource.type"
                                        class="w-4 h-4"
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
                            
                            <!-- User -->
                            <span class="text-xs text-gray-500">•</span>
                            <span class="text-xs text-gray-500">{{ instruction.user?.name || 'AI Generated' }}</span>
                            
                            <!-- Date -->
                            <span class="text-xs text-gray-500">•</span>
                            <span class="text-xs text-gray-500">{{ formatDate(instruction.created_at) }}</span>
                        </div>
                    </div>
                    
                    <!-- Status Badge -->
                    <div class="flex-shrink-0">
                        <div class="flex flex-col items-end">
                            <span :class="getStatusClass(instruction)" class="inline-flex px-2 py-1 text-xs font-medium rounded-full">
                                {{ getDisplayStatus(instruction) }}
                            </span>
                            <span v-if="getSubStatus(instruction)" class="text-xs text-gray-500 mt-1 text-right">
                                {{ getSubStatus(instruction) }}
                            </span>
                        </div>
                    </div>
                </div>
            </div>
        </div>
    </div>
</template>

<script setup lang="ts">
import DataSourceIcon from '../DataSourceIcon.vue'

// Types
interface DataSource {
    id: string
    name: string
    type: string
    status: 'active' | 'inactive'
    description: string
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
    title?: string | null
    formatted_content?: string | null
    thumbs_up: number
    status: 'draft' | 'published' | 'archived'
    category: 'code_gen' | 'data_modeling' | 'general' | 'system' | 'visualizations' | 'dashboard'
    user_id: string
    organization_id: string
    user?: User
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

    // Source info
    source_type?: string
}

interface Props {
    dateRange?: {
        start: string
        end: string
    }
}

const props = withDefaults(defineProps<Props>(), {
    dateRange: () => ({
        start: '',
        end: ''
    })
})

// State
const isLoading = ref(false)
const recentInstructions = ref<Instruction[]>([])

// Methods
const fetchRecentInstructions = async () => {
    // Check if organization is available before making API call
    const { organization, ensureOrganization } = useOrganization()
    
    try {
        await ensureOrganization()
        
        if (!organization.value?.id) {
            console.warn('RecentInstructions: Organization not available, skipping API call')
            return
        }
    } catch (error) {
        console.error('RecentInstructions: Error ensuring organization:', error)
        return
    }
    
    isLoading.value = true
    try {
        const response = await useMyFetch<{ items: Instruction[], total: number, page: number, per_page: number, pages: number }>('/api/instructions', {
            method: 'GET',
            query: {
                limit: 5, // Only fetch 5 recent items
                include_own: true,
                include_drafts: false,
                include_hidden: false,
                status: 'published',
                order_by: 'created_at',
                order_direction: 'desc'
            }
        })

        if (response.error.value) {
            console.error('Error fetching recent instructions:', response.error.value)
            recentInstructions.value = []
        } else if (response.data.value?.items) {
            recentInstructions.value = response.data.value.items
        }
    } catch (error) {
        console.error('Failed to fetch recent instructions:', error)
        recentInstructions.value = []
    } finally {
        isLoading.value = false
    }
}

const openInstruction = (instruction: Instruction) => {
    // Navigate to instructions page or open edit modal
    navigateTo('/console/instructions')
}

const getCategoryIcon = (category: string) => {
    const categoryIcons = {
        code_gen: 'i-heroicons-code-bracket',
        data_modeling: 'i-heroicons-cube',
        general: 'i-heroicons-chat-bubble-bottom-center-text',
        system: 'i-heroicons-cog-6-tooth',
        visualizations: 'i-heroicons-chart-bar',
        dashboard: 'i-heroicons-squares-2x2'
    }
    return categoryIcons[category as keyof typeof categoryIcons] || 'i-heroicons-document-text'
}

const getCategoryIconClass = (category: string) => {
    const categoryClasses = {
        code_gen: 'bg-blue-500',
        data_modeling: 'bg-green-500',
        general: 'bg-purple-500',
        system: 'bg-orange-500',
        visualizations: 'bg-teal-500',
        dashboard: 'bg-indigo-500'
    }
    return categoryClasses[category as keyof typeof categoryClasses] || 'bg-gray-500'
}

const getStatusClass = (instruction: Instruction) => {
    const statusClasses = {
        draft: 'bg-yellow-100 text-yellow-800',
        published: 'bg-green-100 text-green-800',
        archived: 'bg-gray-100 text-gray-800'
    }
    return statusClasses[instruction.status as keyof typeof statusClasses] || 'bg-gray-100 text-gray-800'
}

const getDisplayStatus = (instruction: Instruction) => {
    const statusMap = {
        draft: 'Draft',
        published: 'Published',
        archived: 'Archived'
    }
    return statusMap[instruction.status as keyof typeof statusMap] || instruction.status
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

const formatDate = (dateString: string) => {
    if (!dateString) return ''
    const date = new Date(dateString)
    return date.toLocaleDateString()
}

const getDisplayText = (instruction: Instruction) => {
    // Priority: text > formatted_content > title
    // text is the main instruction content
    if (instruction.text && instruction.text.trim()) {
        const text = instruction.text.trim()
        return text.length > 150 ? text.slice(0, 150) + '...' : text
    }
    // formatted_content for structured/git instructions
    if (instruction.formatted_content && instruction.formatted_content.trim()) {
        const text = instruction.formatted_content.trim()
        return text.length > 150 ? text.slice(0, 150) + '...' : text
    }
    // title as last resort
    if (instruction.title && instruction.title.trim()) {
        return instruction.title.trim()
    }
    return 'No content'
}

// Watch for dateRange changes (though instructions might not be date-filtered)
watch(() => props.dateRange, () => {
    fetchRecentInstructions()
}, { deep: true })

// Initialize
onMounted(async () => {
    // Wait a bit for organization to be loaded by parent components
    await nextTick()
    fetchRecentInstructions()
})
</script> 