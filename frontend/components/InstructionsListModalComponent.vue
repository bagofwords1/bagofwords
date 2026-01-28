<template>
    <UModal v-model="instructionsListModal" :ui="{ width: 'sm:max-w-4xl' }">
        <div class="p-4 relative h-[550px] flex flex-col">
            <!-- Header with close button -->
            <div class="flex items-center justify-between mb-3">
                <h1 class="text-base font-semibold text-gray-900">Instructions</h1>
                <button @click="instructionsListModal = false"
                    class="text-gray-400 hover:text-gray-600 outline-none">
                    <Icon name="heroicons:x-mark" class="w-5 h-5" />
                </button>
            </div>

            <!-- Filter bar with Git button and Add button -->
            <div class="flex items-center justify-between gap-3 mb-3">
                <InstructionsFilterBar
                    :search="inst.filters.search"
                    :source-types="inst.filters.sourceTypes"
                    :available-source-types="availableSourceTypes"
                    :status="inst.filters.status"
                    :load-modes="inst.filters.loadModes"
                    :categories="inst.filters.categories"
                    :data-source-id="inst.filters.dataSourceId"
                    :label-ids="[]"
                    :labels="[]"
                    :data-sources="allDataSources"
                    :compact="true"
                    @update:search="inst.debouncedSearch"
                    @update:source-types="v => inst.setFilter('sourceTypes', v)"
                    @update:status="v => inst.setFilter('status', v)"
                    @update:load-modes="v => inst.setFilter('loadModes', v)"
                    @update:categories="v => inst.setFilter('categories', v)"
                    @update:data-source-id="v => inst.setFilter('dataSourceId', v)"
                    @reset="inst.resetFilters"
                />
                
                <!-- Right side: Git button + Add button -->
                <div class="flex items-center gap-2 flex-shrink-0">
                    <GitConnectionButton
                        :has-connection="hasGitConnections"
                        :connected-repos="gitConnectedRepos"
                        :last-indexed-at="gitLastIndexed"
                        @click="openGitModal"
                    />
                    <UButton
                        icon="i-heroicons-plus"
                        color="blue"
                        size="xs"
                        @click="addInstruction"
                    >
                        {{ canCreateGlobalInstructions ? 'Add Instruction' : 'Suggest' }}
                    </UButton>
                </div>
            </div>

            <!-- Instructions List -->
            <div class="flex-1 min-h-0">
                <!-- Loading state -->
                <div v-if="initialLoading || inst.isLoading.value" class="flex items-center justify-center h-full">
                    <Spinner class="w-6 h-6 text-blue-500" />
                </div>

                <InstructionsTable
                    v-else
                    :instructions="inst.instructions.value"
                    :loading="inst.isLoading.value"
                    :data-sources="allDataSources"
                    :selectable="false"
                    :compact="true"
                    :show-source="true"
                    :show-load-mode="true"
                    :show-labels="false"
                    :show-status="true"
                    :current-page="inst.currentPage.value"
                    :page-size="inst.itemsPerPage.value"
                    :total-items="inst.total.value"
                    :total-pages="inst.pages.value"
                    :visible-pages="inst.visiblePages.value"
                    empty-title="No instructions"
                    empty-message="No instructions found matching your criteria."
                    @click="handleInstructionClick"
                    @page-change="inst.setPage"
                />
            </div>
        </div>

        <!-- Instruction Modal -->
        <InstructionModalComponent
            v-model="showInstructionModal"
            :instruction="editingInstruction"
            :initial-type="canCreateGlobalInstructions ? 'global' : 'private'"
            :is-suggestion="!canCreateGlobalInstructions"
            @instructionSaved="handleInstructionSaved"
        />

        <!-- Instruction Details Modal (Read-only) -->
        <InstructionDetailsModal
            v-model="showDetailsModal"
            :instruction="viewingInstruction"
        />

        <!-- Git Repo Modal -->
        <GitRepoModalComponent
            v-model="showGitModal"
            @changed="handleGitChanged"
        />
    </UModal>
</template>

<script setup lang="ts">
import InstructionModalComponent from '~/components/InstructionModalComponent.vue'
import InstructionDetailsModal from '~/components/InstructionDetailsModal.vue'
import InstructionsTable from '~/components/instructions/InstructionsTable.vue'
import InstructionsFilterBar from '~/components/instructions/InstructionsFilterBar.vue'
import GitConnectionButton from '~/components/instructions/GitConnectionButton.vue'
import GitRepoModalComponent from '~/components/GitRepoModalComponent.vue'
import Spinner from '~/components/Spinner.vue'
import { useInstructions } from '~/composables/useInstructions'
import type { Instruction } from '~/composables/useInstructionHelpers'

const toast = useToast()
const instructionsListModal = ref(false)
const initialLoading = ref(true)
const showInstructionModal = ref(false)
const editingInstruction = ref<Instruction | null>(null)
const showDetailsModal = ref(false)
const viewingInstruction = ref<Instruction | null>(null)
const showGitModal = ref(false)

// Instructions using the composable
const inst = useInstructions({
    autoFetch: false,
    pageSize: 12
})

// Git connection status
const gitConnectedCount = ref(0)
const gitLastIndexed = ref<string | null>(null)
const gitConnectedRepos = ref<{ provider: string; repoName: string }[]>([])
const hasGitConnections = computed(() => gitConnectedCount.value > 0)

// Data sources and source types
const allDataSources = ref<{ id: string; name: string; type: string }[]>([])
const availableSourceTypes = ref<{ value: string; label: string; icon?: string; heroicon?: string }[]>([])

// Check if user can create global instructions
const canCreateGlobalInstructions = computed(() => useCan('create_instructions'))

// Fetch data sources
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

// Fetch available source types
const fetchAvailableSourceTypes = async () => {
    try {
        const { data } = await useMyFetch<{ value: string; label: string; icon?: string; heroicon?: string }[]>('/instructions/source-types', { method: 'GET' })
        availableSourceTypes.value = data.value || []
    } catch (e) {
        console.error('Failed to fetch available source types:', e)
    }
}

// Fetch git status
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
            return
        }

        const repos: { provider: string; repoName: string }[] = []
        let latestIndexed: string | null = null

        for (const repo of repositories.value) {
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
    } catch (e) {
        console.error('Failed to fetch git status:', e)
    }
}

const openGitModal = () => {
    showGitModal.value = true
}

const handleGitChanged = () => {
    fetchGitStatus()
    fetchAvailableSourceTypes()
    inst.refresh()
}

// Methods
const openModal = async (dataSourceIds?: string[]) => {
    initialLoading.value = true
    instructionsListModal.value = true
    await fetchDataSources()
    fetchAvailableSourceTypes()
    fetchGitStatus()

    // Set data source filter if provided (filters to selected + global instructions)
    if (dataSourceIds && dataSourceIds.length > 0) {
        inst.filters.dataSourceIds = dataSourceIds
    } else {
        // Clear filter to show all
        inst.filters.dataSourceIds = []
    }

    await inst.fetchInstructions()
    initialLoading.value = false
}

// Reset state when modal closes
watch(instructionsListModal, (isOpen) => {
    if (!isOpen) {
        initialLoading.value = true
    }
})

const handleInstructionClick = (instruction: Instruction) => {
    if (canCreateGlobalInstructions.value) {
        editingInstruction.value = instruction
        showInstructionModal.value = true
    } else {
        viewingInstruction.value = instruction
        showDetailsModal.value = true
    }
}

const handleInstructionSaved = () => {
    inst.refresh()
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

defineExpose({ openModal })
</script>
