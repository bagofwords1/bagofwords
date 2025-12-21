<template>
    <div class="flex flex-col h-[calc(100vh-100px)]">
        <!-- Optional page header -->
        <div v-if="showHeader" class="flex items-start justify-between mb-6 shrink-0">
            <div>
                <h1 class="text-lg font-semibold">Instructions</h1>
                <p class="mt-2 text-gray-500">Create and manage your instructions</p>
            </div>
            <div class="flex items-center gap-2 mt-1">
                <!-- AI Suggestions button -->
                <UButton
                    :variant="learningEnabled ? 'soft' : 'ghost'"
                    color="gray"
                    size="xs"
                    @click="openLearningSettingsModal"
                >
                    <span v-if="learningEnabled" class="text-amber-500">
                        <UIcon name="i-heroicons-bolt" class="w-3 h-3" />
                    </span>
                    <span v-else>
                        <UIcon name="i-heroicons-bolt-slash" class="w-3 h-3" />
                    </span>
                    AI Suggestions
                </UButton>

                <!-- Add Instruction button -->
                <UButton
                    icon="i-heroicons-plus"
                    color="blue"
                    size="xs"
                    @click="addInstruction"
                >
                    {{ addButtonLabel }}
                </UButton>
            </div>
        </div>

        <!-- Filter row with bulk actions -->
        <div class="flex items-center justify-between gap-4 mb-4 shrink-0">
            <!-- Left: Filters -->
            <InstructionsFilterBar
                :search="inst.filters.search"
                :source-types="inst.filters.sourceTypes"
                :available-source-types="availableSourceTypes"
                :status="inst.filters.status"
                :load-modes="inst.filters.loadModes"
                :categories="inst.filters.categories"
                :data-source-id="inst.filters.dataSourceId"
                :label-ids="labelFilter"
                :labels="allLabels"
                :data-sources="allDataSources"
                @update:search="inst.debouncedSearch"
                @update:source-types="v => inst.setFilter('sourceTypes', v)"
                @update:status="v => inst.setFilter('status', v)"
                @update:load-modes="v => inst.setFilter('loadModes', v)"
                @update:categories="v => inst.setFilter('categories', v)"
                @update:data-source-id="v => inst.setFilter('dataSourceId', v)"
                @update:label-ids="handleLabelFilterChange"
                @label-created="fetchLabels"
                @reset="resetAllFilters"
            />

            <!-- Right: Bulk actions + Git -->
            <div class="flex items-center gap-3 shrink-0">
                <InstructionsBulkBar
                    :selected-count="inst.selectedCount.value"
                    :select-all-mode="inst.selectAllMode.value"
                    :total="inst.total.value"
                    :labels="allLabels"
                    @select-all="inst.selectAll"
                    @clear="inst.clearSelection"
                    @publish="inst.bulkPublish"
                    @archive="inst.bulkArchive"
                    @make-draft="inst.bulkMakeDraft"
                    @load-always="inst.bulkSetLoadAlways"
                    @load-intelligent="inst.bulkSetLoadIntelligent"
                    @load-disabled="inst.bulkSetLoadDisabled"
                    @add-label="inst.bulkAddLabel"
                    @remove-label="inst.bulkRemoveLabel"
                />

                <!-- Git Repositories button -->
                <GitConnectionButton
                    :has-connection="hasGitConnections"
                    :connected-repos="gitConnectedRepos"
                    :last-indexed-at="gitLastIndexed"
                    @click="openGitRepositoriesModal"
                />

                <!-- Build Version Selector -->
                <BuildVersionSelector
                    v-model="selectedBuildId"
                    :builds="availableBuilds"
                    :loading="loadingBuilds"
                />
            </div>
        </div>

        <!-- Instructions Table - fills remaining viewport height -->
        <div class="flex-1 min-h-0">
            <InstructionsTable
                :instructions="inst.instructions.value"
                :loading="inst.isLoading.value || inst.isBulkUpdating.value"
                :selectable="true"
                :selected-ids="inst.selectedIds.value"
                :is-all-page-selected="inst.isAllPageSelected.value"
                :is-some-selected="inst.isSomeSelected.value"
                :show-source="true"
                :show-load-mode="true"
                :show-labels="true"
                :show-status="true"
                :current-page="inst.currentPage.value"
                :page-size="inst.itemsPerPage.value"
                :total-items="inst.total.value"
                :total-pages="inst.pages.value"
                :visible-pages="inst.visiblePages.value"
                empty-title="No instructions"
                empty-message="No instructions found. Create one to get started."
                @click="openInstruction"
                @page-change="inst.setPage"
                @toggle-select="inst.toggleSelection"
                @toggle-page="inst.togglePageSelection"
            />
        </div>

        <!-- Modals -->
        <InstructionModalComponent
            v-model="showInstructionModal"
            :instruction="editingInstruction"
            @instruction-saved="handleInstructionSaved"
        />

        <InstructionLabelsManagerModal
            v-model="showLabelsManagerModal"
            @labels-changed="handleLabelsChanged"
        />

        <InstructionLearningSettingsModal
            v-model="showLearningSettingsModal"
            :settings="learningSettings"
            @saved="handleSettingsSaved"
        />

        <GitRepoModalComponent
            v-model="showGitRepositoriesModal"
            @changed="handleGitChanged"
        />
    </div>
</template>

<script setup lang="ts">
import InstructionModalComponent from '~/components/InstructionModalComponent.vue'
import InstructionLabelsManagerModal from '~/components/InstructionLabelsManagerModal.vue'
import InstructionLearningSettingsModal from '~/components/InstructionLearningSettingsModal.vue'
import GitRepoModalComponent from '~/components/GitRepoModalComponent.vue'
import InstructionsTable from '~/components/instructions/InstructionsTable.vue'
import InstructionsFilterBar from '~/components/instructions/InstructionsFilterBar.vue'
import InstructionsBulkBar from '~/components/instructions/InstructionsBulkBar.vue'
import GitConnectionButton from '~/components/instructions/GitConnectionButton.vue'
import BuildVersionSelector from '~/components/instructions/BuildVersionSelector.vue'
import { useCan } from '~/composables/usePermissions'
import { useInstructions } from '~/composables/useInstructions'
import type { Instruction } from '~/composables/useInstructionHelpers'

// Props
withDefaults(defineProps<{
    showHeader?: boolean
}>(), {
    showHeader: false
})

// Wrapper for fetchBuilds to avoid hoisting issues
const refreshBuilds = () => fetchBuilds()

// Instructions composable with URL persistence
const inst = useInstructions({
    autoFetch: true,
    pageSize: 25,
    persistFiltersInUrl: true,
    onBulkSuccess: refreshBuilds  // Refresh builds list after bulk updates
})

// UI state
const showInstructionModal = ref(false)
const editingInstruction = ref<Instruction | null>(null)
const showLabelsManagerModal = ref(false)
const showLearningSettingsModal = ref(false)
const showGitRepositoriesModal = ref(false)

// Git connection status
const gitConnectedCount = ref(0)
const gitLastIndexed = ref<string | null>(null)
const gitConnectedRepos = ref<{ provider: string; repoName: string }[]>([])

// Labels
const allLabels = ref<{ id: string; name: string; color?: string | null }[]>([])
const labelFilter = ref<string[]>([])

// Data sources
const allDataSources = ref<{ id: string; name: string; type: string }[]>([])

// Available source types for filter
const availableSourceTypes = ref<{ value: string; label: string; icon?: string; heroicon?: string }[]>([])

// Learning settings
const learningEnabled = ref(false)
const learningSettings = ref<{ enabled: boolean; sensitivity: number; conditions: Record<string, boolean>; mode?: 'on' | 'off' } | null>(null)

// Build version selection
const selectedBuildId = ref<string | null>(null)
const availableBuilds = ref<{ value: string; label: string; buildNumber: number; status: string; createdAt: string; source: string }[]>([])
const loadingBuilds = ref(false)

// Computed
const canCreate = computed(() => useCan('create_instructions'))
const addButtonLabel = computed(() => canCreate.value ? 'Add Instruction' : 'Suggest')

const hasGitConnections = computed(() => gitConnectedCount.value > 0)

// Methods
const fetchLabels = async () => {
    try {
        const { data } = await useMyFetch<any[]>('/instructions/labels', { method: 'GET' })
        allLabels.value = data.value || []
    } catch (e) {
        console.error('Failed to fetch labels:', e)
    }
}

const fetchDataSources = async () => {
    try {
        const { data } = await useMyFetch<any[]>('/data_sources/active', { method: 'GET' })
        allDataSources.value = (data.value || []).map((ds: any) => ({
            id: ds.id,
            name: ds.name,
            type: ds.type
        }))
    } catch (e) {
        console.error('Failed to fetch data sources:', e)
    }
}

const fetchAvailableSourceTypes = async () => {
    try {
        const { data } = await useMyFetch<{ value: string; label: string; icon?: string; heroicon?: string }[]>('/instructions/source-types', { method: 'GET' })
        availableSourceTypes.value = data.value || []
    } catch (e) {
        console.error('Failed to fetch available source types:', e)
    }
}

const fetchLearningSettings = async () => {
    try {
        const { data } = await useMyFetch('/organization/settings', { method: 'GET' })
        const suggestInstructions = (data.value as any)?.config?.suggest_instructions
        learningEnabled.value = suggestInstructions?.value ?? false
        learningSettings.value = {
            enabled: suggestInstructions?.value ?? false,
            sensitivity: suggestInstructions?.sensitivity ?? 0.6,
            conditions: suggestInstructions?.conditions ?? {},
            mode: suggestInstructions?.value ? 'on' : 'off'
        }
    } catch (e) {
        console.error('Failed to fetch learning settings:', e)
    }
}

const fetchGitStatus = async () => {
    try {
        if (allDataSources.value.length === 0) return

        // Check all data sources for git connection
        const results: { hasGit: boolean; completedAt: string | null; provider?: string; repoUrl?: string }[] = []
        
        for (const ds of allDataSources.value) {
            let hasGit = false
            let completedAt: string | null = null
            let provider: string | undefined
            let repoUrl: string | undefined
            
            // Fetch full data source which includes git_repository
            const { data: fullDs } = await useMyFetch(`/data_sources/${ds.id}`, { method: 'GET' })
            if (fullDs.value && (fullDs.value as any).git_repository) {
                hasGit = true
                provider = (fullDs.value as any).git_repository.provider
                repoUrl = (fullDs.value as any).git_repository.repo_url
                
                // Only fetch metadata for data sources with git connection
                const { data: metaData } = await useMyFetch(`/data_sources/${ds.id}/metadata_resources`, { method: 'GET' })
                if (metaData.value) {
                    completedAt = (metaData.value as any).completed_at || null
                }
            }
            
            results.push({ hasGit, completedAt, provider, repoUrl })
        }

        let connectedCount = 0
        let latestIndexed: string | null = null
        const repos: { provider: string; repoName: string }[] = []

        for (const result of results) {
            if (result.hasGit) {
                connectedCount++
                if (result.completedAt) {
                    if (!latestIndexed || new Date(result.completedAt) > new Date(latestIndexed)) {
                        latestIndexed = result.completedAt
                    }
                }
                if (result.provider && result.repoUrl) {
                    const repoName = result.repoUrl.split('/').pop()?.replace(/\.git$/, '') || 'Repository'
                    repos.push({ provider: result.provider, repoName })
                }
            }
        }

        gitConnectedCount.value = connectedCount
        gitLastIndexed.value = latestIndexed
        gitConnectedRepos.value = repos
    } catch (e) {
        console.error('Failed to fetch git status:', e)
    }
}

const openGitRepositoriesModal = () => {
    showGitRepositoriesModal.value = true
}

const handleGitChanged = () => {
    fetchGitStatus()
    fetchAvailableSourceTypes()
    inst.refresh()
}

const openInstruction = (instruction: Instruction) => {
    editingInstruction.value = instruction
    showInstructionModal.value = true
}

const addInstruction = () => {
    editingInstruction.value = null
    showInstructionModal.value = true
}

const handleInstructionSaved = () => {
    showInstructionModal.value = false
    fetchAvailableSourceTypes()
    inst.refresh()
}

const openManageLabelsModal = () => {
    showLabelsManagerModal.value = true
}

const handleLabelsChanged = () => {
    fetchLabels()
    inst.refresh()
}

const openLearningSettingsModal = () => {
    showLearningSettingsModal.value = true
}

const handleSettingsSaved = () => {
    fetchLearningSettings()
}

const handleLabelFilterChange = (values: string[]) => {
    labelFilter.value = values
    inst.filters.labelIds = values
    inst.currentPage.value = 1
    inst.fetchInstructions()
}

const resetAllFilters = () => {
    labelFilter.value = []
    inst.resetFilters()
}

// Fetch available builds for version selector
const fetchBuilds = async () => {
    loadingBuilds.value = true
    try {
        // Fetch builds (backend defaults to approved status)
        const { data } = await useMyFetch<{ items: any[]; total: number }>('/api/builds', { 
            method: 'GET',
            query: { limit: 50 }
        })
        if (data.value?.items) {
            // Sort by build_number desc
            const builds = data.value.items
                .sort((a: any, b: any) => b.build_number - a.build_number)
            
            availableBuilds.value = builds.map((build: any) => ({
                value: build.id,
                label: String(build.build_number),
                buildNumber: build.build_number,
                status: build.status,
                createdAt: build.created_at,
                source: build.source,
                gitProvider: build.git_provider
            }))
        }
    } catch (e) {
        console.error('Failed to fetch builds:', e)
    } finally {
        loadingBuilds.value = false
    }
}

// Watch for build selection changes
watch(selectedBuildId, (newBuildId) => {
    inst.filters.buildId = newBuildId
    inst.currentPage.value = 1
    inst.fetchInstructions()
})

// Expose refresh for parent
const refresh = () => {
    inst.refresh()
}

defineExpose({ refresh })

// Initialize
onMounted(async () => {
    fetchLabels()
    await fetchDataSources()
    fetchLearningSettings()
    fetchGitStatus()
    fetchAvailableSourceTypes()
    fetchBuilds()
})
</script>
