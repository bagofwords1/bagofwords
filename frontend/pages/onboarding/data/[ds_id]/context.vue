<template>
  <div class="min-h-screen bg-gray-50 flex items-center justify-center py-12 px-4">
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
            <!-- Instructions List -->
            <div class="space-y-4">
              <div v-if="isLoadingInstructions" class="text-xs text-gray-500 flex items-center gap-2">
                <Spinner class="w-4 h-4" />
                Loading instructions...
              </div>
              <div v-else class="space-y-2">
                <div 
                  v-for="instruction in paginatedInstructions" 
                  :key="instruction.id"
                  class="hover:bg-gray-50 bg-white border border-gray-200 rounded-md p-3 transition-colors relative cursor-pointer"
                  @click="openInstructionEditor(instruction)"
                >
                  <!-- Git and type icons for git-sourced instructions -->
                  <div v-if="instruction.source_type === 'git'" class="flex items-center gap-1 mb-1">
                    <UTooltip text="Git-sourced">
                      <img src="/icons/git-branch.svg" alt="Git" class="h-3 w-3 opacity-60" />
                    </UTooltip>
                    <UTooltip v-if="getResourceTypeIcon(instruction)" :text="getResourceTypeTooltip(instruction)">
                      <img :src="getResourceTypeIcon(instruction) ?? undefined" :alt="getResourceTypeTooltip(instruction)" class="h-3 opacity-60" />
                    </UTooltip>
                    <UTooltip v-else-if="getResourceType(instruction)" :text="getResourceTypeTooltip(instruction)">
                      <UIcon :name="getResourceTypeFallbackIcon(instruction)" class="w-3 h-3 text-gray-400" />
                    </UTooltip>
                  </div>
                  
                  <div class="text-[12px] text-gray-800 leading-relaxed pr-24 whitespace-normal break-words max-w-full">
                    {{ truncateText(instruction.text, 100) }}
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
                      <span class="hover:bg-gray-100 rounded cursor-pointer" @click.stop="rejectInstruction(instruction)">
                        <Icon 
                          name="heroicons:x-mark" 
                          class="w-4 h-4 text-red-500 rounded cursor-pointer" 
                        />
                      </span>
                      <span class="hover:bg-gray-100 rounded cursor-pointer" @click.stop="approveInstruction(instruction)">
                        <Icon 
                          name="heroicons:check" 
                          class="w-4 h-4 text-green-500 rounded cursor-pointer" 
                        />
                      </span>
                    </template>
                  </div>
                </div>

                <!-- Minimalistic Pagination -->
                <div v-if="totalPages > 1" class="flex items-center justify-center gap-1 mt-4">
                  <button 
                    v-for="page in totalPages" 
                    :key="page"
                    @click="currentPage = page"
                    class="w-6 h-6 text-xs rounded-full transition-colors"
                    :class="currentPage === page ? 'bg-blue-500 text-white' : 'text-gray-500 hover:bg-gray-100'"
                  >
                    {{ page }}
                  </button>
                </div>

                <div class="flex items-center justify-between mt-4">
                  <div class="flex items-center gap-2">
                    <UButton
                      color="blue"
                      variant="outline"
                      size="xs"
                      @click="openInstructionModal"
                      icon="heroicons:plus"
                    >
                      Add Instruction
                    </UButton>
                    <button 
                      v-if="allInstructions.length === 0 && hasAttemptedLLMSync"
                      class="text-xs text-gray-500 hover:text-gray-600 p-2 rounded-md"
                      :disabled="isLLMSyncInProgress"
                      @click="runLLMSync"
                    >
                      {{ isLLMSyncInProgress ? 'Generating...' : 'Generate AI Suggestions' }}
                    </button>
                  </div>
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
            </div>

            <!-- Git Integration Card -->
            <div 
              class="bg-blue-50/50 border border-blue-100 rounded-lg p-4 cursor-pointer hover:bg-blue-50 transition-colors"
              @click="showGitModal = true"
            >
              <div class="flex items-center gap-3">
                <GitBranchIcon class="w-5 h-5 text-blue-500 shrink-0" />
                <div class="flex-1">
                  <div class="flex items-center gap-2">
                    <h3 class="text-sm font-semibold text-gray-900">Integrate Git</h3>
                    <div class="flex items-center gap-1">
                      <UTooltip text="Tableau">
                        <img src="/icons/tableau.png" alt="Tableau" class="h-2.5 inline opacity-60" />
                      </UTooltip>
                      <UTooltip text="dbt">
                        <img src="/icons/dbt.png" alt="dbt" class="h-2.5 inline opacity-60" />
                      </UTooltip>
                      <UTooltip text="LookML">
                        <img src="/icons/lookml.png" alt="LookML" class="h-2.5 inline opacity-60" />
                      </UTooltip>
                      <UTooltip text="Markdown">
                        <img src="/icons/markdown.png" alt="Markdown" class="h-2.5 inline opacity-60" />
                      </UTooltip>
                    </div>
                  </div>
                  <p class="text-xs text-gray-500 mt-0.5">
                    <template v-if="integration?.git_repository">
                      <span class="flex items-center gap-1.5">
                        <UIcon name="heroicons:check-circle" class="w-3 h-3 text-green-500" />
                        Connected to {{ repoDisplayName }}
                        <span v-if="isIndexingGit" class="text-amber-500 flex items-center gap-1">
                          <UIcon name="heroicons:arrow-path" class="w-3 h-3 animate-spin" />
                          Indexing...
                        </span>
                      </span>
                    </template>
                    <template v-else>
                      Connect a Git repo to load dbt models, markdown docs, and other resources.
                    </template>
                  </p>
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
        @update:modelValue="handleGitModalClose"
        @changed="() => { fetchIntegration(); fetchGitInstructions(); }"
      />
    </div>
  <UModal v-model="showInstructionCreate" :ui="{ width: 'sm:max-w-2xl' }">
    <div>
      <InstructionGlobalCreateComponent @instructionSaved="() => { showInstructionCreate = false; fetchInstructions(); fetchGitInstructions(); }" @cancel="() => { showInstructionCreate = false }" />
    </div>
  </UModal>

  <!-- Instruction Editor Modal -->
  <InstructionModalComponent 
    v-model="showInstructionEditor"
    :instruction="selectedInstruction"
    @instructionSaved="handleInstructionEditorSaved"
  />


  </div>
</template>

<script setup lang="ts">
definePageMeta({ auth: true, layout: 'onboarding' })
import OnboardingView from '@/components/onboarding/OnboardingView.vue'
import InstructionGlobalCreateComponent from '@/components/InstructionGlobalCreateComponent.vue'
import GitRepoModalComponent from '@/components/GitRepoModalComponent.vue'
import InstructionModalComponent from '@/components/InstructionModalComponent.vue'
import GitBranchIcon from '@/components/icons/GitBranchIcon.vue'
import Spinner from '~/components/Spinner.vue'
import BuildVersionSelector from '~/components/instructions/BuildVersionSelector.vue'
import { useCan } from '~/composables/usePermissions'

const { getResourceType, getResourceTypeIcon, getResourceTypeTooltip, getResourceTypeFallbackIcon } = useInstructionHelpers()
const canViewBuilds = computed(() => useCan('view_builds'))

// Pagination
const PAGE_SIZE = 7
const currentPage = ref(1)

const route = useRoute()
const { updateOnboarding } = useOnboarding()
const router = useRouter()

const dsId = computed(() => String(route.params.ds_id || ''))
const saving = ref(false)
const isLoadingInstructions = ref(true)
const isLLMSyncInProgress = ref(false)
const showInstructionCreate = ref(false)
const showGitModal = ref(false)
const hasAttemptedLLMSync = ref(false)

// Instruction editor
const showInstructionEditor = ref(false)
const selectedInstruction = ref<any>(null)

// Git-sourced instructions
const gitInstructions = ref<any[]>([])
const isLoadingGitInstructions = ref(false)
const isIndexingGit = computed(() => integration.value?.git_repository?.status === 'pending')

// Build version selection
const selectedBuildId = ref<string | null>(null)
const availableBuilds = ref<{ value: string; label: string; buildNumber: number; status: string; createdAt: string; source: string }[]>([])
const loadingBuilds = ref(false)

// Merge suggested and git instructions into one list
const allInstructions = computed(() => {
  const suggested = suggestedInstructions.value.filter(i => i.source_type !== 'git')
  const git = gitInstructions.value.map(i => ({ ...i, source_type: 'git' }))
  return [...suggested, ...git]
})

// Pagination computed
const totalPages = computed(() => Math.ceil(allInstructions.value.length / PAGE_SIZE))
const paginatedInstructions = computed(() => {
  const start = (currentPage.value - 1) * PAGE_SIZE
  return allInstructions.value.slice(start, start + PAGE_SIZE)
})

// Text truncation
function truncateText(text: string, maxLength: number): string {
  if (!text || text.length <= maxLength) return text
  return text.slice(0, maxLength) + '...'
}

// Open instruction in editor
function openInstructionEditor(instruction: any) {
  selectedInstruction.value = instruction
  showInstructionEditor.value = true
}

function handleInstructionEditorSaved() {
  showInstructionEditor.value = false
  selectedInstruction.value = null
  fetchInstructions()
  fetchGitInstructions()
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
    // Fetch instructions including drafts (for onboarding suggestions)
    const params: any = { limit: 30, include_drafts: true, include_own: true }
    if (dsId.value) params.data_source_id = dsId.value
    const { data, error } = await useMyFetch<any>('/instructions', { method: 'GET', query: params })
    if (!error.value && data.value) {
      // Handle paginated response format: { items: [...], total: ... }
      const responseData = data.value
      const instructions = responseData.items || responseData || []
      suggestedInstructions.value = Array.isArray(instructions) ? instructions : []
      
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
  // Respect the use_llm_sync flag from the data source
  const llmEnabled = integration.value?.use_llm_sync !== false
  return llmEnabled && 
         suggestedInstructions.value.length === 0 && 
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

async function fetchGitInstructions() {
  if (!dsId.value) return
  isLoadingGitInstructions.value = true
  try {
    const params: any = { 
      limit: 30,
      data_source_id: dsId.value,
      source_types: 'git,dbt,markdown'
    }
    const { data, error } = await useMyFetch<any>('/instructions', { method: 'GET', query: params })
    if (!error.value && data.value) {
      const responseData = data.value
      const instructions = responseData.items || responseData || []
      gitInstructions.value = Array.isArray(instructions) ? instructions : []
    }
  } finally {
    isLoadingGitInstructions.value = false
  }
}

function handleGitModalClose(value: boolean) {
  if (!value) {
    fetchIntegration()
    fetchGitInstructions()
  }
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

// Handle rollback - refresh builds and instructions
async function handleRollback(newBuildId: string) {
  await fetchBuilds()
  selectedBuildId.value = null // Reset to main/latest
  await fetchInstructions()
  await fetchGitInstructions()
}

async function handleSave() {
  if (saving.value) return
  saving.value = true
  
  try {
    // Update onboarding as completed - OnboardingView will automatically redirect
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
  const instructionsPromise = fetchInstructions()
  const gitInstructionsPromise = fetchGitInstructions()
  const buildsPromise = fetchBuilds()

  await Promise.all([integrationPromise, instructionsPromise, gitInstructionsPromise, buildsPromise])

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

