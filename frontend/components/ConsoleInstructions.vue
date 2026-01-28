<template>
    <div class="flex flex-col h-[calc(100vh-100px)]">
        <!-- Optional page header -->
        <div v-if="showHeader" class="flex items-start justify-between mb-6 shrink-0">
            <div>
                <h1 class="text-lg font-semibold">Instructions</h1>
                <p class="mt-2 text-gray-500">Create and manage your instructions</p>
            </div>
            <div class="flex items-center gap-2 mt-1">
                <!-- AI Suggestions button - only for users who can modify settings -->
                <UButton
                    v-if="canModifySettings"
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

                <!-- Pending Review button (only show when there is something pending) -->
                <UChip v-if="pendingSuggestionCount > 0" :text="pendingSuggestionCount" size="xl">
                    <UButton
                        color="white"
                        size="xs"
                        @click="showSuggestionsModal = true"
                    >
                        Pending Review
                    </UButton>
                </UChip>

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
                :data-source-ids="inst.filters.dataSourceIds"
                :label-ids="labelFilter"
                :labels="allLabels"
                :data-sources="allDataSources"
                :hide-domain-filter="true"
                @update:search="inst.debouncedSearch"
                @update:source-types="v => inst.setFilter('sourceTypes', v)"
                @update:status="v => inst.setFilter('status', v)"
                @update:load-modes="v => inst.setFilter('loadModes', v)"
                @update:categories="v => inst.setFilter('categories', v)"
                @update:data-source-ids="v => inst.setFilter('dataSourceIds', v)"
                @update:label-ids="handleLabelFilterChange"
                @label-created="fetchLabels"
                @reset="resetAllFilters"
            />

            <!-- Right: Bulk actions + Git -->
            <div class="flex items-center gap-3 shrink-0">
                <InstructionsBulkBar
                    v-if="canUpdateInstructions"
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
                    @open-scope-modal="showBulkScopeModal = true"
                    @open-labels-modal="showBulkLabelsModal = true"
                    @delete="handleBulkDelete"
                />

                <!-- Git Repositories button -->
                <GitConnectionButton
                    :has-connection="hasGitConnections"
                    :connected-repos="gitConnectedRepos"
                    :last-indexed-at="gitLastIndexed"
                    @click="openGitRepositoriesModal"
                />

                <!-- Build Version Selector - only show if user can view builds -->
                <BuildVersionSelector
                    v-if="canViewBuilds"
                    v-model="selectedBuildId"
                    :loading="loadingBuilds"
                    :git-repo-id="gitRepoId"
                    @rollback="handleRollback"
                />
            </div>
        </div>

        <!-- Instructions Table - fills remaining viewport height -->
        <div class="flex-1 min-h-0">
            <InstructionsTable
                :instructions="inst.instructions.value"
                :loading="inst.isLoading.value || inst.isBulkUpdating.value"
                :data-sources="allDataSources"
                :selectable="canUpdateInstructions"
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
            @open-instruction="handleOpenInstruction"
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

        <BuildExplorerModal
            v-model="showSuggestionsModal"
            :suggestions-mode="true"
            :user-only="!canApproveSuggestions"
            :git-repo-id="gitRepoId"
            @rollback="handleSuggestionsRollback"
        />

        <BulkScopeModal
            v-model="showBulkScopeModal"
            :data-sources="allDataSources"
            @set-scope="inst.bulkSetDataSources"
            @clear-scope="inst.bulkClearDataSources"
        />

        <BulkLabelsModal
            v-model="showBulkLabelsModal"
            :labels="allLabels"
            @set-labels="inst.bulkSetLabels"
            @clear-labels="inst.bulkClearLabels"
        />
    </div>
</template>

<script setup lang="ts">
import InstructionModalComponent from '~/components/InstructionModalComponent.vue'
import InstructionLabelsManagerModal from '~/components/InstructionLabelsManagerModal.vue'
import InstructionLearningSettingsModal from '~/components/InstructionLearningSettingsModal.vue'
import GitRepoModalComponent from '~/components/GitRepoModalComponent.vue'
import BuildExplorerModal from '~/components/instructions/BuildExplorerModal.vue'
import InstructionsTable from '~/components/instructions/InstructionsTable.vue'
import InstructionsFilterBar from '~/components/instructions/InstructionsFilterBar.vue'
import InstructionsBulkBar from '~/components/instructions/InstructionsBulkBar.vue'
import BulkScopeModal from '~/components/instructions/BulkScopeModal.vue'
import BulkLabelsModal from '~/components/instructions/BulkLabelsModal.vue'
import GitConnectionButton from '~/components/instructions/GitConnectionButton.vue'
import BuildVersionSelector from '~/components/instructions/BuildVersionSelector.vue'
import { useCan } from '~/composables/usePermissions'
import { useInstructions } from '~/composables/useInstructions'
import { useDomain } from '~/composables/useDomain'
import type { Instruction } from '~/composables/useInstructionHelpers'

// Props
withDefaults(defineProps<{
    showHeader?: boolean
}>(), {
    showHeader: false
})

// Domain filtering
const { selectedDomains } = useDomain()

// Wrapper for fetchBuilds to avoid hoisting issues
const refreshBuilds = () => fetchBuilds()

// Instructions composable with URL persistence and domain filtering
const inst = useInstructions({
    autoFetch: true,
    pageSize: 25,
    persistFiltersInUrl: true,
    dataSourceIds: selectedDomains,  // Pass selected domains for filtering
    onBulkSuccess: refreshBuilds  // Refresh builds list after bulk updates
})

// UI state
const showInstructionModal = ref(false)
const editingInstruction = ref<Instruction | null>(null)
const showLabelsManagerModal = ref(false)
const showLearningSettingsModal = ref(false)
const showGitRepositoriesModal = ref(false)
const showSuggestionsModal = ref(false)
const showBulkScopeModal = ref(false)
const showBulkLabelsModal = ref(false)
const pendingSuggestionCount = ref(0)

// Git connection status
const gitConnectedCount = ref(0)
const gitLastIndexed = ref<string | null>(null)
const gitConnectedRepos = ref<{ provider: string; repoName: string }[]>([])
const gitRepoId = ref<string>('')  // First connected git repo ID for push operations

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
const availableBuilds = ref<{ value: string; label: string; buildNumber: number; status: string; createdAt: string; source: string; isMain?: boolean }[]>([])
const loadingBuilds = ref(false)

// Computed
const canCreate = computed(() => useCan('create_instructions'))
const canUpdateInstructions = computed(() => useCan('update_instructions'))
const canViewBuilds = computed(() => useCan('view_builds'))
const canApproveSuggestions = computed(() => useCan('create_builds'))
const canModifySettings = computed(() => useCan('modify_settings'))
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
        // Fetch all org-level git repositories
        const { data: repositories } = await useMyFetch<Array<{
            id: string
            provider: string
            repo_url: string
            last_indexed_at: string | null
        }>>('/git/repositories', { method: 'GET' })
        
        if (!repositories.value || repositories.value.length === 0) {
            gitConnectedCount.value = 0
            gitLastIndexed.value = null
            gitConnectedRepos.value = []
            gitRepoId.value = ''
            return
        }

        const repos: { provider: string; repoName: string }[] = []
        let latestIndexed: string | null = null
        let firstRepoId: string | undefined

        for (const repo of repositories.value) {
            // Track the first connected git repo for push operations
            if (!firstRepoId) {
                firstRepoId = repo.id
            }
            
            // Track latest indexed time
            if (repo.last_indexed_at) {
                if (!latestIndexed || new Date(repo.last_indexed_at) > new Date(latestIndexed)) {
                    latestIndexed = repo.last_indexed_at
                }
            }
            
            // Extract repo name from URL
            const repoName = repo.repo_url.split('/').pop()?.replace(/\.git$/, '') || 'Repository'
            repos.push({ provider: repo.provider, repoName })
        }

        gitConnectedCount.value = repositories.value.length
        gitLastIndexed.value = latestIndexed
        gitConnectedRepos.value = repos
        gitRepoId.value = firstRepoId || ''
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

const handleOpenInstruction = (instruction: Instruction) => {
    // Open the clicked instruction in view mode
    editingInstruction.value = instruction
}

const openManageLabelsModal = () => {
    showLabelsManagerModal.value = true
}

const handleLabelsChanged = () => {
    fetchLabels()
    inst.refresh()
}

const handleBulkDelete = async () => {
    // Show confirmation before deleting
    const count = inst.selectedCount.value
    const confirmed = window.confirm(
        `Are you sure you want to delete ${count} instruction${count !== 1 ? 's' : ''}? This action cannot be undone.`
    )
    
    if (confirmed) {
        await inst.bulkDelete()
    }
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
                gitProvider: build.git_provider,
                isMain: build.is_main
            }))
        }
    } catch (e) {
        console.error('Failed to fetch builds:', e)
    } finally {
        loadingBuilds.value = false
    }
}

// Handle rollback - refresh builds and instructions
const handleRollback = async (newBuildId: string) => {
    // Refresh builds list to show the new rollback build
    await fetchBuilds()
    // Select the new build (which should be main)
    selectedBuildId.value = null // Reset to main/latest
    // Refresh instructions
    inst.refresh()
}

// Handle rollback from suggestions modal
const handleSuggestionsRollback = async (newBuildId: string) => {
    await handleRollback(newBuildId)
    // Refresh pending count since a suggestion was published
    fetchPendingSuggestionCount()
}

// Fetch count of pending suggestions
const fetchPendingSuggestionCount = async () => {
    try {
        const { data } = await useMyFetch<{ items: any[]; total: number }>('/builds', {
            method: 'GET',
            query: { status: 'pending_approval', limit: 1 }
        })
        pendingSuggestionCount.value = data.value?.total || 0
    } catch (e) {
        console.error('Failed to fetch pending suggestion count:', e)
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
    fetchPendingSuggestionCount()
})
</script>
