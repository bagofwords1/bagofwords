<template>
    <div class="py-6">
        <div class="border border-gray-200 rounded-lg p-6">
            <!-- Enrichment -->
            <div class="bg-white">
                <div @click="toggleEnrichmentSection" class="flex items-center justify-between cursor-pointer hover:bg-gray-50">
                    <div class="flex items-center border-b border-gray-200 pb-3 w-full">
                        <h3 class="text-lg mt-1 font-semibold text-gray-900">Connect your git repository and load dbt, Dataform, LookML, markdown, and Tableau metadata files</h3>
                    </div>
                </div>
                <div v-if="enrichmentExpanded" class="">
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
                        <template v-else-if="integration?.git_repository">
                            <div class="inline-flex items-center gap-3">
                                <UTooltip :text="integration.git_repository.repo_url">
                                    <UButton icon="heroicons:code-bracket" color="gray" :label="repoDisplayName" class="bg-white border border-gray-300 text-gray-500 px-4 py-2 text-xs rounded-md hover:bg-gray-200" @click="canUpdateDataSource ? showGitModal = true : null" :disabled="!canUpdateDataSource" />
                                </UTooltip>
                                <div class="flex items-center gap-2 text-xs text-gray-500">
                                    <template v-if="isIndexing">
                                        <Spinner class="w-3 h-3" />
                                        <span>Indexing...</span>
                                    </template>
                                    <template v-else-if="repoStatus === 'failed'">
                                        <UIcon name="heroicons:exclamation-circle" class="w-4 h-4 text-red-500" />
                                        <span class="text-red-500">Failed</span>
                                    </template>
                                    <template v-else-if="lastIndexedAt">
                                        <UIcon name="heroicons:check-circle" class="w-4 h-4 text-green-500" />
                                        <span>Indexed {{ lastIndexedAt }}</span>
                                    </template>
                                </div>
                            </div>
                        </template>
                        <UButton v-else-if="canUpdateDataSource" icon="heroicons:code-bracket" class="bg-white border border-gray-300 rounded-lg px-3 py-1 text-xs text-black hover:bg-gray-50" @click="showGitModal = true">Integrate</UButton>
                    </div>
                    <div
                      v-if="showNoFilesMessage"
                      class="mt-1 text-xs text-gray-500"
                    >
                      No dbt, Dataform, LookML, markdown, or Tableau metadata files were detected in this repository yet.
                    </div>
                    <ResourcesSelector ref="resourcesRef" :ds-id="String(dsId)" :can-update="canUpdateDataSource" @saved="onResourcesSaved" @error="onResourcesError" />
                </div>
            </div>

                    <GitRepoModalComponent v-model="showGitModal" :datasource-id="String(dsId)" :git-repository="integration?.git_repository" :metadata-resources="metadataResources" @changed="handleGitRepoChanged" />
        </div>
    </div>
</template>

<script setup lang="ts">
definePageMeta({ auth: true, layout: 'integrations' })
import GitRepoModalComponent from '@/components/GitRepoModalComponent.vue'
import ResourcesSelector from '~/components/datasources/ResourcesSelector.vue'
import { useCan } from '~/composables/usePermissions'
import Spinner from '@/components/Spinner.vue'

const route = useRoute()
const dsId = computed(() => String(route.params.id || ''))

const canUpdateDataSource = computed(() => useCan('update_data_source'))

const showGitModal = ref(false)
const enrichmentExpanded = ref(true)
const isLoading = ref(false)

const integration = ref<any>(null)
const metadataResources = ref<any>({ resources: [] })
const resourcesRef = ref<any>(null)
let pollInterval: ReturnType<typeof setInterval> | null = null

const repoDisplayName = computed(() => {
  const url = integration.value?.git_repository?.repo_url || ''
  const tail = String(url).split('/')?.pop() || ''
  return tail.replace(/\.git$/, '') || 'Repository'
})

// Get status from metadataResources (if full job response) or git_repository
const repoStatus = computed(() => {
  // metadataResources might contain full job data with status
  const jobStatus = metadataResources.value?.status
  if (jobStatus) return jobStatus
  // Fallback to git_repository.status
  return integration.value?.git_repository?.status || null
})
const isIndexing = computed(() => ['pending', 'indexing', 'running'].includes(repoStatus.value))

const lastIndexedAt = computed(() => {
  // metadataResources might contain completed_at from the job
  const jobCompletedAt = metadataResources.value?.completed_at
  const dt = jobCompletedAt || integration.value?.git_repository?.last_indexed_at
  if (!dt) return null
  const date = new Date(dt)
  const now = new Date()
  const diffMs = now.getTime() - date.getTime()
  const diffMins = Math.floor(diffMs / 60000)
  const diffHours = Math.floor(diffMs / 3600000)
  const diffDays = Math.floor(diffMs / 86400000)
  if (diffMins < 1) return 'just now'
  if (diffMins < 60) return `${diffMins}m ago`
  if (diffHours < 24) return `${diffHours}h ago`
  if (diffDays < 7) return `${diffDays}d ago`
  return date.toLocaleDateString()
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

function startPolling() {
  stopPolling()
  pollInterval = setInterval(async () => {
    await fetchMetadataResources(true)
    // Stop polling once indexing is done
    if (!isIndexing.value) {
      stopPolling()
      resourcesRef.value?.refresh?.()
    }
  }, 5000)
}

function stopPolling() {
  if (pollInterval) {
    clearInterval(pollInterval)
    pollInterval = null
  }
}

// Watch for indexing status changes to start/stop polling
watch(isIndexing, (val) => {
  if (val) startPolling()
  else stopPolling()
})

function onResourcesSaved() {}
function onResourcesError(e: any) { console.error(e) }

function toggleEnrichmentSection() { enrichmentExpanded.value = !enrichmentExpanded.value }

function handleGitRepoChanged() {
  // Only refresh when actual changes were made (save, delete, reindex)
  fetchIntegration()
  fetchMetadataResources()
  resourcesRef.value?.refresh?.()
  // Start polling if indexing was triggered
  setTimeout(() => {
    if (isIndexing.value) startPolling()
  }, 500)
}

const showNoFilesMessage = computed(() => {
  if (isLoading.value) return false
  if (!integration.value?.git_repository) return false

  const payload: any = metadataResources.value || {}
  const items = Array.isArray(payload.items) ? payload.items : []
  const legacyResources = Array.isArray(payload.resources) ? payload.resources : []
  const total =
    typeof payload.total === 'number'
      ? payload.total
      : (items.length || legacyResources.length)

  return total === 0
})

onMounted(async () => {
  isLoading.value = true
  try {
    await fetchIntegration(true)
    await fetchMetadataResources(true)
  } finally {
    isLoading.value = false
  }
  // Start polling if already indexing
  if (isIndexing.value) startPolling()
})

onBeforeUnmount(() => {
  stopPolling()
})
</script>


