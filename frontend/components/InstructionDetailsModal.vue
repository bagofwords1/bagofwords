<template>
    <UModal v-model="isOpen" :ui="{ width: 'sm:max-w-2xl' }">
        <div class="p-6">
            <!-- Header -->
            <div class="flex items-center justify-between mb-4">
                <h2 class="text-lg font-medium text-gray-900">Instruction Details</h2>
                <button @click="close" class="text-gray-400 hover:text-gray-600">
                    <Icon name="heroicons:x-mark" class="w-5 h-5" />
                </button>
            </div>

            <!-- Content -->
            <div v-if="instruction" class="space-y-4">
                <!-- Instruction Text -->
                <div class="bg-gray-50 rounded-lg p-4 border">
                    <p class="text-gray-900 leading-relaxed whitespace-pre-wrap">{{ instruction.text }}</p>
                </div>

                <!-- Data Source Access -->
                <div>
                    <h3 class="text-xs font-medium text-gray-600 mb-1">Data Sources</h3>
                    <div v-if="instruction.data_sources.length === 0" class="flex items-center gap-2 text-xs text-gray-500">
                        <Icon name="heroicons:globe-alt" class="w-3 h-3 text-blue-500" />
                        <span>All Data Sources (Global access)</span>
                    </div>
                    <div v-else class="flex flex-wrap gap-2">
                        <div v-for="dataSource in instruction.data_sources" 
                             :key="dataSource.id" 
                             class="flex items-center gap-1.5 text-xs text-gray-600">
                            <DataSourceIcon :type="dataSource.type" class="w-3 h-3" />
                            <span>{{ dataSource.name }}</span>
                        </div>
                    </div>
                </div>

                <!-- References -->
                <div v-if="hasReferences">
                    <h3 class="text-xs font-medium text-gray-600 mb-1">References</h3>
                    <div class="flex flex-wrap gap-2">
                        <div v-for="ref in (instruction as any).references" 
                             :key="ref.id" 
                             class="flex items-center gap-1.5 text-xs text-gray-600 bg-gray-100 p-1 rounded-md">
                             <DataSourceIcon :type="ref.data_source_type" class="w-3 h-3" />
                             <span>{{ ref.data_source_name }}</span>
                            <UIcon :name="getRefIcon(ref.object_type)" class="w-3 h-3" />
                            <span>{{ getRefDisplayName(ref) }}</span>
                        </div>
                    </div>
                </div>
                <div v-else>
                    <h3 class="text-xs font-medium text-gray-600 mb-1">References</h3>
                    <div class="text-xs text-gray-400">
                        No references defined
                    </div>
                </div>

                <!-- Compact Metadata -->
                <div class="pt-2 border-t border-gray-200">
                    <div class="flex flex-wrap items-center gap-x-4 gap-y-1 text-xs text-gray-500">
                        <!-- Status -->
                        <div class="flex items-center gap-1">
                            <div :class="getStatusIconClass(instruction)" class="w-2 h-2 rounded-full"></div>
                            <span>{{ getDisplayStatus(instruction) }}</span>
                            <span v-if="getSubStatus(instruction)">• {{ getSubStatus(instruction) }}</span>
                        </div>
                        
                        <!-- Category -->
                        <div class="flex items-center gap-1">
                            <Icon :name="getCategoryIcon(instruction.category)" class="w-3 h-3" />
                            <span>{{ formatCategory(instruction.category) }}</span>
                        </div>
                        
                        <!-- Created info -->
                        <div>
                            Created by {{ instruction.user?.name || 'Unknown User' }} on {{ formatShortDate(instruction.created_at) }}
                        </div>
                        
                        <!-- Reviewer info (if applicable) -->
                        <div v-if="instruction.reviewed_by">
                            Reviewed by {{ instruction.reviewed_by.name }}
                        </div>
                    </div>
                </div>
            </div>

            <!-- Footer -->
            <div class="mt-6 flex justify-end">
                <UButton color="gray" variant="outline" size="sm" @click="close">
                    Close
                </UButton>
            </div>
        </div>
    </UModal>
</template>

<script setup lang="ts">
import DataSourceIcon from '~/components/DataSourceIcon.vue';

// Define interfaces
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

interface InstructionReference {
    id: string
    object_type: string
    object_id: string
    column_name?: string
    relation_type?: string
    display_text?: string
    object?: any
}

interface Instruction {
    id: string
    text: string
    thumbs_up: number
    status: 'draft' | 'published' | 'archived'
    category: 'code_gen' | 'data_modeling' | 'general' | 'system' | 'visualizations' | 'dashboard'
    user_id: string
    organization_id: string
    user: User
    data_sources: DataSource[]
    created_at: string
    updated_at: string
    private_status: string | null
    global_status: string | null
    is_seen: boolean
    can_user_toggle: boolean
    reviewed_by_user_id: string | null
    reviewed_by?: User
    references?: InstructionReference[]
}

// Props
interface Props {
    modelValue: boolean
    instruction: Instruction | null
}

const props = defineProps<Props>()

// Emits
const emit = defineEmits<{
    'update:modelValue': [value: boolean]
}>()

// Computed
const isOpen = computed({
    get: () => props.modelValue,
    set: (value) => emit('update:modelValue', value)
})

const hasReferences = computed(() => {
    return (props.instruction as any)?.references && (props.instruction as any).references.length > 0
})

// Methods
const close = () => {
    isOpen.value = false
}

const formatDate = (dateString: string) => {
    return new Date(dateString).toLocaleDateString('en-US', {
        year: 'numeric',
        month: 'long',
        day: 'numeric',
        hour: '2-digit',
        minute: '2-digit'
    })
}

const formatShortDate = (dateString: string) => {
    return new Date(dateString).toLocaleDateString('en-US', {
        year: 'numeric',
        month: 'short',
        day: 'numeric'
    })
}

const getDisplayStatus = (instruction: Instruction) => {
    return formatStatus(instruction.status)
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

const formatStatus = (status: string) => {
    const statusMap = {
        draft: 'Draft',
        published: 'Published',
        archived: 'Archived'
    }
    return statusMap[status as keyof typeof statusMap] || status
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

const getRefIcon = (type: string) => {
    if (type === 'metadata_resource') return 'i-heroicons-rectangle-stack'
    if (type === 'datasource_table') return 'i-heroicons-table-cells'
    if (type === 'memory') return 'i-heroicons-book-open'
    return 'i-heroicons-circle'
}

const getRefDisplayName = (ref: InstructionReference) => {
    if (ref.display_text) return ref.display_text
    if (ref.object?.name) return ref.object.name
    if (ref.object?.title) return ref.object.title
    return ref.object_id
}
</script>