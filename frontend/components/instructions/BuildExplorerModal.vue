<template>
    <UModal v-model="isOpen" :ui="{ width: 'sm:max-w-5xl' }">
        <UCard :ui="{ body: { padding: '' }, header: { padding: 'px-4 py-3' } }">
            <!-- Header -->
            <template #header>
                <div class="flex items-center justify-between">
                    <h3 class="text-base font-semibold text-gray-900">
                        Version Explorer
                    </h3>
                    <UButton
                        color="gray"
                        variant="ghost"
                        icon="i-heroicons-x-mark-20-solid"
                        size="xs"
                        @click="close"
                    />
                </div>
            </template>

            <!-- Two-pane layout -->
            <div class="flex h-[600px]">
                <!-- Left Pane: Builds List -->
                <div class="w-56 border-r border-gray-200 flex flex-col bg-gray-50/50">
                    <div class="px-3 py-2 border-b border-gray-200 bg-white">
                        <span class="text-xs font-medium text-gray-600">Builds</span>
                    </div>
                    
                    <!-- Loading builds -->
                    <div v-if="loadingBuilds" class="flex-1 flex items-center justify-center">
                        <Spinner class="w-5 h-5 text-gray-400" />
                    </div>
                    
                    <!-- Builds list -->
                    <div v-else class="flex-1 overflow-y-auto">
                        <div v-if="!builds.length" class="p-3 text-xs text-gray-400 text-center">
                            No builds found
                        </div>
                        <button
                            v-for="build in builds"
                            :key="build.id"
                            @click="selectBuild(build)"
                            class="w-full px-3 py-2 text-left border-b border-gray-100 hover:bg-white transition-colors"
                            :class="{ 'bg-white border-l-2 border-l-blue-500': selectedBuild?.id === build.id }"
                        >
                            <div class="flex items-center justify-between">
                                <span class="text-xs font-medium text-gray-800">Build #{{ build.build_number }}</span>
                                <span v-if="build.is_main" class="text-[9px] px-1.5 py-0.5 bg-green-100 text-green-700 rounded">Active</span>
                            </div>
                            <div class="text-[10px] text-gray-500 mt-0.5">
                                {{ formatDate(build.created_at) }}
                            </div>
                            <div class="text-[10px] text-gray-400 mt-0.5">
                                {{ build.total_instructions || 0 }} instructions
                            </div>
                        </button>
                    </div>
                </div>

                <!-- Right Pane: Build Details -->
                <div class="flex-1 flex flex-col min-w-0">
                    <!-- No build selected -->
                    <div v-if="!selectedBuild" class="flex-1 flex items-center justify-center">
                        <div class="text-center text-gray-400">
                            <UIcon name="i-heroicons-document-magnifying-glass" class="w-8 h-8 mx-auto mb-2" />
                            <p class="text-sm">Select a build to view details</p>
                        </div>
                    </div>

                    <template v-else>
                        <!-- Build Header -->
                        <div class="px-4 py-3 border-b border-gray-200 bg-white shrink-0">
                            <div class="flex items-center justify-between">
                                <div>
                                    <h4 class="text-sm font-semibold text-gray-900">Build #{{ selectedBuild.build_number }}</h4>
                                    <p class="text-[10px] text-gray-500 mt-0.5">{{ formatDateTime(selectedBuild.created_at) }}</p>
                                </div>
                                <div class="flex items-center gap-2">
                                    <span v-if="selectedBuild.is_main" class="text-[10px] px-2 py-0.5 bg-green-100 text-green-700 rounded-full">
                                        Active
                                    </span>
                                    <!-- Rollback button - only show for non-main approved builds with permission -->
                                    <UButton
                                        v-if="!selectedBuild.is_main && selectedBuild.status === 'approved' && canCreateBuilds"
                                        color="amber"
                                        variant="soft"
                                        size="xs"
                                        :icon="rollingBack ? undefined : 'i-heroicons-arrow-path'"
                                        :loading="rollingBack"
                                        @click="rollbackToBuild"
                                    >
                                        Rollback to this version
                                    </UButton>
                                </div>
                            </div>
                        </div>

                        <!-- Loading State -->
                        <div v-if="loadingBuildContent" class="flex-1 flex items-center justify-center">
                            <div class="text-center">
                                <Spinner class="w-6 h-6 text-gray-400 mx-auto mb-2" />
                                <p class="text-xs text-gray-500">Loading build content...</p>
                            </div>
                        </div>

                        <!-- Diff Section (if previous build exists) -->
                        <div v-if="!loadingBuildContent && diffData && hasDiffChanges" class="border-b border-gray-200 bg-gradient-to-r from-gray-50 to-white shrink-0">
                            <button
                                @click="diffExpanded = !diffExpanded"
                                class="w-full px-4 py-2 flex items-center justify-between hover:bg-gray-50/50 transition-colors"
                            >
                                <div class="flex items-center gap-2">
                                    <UIcon 
                                        :name="diffExpanded ? 'i-heroicons-chevron-down' : 'i-heroicons-chevron-right'" 
                                        class="w-3 h-3 text-gray-400"
                                    />
                                    <span class="text-xs font-medium text-gray-700">Changes from Build #{{ diffData.build_a_number }}</span>
                                </div>
                                <div class="flex gap-1.5">
                                    <span v-if="diffData.added_count" class="text-[9px] px-1.5 py-0.5 bg-green-100 text-green-700 rounded">
                                        +{{ diffData.added_count }}
                                    </span>
                                    <span v-if="diffData.modified_count" class="text-[9px] px-1.5 py-0.5 bg-amber-100 text-amber-700 rounded">
                                        ~{{ diffData.modified_count }}
                                    </span>
                                    <span v-if="diffData.removed_count" class="text-[9px] px-1.5 py-0.5 bg-red-100 text-red-700 rounded">
                                        −{{ diffData.removed_count }}
                                    </span>
                                </div>
                            </button>
                            
                            <!-- Expanded diff content -->
                            <div v-if="diffExpanded" class="px-4 pb-3 space-y-2">
                                <!-- Added -->
                                <div v-if="addedItems.length" class="space-y-1">
                                    <div class="text-[10px] font-medium text-green-700">Added</div>
                                    <div 
                                        v-for="item in addedItems.slice(0, 5)" 
                                        :key="item.instruction_id"
                                        class="border-l-2 border-l-green-400 bg-green-50/30 pl-2 py-1 rounded-r text-[10px] text-gray-700"
                                    >
                                        {{ truncateText(item.title || item.text, 80) }}
                                    </div>
                                    <div v-if="addedItems.length > 5" class="text-[10px] text-gray-400 pl-2">
                                        +{{ addedItems.length - 5 }} more
                                    </div>
                                </div>
                                
                                <!-- Modified -->
                                <div v-if="modifiedItems.length" class="space-y-1">
                                    <div class="text-[10px] font-medium text-amber-700">Modified</div>
                                    <div 
                                        v-for="item in modifiedItems.slice(0, 5)" 
                                        :key="item.instruction_id"
                                        class="border-l-2 border-l-amber-400 bg-amber-50/30 pl-2 py-1.5 rounded-r"
                                    >
                                        <div class="text-[10px] text-gray-700 font-medium">
                                            {{ truncateText(item.title || item.text, 80) }}
                                        </div>
                                        <!-- Show what changed -->
                                        <div class="flex flex-wrap gap-1 mt-1">
                                            <span 
                                                v-if="item.changed_fields?.includes('text')"
                                                class="inline-flex items-center gap-0.5 px-1 py-0.5 bg-amber-100 text-amber-700 rounded text-[9px]"
                                            >
                                                <UIcon name="i-heroicons-document-text" class="w-2.5 h-2.5" />
                                                text
                                            </span>
                                            <span 
                                                v-if="item.changed_fields?.includes('status')"
                                                class="inline-flex items-center gap-0.5 px-1 py-0.5 bg-blue-100 text-blue-700 rounded text-[9px]"
                                            >
                                                <UIcon name="i-heroicons-flag" class="w-2.5 h-2.5" />
                                                {{ item.previous_status }} → {{ item.status }}
                                            </span>
                                            <span 
                                                v-if="item.changed_fields?.includes('load_mode')"
                                                class="inline-flex items-center gap-0.5 px-1 py-0.5 bg-purple-100 text-purple-700 rounded text-[9px]"
                                            >
                                                <UIcon name="i-heroicons-bolt" class="w-2.5 h-2.5" />
                                                {{ formatLoadMode(item.previous_load_mode) }} → {{ formatLoadMode(item.load_mode) }}
                                            </span>
                                            <span 
                                                v-if="item.changed_fields?.includes('category')"
                                                class="inline-flex items-center gap-0.5 px-1 py-0.5 bg-gray-100 text-gray-700 rounded text-[9px]"
                                            >
                                                <UIcon name="i-heroicons-tag" class="w-2.5 h-2.5" />
                                                category
                                            </span>
                                            <span 
                                                v-if="item.changed_fields?.includes('references')"
                                                class="inline-flex items-center gap-0.5 px-1 py-0.5 bg-teal-100 text-teal-700 rounded text-[9px]"
                                            >
                                                <UIcon name="i-heroicons-table-cells" class="w-2.5 h-2.5" />
                                                references
                                                <template v-if="item.references_added">+{{ item.references_added }}</template>
                                                <template v-if="item.references_removed">-{{ item.references_removed }}</template>
                                            </span>
                                            <span 
                                                v-if="item.changed_fields?.includes('title')"
                                                class="inline-flex items-center gap-0.5 px-1 py-0.5 bg-gray-100 text-gray-600 rounded text-[9px]"
                                            >
                                                title
                                            </span>
                                        </div>
                                    </div>
                                    <div v-if="modifiedItems.length > 5" class="text-[10px] text-gray-400 pl-2">
                                        +{{ modifiedItems.length - 5 }} more
                                    </div>
                                </div>
                                
                                <!-- Removed -->
                                <div v-if="removedItems.length" class="space-y-1">
                                    <div class="text-[10px] font-medium text-red-700">Removed</div>
                                    <div 
                                        v-for="item in removedItems.slice(0, 5)" 
                                        :key="item.instruction_id"
                                        class="border-l-2 border-l-red-400 bg-red-50/30 pl-2 py-1 rounded-r text-[10px] text-gray-700"
                                    >
                                        {{ truncateText(item.title || item.text, 80) }}
                                    </div>
                                    <div v-if="removedItems.length > 5" class="text-[10px] text-gray-400 pl-2">
                                        +{{ removedItems.length - 5 }} more
                                    </div>
                                </div>
                            </div>
                        </div>

                        <!-- Instructions Section (Collapsible) -->
                        <div v-if="!loadingBuildContent" class="flex-1 overflow-hidden flex flex-col min-h-0">
                            <!-- Instructions Header -->
                            <button
                                @click="instructionsExpanded = !instructionsExpanded"
                                class="w-full px-4 py-2 flex items-center justify-between hover:bg-gray-50/50 transition-colors border-b border-gray-100 shrink-0 bg-gray-50/50"
                            >
                                <div class="flex items-center gap-2">
                                    <UIcon 
                                        :name="instructionsExpanded ? 'i-heroicons-chevron-down' : 'i-heroicons-chevron-right'" 
                                        class="w-3 h-3 text-gray-400"
                                    />
                                    <span class="text-xs font-medium text-gray-700">Instructions</span>
                                    <span class="text-[10px] text-gray-400">({{ totalInstructions }})</span>
                                </div>
                            </button>
                            
                            <!-- Instructions Content -->
                            <div v-if="instructionsExpanded" class="flex-1 overflow-hidden flex flex-col min-h-0">
                                <div class="flex-1 overflow-auto p-3">
                                    <div class="bg-white border border-gray-200 rounded-lg overflow-hidden">
                                        <InstructionsTable
                                            :instructions="instructions"
                                            :loading="loadingInstructions"
                                            :compact="true"
                                            :show-source="true"
                                            :show-category="true"
                                            :show-data-source="false"
                                            :show-load-mode="true"
                                            :show-labels="false"
                                            :show-status="true"
                                            :show-pagination="totalInstructions > pageSize"
                                            :current-page="currentPage"
                                            :page-size="pageSize"
                                            :total-items="totalInstructions"
                                            :total-pages="totalPages"
                                            :visible-pages="visiblePages"
                                            empty-title="No instructions"
                                            empty-message="This build contains no instructions."
                                            @page-change="handlePageChange"
                                            @click="handleInstructionClick"
                                        />
                                    </div>
                                </div>
                            </div>
                        </div>
                    </template>
                </div>
            </div>
        </UCard>
    </UModal>
</template>

<script setup lang="ts">
import Spinner from '~/components/Spinner.vue'
import InstructionsTable from '~/components/instructions/InstructionsTable.vue'
import type { Instruction } from '~/composables/useInstructionHelpers'
import { useCan } from '~/composables/usePermissions'

interface Build {
    id: string
    build_number: number
    is_main: boolean
    status: string
    created_at: string
    total_instructions?: number
    added_count?: number
    modified_count?: number
    removed_count?: number
}

interface DiffInstructionItem {
    instruction_id: string
    change_type: 'added' | 'removed' | 'modified'
    title?: string
    text: string
    category?: string
    source_type?: string
    status?: string
    load_mode?: string
    previous_text?: string
    previous_title?: string
    previous_status?: string
    previous_load_mode?: string
    previous_category?: string
    changed_fields?: string[]
    // References changes
    references_added?: number
    references_removed?: number
}

interface BuildDiffDetailedResponse {
    build_a_id: string
    build_b_id: string
    build_a_number: number
    build_b_number: number
    items: DiffInstructionItem[]
    added_count: number
    modified_count: number
    removed_count: number
}

interface BuildContent {
    id: string
    build_id: string
    instruction_id: string
    instruction_version_id: string
    version_number?: number
    text?: string
    title?: string
    content_hash?: string
    load_mode?: string
    instruction_status?: string
    instruction_category?: string
}

interface Props {
    modelValue: boolean
    buildId?: string
    compareToBuildId?: string
}

const props = defineProps<Props>()
const emit = defineEmits<{
    'update:modelValue': [value: boolean]
    'rollback': [newBuildId: string]
}>()

// State
const loadingBuilds = ref(false)
const loadingInstructions = ref(false)
const loadingDiff = ref(false)
const rollingBack = ref(false)
const builds = ref<Build[]>([])
const selectedBuild = ref<Build | null>(null)
const instructions = ref<Instruction[]>([])
const diffData = ref<BuildDiffDetailedResponse | null>(null)
const diffExpanded = ref(true)
const instructionsExpanded = ref(false)

const toast = useToast()

// Permission check - use computed for reactivity
const canCreateBuilds = computed(() => useCan('create_builds'))

// Pagination
const currentPage = ref(1)
const pageSize = ref(25)
const totalInstructions = ref(0)

const totalPages = computed(() => Math.ceil(totalInstructions.value / pageSize.value) || 1)
const visiblePages = computed(() => {
    const pages: number[] = []
    const total = totalPages.value
    const current = currentPage.value
    
    let start = Math.max(1, current - 2)
    let end = Math.min(total, start + 4)
    
    if (end - start < 4) {
        start = Math.max(1, end - 4)
    }
    
    for (let i = start; i <= end; i++) {
        pages.push(i)
    }
    return pages
})

const isOpen = computed({
    get: () => props.modelValue,
    set: (value) => emit('update:modelValue', value)
})

// Computed - filter diff items by type
const addedItems = computed(() => 
    diffData.value?.items.filter(i => i.change_type === 'added') || []
)
const modifiedItems = computed(() => 
    diffData.value?.items.filter(i => i.change_type === 'modified') || []
)
const removedItems = computed(() => 
    diffData.value?.items.filter(i => i.change_type === 'removed') || []
)
const hasDiffChanges = computed(() => 
    (diffData.value?.added_count || 0) + 
    (diffData.value?.modified_count || 0) + 
    (diffData.value?.removed_count || 0) > 0
)

const loadingBuildContent = computed(() => loadingDiff.value || loadingInstructions.value)

// Methods
const truncateText = (text: string, maxLength: number) => {
    if (!text) return ''
    return text.length > maxLength ? text.slice(0, maxLength) + '...' : text
}

const formatLoadMode = (mode?: string) => {
    if (!mode) return '?'
    const labels: Record<string, string> = {
        always: 'Always',
        intelligent: 'Smart',
        disabled: 'Disabled'
    }
    return labels[mode] || mode
}

const fetchBuilds = async () => {
    loadingBuilds.value = true
    try {
        const response = await useMyFetch<{ items: Build[], total: number }>('/builds?limit=50')
        if (response.data.value) {
            builds.value = response.data.value.items || []
        }
    } catch (e) {
        console.error('Failed to fetch builds:', e)
    } finally {
        loadingBuilds.value = false
    }
}

const selectBuild = async (build: Build) => {
    selectedBuild.value = build
    currentPage.value = 1
    
    // Fetch instructions and diff in parallel
    await Promise.all([
        fetchInstructions(),
        fetchDiff()
    ])
}

const fetchInstructions = async () => {
    if (!selectedBuild.value) return
    
    loadingInstructions.value = true
    try {
        const response = await useMyFetch<{ items: BuildContent[], total: number, build_id: string, build_number: number }>(
            `/builds/${selectedBuild.value.id}/contents`
        )
        if (response.data.value) {
            const data = response.data.value
            // Transform BuildContent to Instruction format for InstructionsTable
            instructions.value = (data.items || []).map((content: BuildContent) => ({
                id: content.instruction_id,
                text: content.text || '',
                title: content.title,
                status: content.instruction_status || 'published',
                category: content.instruction_category || 'general',
                load_mode: content.load_mode || 'always',
                source_type: 'user', // Not available in BuildContent, default to user
                version_number: content.version_number,
                // Required fields with defaults for InstructionsTable compatibility
                organization_id: '',
                data_sources: [],
                created_at: '',
                updated_at: '',
            })) as unknown as Instruction[]
            totalInstructions.value = data.total || instructions.value.length
        }
    } catch (e) {
        console.error('Failed to fetch instructions:', e)
        instructions.value = []
        totalInstructions.value = 0
    } finally {
        loadingInstructions.value = false
    }
}

const fetchDiff = async () => {
    if (!selectedBuild.value) return
    
    // Find previous build
    const currentIndex = builds.value.findIndex(b => b.id === selectedBuild.value?.id)
    const previousBuild = builds.value[currentIndex + 1]
    
    if (!previousBuild) {
        diffData.value = null
        return
    }
    
    loadingDiff.value = true
    try {
        const response = await useMyFetch<BuildDiffDetailedResponse>(
            `/builds/${selectedBuild.value.id}/diff/details?compare_to=${previousBuild.id}`
        )
        if (response.data.value) {
            diffData.value = response.data.value
        }
    } catch (e) {
        console.error('Failed to fetch diff:', e)
        diffData.value = null
    } finally {
        loadingDiff.value = false
    }
}

const close = () => {
    emit('update:modelValue', false)
}

const rollbackToBuild = async () => {
    if (!selectedBuild.value || rollingBack.value) return
    
    rollingBack.value = true
    try {
        const response = await useMyFetch(`/builds/${selectedBuild.value.id}/rollback`, {
            method: 'POST'
        })
        
        if (response.error.value) {
            throw new Error((response.error.value as any)?.data?.detail || 'Failed to rollback')
        }
        
        toast.add({
            title: 'Rollback successful',
            description: `Created new build from Build #${selectedBuild.value.build_number}`,
            color: 'green',
            icon: 'i-heroicons-check-circle'
        })
        
        // Refresh builds list and select the new main build
        await fetchBuilds()
        const newMainBuild = builds.value.find(b => b.is_main)
        if (newMainBuild) {
            await selectBuild(newMainBuild)
            // Emit rollback event so parent can refresh its data
            emit('rollback', newMainBuild.id)
        }
    } catch (e: any) {
        console.error('Rollback failed:', e)
        toast.add({
            title: 'Rollback failed',
            description: e.message || 'An error occurred',
            color: 'red',
            icon: 'i-heroicons-x-circle'
        })
    } finally {
        rollingBack.value = false
    }
}

const handlePageChange = (page: number) => {
    currentPage.value = page
    // Note: The current /builds/{id}/contents endpoint doesn't support pagination
    // If pagination is needed, the API would need to be extended
}

const handleInstructionClick = (instruction: Instruction) => {
    // Could emit an event or open a detail view
    console.log('Clicked instruction:', instruction.id)
}

const formatDate = (dateStr: string) => {
    try {
        return new Date(dateStr).toLocaleDateString('en-US', { 
            month: 'short', 
            day: 'numeric' 
        })
    } catch {
        return dateStr
    }
}

const formatDateTime = (dateStr: string) => {
    try {
        return new Date(dateStr).toLocaleString('en-US', { 
            month: 'short', 
            day: 'numeric',
            hour: 'numeric',
            minute: '2-digit'
        })
    } catch {
        return dateStr
    }
}

// Watch for modal opening
watch(() => props.modelValue, async (newValue) => {
    if (newValue) {
        await fetchBuilds()
        
        // If a buildId was provided, select it
        if (props.buildId) {
            const build = builds.value.find(b => b.id === props.buildId)
            if (build) {
                await selectBuild(build)
            }
        } else if (builds.value.length) {
            // Select the first (most recent) build
            await selectBuild(builds.value[0])
        }
    } else {
        // Reset state on close
        selectedBuild.value = null
        instructions.value = []
        diffData.value = null
        currentPage.value = 1
    }
})
</script>
