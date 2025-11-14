<template>
    <div class="py-6">
        <div class="border border-gray-200 rounded-lg p-6">
            <!-- Enrichment -->
            <div class="bg-white">
                <div @click="toggleEnrichmentSection" class="flex items-center justify-between cursor-pointer hover:bg-gray-50">
                    <div class="flex items-center border-b border-gray-200 pb-3 w-full">
                        <h3 class="text-lg mt-1 font-semibold text-gray-900">Connect Tableau, dbt, Dataform, and your AGENTS.md files</h3>
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
                        <div v-if="isLoadingIntegration" class="inline-flex items-center text-gray-500 text-xs">
                            <Spinner class="w-4 h-4 mr-2" />
                            Loading repository...
                        </div>
                        <UTooltip v-if="integration?.git_repository" :text="integration.git_repository.repo_url">
                            <UButton icon="heroicons:code-bracket" color="gray" :label="repoDisplayName" class="bg-white border border-gray-300 text-gray-500 px-4 py-2 text-xs rounded-md hover:bg-gray-200" @click="canUpdateDataSource ? showGitModal = true : null" :disabled="!canUpdateDataSource" />
                        </UTooltip>
                        <UButton v-else-if="canUpdateDataSource && !isLoadingIntegration" icon="heroicons:code-bracket" class="bg-white border border-gray-300 rounded-lg px-3 py-1 text-xs text-black hover:bg-gray-50" @click="showGitModal = true">Integrate</UButton>
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

                    <GitRepoModalComponent v-model="showGitModal" :datasource-id="String(dsId)" :git-repository="integration?.git_repository" :metadata-resources="metadataResources" @update:modelValue="handleGitModalClose" />
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
const isLoadingMetadataResources = ref(false)
const isLoadingIntegration = ref(false)

const integration = ref<any>(null)
const metadataResources = ref<any>({ resources: [] })
const resourcesRef = ref<any>(null)

const repoDisplayName = computed(() => {
  const url = integration.value?.git_repository?.repo_url || ''
  const tail = String(url).split('/')?.pop() || ''
  return tail.replace(/\.git$/, '') || 'Repository'
})

async function fetchIntegration() {
  if (!dsId.value) return
  isLoadingIntegration.value = true
  try {
    const response = await useMyFetch(`/data_sources/${dsId.value}`, { method: 'GET' })
    if ((response.status as any)?.value === 'success') integration.value = (response.data as any)?.value
  } finally {
    isLoadingIntegration.value = false
  }
}

async function fetchMetadataResources() {
  if (!dsId.value) return
  isLoadingMetadataResources.value = true
  try {
    const response = await useMyFetch(`/data_sources/${dsId.value}/metadata_resources`, { method: 'GET' })
    metadataResources.value = (response.data as any)?.value || { resources: [] }
  } finally {
    isLoadingMetadataResources.value = false
  }
}

function onResourcesSaved() {}
function onResourcesError(e: any) { console.error(e) }

function toggleEnrichmentSection() { enrichmentExpanded.value = !enrichmentExpanded.value }
function handleGitModalClose(value: boolean) { if (!value) { fetchMetadataResources(); fetchIntegration(); resourcesRef.value?.refresh?.() } }

const showNoFilesMessage = computed(() => {
  if (isLoadingMetadataResources.value) return false
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
  await fetchIntegration()
  await fetchMetadataResources()
})
</script>


