<template>
    <div class="py-6">
        <div class="border border-gray-200 rounded-lg p-6">
            <!-- Enrichment -->
            <div class="bg-white">
                <div @click="toggleEnrichmentSection" class="flex items-center justify-between cursor-pointer hover:bg-gray-50">
                    <div class="flex items-center border-b border-gray-200 pb-3 w-full">
                        <h3 class="text-lg mt-1 font-semibold text-gray-900">Connect Tableau, dbt, and your AGENTS.md files</h3>
                    </div>
                </div>
                <div v-if="enrichmentExpanded" class="">
                    <div class="text-left mb-4 mt-5">
                        <p class="text-sm text-gray-500 leading-relaxed">
                            Connect additional context from Tableau, dbt, LookML, code, and markdown files to your data sources. It will be used by AI agents throughout data analysis.
                            <br />
                            Integration is via git repository.
                        </p>
                        <div class="flex mt-4 mb-4 items-center space-x-3">
                            <UTooltip text="Tableau"><img src="/public/icons/tableau.png" alt="Tableau" class="h-5 inline" /></UTooltip>
                            <UTooltip text="dbt"><img src="/public/icons/dbt.png" alt="dbt" class="h-5 inline" /></UTooltip>
                            <UTooltip text="LookML"><img src="/public/icons/lookml.png" alt="LookML" class="h-5 inline" /></UTooltip>
                            <UTooltip text="Markdown"><img src="/public/icons/markdown.png" alt="Markdown" class="h-5 inline" /></UTooltip>
                        </div>
                    </div>

                    <div class="mb-4 mt-6">
                        <UTooltip v-if="integration?.git_repository" :text="integration.git_repository.repo_url">
                            <UButton icon="heroicons:code-bracket" :label="repoDisplayName" class="bg-white border border-gray-300 text-gray-500 px-4 py-2 text-xs rounded-md hover:bg-gray-200" @click="showGitModal = true" />
                        </UTooltip>
                        <UButton v-else icon="heroicons:code-bracket" class="bg-white border border-gray-300 rounded-lg px-3 py-1 text-xs text-black hover:bg-gray-50" @click="showGitModal = true">Integrate</UButton>
                    </div>

                    <div>
                        <div v-if="isLoadingMetadataResources" class="text-xs text-gray-500 flex items-center gap-2">
                            <Spinner class="w-4 h-4" />
                            Loading resources...
                        </div>
                        <div v-else>
                            <div v-if="metadataResources?.resources && metadataResources.resources.length > 0">
                                <div class="mb-2">
                                    <input v-model="resourceSearch" type="text" placeholder="Search resources..." class="border border-gray-300 rounded-lg px-3 py-2 w-full h-9 text-sm focus:outline-none focus:border-blue-500" />
                                    <div class="mt-1 text-xs text-gray-500 text-right">{{ filteredResources.length }} of {{ totalResources }} shown</div>
                                </div>
                                <div class="border border-gray-100 rounded max-h-64 overflow-y-auto min-h-[120px]">
                                    <ul class="divide-y divide-gray-100">
                                        <li v-for="res in filteredResources" :key="res.id" class="py-2 px-3">
                                            <div class="flex items-center">
                                                <UCheckbox v-if="canUpdateDataSource" v-model="res.is_active" class="mr-2" />
                                                <div class="font-semibold text-gray-600 cursor-pointer flex items-center" @click="toggleResource(res)">
                                                    <UIcon :name="expandedResources[res.id] ? 'heroicons-chevron-down' : 'heroicons-chevron-right'" class="w-4 h-4 mr-1" />
                                                    <UIcon v-if="res.resource_type === 'model' || res.resource_type === 'model_config'" name="heroicons:cube" class="w-4 h-4 text-gray-500 mr-1" />
                                                    <UIcon v-else-if="res.resource_type === 'metric'" name="heroicons:hashtag" class="w-4 h-4 text-gray-500 mr-1" />
                                                    <span class="text-sm">{{ res.name }}</span>
                                                </div>
                                            </div>
                                            <div v-if="expandedResources[res.id]" class="ml-6 mt-2">
                                                <ResourceDisplay :resource="res" />
                                            </div>
                                        </li>
                                    </ul>
                                </div>
                                <div class="pt-3 text-left" v-if="canUpdateDataSource">
                                    <button @click="updateResourceStatus" :disabled="isUpdatingResources" class="bg-blue-500 hover:bg-blue-600 text-white text-xs font-medium py-1.5 px-3 rounded disabled:opacity-50">
                                        <UIcon v-if="isUpdatingResources" name="heroicons:arrow-path" class="w-4 h-4 animate-spin inline mr-1" />
                                        {{ isUpdatingResources ? 'Saving...' : 'Save Resources' }}
                                    </button>
                                </div>
                            </div>
                            <div v-else class="text-xs text-gray-500"></div>
                        </div>
                    </div>
                </div>
            </div>

            <UModal v-model="showGitModal" :ui="{ width: 'sm:max-w-2xl' }">
                <div class="p-4">
                    <GitRepoModalComponent v-model="showGitModal" :datasource-id="String(dsId)" :git-repository="integration?.git_repository" :metadata-resources="metadataResources" @update:modelValue="handleGitModalClose" />
                </div>
            </UModal>
        </div>
    </div>
</template>

<script setup lang="ts">
definePageMeta({ auth: true, layout: 'integrations' })
import Spinner from '@/components/Spinner.vue'
import GitRepoModalComponent from '@/components/GitRepoModalComponent.vue'
import ResourceDisplay from '~/components/ResourceDisplay.vue'
import { useCan } from '~/composables/usePermissions'

const route = useRoute()
const dsId = computed(() => String(route.params.id || ''))

const canUpdateDataSource = computed(() => useCan('update_data_source'))

const showGitModal = ref(false)
const enrichmentExpanded = ref(true)
const isLoadingMetadataResources = ref(false)
const isUpdatingResources = ref(false)

const integration = ref<any>(null)
const metadataResources = ref<any>({ resources: [] })
const resourceSearch = ref('')
const expandedResources = ref<Record<string, boolean>>({})

const totalResources = computed(() => metadataResources.value?.resources?.length || 0)
const filteredResources = computed(() => {
  const q = resourceSearch.value.trim().toLowerCase()
  const list = metadataResources.value?.resources || []
  if (!q) return list
  return list.filter((r: any) => String(r.name || '').toLowerCase().includes(q))
})

function toggleResource(resource: any) {
  expandedResources.value[resource.id] = !expandedResources.value[resource.id]
}

const repoDisplayName = computed(() => {
  const url = integration.value?.git_repository?.repo_url || ''
  const tail = String(url).split('/')?.pop() || ''
  return tail.replace(/\.git$/, '') || 'Repository'
})

async function fetchIntegration() {
  if (!dsId.value) return
  const response = await useMyFetch(`/data_sources/${dsId.value}`, { method: 'GET' })
  if ((response.status as any)?.value === 'success') integration.value = (response.data as any)?.value
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

async function updateResourceStatus() {
  if (!dsId.value || !metadataResources.value?.resources) return
  isUpdatingResources.value = true
  try {
    const res = await useMyFetch(`/data_sources/${dsId.value}/update_metadata_resources`, { method: 'PUT', body: metadataResources.value.resources })
    if ((res.status as any)?.value === 'success') {
      metadataResources.value = (res.data as any)?.value || metadataResources.value
    }
  } finally {
    isUpdatingResources.value = false
  }
}

function toggleEnrichmentSection() { enrichmentExpanded.value = !enrichmentExpanded.value }
function handleGitModalClose(value: boolean) { if (!value) { fetchMetadataResources(); fetchIntegration() } }

onMounted(async () => {
  await fetchIntegration()
  await fetchMetadataResources()
})
</script>


