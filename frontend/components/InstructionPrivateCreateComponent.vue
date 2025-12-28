<template>
    <div class="flex flex-col h-full">
        <!-- VIEW MODE: Read-only display for existing instructions (permission-based) -->
        <div v-if="isReadOnly" class="flex-1 flex flex-col min-h-0">
            <!-- Scrollable content area -->
            <div class="flex-1 overflow-y-auto px-6 py-5 space-y-5">

                <!-- Read-only Notice -->
                <div class="flex items-center gap-1.5 text-[11px] text-gray-400">
                    <Icon name="heroicons:eye" class="w-3 h-3" />
                    <span>View only</span>
                </div>

                <!-- Content Display -->
                <div class="border border-gray-200 rounded-xl overflow-hidden bg-white">
                    <!-- Header with file path and git sync status -->
                    <div class="flex items-center justify-between px-3 py-1.5 bg-gray-50 border-b border-gray-100">
                        <div class="flex items-center gap-2 min-w-0">
                            <Icon v-if="props.isGitSourced" name="heroicons:code-bracket" class="w-3 h-3 text-gray-400 shrink-0" />
                            <span v-if="filePath" class="text-xs font-mono text-gray-600 truncate">{{ filePath }}</span>
                            <span v-else class="text-xs font-medium text-gray-500">Content</span>
                        </div>
                        <div v-if="props.isGitSourced" class="flex items-center gap-2 shrink-0">
                            <span v-if="props.isGitSynced" class="flex items-center gap-1 text-[10px] text-green-600 bg-green-50 px-1.5 py-0.5 rounded">
                                <GitBranchIcon class="w-3 h-3" />
                                Synced
                            </span>
                            <span v-else class="text-[10px] text-gray-400 bg-gray-100 px-1.5 py-0.5 rounded">Unlinked</span>
                        </div>
                    </div>
                    
                    <!-- Markdown rendered content (for .md files or non-git-linked) -->
                    <div v-if="shouldRenderAsMarkdown" class="p-4 markdown-wrapper">
                        <MDC :value="sharedForm.text || ''" class="markdown-content" />
                    </div>
                    
                    <!-- Code block for other file types -->
                    <div v-else class="p-4 bg-gray-50">
                        <pre class="text-xs leading-relaxed font-mono text-gray-800 whitespace-pre-wrap overflow-x-auto"><code>{{ sharedForm.text }}</code></pre>
                    </div>
                </div>

                <!-- Metadata Display (read-only) -->
                <div class="flex flex-wrap items-center gap-3 text-xs">
                    <!-- Category -->
                    <div class="flex items-center gap-1.5">
                        <span class="text-gray-400">Category:</span>
                        <div class="inline-flex items-center text-gray-700">
                            <Icon :name="getCategoryIcon(sharedForm.category)" class="w-3 h-3 mr-1" />
                            {{ formatCategory(sharedForm.category) }}
                        </div>
                    </div>

                    <!-- Load Mode -->
                    <div class="flex items-center gap-1.5">
                        <span class="text-gray-400">Loading:</span>
                        <div class="inline-flex items-center text-gray-700">
                            <Icon :name="getLoadModeIcon(sharedForm.load_mode)" class="w-3 h-3 mr-1" />
                            {{ getLoadModeLabel(sharedForm.load_mode) }}
                        </div>
                    </div>
                </div>

                <!-- Labels (read-only) -->
                <div v-if="selectedLabelObjects.length > 0" class="flex items-center gap-2">
                    <span class="text-[11px] text-gray-400">Labels:</span>
                    <div class="flex flex-wrap gap-1">
                        <span
                            v-for="label in selectedLabelObjects"
                            :key="label.id"
                            class="inline-flex items-center gap-1 px-1.5 py-0.5 rounded-full text-[10px]"
                            :style="{ backgroundColor: (label.color || '#94a3b8') + '20', color: '#1F2937' }"
                        >
                            <span class="w-1.5 h-1.5 rounded-full" :style="{ backgroundColor: label.color || '#94a3b8' }"></span>
                            {{ label.name }}
                        </span>
                    </div>
                </div>

                <!-- Scope (read-only) -->
                <div class="flex flex-wrap items-center gap-4 text-xs">
                    <!-- Data Sources -->
                    <div class="flex items-center gap-1.5">
                        <span class="text-gray-400">Sources:</span>
                        <span v-if="isAllDataSourcesSelected" class="text-gray-700">All sources</span>
                        <div v-else-if="getSelectedDataSourceObjects.length > 0" class="flex items-center gap-1">
                            <div class="flex -space-x-1">
                                <DataSourceIcon 
                                    v-for="ds in getSelectedDataSourceObjects.slice(0, 3)" 
                                    :key="ds.id" 
                                    :type="ds.type" 
                                    class="h-4 w-4 border border-white rounded" 
                                />
                            </div>
                            <span class="text-gray-700">{{ getSelectedDataSourceObjects.length }} source{{ getSelectedDataSourceObjects.length > 1 ? 's' : '' }}</span>
                        </div>
                        <span v-else class="text-gray-400">None</span>
                    </div>

                    <!-- Tables -->
                    <div class="flex items-center gap-1.5">
                        <span class="text-gray-400">Tables:</span>
                        <span v-if="selectedReferences.length === 0" class="text-gray-400">None</span>
                        <span v-else class="text-gray-700">{{ selectedReferences.length }} table{{ selectedReferences.length > 1 ? 's' : '' }}</span>
                    </div>
                </div>

            </div>
            
            <!-- View Mode Actions (fixed at bottom) -->
            <div class="shrink-0 bg-white border-t px-5 py-3">
                <div class="flex justify-end">
                    <UButton color="gray" variant="ghost" size="xs" @click="$emit('cancel')">
                        Close
                    </UButton>
                </div>
            </div>
        </div>

        <!-- EDIT MODE: Form for creating/editing instructions -->
        <form v-else @submit.prevent="submitForm" class="flex-1 flex flex-col min-h-0">
            <!-- Scrollable content area -->
            <div class="flex-1 overflow-y-auto px-6 py-5 space-y-5">
                
                <!-- Git Source Info -->
                <div v-if="props.isGitSourced" class="flex items-center gap-1.5 text-xs text-gray-500">
                    <Icon name="heroicons:code-bracket" class="w-3 h-3 text-gray-400 shrink-0" />
                    <span class="truncate font-mono text-[11px]">
                        {{ instruction?.structured_data?.path || instruction?.title || 'Git Repository' }}
                    </span>
                    <span class="text-gray-300">·</span>
                    <UTooltip 
                        v-if="props.isGitSynced"
                        text="Stop syncing from git. You'll be able to edit manually."
                        :popper="{ placement: 'top' }"
                    >
                        <button 
                            type="button"
                            class="text-[11px] text-gray-400 hover:text-orange-500 transition-colors"
                            @click="$emit('unlink-from-git')"
                        >
                            Unlink
                        </button>
                    </UTooltip>
                    <template v-else>
                        <span class="text-[10px] text-gray-400">Unlinked</span>
                        <UTooltip 
                            text="Resume syncing from git"
                            :popper="{ placement: 'top' }"
                        >
                            <button 
                                type="button"
                                class="text-[11px] text-blue-500 hover:text-blue-600 transition-colors"
                                @click="$emit('relink-to-git')"
                            >
                                Relink
                            </button>
                        </UTooltip>
                    </template>
                </div>

                <!-- Build Approval Notice (shown to non-admins creating new instructions) -->
                <div v-if="showBuildApprovalNotice" class="flex items-center gap-2 p-2.5 bg-blue-50 border border-blue-200 rounded-lg">
                    <Icon name="heroicons:clock" class="w-4 h-4 text-blue-600 shrink-0" />
                    <div class="min-w-0">
                        <span class="text-xs font-medium text-blue-800">Pending Build Approval</span>
                        <p class="text-[11px] text-blue-600 mt-0.5">This instruction will be added to a build for admin review.</p>
                    </div>
                </div>

                <!-- Hero Textarea / Code Editor -->
                <div class="border border-gray-200 rounded-xl overflow-hidden focus-within:ring-2 focus-within:ring-blue-100 focus-within:border-blue-400">
                    <!-- Header with title and code view toggle -->
                    <div class="flex items-center justify-between px-3 py-1.5 bg-white border-b border-gray-100">
                        <div class="flex items-center gap-2 min-w-0">
                            <Icon v-if="props.isGitSourced" name="heroicons:code-bracket" class="w-3 h-3 text-gray-400 shrink-0" />
                            <span v-if="filePath" class="text-xs font-mono text-gray-600 truncate">{{ filePath }}</span>
                            <span v-else class="text-xs font-medium text-gray-500">Instruction</span>
                        </div>
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
                    <textarea 
                        v-if="!codeView"
                        :value="sharedForm.text"
                        @input="updateForm({ text: ($event.target as HTMLTextAreaElement).value })"
                        placeholder="Describe the instruction for the AI agent...

Examples:
• When querying revenue, always filter out cancelled orders
• Use the customers_v2 table instead of the deprecated customers table  
• Calculate MRR as sum of active subscription amounts"
                        class="w-full min-h-[210px] text-xs leading-relaxed p-4
                               border-0 resize-y
                               focus:ring-0 focus:outline-none
                               placeholder:text-gray-400"
                        :required="true"
                    />
                    
                    <!-- Code editor (Monaco with white background) -->
                    <ClientOnly v-else>
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
                            style="height: 210px"
                        />
                    </ClientOnly>
                    
                    <!-- Action buttons row -->
                    <div class="px-3 py-2 bg-gray-50 border-t border-gray-100 flex items-center gap-2">
                        <button 
                            type="button"
                            @click="enhanceInstruction"
                            :disabled="isEnhancing || !sharedForm.text?.trim()"
                            class="inline-flex items-center gap-1 px-2.5 py-1 
                                   bg-white border border-gray-200 rounded-full
                                   text-xs text-gray-600
                                   hover:bg-gray-50 hover:border-gray-300
                                   disabled:opacity-50 disabled:cursor-not-allowed
                                   transition-all"
                        >
                            <Spinner v-if="isEnhancing" class="w-3.5 h-3.5" />
                            <Icon v-else name="heroicons:sparkles" class="w-3.5 h-3.5 text-purple-500" />
                            {{ isEnhancing ? 'Enhancing...' : 'Enhance' }}
                        </button>
                        <button 
                            type="button"
                            @click="$emit('toggle-analyze')"
                            class="inline-flex items-center gap-1 px-2.5 py-1 
                                   bg-white border border-gray-200 rounded-full
                                   text-xs text-gray-500
                                   hover:bg-gray-50 hover:border-gray-300 hover:text-gray-700
                                   transition-all"
                        >
                            <Icon name="heroicons:chart-bar" class="w-3.5 h-3.5" />
                            Analyze
                        </button>
                    </div>
                </div>

                <!-- Horizontal Config Row -->
                <div class="flex flex-wrap items-center gap-2 p-2.5 bg-gray-50 rounded-lg">
                    <!-- Category -->
                    <USelectMenu 
                        :model-value="sharedForm.category"
                        @update:model-value="updateForm({ category: $event })"
                        :options="categoryOptions" 
                        option-attribute="label" 
                        value-attribute="value" 
                        size="xs"
                        class="min-w-[120px]"
                    >
                        <template #label>
                            <div class="inline-flex items-center text-xs text-gray-700">
                                <Icon :name="getCategoryIcon(sharedForm.category)" class="w-3 h-3 mr-1" />
                                {{ formatCategory(sharedForm.category) }}
                            </div>
                        </template>
                        <template #option="{ option }">
                            <div class="flex items-center gap-1.5">
                                <Icon :name="getCategoryIcon(option.value)" class="w-3 h-3" />
                                <span class="text-xs">{{ option.label }}</span>
                            </div>
                        </template>
                    </USelectMenu>

                    <!-- AI Context Loading -->
                    <USelectMenu 
                        :model-value="sharedForm.load_mode"
                        @update:model-value="updateForm({ load_mode: $event })"
                        :options="loadModeOptions" 
                        option-attribute="label" 
                        value-attribute="value" 
                        size="xs"
                        class="w-auto"
                        :ui-menu="{ width: 'w-60' }"
                    >
                        <template #label>
                            <div class="inline-flex items-center text-xs text-gray-700">
                                <Icon :name="getLoadModeIcon(sharedForm.load_mode)" class="w-3 h-3 mr-1" />
                                {{ getLoadModeLabel(sharedForm.load_mode) }}
                            </div>
                        </template>
                        <template #option="{ option }">
                            <div class="flex flex-col gap-0.5 py-0.5">
                                <div class="flex items-center gap-1.5">
                                    <Icon :name="getLoadModeIcon(option.value)" class="w-3 h-3" />
                                    <span class="text-xs font-medium">{{ option.label }}</span>
                                </div>
                                <span class="text-[10px] text-gray-500 ml-4">{{ option.description }}</span>
                            </div>
                        </template>
                    </USelectMenu>

                    <!-- Labels (select only, no create) -->
                    <USelectMenu
                        :model-value="selectedLabelIds"
                        @update:modelValue="handleLabelSelectionChange"
                        :options="availableLabels"
                        option-attribute="name"
                        value-attribute="id"
                        multiple
                        size="xs"
                        class="flex-1 min-w-[120px]"
                        searchable
                        searchable-placeholder="Search labels..."
                    >
                        <template #label>
                            <div class="flex items-center flex-wrap gap-1">
                                <span v-if="selectedLabelObjects.length === 0" class="text-gray-500 text-xs">+ Labels</span>
                                <span
                                    v-for="label in selectedLabelObjects.slice(0, 2)"
                                    :key="label.id"
                                    class="inline-flex items-center gap-1 px-1.5 py-0.5 rounded-full text-[10px]"
                                    :style="{ backgroundColor: (label.color || '#94a3b8') + '20', color: '#1F2937' }"
                                >
                                    <span class="w-1.5 h-1.5 rounded-full" :style="{ backgroundColor: label.color || '#94a3b8' }"></span>
                                    {{ label.name }}
                                </span>
                                <span v-if="selectedLabelObjects.length > 2" class="text-[10px] text-gray-500">
                                    +{{ selectedLabelObjects.length - 2 }}
                                </span>
                            </div>
                        </template>
                        <template #option="{ option }">
                            <div class="flex items-center w-full py-0.5 gap-1">
                                <span class="w-2 h-2 rounded-full flex-shrink-0" :style="{ backgroundColor: option.color || '#94a3b8' }"></span>
                                <div class="min-w-0 flex-1">
                                    <p class="text-[11px] font-medium text-gray-900 truncate">{{ option.name }}</p>
                                </div>
                            </div>
                        </template>
                    </USelectMenu>
                </div>

                <!-- Scope Row -->
                <div class="flex items-center gap-3">
                    <span class="text-[11px] text-gray-500 shrink-0">Scope:</span>
                    
                    <!-- Data Sources -->
                    <USelectMenu 
                        :model-value="selectedDataSources"
                        @update:model-value="updateDataSources"
                        :options="dataSourceOptions" 
                        option-attribute="name"
                        value-attribute="id"
                        size="xs"
                        multiple
                        class="min-w-[200px]"
                    >
                        <template #label>
                            <span v-if="isAllDataSourcesSelected" class="text-xs text-gray-700">All sources</span>
                            <span v-else-if="selectedDataSources.length === 0" class="text-gray-400 text-xs">Sources</span>
                            <span v-else class="text-xs text-gray-700">{{ getSelectedDataSourceObjects.length }} source{{ getSelectedDataSourceObjects.length > 1 ? 's' : '' }}</span>
                        </template>
                        <template #option="{ option }">
                            <div class="flex items-center justify-between w-full py-0.5 pr-1">
                                <div class="flex items-center">
                                    <div v-if="option.id === 'all'" class="flex -space-x-1 mr-1.5">
                                        <DataSourceIcon v-for="ds in availableDataSources.slice(0, 3)" :key="ds.id" :type="ds.type" class="h-3 w-3 border border-white rounded" />
                                    </div>
                                    <DataSourceIcon v-else :type="option.type" class="w-3 h-3 mr-1.5" />
                                    <span class="text-xs">{{ option.name }}</span>
                                </div>
                                <UCheckbox :model-value="option.id === 'all' ? isAllDataSourcesSelected : selectedDataSources.includes(String(option.id))" @update:model-value="handleDataSourceToggle(String(option.id))" @click.stop class="flex-shrink-0 ml-1" />
                            </div>
                        </template>
                    </USelectMenu>

                    <!-- Tables -->
                    <USelectMenu
                        :options="filteredMentionableOptions"
                        option-attribute="name"
                        value-attribute="id"
                        size="xs"
                        multiple
                        searchable
                        searchable-placeholder="Search tables..."
                        :model-value="selectedReferenceIds"
                        @update:model-value="handleReferencesChange"
                        class="min-w-[200px]"
                    >
                        <template #label>
                            <span v-if="selectedReferences.length === 0" class="text-gray-400 text-xs">Tables</span>
                            <span v-else class="text-xs text-gray-700">{{ selectedReferences.length }} table{{ selectedReferences.length > 1 ? 's' : '' }}</span>
                        </template>
                        <template #option="{ option }">
                            <div class="w-full py-0.5">
                                <div class="flex items-center gap-1.5">
                                    <UCheckbox :model-value="selectedReferenceIds.includes(String(option.id))" @update:model-value="toggleReference(String(option.id))" @click.stop @mousedown.stop class="flex-shrink-0" />
                                    <UIcon :name="getRefIcon(option.type)" class="w-3 h-3 text-gray-500 flex-shrink-0" />
                                    <span class="text-xs font-medium text-gray-900 truncate">{{ option.name }}</span>
                                </div>
                                <div class="flex items-center gap-1.5 ml-6">
                                    <DataSourceIcon :type="option.data_source_type" class="w-2.5 h-2.5 flex-shrink-0" />
                                    <span class="text-[10px] text-gray-500 truncate">{{ option.data_source_name }}</span>
                                </div>
                            </div>
                        </template>
                    </USelectMenu>
                </div>

            </div>
            
            <!-- Form Actions (fixed at bottom) -->
            <div class="shrink-0 bg-white border-t px-5 py-3">
                <div class="flex justify-between items-center">
                    <!-- Delete button (only show when editing) -->
                    <UButton 
                        v-if="isEditing"
                        size="xs"
                        color="red" 
                        variant="ghost" 
                        @click="confirmDelete"
                        :loading="isDeleting"
                    >
                        <Icon name="heroicons:trash" class="w-3.5 h-3.5 mr-1" />
                        Delete
                    </UButton>
                    
                    <div class="flex gap-2" :class="{ 'ml-auto': !isEditing }">
                        <UButton color="gray" variant="ghost" size="xs" @click="$emit('cancel')">
                            Cancel
                        </UButton>
                        <UButton 
                            type="submit" 
                            size="xs" 
                            color="blue"
                            :loading="isSubmitting"
                        >
                            {{ isEditing ? 'Update' : 'Create' }}
                        </UButton>
                    </div>
                </div>
            </div>
        </form>
    </div>
</template>

<script setup lang="ts">
import DataSourceIcon from '~/components/DataSourceIcon.vue'
import Spinner from '~/components/Spinner.vue'
import GitBranchIcon from '~/components/icons/GitBranchIcon.vue'
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

interface InstructionLabel {
    id: string
    name: string
    color?: string | null
}

interface MentionableItem {
    id: string
    type: 'metadata_resource' | 'datasource_table'
    name: string
    data_source_id?: string
    data_source_type?: string
    data_source_name?: string
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

const emit = defineEmits(['instructionSaved', 'cancel', 'updateForm', 'updateDataSources', 'unlink-from-git', 'relink-to-git', 'toggle-analyze'])

// Reactive state
const toast = useToast()
const isSubmitting = ref(false)
const isDeleting = ref(false)
const isEnhancing = ref(false)
const availableDataSources = ref<DataSource[]>([])
const mentionableOptions = ref<MentionableItem[]>([])
const selectedReferences = ref<MentionableItem[]>([])
const availableLabels = ref<InstructionLabel[]>([])
const selectedLabelIds = ref<string[]>([])
const codeView = ref(false)

// Show build approval notice for non-admins creating new instructions
// (Non-admin created builds need admin approval before being deployed)
const isNonAdmin = computed(() => !useCan('create_instructions'))
const showBuildApprovalNotice = computed(() => !isEditing.value && isNonAdmin.value)

// Computed properties
const isEditing = computed(() => !!props.instruction)

// Read-only mode: non-admin users viewing an existing instruction
const isReadOnly = computed(() => isEditing.value && !useCan('create_instructions'))

// Get file path from instruction (git path or title)
const filePath = computed(() => {
    return props.instruction?.structured_data?.path || props.instruction?.title || null
})

// Get file extension from git path or title
const fileExtension = computed(() => {
    const path = filePath.value || ''
    const match = path.match(/\.([^.]+)$/)
    return match ? match[1].toLowerCase() : null
})

// Determine if content should be rendered as markdown
const shouldRenderAsMarkdown = computed(() => {
    // Render as markdown if:
    // 1. It's a .md file
    // 2. OR it's not git-linked (manually created instruction)
    if (fileExtension.value === 'md') return true
    if (!props.isGitSourced) return true
    return false
})

const dataSourceOptions = computed(() => {
    const allOption = {
        id: 'all',
        name: 'All Data Sources',
        type: 'all'
    }
    return [allOption, ...availableDataSources.value]
})

const isAllDataSourcesSelected = computed(() => {
    return props.selectedDataSources.includes('all') || props.selectedDataSources.length === 0
})

const getSelectedDataSourceObjects = computed(() => {
    return availableDataSources.value.filter(ds => props.selectedDataSources.includes(ds.id))
})

const selectedReferenceIds = computed(() => selectedReferences.value.map(r => r.id))

const selectedLabelObjects = computed(() => {
    return availableLabels.value.filter(l => selectedLabelIds.value.includes(l.id))
})

// Filter mentionable options based on selected data sources
const filteredMentionableOptions = computed(() => {
    if (isAllDataSourcesSelected.value) {
        return mentionableOptions.value
    }
    
    return mentionableOptions.value.filter(option => {
        if (option.data_source_id) {
            return props.selectedDataSources.includes(option.data_source_id)
        }
        
        return true
    })
})

// Load mode options for dropdown
const loadModeOptions = [
    { value: 'always' as const, label: 'Always', description: 'Always included in AI context' },
    { value: 'intelligent' as const, label: 'Smart', description: 'Included only when relevant to the query' },
    { value: 'disabled' as const, label: 'Disabled', description: 'Never included in AI context' }
]

const getLoadModeIcon = (mode: string) => {
    const icons: Record<string, string> = {
        always: 'heroicons:bolt',
        intelligent: 'heroicons:sparkles',
        disabled: 'heroicons:x-circle'
    }
    return icons[mode] || 'heroicons:bolt'
}

const getLoadModeLabel = (mode: string) => {
    const labels: Record<string, string> = {
        always: 'Always',
        intelligent: 'Smart',
        disabled: 'Disabled'
    }
    return labels[mode] || mode
}

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

const enhanceInstruction = async () => {
    if (isEnhancing.value || !props.sharedForm.text?.trim()) return
    
    isEnhancing.value = true
    
    try {
        const response = await useMyFetch('/instructions/enhance', {
            method: 'POST',
            body: { text: props.sharedForm.text }
        })
        if (response.status.value === 'success') {
            updateForm({ text: response.data.value as string })
        } else {
            throw new Error('Enhance failed')
        }
    } catch (error) {
        console.error('Error enhancing instruction:', error)
        toast.add({
            title: 'Error',
            description: 'Failed to enhance instruction',
            color: 'red'
        })
    } finally {
        isEnhancing.value = false
    }
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

const getRefIcon = (type: string) => {
    if (type === 'metadata_resource') return 'i-heroicons-rectangle-stack'
    if (type === 'datasource_table') return 'i-heroicons-table-cells'
    return 'i-heroicons-circle'
}

const handleReferencesChange = (ids: string[]) => {
    const idSet = new Set(ids)
    selectedReferences.value = filteredMentionableOptions.value.filter(m => idSet.has(m.id))
}

const toggleReference = (id: string) => {
    const currentIds = new Set(selectedReferenceIds.value.map(String))
    if (currentIds.has(id)) {
        currentIds.delete(id)
    } else {
        currentIds.add(id)
    }
    handleReferencesChange(Array.from(currentIds))
}

const validateSelectedReferences = () => {
    const validReferenceIds = new Set(filteredMentionableOptions.value.map(m => m.id))
    selectedReferences.value = selectedReferences.value.filter(ref => validReferenceIds.has(ref.id))
}

const fetchLabels = async () => {
    try {
        const { data, error } = await useMyFetch<InstructionLabel[]>('/instructions/labels', { method: 'GET' })
        if (!error.value && Array.isArray(data.value)) {
            availableLabels.value = data.value
        }
    } catch (err) {
        console.error('Error fetching labels:', err)
    }
}

const handleLabelSelectionChange = (ids: string[]) => {
    selectedLabelIds.value = (ids || []).filter(id => id)
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

// Full instruction data (fetched separately to get references)
const fullInstruction = ref<any>(null)

const fetchFullInstruction = async () => {
    if (!props.instruction?.id) return
    
    try {
        const { data, error } = await useMyFetch<any>(`/instructions/${props.instruction.id}`, { method: 'GET' })
        if (!error.value && data.value) {
            fullInstruction.value = data.value
        }
    } catch (err) {
        console.error('Error fetching full instruction:', err)
    }
}

const initReferencesFromInstruction = () => {
    const instruction = fullInstruction.value || props.instruction
    
    if (instruction && Array.isArray(instruction.references)) {
        const map: Record<string, MentionableItem> = {}
        for (const m of mentionableOptions.value) map[m.id] = m
        
        const seenObjectIds = new Set<string>()
        const preselected: MentionableItem[] = []
        
        for (const r of instruction.references) {
            if (seenObjectIds.has(r.object_id)) continue
            seenObjectIds.add(r.object_id)
            
            const existing = map[r.object_id]
            if (existing) {
                preselected.push({ ...existing, column_name: r.column_name || null })
            } else {
                preselected.push({ id: r.object_id, type: r.object_type, name: r.display_text || r.object_type, column_name: r.column_name || null })
            }
        }
        selectedReferences.value = preselected
    }
}

const initLabelsFromInstruction = () => {
    const instruction = fullInstruction.value || props.instruction
    if (instruction && Array.isArray(instruction.labels)) {
        selectedLabelIds.value = instruction.labels.map((l: any) => l.id)
    }
}

// Event handlers
const submitForm = async () => {
    if (isSubmitting.value) return
    
    isSubmitting.value = true
    
    try {
        // SIMPLIFIED: All instructions are "published" (content ready)
        // Approval workflow is handled by builds, not instruction status
        const payload = {
            text: props.sharedForm.text,
            status: 'published',  // Always published - build handles approval
            category: props.sharedForm.category,
            is_seen: true,
            can_user_toggle: true,
            load_mode: props.sharedForm.load_mode || 'always',
            data_source_ids: isAllDataSourcesSelected.value ? [] : props.selectedDataSources,
            label_ids: selectedLabelIds.value,
            references: selectedReferences.value.map(r => ({
                object_type: r.type,
                object_id: r.id,
                column_name: r.column_name || null,
                relation_type: 'scope'
            }))
        }

        let response
        if (isEditing.value) {
            response = await useMyFetch(`/instructions/${props.instruction.id}`, {
                method: 'PUT',
                body: payload
            })
        } else {
            response = await useMyFetch('/instructions', {
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
        `Are you sure you want to delete this instruction?`
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

// Lifecycle
onMounted(async () => {
    fetchDataSources()
    fetchLabels()
    await fetchFullInstruction()
    await fetchAvailableReferences()
    initReferencesFromInstruction()
    initLabelsFromInstruction()
})

watch(() => props.instruction, async () => {
    await fetchFullInstruction()
    initReferencesFromInstruction()
    initLabelsFromInstruction()
})

// Validate references when data sources change
watch(() => props.selectedDataSources, () => {
    validateSelectedReferences()
}, { deep: true })
</script>

<style scoped>
/* Markdown wrapper styles for instruction content */
.markdown-wrapper :deep(.markdown-content) {
    @apply leading-relaxed text-sm text-gray-800;

    p {
        margin-bottom: 1em;
    }
    p:last-child {
        margin-bottom: 0;
    }

    :where(h1, h2, h3, h4, h5, h6) {
        @apply font-semibold mb-3 mt-4 text-gray-900;
    }

    h1 { @apply text-xl; }
    h2 { @apply text-lg; }
    h3 { @apply text-base; }
    h4 { @apply text-sm; }

    /* Prevent anchor links inside headings from looking like links - needs high specificity */
    h1 a, h2 a, h3 a, h4 a, h5 a, h6 a {
        color: inherit !important;
        text-decoration: none !important;
    }

    ul, ol { @apply pl-5 mb-3; }
    ul { @apply list-disc; }
    ol { @apply list-decimal; }
    li { @apply mb-1; }

    /* Code blocks (fenced with ```) */
    pre {
        @apply bg-gray-50 p-3 rounded-lg mb-3 overflow-x-auto text-xs;
        white-space: pre-wrap;
        word-wrap: break-word;
    }
    pre code {
        background: none;
        padding: 0;
        border-radius: 0;
        font-size: 12px;
        line-height: 1.5;
        display: block;
        white-space: pre-wrap;
        word-wrap: break-word;
    }
    /* Inline code (single backticks) */
    code {
        @apply bg-gray-100 px-1.5 py-0.5 rounded font-mono text-xs;
        color: #374151;
    }
    
    /* Regular links - but not inside headings */
    a { 
        @apply text-blue-600 hover:text-blue-800 underline;
    }
    
    blockquote { 
        @apply border-l-4 border-gray-200 pl-4 italic my-3 text-gray-600; 
    }
    
    table { @apply w-full border-collapse mb-3; }
    table th, table td { @apply border border-gray-200 p-2 text-xs bg-white; }
    
    hr {
        @apply my-4 border-gray-200;
    }

    strong {
        @apply font-semibold;
    }

    em {
        @apply italic;
    }
}
</style>
