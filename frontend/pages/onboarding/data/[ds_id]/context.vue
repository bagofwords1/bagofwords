<template>
  <div class="min-h-screen bg-gray-50 flex items-center justify-center py-12 px-4">
    <!-- Page-level loading overlay so spinner shows even before slot renders -->
    <div v-if="isLoading" class="fixed inset-0 z-50 flex items-center justify-center bg-white/70">
      <div class="flex items-center gap-2 text-gray-700">
        <Spinner class="w-5 h-5" />
        <span class="text-sm">{{ loadingText }}</span>
      </div>
    </div>
    <div class="w-full max-w-6xl">
      <OnboardingView forcedStepKey="instructions_added" :hideNextButton="true">
        <template #instructions>
          <!-- Loading State -->
            <div v-if="isLoading" class="flex items-center justify-center min-h-[400px] space-x-2">
              <Spinner class="w-4 h-4" />
              <span class="thinking-shimmer text-sm">{{ loadingText }}</span>
            </div>

          <!-- Content Sections -->
          <div v-else class="space-y-6 fade-in">
            <!-- Suggested Instructions -->
            <div class="bg-white border border-gray-200 rounded-lg transition-all duration-500 ease-in-out">
              <div 
                @click="toggleInstructionsSection"
                class="flex items-center justify-between cursor-pointer p-3 hover:bg-gray-50"
              >
                <div class="flex items-center">
                  <UIcon 
                    :name="instructionsExpanded ? 'heroicons:chevron-down' : 'heroicons:chevron-right'" 
                    class="w-5 h-5 text-gray-500 mr-2 transition-transform duration-200"
                  />
                  <h3 class="text-sm font-semibold text-gray-900">Suggested Instructions</h3>
                </div>
              </div>

              <div v-if="instructionsExpanded" class="px-3 pb-3">
                <div class="text-left mb-4">
                  <p class="text-xs leading-relaxed text-gray-500">Custom instructions are great for business-specific context, glossary and useful code guidelines/snippets.</p>
                </div>
                
                <div class="space-y-3">
                  <div v-if="isLoadingInstructions" class="text-xs text-gray-500 flex items-center gap-2">
                    <Spinner class="w-4 h-4" />
                    Loading instructions...
                  </div>
                  <div v-else>
                    <div 
                      v-for="instruction in suggestedInstructions" 
                      :key="instruction.id"
                      class="hover:bg-gray-50 bg-white mt-2 border border-gray-200 rounded-md p-3 transition-colors relative"
                    >
                      <div class="text-[12px] text-gray-800 leading-relaxed pr-24 whitespace-normal break-words max-w-full">
                        {{ instruction.text }}
                      </div>
                      
                      <div class="absolute top-2 right-2 flex items-center gap-2">
                        <template v-if="instructionAction[instruction.id]">
                          <span 
                            class="px-2 py-0.5 text-[11px] rounded-full border"
                            :class="instructionAction[instruction.id] === 'approved' ? 'bg-green-50 text-green-700 border-green-100' : 'bg-red-50 text-red-700 border-red-100'"
                          >
                            {{ instructionAction[instruction.id] === 'approved' ? 'Approved' : 'Removed' }}
                          </span>
                        </template>
                        <template v-else>
                          <span class="hover:bg-gray-100 rounded cursor-pointer" @click="rejectInstruction(instruction)">
                            <Icon 
                              name="heroicons:x-mark" 
                              class="w-4 h-4 text-red-500 rounded cursor-pointer" 
                            />
                          </span>
                          <span class="hover:bg-gray-100 rounded cursor-pointer" @click="approveInstruction(instruction)">
                            <Icon 
                              name="heroicons:check" 
                              class="w-4 h-4 text-green-500 rounded cursor-pointer" 
                            />
                          </span>
                        </template>
                      </div>
                    </div>
                    <div class="flex items-center gap-2">
                      <button class="text-xs text-blue-500 hover:text-blue-600 p-2 rounded-md" @click="openInstructionModal">
                        Add Custom Instruction
                      </button>
                      <button 
                        v-if="suggestedInstructions.length === 0 && hasAttemptedLLMSync"
                        class="text-xs text-gray-500 hover:text-gray-600 p-2 rounded-md"
                        :disabled="isLLMSyncInProgress"
                        @click="runLLMSync"
                      >
                        {{ isLLMSyncInProgress ? 'Generating...' : 'Generate AI Suggestions' }}
                      </button>
                    </div>
                  </div>
                </div>
              </div>
            </div>

            <!-- Context Enrichment -->
            <div class="bg-white border border-gray-200 rounded-lg transition-all duration-500 ease-in-out">
              <div 
                @click="toggleEnrichmentSection"
                class="flex items-center justify-between cursor-pointer p-3 hover:bg-gray-50"
              >
                <div class="flex items-center">
                  <UIcon 
                    :name="enrichmentExpanded ? 'heroicons:chevron-down' : 'heroicons:chevron-right'" 
                    class="w-5 h-5 text-gray-500 mr-2 transition-transform duration-200"
                  />
                  <h3 class="text-sm font-semibold text-gray-900">Enrich Context</h3>
                </div>
                <div class="flex items-center gap-2">
                  <UTooltip text="Tableau">
                    <img src="/icons/tableau.png" alt="Tableau" class="h-3 inline" />
                  </UTooltip>
                  <UTooltip text="dbt">
                    <img src="/icons/dbt.png" alt="dbt" class="h-3 inline" />
                  </UTooltip>
                  <UTooltip text="LookML">
                    <img src="/icons/lookml.png" alt="LookML" class="h-3 inline" />
                  </UTooltip>
                  <UTooltip text="Markdown">
                    <img src="/icons/markdown.png" alt="Markdown" class="h-3 inline" />
                  </UTooltip>
                </div>
              </div>

              <div v-if="enrichmentExpanded" class="px-3 pb-3">
                <div class="text-center mb-4 mt-5">
                  <p class="text-sm text-gray-500">Connect a Git repo to load dbt/markdown resources, then toggle items to include them in AI context.</p>
                </div>
                <div class="flex items-center justify-center gap-2">
                  <UTooltip text="Tableau">
                    <img src="/icons/tableau.png" alt="Tableau" class="h-5 inline" />
                  </UTooltip>
                  <UTooltip text="dbt">
                    <img src="/icons/dbt.png" alt="dbt" class="h-5 inline" />
                  </UTooltip>
                  <UTooltip text="LookML">
                    <img src="/icons/lookml.png" alt="LookML" class="h-5 inline" />
                  </UTooltip>
                  <UTooltip text="Markdown">
                    <img src="/icons/markdown.png" alt="Markdown" class="h-5 inline" />
                  </UTooltip>
                </div>
                
                <div class="text-center mb-4 mt-6">
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
                    class="bg-white border border-gray-300 rounded-lg px-3 py-1 text-xs text-black hover:bg-gray-50"
                    @click="showGitModal = true"
                  >
                    Integrate
                  </UButton>
                </div>

                <div v-if="integration?.git_repository?.status === 'pending'" class="flex items-center justify-center text-xs text-gray-500">
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
                                <span class="text-sm text-gray-800 ">{{ res.name }}</span>
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
                          class="hidden bg-blue-500 hover:bg-blue-600 text-white text-xs font-medium py-1.5 px-3 rounded disabled:opacity-50"
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
                class="bg-blue-500 hover:bg-blue-600 text-white text-xs font-medium py-1.5 px-3 rounded disabled:opacity-50"
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
  <UModal v-model="showInstructionCreate" :ui="{ width: 'sm:max-w-2xl' }">
    <div>
      <InstructionGlobalCreateComponent @instructionSaved="() => { showInstructionCreate = false; fetchInstructions(); }" @cancel="() => { showInstructionCreate = false }" />
    </div>
  </UModal>


  </div>
</template>

<script setup lang="ts">
definePageMeta({ auth: true, layout: 'onboarding' })
import OnboardingView from '@/components/onboarding/OnboardingView.vue'
import InstructionGlobalCreateComponent from '@/components/InstructionGlobalCreateComponent.vue'
import GitRepoModalComponent from '@/components/GitRepoModalComponent.vue'
import ResourceDisplay from '~/components/ResourceDisplay.vue'

const route = useRoute()
const { updateOnboarding } = useOnboarding()
const router = useRouter()

const dsId = computed(() => String(route.params.ds_id || ''))
const saving = ref(false)
const isLoadingInstructions = ref(true)
const isLLMSyncInProgress = ref(false)
const showInstructionCreate = ref(false)
const showGitModal = ref(false)
const isLoadingMetadataResources = ref(false)
const isUpdatingResources = ref(false)
const hasAttemptedLLMSync = ref(false)
const metadataResources = ref<any>({ resources: [] })
const resourceSearch = ref('')
const enrichmentExpanded = ref(true)
const instructionsExpanded = ref(true)
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

function toggleEnrichmentSection() {
  enrichmentExpanded.value = !enrichmentExpanded.value
}

function toggleInstructionsSection() {
  instructionsExpanded.value = !instructionsExpanded.value
}

const integration = ref<any>(null)
const repoDisplayName = computed(() => {
  const url = integration.value?.git_repository?.repo_url || ''
  const tail = String(url).split('/')?.pop() || ''
  return tail.replace(/\.git$/, '') || 'Repository'
})

// Global loading gate for the instructions section
const isLoading = computed(() => isLLMSyncInProgress.value || isLoadingInstructions.value)
const loadingText = computed(() => isLLMSyncInProgress.value ? 'Thinking...' : 'Loading instructions...')

// Suggested instructions fetched from API (published, filtered to this data source)
const suggestedInstructions = ref<any[]>([])
const instructionAction = ref<Record<string, 'approved' | 'removed'>>({})

function openInstructionModal() {
  showInstructionCreate.value = true
}

async function fetchInstructions() {
  isLoadingInstructions.value = true
  try {
    // Leverage same API shape as InstructionsListModal: published instructions; filter by ds via query if backend supports it
    const params: any = { limit: 100 }
    if (dsId.value) params.data_source_id = dsId.value
    const { data, error } = await useMyFetch<any[]>('/instructions', { method: 'GET', query: params })
    if (!error.value && data.value) {
      suggestedInstructions.value = data.value
      const map: Record<string, 'approved' | 'removed'> = {}
      for (const inst of suggestedInstructions.value) {
        const gs = (inst as any).global_status
        const st = (inst as any).status
        if (gs === 'approved' && st === 'published') {
          map[inst.id] = 'approved'
        } else if (gs === 'rejected' || st === 'archived') {
          map[inst.id] = 'removed'
        }
      }
      instructionAction.value = map
    }
  } finally {
    isLoadingInstructions.value = false
  }
}

function getLLMSyncKey() {
  return `llm_sync_attempted_${dsId.value}`
}

function hasTriedLLMSyncBefore() {
  if (typeof window === 'undefined') return false
  return localStorage.getItem(getLLMSyncKey()) === 'true'
}

function markLLMSyncAttempted() {
  if (typeof window !== 'undefined') {
    localStorage.setItem(getLLMSyncKey(), 'true')
  }
  hasAttemptedLLMSync.value = true
}

function shouldRunLLMSync() {
  return suggestedInstructions.value.length === 0 && 
         !hasAttemptedLLMSync.value && 
         !hasTriedLLMSyncBefore()
}

async function runLLMSync() {
  if (!dsId.value) return
  
  isLLMSyncInProgress.value = true
  try {
    await useMyFetch(`/data_sources/${dsId.value}/llm_sync`, { method: 'POST' })
    // Mark that we've attempted LLM sync for this data source
    markLLMSyncAttempted()
    // After llm_sync completes, refresh the instructions list
    await fetchInstructions()
  } catch (error) {
    console.error('LLM sync failed:', error)
    // Even if it fails, mark as attempted to avoid retrying immediately
    markLLMSyncAttempted()
  } finally {
    isLLMSyncInProgress.value = false
  }
}

async function approveInstruction(instruction: any) {
  try {
    const payload = {
      // Approve: status published, global_status approved, keep visible
      status: 'published',
      global_status: 'approved',
      is_seen: true
    }
    const res = await useMyFetch(`/instructions/${instruction.id}`, { method: 'PUT', body: payload })
    if ((res.status as any)?.value === 'success') {
      instructionAction.value[instruction.id] = 'approved'
    }
  } catch (e) {
    console.error('Failed to approve instruction', e)
  }
}

async function rejectInstruction(instruction: any) {
  try {
    const payload = {
      // Reject: archive and mark global_status rejected
      status: 'archived',
      global_status: 'rejected',
      is_seen: true
    }
    const res = await useMyFetch(`/instructions/${instruction.id}`, { method: 'PUT', body: payload })
    if ((res.status as any)?.value === 'success') {
      instructionAction.value[instruction.id] = 'removed'
    }
  } catch (e) {
    console.error('Failed to reject instruction', e)
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
    // No longer using selection logic
    const selectedInstructionTexts: string[] = []
    
    // TODO: Save instructions and enrichment preferences
    // const payload = {
    //   instructions: selectedInstructionTexts,
    //   enrichments: []
    // }
    // await useMyFetch(`/data_sources/${dsId.value}/context`, { method: 'POST', body: payload })
    
    // Update onboarding as completed - OnboardingView will automatically redirect
    await updateResourceStatus()
    try {
      await updateOnboarding({ current_step: 'instructions_added' as any, completed: true as any, dismissed: false as any })
    } catch (e) {
      console.warn('Failed to update onboarding, continuing to home:', e)
      // Fallback: navigate manually if onboarding update fails
      await navigateTo({path: '/', query: { setup: 'done' } })
    }
  } finally {
    saving.value = false
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
  // Initialize the attempted state from localStorage
  hasAttemptedLLMSync.value = hasTriedLLMSyncBefore()

  // Kick off all fetches in parallel so loading UI appears immediately
  const integrationPromise = fetchIntegration()
  const metadataPromise = fetchMetadataResources()
  const instructionsPromise = fetchInstructions()

  await Promise.all([integrationPromise, metadataPromise, instructionsPromise])

  // Only run LLM sync if conditions are met
  if (shouldRunLLMSync()) {
    await runLLMSync()
  }
})

</script>

<style scoped>
.fade-in {
  animation: fadeIn 0.5s ease-in-out;
}

@keyframes fadeIn {
  from {
    opacity: 0;
    transform: translateY(10px);
  }
  to {
    opacity: 1;
    transform: translateY(0);
  }
}

@keyframes shimmer {
  0% { background-position: -100% 0; }
  100% { background-position: 100% 0; }
}

.thinking-shimmer {
  background: linear-gradient(90deg, #888 0%, #999 25%, #ccc 50%, #999 75%, #888 100%);
  background-size: 200% 100%;
  -webkit-background-clip: text;
  background-clip: text;
  color: transparent;
  animation: shimmer 2s linear infinite;
  font-weight: 400;
  opacity: 1;
}
</style>

