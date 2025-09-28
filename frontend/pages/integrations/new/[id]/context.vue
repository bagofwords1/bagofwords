<template>

  <div class="min-h-screen py-10 px-4 md:w-1/2 mx-auto text-sm">
      <div class="w-full px-4 pl-0 py-4">
      <div>
        <h1 class="text-lg font-semibold text-center">Integrations</h1>
        <p class="mt-4 text-gray-500 text-center">Connect and manage your data sources</p>
      </div>
        <WizardSteps class="mb-5 mt-4" current="context" :ds-id="dsId" />

      <!-- Loading State -->
      <div v-if="isLLMSyncInProgress" class="flex items-center justify-center min-h-[200px] space-x-2">
        <Spinner class="w-4 h-4" />
        <span class="thinking-shimmer text-sm">Thinking...</span>
      </div>

      <div v-else class="space-y-6">
        <!-- Suggested Instructions -->
        <div class="bg-white">
          <div @click="toggleInstructionsSection" class="flex items-center justify-between cursor-pointer hover:bg-gray-50">
            <div class="flex items-center border-b border-gray-200 pb-3">
              <h3 class="text-lg mt-4 font-semibold text-gray-900">Add custom AI rules and instructions</h3>
            </div>
          </div>
          <div v-if="instructionsExpanded" class="">
            <div class="text-left mb-4">
              <p class="text-sm mt-4 text-gray-500">Custom instructions are great for business-specific context, glossary and useful code guidelines/snippets.</p>
            </div>
            <div class="space-y-3">
              <div v-if="isLoadingInstructions" class="text-xs text-gray-500 flex items-center gap-2">
                <Spinner class="w-4 h-4" />
                Loading instructions...
              </div>
              <div v-else>
                <div v-for="instruction in suggestedInstructions" :key="instruction.id" class="hover:bg-gray-50 bg-white mt-2 border border-gray-200 rounded-md p-3 relative">
                  <div class="text-[12px] text-gray-800 leading-relaxed pr-24 break-words">
                    {{ instruction.text }}
                  </div>
                  <div class="absolute top-2 right-2 flex items-center gap-2">
                    <template v-if="instructionAction[instruction.id]">
                      <span class="px-2 py-0.5 text-[11px] rounded-full border" :class="instructionAction[instruction.id] === 'approved' ? 'bg-green-50 text-green-700 border-green-100' : 'bg-red-50 text-red-700 border-red-100'">
                        {{ instructionAction[instruction.id] === 'approved' ? 'Approved' : 'Removed' }}
                      </span>
                    </template>
                    <template v-else>
                      <span class="hover:bg-gray-100 rounded cursor-pointer" @click="rejectInstruction(instruction)">
                        <Icon name="heroicons:x-mark" class="w-4 h-4 text-red-500" />
                      </span>
                      <span class="hover:bg-gray-100 rounded cursor-pointer" @click="approveInstruction(instruction)">
                        <Icon name="heroicons:check" class="w-4 h-4 text-green-500" />
                      </span>
                    </template>
                  </div>
                </div>
                <div class="flex items-center gap-2">
                  <button class="text-xs text-blue-500 hover:text-blue-600 p-2 rounded-md" @click="openInstructionModal">Add Custom Instruction</button>
                  <button v-if="suggestedInstructions.length === 0 && hasAttemptedLLMSync" class="text-xs text-gray-500 hover:text-gray-600 p-2 rounded-md" :disabled="isLLMSyncInProgress" @click="runLLMSync">
                    {{ isLLMSyncInProgress ? 'Generating...' : 'Generate AI Suggestions' }}
                  </button>
                </div>
              </div>
            </div>
          </div>
        </div>

        <!-- Enrichment -->
        <div class="bg-white">
          <div @click="toggleEnrichmentSection" class="flex items-center justify-between cursor-pointer hover:bg-gray-50">
            <div class="flex items-center border-b border-gray-200 pb-3">
              <h3 class="text-lg mt-4 font-semibold text-gray-900">Connect Tableau, dbt, and your AGENTS.md files</h3>
            </div>

          </div>
          <div v-if="enrichmentExpanded" class="">
            <div class="text-left mb-4 mt-5">
              <p class="text-sm text-gray-500 leading-relaxed">

                Connect additional context from Tableau, dbt, LookML, code, and markdown files to your data sources. It will be used by AI agents to throught out data analysis. 
                <br />
                Integration is via git repository.
              </p>
            <div class="flex mt-4 mb-4">
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
            <ResourcesSelector :ds-id="String(dsId)" @saved="() => {}" @error="(e:any) => console.error(e)" />
          </div>
        </div>

        <div class="flex justify-end pt-4">
          <button @click="handleSave" :disabled="saving" class="bg-blue-500 hover:bg-blue-600 text-white text-xs font-medium py-1.5 px-3 rounded disabled:opacity-50">
            <span v-if="saving">Saving...</span>
            <span v-else>Save & Continue</span>
          </button>
        </div>

        <UModal v-model="showInstructionCreate" :ui="{ width: 'sm:max-w-2xl' }">
          <div>
            <InstructionGlobalCreateComponent @instructionSaved="() => { showInstructionCreate = false; fetchInstructions(); }" @cancel="() => { showInstructionCreate = false }" />
          </div>
        </UModal>

        <GitRepoModalComponent v-model="showGitModal" :datasource-id="String(dsId)" :git-repository="integration?.git_repository" :metadata-resources="metadataResources" @update:modelValue="handleGitModalClose" />
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
definePageMeta({ auth: true })
import Spinner from '@/components/Spinner.vue'
import InstructionGlobalCreateComponent from '@/components/InstructionGlobalCreateComponent.vue'
import GitRepoModalComponent from '@/components/GitRepoModalComponent.vue'
import ResourcesSelector from '~/components/datasources/ResourcesSelector.vue'
import WizardSteps from '@/components/datasources/WizardSteps.vue'

const route = useRoute()
const router = useRouter()
const dsId = computed(() => String(route.params.id || ''))

const saving = ref(false)
const isLoadingInstructions = ref(false)
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

const integration = ref<any>(null)

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

function toggleEnrichmentSection() { enrichmentExpanded.value = !enrichmentExpanded.value }
function toggleInstructionsSection() { instructionsExpanded.value = !instructionsExpanded.value }

const repoDisplayName = computed(() => {
  const url = integration.value?.git_repository?.repo_url || ''
  const tail = String(url).split('/')?.pop() || ''
  return tail.replace(/\.git$/, '') || 'Repository'
})

const suggestedInstructions = ref<any[]>([])
const instructionAction = ref<Record<string, 'approved' | 'removed'>>({})

function openInstructionModal() { showInstructionCreate.value = true }

async function fetchInstructions() {
  isLoadingInstructions.value = true
  try {
    const params: any = { limit: 100 }
    if (dsId.value) params.data_source_id = dsId.value
    const { data, error } = await useMyFetch<any[]>('/instructions', { method: 'GET', query: params })
    if (!error.value && data.value) {
      suggestedInstructions.value = data.value
      const map: Record<string, 'approved' | 'removed'> = {}
      for (const inst of suggestedInstructions.value) {
        const gs = (inst as any).global_status
        const st = (inst as any).status
        if (gs === 'approved' && st === 'published') map[inst.id] = 'approved'
        else if (gs === 'rejected' || st === 'archived') map[inst.id] = 'removed'
      }
      instructionAction.value = map
    }
  } finally {
    isLoadingInstructions.value = false
  }
}

function getLLMSyncKey() { return `llm_sync_attempted_${dsId.value}` }
function hasTriedLLMSyncBefore() { if (typeof window === 'undefined') return false; return localStorage.getItem(getLLMSyncKey()) === 'true' }
function markLLMSyncAttempted() { if (typeof window !== 'undefined') localStorage.setItem(getLLMSyncKey(), 'true'); hasAttemptedLLMSync.value = true }
function shouldRunLLMSync() { return suggestedInstructions.value.length === 0 && !hasAttemptedLLMSync.value && !hasTriedLLMSyncBefore() }

async function runLLMSync() {
  if (!dsId.value) return
  isLLMSyncInProgress.value = true
  try {
    await useMyFetch(`/data_sources/${dsId.value}/llm_sync`, { method: 'POST' })
    markLLMSyncAttempted()
    await fetchInstructions()
  } catch (e) {
    markLLMSyncAttempted()
  } finally {
    isLLMSyncInProgress.value = false
  }
}

async function approveInstruction(instruction: any) {
  try {
    const payload = { status: 'published', global_status: 'approved', is_seen: true }
    const res = await useMyFetch(`/instructions/${instruction.id}`, { method: 'PUT', body: payload })
    if ((res.status as any)?.value === 'success') instructionAction.value[instruction.id] = 'approved'
  } catch (e) {}
}

async function rejectInstruction(instruction: any) {
  try {
    const payload = { status: 'archived', global_status: 'rejected', is_seen: true }
    const res = await useMyFetch(`/instructions/${instruction.id}`, { method: 'PUT', body: payload })
    if ((res.status as any)?.value === 'success') instructionAction.value[instruction.id] = 'removed'
  } catch (e) {}
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

function handleGitModalClose(value: boolean) { if (!value) { fetchMetadataResources(); fetchIntegration() } }

async function handleSave() {
  if (saving.value) return
  saving.value = true
  try {
    // Persist any necessary context here if applicable
    router.replace(`/integrations/${dsId.value}`)
  } finally {
    saving.value = false
  }
}

async function fetchIntegration() {
  if (!dsId.value) return
  const response = await useMyFetch(`/data_sources/${dsId.value}`, { method: 'GET' })
  if ((response.status as any)?.value === 'success') integration.value = (response.data as any)?.value
}

onMounted(async () => {
  await fetchIntegration()
  await fetchMetadataResources()
  await fetchInstructions()
  if (shouldRunLLMSync()) await runLLMSync()
})
</script>

<style scoped>
.thinking-shimmer {
  background: linear-gradient(90deg, #888 0%, #999 25%, #ccc 50%, #999 75%, #888 100%);
  background-size: 200% 100%;
  -webkit-background-clip: text;
  background-clip: text;
  color: transparent;
  animation: shimmer 2s linear infinite;
}
@keyframes shimmer { 0% { background-position: -100% 0; } 100% { background-position: 100% 0; } }
</style>


