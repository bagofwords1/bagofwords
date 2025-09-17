<template>
  <div class="min-h-screen bg-gray-50 flex items-center justify-center py-12 px-4">
    <div class="w-full max-w-6xl">
      <OnboardingView forcedStepKey="instructions_added" :hideNextButton="true">
        <template #instructions>
          <div class="space-y-6">
            <!-- Suggested Instructions -->
            <div>
              <h3 class="text-sm font-medium text-gray-900 mb-1">Suggested Instructions</h3>
              <p class="text-xs text-gray-500 mb-3">Add a few instructions to guide the AI with business context.</p>
              <div class="space-y-2">
                <div 
                  v-for="instruction in suggestedInstructions" 
                  :key="instruction.id"
                  @click="toggleInstruction(instruction)"
                  class="flex items-center gap-3 p-3 border border-gray-200 rounded-lg cursor-pointer hover:bg-gray-50"
                  :class="{ 'bg-blue-50 border-blue-200': instruction.selected }"
                >
                  <UCheckbox color="blue" v-model="instruction.selected" />
                  <span class="text-sm text-gray-800">{{ instruction.text }}</span>
                </div>
              </div>
            </div>

            <!-- Context Enrichment -->
            <div>
              <h3 class="text-sm font-medium text-gray-900 mb-1">Context Enrichment</h3>
              <p class="text-xs text-gray-500 mb-3">Connect a Git repo to load dbt/markdown resources, then toggle items to include them in AI context.</p>
              <div class="bg-white border border-gray-200 rounded-lg p-4">
                <div class="flex items-center justify-between">
                  <div class="flex items-center">
                    <UTooltip text="Tableau">
                    <img src="/icons/tableau.png" alt="Tableau" class="h-5 mr-4 inline" />
                    </UTooltip>
                    <UTooltip text="dbt">
                    <img src="/icons/dbt.png" alt="dbt" class="h-5 mr-4 inline" />
                    </UTooltip>
                    <UTooltip text="LookML">
                    <img src="/icons/lookml.png" alt="dbt" class="h-5 mr-4 inline" />
                    </UTooltip>
                    <UTooltip text="Markdown">
                    <img src="/icons/markdown.png" alt="dbt" class="h-5 mr-4 inline" />
                    </UTooltip>
                  </div>
                  <div>
                    <UTooltip v-if="integration?.git_repository" :text="integration.git_repository.repo_url">
                      <UButton
                        icon="heroicons:code-bracket"
                        :label="repoDisplayName"
                        class="bg-white border border-gray-300 text-gray-500 px-4 py-2 text-xs rounded-md hover:bg-gray-200"
                        @click="showGitModal = true"
                      />
                    </UTooltip>
                    <UButton
                      v-else
                      icon="heroicons:code-bracket"
                      class="bg-white border border-gray-300 rounded-lg px-4 py-2 text-sm text-black hover:bg-gray-50"
                      @click="showGitModal = true"
                    >
                      Integrate
                    </UButton>
                  </div>
                </div>

                <div v-if="integration?.git_repository?.status === 'pending'" class="flex items-center mt-3 text-xs text-gray-500">
                  <UIcon name="heroicons:arrow-path" class="w-4 h-4 animate-spin mr-2" />
                  Indexing in progress... Resources will appear automatically when complete.
                </div>

                <div class="mt-4">
                  <div v-if="isLoadingMetadataResources" class="text-xs text-gray-500 flex items-center gap-2">
                    <UIcon name="heroicons:arrow-path" class="w-4 h-4 animate-spin" />
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
                              <UCheckbox v-model="res.is_active" class="mr-2" />
                              <div class="font-semibold text-gray-500 cursor-pointer flex items-center" @click="toggleResource(res)">
                                <UIcon :name="expandedResources[res.id] ? 'heroicons-chevron-down' : 'heroicons-chevron-right'" class="w-4 h-4 mr-1" />
                                <UIcon v-if="res.resource_type === 'model' || res.resource_type === 'model_config'" name="heroicons:cube" class="w-4 h-4 text-gray-500 mr-1" />
                                <UIcon v-else-if="res.resource_type === 'metric'" name="heroicons:hashtag" class="w-4 h-4 text-gray-500 mr-1" />
                                <span class="text-sm text-gray-800 truncate">{{ res.name }}</span>
                              </div>
                            </div>
                            <div v-if="expandedResources[res.id]" class="ml-6 mt-2">
                              <ResourceDisplay :resource="res" />
                            </div>
                          </li>
                        </ul>
                      </div>
                      <div class="pt-3 text-left">
                        <button 
                          @click="updateResourceStatus" 
                          :disabled="isUpdatingResources" 
                          class="bg-gray-900 hover:bg-black text-white text-xs font-medium py-1.5 px-3 rounded disabled:opacity-50"
                        >
                          <UIcon v-if="isUpdatingResources" name="heroicons:arrow-path" class="w-4 h-4 animate-spin inline mr-1" />
                          {{ isUpdatingResources ? 'Saving...' : 'Save Resources' }}
                        </button>
                      </div>
                    </div>
                    <div v-else class="text-xs text-gray-500 hidden"></div>
                  </div>
                </div>
              </div>
            </div>

            <!-- Save Button -->
            <div class="flex justify-end pt-4">
              <button 
                @click="handleSave" 
                :disabled="saving"
                class="bg-gray-900 hover:bg-black text-white text-sm font-medium py-2.5 px-5 rounded-lg disabled:opacity-50"
              >
                <span v-if="saving">Saving...</span>
                <span v-else>Save & Continue</span>
              </button>
            </div>
          </div>
        </template>
      </OnboardingView>
      <div class="text-center mt-6">
        <button @click="skipForNow" class="text-gray-500 hover:text-gray-700 text-sm">Skip onboarding</button>
      </div>
      <!-- Git Modal -->
      <GitRepoModalComponent 
        v-model="showGitModal"
        :datasource-id="String(dsId)"
        :git-repository="integration?.git_repository"
        :metadata-resources="metadataResources"
        @update:modelValue="handleGitModalClose"
      />
    </div>
  </div>
</template>

<script setup lang="ts">
definePageMeta({ auth: true, layout: 'onboarding' })
import OnboardingView from '@/components/onboarding/OnboardingView.vue'
import GitRepoModalComponent from '@/components/GitRepoModalComponent.vue'
import ResourceDisplay from '~/components/ResourceDisplay.vue'

const route = useRoute()
const { updateOnboarding } = useOnboarding()
const router = useRouter()

const dsId = computed(() => String(route.params.ds_id || ''))
const saving = ref(false)
const showGitModal = ref(false)
const isLoadingMetadataResources = ref(false)
const isUpdatingResources = ref(false)
const metadataResources = ref<any>({ resources: [] })
const resourceSearch = ref('')
const totalResources = computed(() => metadataResources.value?.resources?.length || 0)
const filteredResources = computed(() => {
  const q = resourceSearch.value.trim().toLowerCase()
  const list = metadataResources.value?.resources || []
  if (!q) return list
  return list.filter((r: any) => String(r.name || '').toLowerCase().includes(q))
})
const expandedResources = ref<Record<string, boolean>>({})
function toggleResource(resource: any) {
  expandedResources.value[resource.id] = !expandedResources.value[resource.id]
}

const integration = ref<any>(null)
const repoDisplayName = computed(() => {
  const url = integration.value?.git_repository?.repo_url || ''
  const tail = String(url).split('/')?.pop() || ''
  return tail.replace(/\.git$/, '') || 'Repository'
})

// Mock suggested instructions
const suggestedInstructions = ref([
  { id: 1, text: "Focus on sales performance metrics and revenue trends", selected: false },
  { id: 2, text: "Analyze customer behavior and segmentation patterns", selected: false },
  { id: 3, text: "Monitor operational efficiency and cost optimization", selected: false },
  { id: 4, text: "Track product performance and market analytics", selected: false },
  { id: 5, text: "Generate executive-level summaries and KPI reports", selected: false }
])

const hasSelectedInstructions = computed(() => 
  suggestedInstructions.value.some(i => i.selected)
)

function toggleInstruction(instruction: any) {
  instruction.selected = !instruction.selected
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

function handleGitModalClose(value: boolean) {
  if (!value) {
    fetchMetadataResources()
    fetchIntegration()
  }
}

async function handleSave() {
  if (saving.value) return
  saving.value = true
  
  try {
    const selectedInstructionTexts = suggestedInstructions.value
      .filter(i => i.selected)
      .map(i => i.text)
    
    // TODO: Save instructions and enrichment preferences
    // const payload = {
    //   instructions: selectedInstructionTexts,
    //   enrichments: []
    // }
    // await useMyFetch(`/data_sources/${dsId.value}/context`, { method: 'POST', body: payload })
    
    try {
      await updateOnboarding({ current_step: 'completed' as any, completed: true as any, dismissed: false as any })
    } catch (e) {
      console.warn('Failed to update onboarding, continuing to home:', e)
    }
  } finally {
    saving.value = false
    router.push({ path: '/', query: { setup: 'done' } })
  }
}

async function skipForNow() { 
  await updateOnboarding({ dismissed: true }) 
  router.push('/') 
}

async function fetchIntegration() {
  if (!dsId.value) return
  const response = await useMyFetch(`/data_sources/${dsId.value}`, { method: 'GET' })
  if ((response.status as any)?.value === 'success') {
    integration.value = (response.data as any)?.value
  }
}

onMounted(async () => {
  await fetchIntegration()
  await fetchMetadataResources()
})
</script>
