<template>
    <div class="py-6 space-y-6">
        <!-- Connect Git Repository Section (only shown when not connected) -->
        <div v-if="!hasGitConnection" class="border border-gray-200 rounded-lg p-6">
            <div class="bg-white">
                <div class="flex items-center border-b border-gray-200 pb-3 w-full">
                    <h3 class="text-lg mt-1 font-semibold text-gray-900">Connect your git repository and load dbt, Dataform, LookML, markdown, and Tableau metadata files</h3>
                </div>
                <div class="text-left mb-4 mt-5">
                    <p class="text-sm text-gray-500 leading-relaxed">
                        Connect additional context from Tableau, dbt, Dataform, LookML, code, and markdown files to your data sources. It will be used by AI agents throughout data analysis.
                        <br />
                        Integration is via git repository.
                    </p>
                    <div class="flex mt-4 mb-4 items-center space-x-3">
                        <UTooltip text="Tableau"><img src="/public/icons/tableau.png" alt="Tableau" class="h-5 inline" /></UTooltip>
                        <UTooltip text="dbt"><img src="/public/icons/dbt.png" alt="dbt" class="h-5 inline" /></UTooltip>
                        <UTooltip text="Dataform"><img src="/public/icons/dataform.png" alt="Dataform" class="h-5 inline" /></UTooltip>
                        <UTooltip text="LookML"><img src="/public/icons/lookml.png" alt="LookML" class="h-5 inline" /></UTooltip>
                        <UTooltip text="Markdown"><img src="/public/icons/markdown.png" alt="Markdown" class="h-5 inline" /></UTooltip>
                    </div>
                </div>

                <div class="mb-4 mt-6">
                    <div v-if="isLoading" class="inline-flex items-center text-gray-500 text-xs">
                        <Spinner class="w-4 h-4 mr-2" />
                        Loading...
                    </div>
                    <UButton v-else-if="canUpdateDataSource" icon="heroicons:code-bracket" class="bg-white border border-gray-300 rounded-lg px-3 py-1 text-xs text-black hover:bg-gray-50" @click="showGitModal = true">Connect Git Repository</UButton>
                </div>
            </div>
        </div>

        <GitRepoModalComponent v-model="showGitModal" :datasource-id="String(dsId)" :git-repository="integration?.git_repository" :metadata-resources="metadataResources" @changed="handleGitRepoChanged" />

        <!-- Instructions Section (only shown when git is connected) -->
        <div v-if="hasGitConnection" class="border border-gray-200 rounded-lg p-6">
            <!-- Header with filter bar, bulk actions, and count -->
            <div class="flex items-center justify-between gap-4 mb-3">
                <InstructionsFilterBar
                    :search="inst.filters.search"
                    :source-types="inst.filters.sourceTypes"
                    :available-source-types="availableSourceTypes"
                    :status="inst.filters.status"
                    :load-modes="inst.filters.loadModes"
                    :categories="inst.filters.categories"
                    :data-source-id="null"
                    :label-ids="labelFilter"
                    :labels="allLabels"
                    :data-sources="[]"
                    @update:search="inst.debouncedSearch"
                    @update:source-types="v => inst.setFilter('sourceTypes', v)"
                    @update:status="v => inst.setFilter('status', v)"
                    @update:load-modes="v => inst.setFilter('loadModes', v)"
                    @update:categories="v => inst.setFilter('categories', v)"
                    @update:label-ids="handleLabelFilterChange"
                    @label-created="fetchLabels"
                    @reset="resetAllFilters"
                />
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
                        :has-connection="hasGitConnection"
                        :connected-repos="gitConnectedRepos"
                        :last-indexed-at="lastIndexedAt"
                        :custom-tooltip="gitStatusTooltip"
                        @click="showGitModal = true"
                    />

                    <!-- Build Version Selector - only show if user can view builds -->
                    <BuildVersionSelector
                        v-if="canViewBuilds"
                        v-model="selectedBuildId"
                        :builds="availableBuilds"
                        :loading="loadingBuilds"
                        :git-repo-id="integration?.git_repository?.id || ''"
                        @rollback="handleRollback"
                    />
                </div>
            </div>

            <!-- Instructions Table -->
            <div class="h-[calc(100vh-280px)]">
                <InstructionsTable
                    :instructions="inst.instructions.value"
                    :loading="inst.isLoading.value || isIndexing"
                    :selectable="true"
                    :selected-ids="inst.selectedIds.value"
                    :is-all-page-selected="inst.isAllPageSelected.value"
                    :is-some-selected="inst.isSomeSelected.value"
                    :show-source="true"
                    :show-data-source="false"
                    :show-load-mode="true"
                    :show-labels="true"
                    :show-status="true"
                    :current-page="inst.currentPage.value"
                    :page-size="inst.itemsPerPage.value"
                    :total-items="inst.total.value"
                    :total-pages="inst.pages.value"
                    :visible-pages="inst.visiblePages.value"
                    empty-title="No instructions"
                    empty-message="No instructions associated with this data source yet."
                    @click="openInstruction"
                    @page-change="inst.setPage"
                    @toggle-select="inst.toggleSelection"
                    @toggle-page="inst.togglePageSelection"
                />
            </div>
        </div>

        <!-- Instruction Modal -->
        <InstructionModalComponent 
            v-model="showInstructionModal" 
            :instruction="selectedInstruction"
            @instruction-saved="handleInstructionSaved"
        />
    </div>
</template>

<script setup lang="ts">
definePageMeta({ auth: true, layout: 'integrations' })
import GitRepoModalComponent from '@/components/GitRepoModalComponent.vue'
import InstructionModalComponent from '~/components/InstructionModalComponent.vue'
import InstructionsTable from '~/components/instructions/InstructionsTable.vue'
import InstructionsFilterBar from '~/components/instructions/InstructionsFilterBar.vue'
import InstructionsBulkBar from '~/components/instructions/InstructionsBulkBar.vue'
import GitConnectionButton from '~/components/instructions/GitConnectionButton.vue'
import BuildVersionSelector from '~/components/instructions/BuildVersionSelector.vue'
import { useCan } from '~/composables/usePermissions'
import { useInstructions } from '~/composables/useInstructions'
import type { Instruction } from '~/composables/useInstructionHelpers'
import Spinner from '@/components/Spinner.vue'

const route = useRoute()
const dsId = computed(() => String(route.params.id || ''))

const canUpdateDataSource = computed(() => useCan('update_data_source'))
const canViewBuilds = computed(() => useCan('view_builds'))

const showGitModal = ref(false)
const isLoading = ref(false)

const integration = ref<any>(null)
const metadataResources = ref<any>({ resources: [] })
let pollInterval: ReturnType<typeof setInterval> | null = null

// Instructions using the composable
const inst = useInstructions({
    dataSourceId: dsId,
    autoFetch: false,
    pageSize: 15
})

const showInstructionModal = ref(false)
const selectedInstruction = ref<Instruction | null>(null)

// Labels and source types for filters
const allLabels = ref<{ id: string; name: string; color?: string | null }[]>([])
const labelFilter = ref<string[]>([])
const availableSourceTypes = ref<{ value: string; label: string; icon?: string; heroicon?: string }[]>([])

// Build version selection
const selectedBuildId = ref<string | null>(null)
const availableBuilds = ref<{ value: string; label: string; buildNumber: number; status: string; createdAt: string; source: string }[]>([])
const loadingBuilds = ref(false)

const repoDisplayName = computed(() => {
  const url = integration.value?.git_repository?.repo_url || ''
  const tail = String(url).split('/')?.pop() || ''
  return tail.replace(/\.git$/, '') || 'Repository'
})

const repoStatus = computed(() => {
  const jobStatus = metadataResources.value?.status
  if (jobStatus) return jobStatus
  return integration.value?.git_repository?.status || null
})
const isIndexing = computed(() => ['pending', 'indexing', 'running'].includes(repoStatus.value))
const hasGitConnection = computed(() => !!integration.value?.git_repository)

// Git connected repos for the button component
const gitConnectedRepos = computed(() => {
  if (!hasGitConnection.value) return []
  const provider = integration.value?.git_repository?.provider || 'custom'
  return [{ provider, repoName: repoDisplayName.value }]
})

const gitStatusTooltip = computed(() => {
  if (!hasGitConnection.value) return 'Connect a Git repository'
  if (isIndexing.value) return 'Indexing...'
  if (lastIndexedAt.value) {
    const date = new Date(lastIndexedAt.value)
    const now = new Date()
    const diffMs = now.getTime() - date.getTime()
    const diffMins = Math.floor(diffMs / 60000)
    const diffHours = Math.floor(diffMs / 3600000)
    const diffDays = Math.floor(diffMs / 86400000)
    let timeAgo = ''
    if (diffMins < 1) timeAgo = 'just now'
    else if (diffMins < 60) timeAgo = `${diffMins}m ago`
    else if (diffHours < 24) timeAgo = `${diffHours}h ago`
    else timeAgo = `${diffDays}d ago`
    return `Last indexed ${timeAgo}`
  }
  return 'Connected'
})

const lastIndexedAt = computed(() => {
  return metadataResources.value?.completed_at || integration.value?.git_repository?.last_indexed_at || null
})

async function fetchIntegration(silent = false) {
  if (!dsId.value) return
  if (!silent) isLoading.value = true
  try {
    const response = await useMyFetch(`/data_sources/${dsId.value}`, { method: 'GET' })
    if ((response.status as any)?.value === 'success') integration.value = (response.data as any)?.value
  } finally {
    if (!silent) isLoading.value = false
  }
}

async function fetchMetadataResources(silent = false) {
  if (!dsId.value) return
  try {
    const response = await useMyFetch(`/data_sources/${dsId.value}/metadata_resources`, { method: 'GET' })
    metadataResources.value = (response.data as any)?.value || { resources: [] }
  } catch (e) {
    // ignore
  }
}

function openInstruction(instruction: Instruction) {
  selectedInstruction.value = instruction
  showInstructionModal.value = true
}

function handleInstructionSaved() {
  inst.refresh()
  fetchAvailableSourceTypes()
}

async function fetchLabels() {
  try {
    const { data } = await useMyFetch<any[]>('/instructions/labels', { method: 'GET' })
    allLabels.value = data.value || []
  } catch (e) {
    console.error('Failed to fetch labels:', e)
  }
}

async function fetchAvailableSourceTypes() {
  try {
    const { data } = await useMyFetch<{ value: string; label: string; icon?: string; heroicon?: string }[]>('/instructions/source-types', { method: 'GET' })
    availableSourceTypes.value = data.value || []
  } catch (e) {
    console.error('Failed to fetch available source types:', e)
  }
}

function handleLabelFilterChange(values: string[]) {
  labelFilter.value = values
  inst.filters.labelIds = values
  inst.currentPage.value = 1
  inst.fetchInstructions()
}

function resetAllFilters() {
  labelFilter.value = []
  inst.resetFilters()
}

// Fetch available builds for version selector
async function fetchBuilds() {
  loadingBuilds.value = true
  try {
    const { data } = await useMyFetch<{ items: any[]; total: number }>('/api/builds', { 
      method: 'GET',
      query: { limit: 50 }
    })
    if (data.value?.items) {
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

// Handle rollback - refresh builds and instructions
async function handleRollback(newBuildId: string) {
  await fetchBuilds()
  selectedBuildId.value = null // Reset to main/latest
  inst.refresh()
}

function startPolling() {
  stopPolling()
  pollInterval = setInterval(async () => {
    await fetchMetadataResources(true)
    if (!isIndexing.value) {
      stopPolling()
      inst.refresh()
    }
  }, 5000)
}

function stopPolling() {
  if (pollInterval) {
    clearInterval(pollInterval)
    pollInterval = null
  }
}

watch(isIndexing, (val) => {
  if (val) startPolling()
  else stopPolling()
})

function handleGitRepoChanged() {
  fetchIntegration()
  fetchMetadataResources()
  inst.refresh()
  setTimeout(() => {
    if (isIndexing.value) startPolling()
  }, 500)
}

onMounted(async () => {
  isLoading.value = true
  try {
    await fetchIntegration(true)
    await fetchMetadataResources(true)
    await inst.fetchInstructions()
    fetchLabels()
    fetchAvailableSourceTypes()
    fetchBuilds()
  } finally {
    isLoading.value = false
  }
  if (isIndexing.value) startPolling()
})

onBeforeUnmount(() => {
  stopPolling()
})
</script>
