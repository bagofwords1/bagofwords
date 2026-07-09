<template>
  <UModal v-model="isOpen" :ui="{ width: 'sm:max-w-2xl' }" :prevent-close="step !== 'connect'">
    <div class="p-5">
      <!-- Header -->
      <div class="flex items-center justify-between mb-1">
        <h3 class="text-lg font-semibold text-gray-900 dark:text-white">Create Data Agent</h3>
        <button class="text-gray-400 dark:text-gray-500 hover:text-gray-600 dark:hover:text-gray-300" @click="isOpen = false">
          <UIcon name="heroicons-x-mark" class="w-5 h-5" />
        </button>
      </div>
      <p class="text-sm text-gray-500 dark:text-gray-400">Set data source, select tables, and define additional context</p>

      <!-- Stepper -->
      <nav class="w-full my-5">
        <ol class="flex justify-center items-center gap-4 text-xs">
          <li v-for="(s, idx) in steps" :key="s.key" class="flex items-center gap-2">
            <span class="flex items-center gap-2">
              <span :class="circleClass(s.key)" class="w-5 h-5 rounded-full flex items-center justify-center">
                <UIcon v-if="isDone(s.key)" name="heroicons-check" class="w-3.5 h-3.5" />
                <span v-else>{{ idx + 1 }}</span>
              </span>
              <span :class="s.key === step ? 'text-gray-900 dark:text-white' : 'text-gray-500 dark:text-gray-400'">{{ s.label }}</span>
            </span>
            <span v-if="idx < steps.length - 1" class="mx-2 w-6 h-px bg-gray-200 dark:bg-gray-800"></span>
          </li>
        </ol>
      </nav>

      <!-- ── Step 1: Connection ───────────────────────────────────── -->
      <div v-if="step === 'connect'">
        <!-- Loading connections -->
        <div v-if="loadingConnections" class="flex flex-col items-center justify-center py-16">
          <Spinner class="h-4 w-4 text-gray-400 dark:text-gray-500" />
          <p class="text-sm text-gray-500 dark:text-gray-400 mt-2">Loading connections...</p>
        </div>

        <div v-else class="bg-white dark:bg-gray-900 rounded-lg border border-gray-200 dark:border-gray-800 p-4">
          <!-- Agent name -->
          <div class="mb-4">
            <label class="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
              Name <span class="text-red-500">*</span>
            </label>
            <UInput
              v-model="agentName"
              placeholder="e.g., Sales, Marketing, Finance"
              size="lg"
              :disabled="creatingFromConnection"
            />
          </div>

          <!-- Connection selector (multi-select for existing connections) -->
          <div class="mb-4">
            <label class="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
              Connections <span class="text-red-500">*</span>
            </label>
            <USelectMenu
              v-model="selectedConnections"
              :options="connections"
              placeholder="Select connections"
              size="lg"
              :disabled="creatingFromConnection"
              by="id"
              multiple
              searchable
              searchable-placeholder="Search connections..."
              option-attribute="name"
              :search-attributes="['name', 'type']"
            >
              <template #label>
                <div v-if="selectedConnections.length > 0" class="flex items-center gap-1.5 flex-wrap">
                  <template v-for="conn in selectedConnections" :key="conn.id">
                    <div class="flex items-center gap-1 bg-gray-100 dark:bg-gray-800 rounded px-1.5 py-0.5">
                      <DataSourceIcon :type="conn.type" class="h-3.5 flex-shrink-0" />
                      <span class="text-xs truncate max-w-[100px]">{{ conn.name }}</span>
                    </div>
                  </template>
                </div>
                <span v-else class="text-gray-400 dark:text-gray-500">Select connections</span>
              </template>
              <template #option="{ option }">
                <div class="flex items-center gap-2 w-full">
                  <DataSourceIcon :type="option.type" class="h-4 flex-shrink-0" />
                  <div class="flex-1 min-w-0">
                    <div class="font-medium truncate">{{ option.name }}</div>
                    <div class="text-[10px] text-gray-400 dark:text-gray-500">
                      {{ option.table_count || 0 }} tables · {{ option.agent_count || 0 }} agents
                    </div>
                  </div>
                </div>
              </template>
            </USelectMenu>
            <button
              type="button"
              class="mt-2 inline-flex items-center gap-1.5 text-xs text-blue-600 hover:text-blue-700"
              :disabled="creatingFromConnection"
              @click="showAddConnectionModal = true"
            >
              <UIcon name="heroicons-plus-circle" class="h-3.5 w-3.5" />
              <span>Create new connection</span>
            </button>
          </div>

          <!-- Existing connection flow (main form) -->
          <div v-if="selectedConnections.length > 0">
            <div class="flex items-center gap-2 mb-4">
              <UToggle v-model="useLlmSync" :disabled="creatingFromConnection" size="xs" color="blue" />
              <span class="text-xs text-gray-700 dark:text-gray-300">Use LLM to learn agent</span>
            </div>

            <div v-if="errorMessage" class="p-3 bg-red-50 text-red-700 dark:bg-red-500/10 dark:text-red-400 rounded-lg text-sm mb-4">
              {{ errorMessage }}
            </div>

            <div class="flex justify-between items-center pt-4 border-t border-gray-100 dark:border-gray-800">
              <button class="text-sm text-gray-500 dark:text-gray-400 hover:text-gray-700 dark:hover:text-gray-300" @click="isOpen = false">
                ← Cancel
              </button>
              <UButton
                color="blue"
                size="xs"
                :loading="creatingFromConnection"
                :disabled="!canSubmitExisting"
                @click="createAgentFromExistingConnection"
              >
                Save & Continue
              </UButton>
            </div>
          </div>

          <!-- No selection yet (just show cancel) -->
          <div v-else class="flex justify-start pt-4 border-t border-gray-100 dark:border-gray-800">
            <button class="text-sm text-gray-500 dark:text-gray-400 hover:text-gray-700 dark:hover:text-gray-300" @click="isOpen = false">
              ← Cancel
            </button>
          </div>
        </div>
      </div>

      <!-- ── Step 2: Select Tables ────────────────────────────────── -->
      <div v-else-if="step === 'schema'">
        <p class="text-sm text-gray-500 dark:text-gray-400 text-center mb-4">Choose 5-20 related tables for this agent. You can always add more later.</p>
        <div class="bg-white dark:bg-gray-900 rounded-lg">
          <TablesSelector
            :ds-id="dsId"
            schema="full"
            :can-update="true"
            :show-refresh="true"
            :show-save="true"
            :show-header="true"
            header-title="Select tables"
            header-subtitle="Choose 5-20 related tables. Start focused, you can always add more later."
            save-label="Save & Continue"
            :skip-refresh-on-save="true"
            @saved="step = 'context'"
          />
        </div>
      </div>

      <!-- ── Step 3: Set Context ──────────────────────────────────── -->
      <div v-else-if="step === 'context'">
        <div class="space-y-6">
          <!-- Instruction editor -->
          <div>
            <h3 class="text-base font-semibold text-gray-900 dark:text-white mb-1">Add custom AI rules and instructions</h3>
            <p class="text-sm text-gray-500 dark:text-gray-400 mb-3">Business-specific context, glossary, and useful code guidelines.</p>

            <div class="border border-gray-200 dark:border-gray-800 rounded-md px-3 py-2 focus-within:ring-2 focus-within:ring-blue-100 focus-within:border-blue-400">
              <!-- Loading overlay -->
              <div v-if="loadingDraft" class="flex items-center justify-center gap-2 py-10 text-xs text-gray-400 dark:text-gray-500">
                <Spinner class="w-4 h-4" />
                <span>Generating overview instruction…</span>
              </div>

              <InstructionEditor
                v-else
                v-model="instructionText"
                mode="wysiwyg"
                :editable="true"
                :data-source-ids="dsId ? [dsId] : []"
                placeholder="Describe business rules, metric definitions, or query guidelines… (type @ to mention a table or instruction)"
              />
            </div>
          </div>

          <!-- Git integration — only shown when no repo is connected -->
          <div v-if="!integration?.git_repository" class="flex items-center gap-1.5 text-xs text-gray-400 dark:text-gray-500">
            <GitBranchIcon class="w-3.5 h-3.5" />
            <span>Connect a git repository for Tableau, dbt, and markdown context —</span>
            <button class="text-blue-500 hover:text-blue-600 underline-offset-2 hover:underline" @click="showGitModal = true">integrate</button>
          </div>

          <div class="flex justify-end pt-4">
            <button @click="handleSave" :disabled="saving || loadingDraft" class="bg-blue-500 hover:bg-blue-600 text-white text-xs font-medium py-1.5 px-3 rounded disabled:opacity-50">
              <span v-if="saving">Saving...</span>
              <span v-else>Finish</span>
            </button>
          </div>

          <GitRepoModalComponent v-model="showGitModal" :datasource-id="String(dsId)" :git-repository="integration?.git_repository" :metadata-resources="{ resources: [] }" @update:modelValue="handleGitModalClose" />
        </div>
      </div>
    </div>

    <!-- Add Connection Modal (nested — for creating a brand new connection) -->
    <AddConnectionModal v-model="showAddConnectionModal" :skipSuccessStep="true" @created="handleNewConnectionCreated" />
  </UModal>
</template>

<script setup lang="ts">
import Spinner from '~/components/Spinner.vue'
import TablesSelector from '@/components/datasources/TablesSelector.vue'
import AddConnectionModal from '~/components/AddConnectionModal.vue'
import GitRepoModalComponent from '@/components/GitRepoModalComponent.vue'
import DataSourceIcon from '~/components/DataSourceIcon.vue'
import GitBranchIcon from '~/components/icons/GitBranchIcon.vue'
import InstructionEditor from '~/components/instructions/InstructionEditor.vue'

const props = defineProps<{ modelValue: boolean }>()
const emit = defineEmits<{
  (e: 'update:modelValue', value: boolean): void
  (e: 'finished', dsId: string): void
}>()

const toast = useToast()
const { getErrorMessage } = useErrorMessage()

const isOpen = computed({
  get: () => props.modelValue,
  set: (v) => emit('update:modelValue', v),
})

// ── Wizard step state ───────────────────────────────────────────────────────
const steps = [
  { key: 'connect', label: 'Connection' },
  { key: 'schema', label: 'Select Tables' },
  { key: 'context', label: 'Set Context' },
] as const
const step = ref<'connect' | 'schema' | 'context'>('connect')
const order = ['connect', 'schema', 'context']
function isDone(key: string) {
  return order.indexOf(key) < order.indexOf(step.value)
}
function circleClass(key: string) {
  if (isDone(key)) return 'bg-green-100 text-green-600 dark:bg-green-500/10 dark:text-green-400'
  if (key === step.value) return 'bg-gray-900 text-white'
  return 'bg-gray-100 text-gray-500 dark:bg-gray-800 dark:text-gray-400'
}

// The created agent / data source id (carries across steps)
const dsId = ref('')

// Reset everything when the modal opens.
watch(isOpen, (val) => {
  if (val) {
    step.value = 'connect'
    dsId.value = ''
    agentName.value = ''
    selectedConnections.value = []
    useLlmSync.value = true
    creatingFromConnection.value = false
    errorMessage.value = ''
    instructionText.value = ''
    draftInstructionId.value = null
    integration.value = null
    loadConnections()
  }
})

// ── Step 1: Connection ──────────────────────────────────────────────────────
interface Connection {
  id: string
  name: string
  type: string
  table_count?: number
  agent_count?: number
}

const connections = ref<Connection[]>([])
const loadingConnections = ref(true)
const selectedConnections = ref<Connection[]>([])
const agentName = ref('')
const useLlmSync = ref(true)
const creatingFromConnection = ref(false)
const errorMessage = ref('')
const showAddConnectionModal = ref(false)

const canSubmitExisting = computed(() =>
  selectedConnections.value.length > 0 &&
  agentName.value.trim().length > 0 &&
  !creatingFromConnection.value
)

async function loadConnections() {
  loadingConnections.value = true
  try {
    const response = await useMyFetch('/connections', { method: 'GET' })
    connections.value = (response.data.value || []) as Connection[]
    // Single connection — auto-select it (matches /agents/new behaviour).
    if (connections.value.length === 1 && selectedConnections.value.length === 0) {
      selectedConnections.value = [connections.value[0]]
    }
  } catch (err) {
    console.error('Failed to load connections:', err)
  } finally {
    loadingConnections.value = false
  }
}

async function handleNewConnectionCreated(connectionData: any) {
  await loadConnections()
  if (connectionData?.id) {
    const newConn = connections.value.find(c => c.id === connectionData.id)
    if (newConn && !selectedConnections.value.some(c => c.id === newConn.id)) {
      selectedConnections.value = [...selectedConnections.value, newConn]
    }
  }
}

async function createAgentFromExistingConnection() {
  if (selectedConnections.value.length === 0 || !agentName.value.trim()) return
  creatingFromConnection.value = true
  errorMessage.value = ''
  try {
    const payload: Record<string, any> = {
      name: agentName.value.trim(),
      connection_ids: selectedConnections.value.map(c => c.id),
      use_llm_sync: useLlmSync.value,
      is_public: false,
      generate_summary: false,
      generate_conversation_starters: false,
      generate_ai_rules: false,
    }
    const response = await useMyFetch('/data_sources', { method: 'POST', body: payload })
    if (response.error.value) {
      const errData = (response.error.value as any).data as any
      errorMessage.value = errData?.detail || 'Failed to create agent'
      return
    }
    const result = response.data.value as any
    if (result?.id) {
      dsId.value = result.id
      step.value = 'schema'
    } else {
      isOpen.value = false
    }
  } catch (err: any) {
    errorMessage.value = err?.message || 'An error occurred'
  } finally {
    creatingFromConnection.value = false
  }
}

// ── Step 3: Set Context ─────────────────────────────────────────────────────
const saving = ref(false)
const loadingDraft = ref(false)
const showGitModal = ref(false)
const integration = ref<any>(null)
const draftInstructionId = ref<string | null>(null)
const instructionText = ref('')

// Kick off the context step's data loads when we arrive on it.
watch(step, (s) => {
  if (s === 'context') {
    fetchIntegration()
    loadDraftInstruction()
  }
})

async function fetchIntegration() {
  if (!dsId.value) return
  const response = await useMyFetch(`/data_sources/${dsId.value}`, { method: 'GET' })
  if ((response.status as any)?.value === 'success') integration.value = (response.data as any)?.value
}

function handleGitModalClose(value: boolean) { if (!value) fetchIntegration() }

async function loadDraftInstruction() {
  if (!dsId.value) return
  loadingDraft.value = true
  try {
    const { data: syncData } = await useMyFetch<any>(`/data_sources/${dsId.value}/llm_sync`, { method: 'POST' })
    const instructionId = syncData.value?.onboarding_instruction?.id
    if (instructionId) {
      const { data, error } = await useMyFetch<any>(`/instructions/${instructionId}`, { method: 'GET' })
      if (!error.value && data.value) {
        instructionText.value = data.value.text || ''
        draftInstructionId.value = instructionId
      }
    }
  } catch {} finally {
    loadingDraft.value = false
  }
}

// ── Save (final step) ───────────────────────────────────────────────────────
async function handleSave() {
  if (saving.value) return
  saving.value = true
  try {
    const text = instructionText.value.trim()
    let primaryInstructionId: string | null = null

    if (draftInstructionId.value) {
      if (text) {
        await useMyFetchStrict(`/instructions/${draftInstructionId.value}`, {
          method: 'PUT',
          body: { text, status: 'published' },
        })
        primaryInstructionId = draftInstructionId.value
      } else {
        await useMyFetchStrict(`/instructions/${draftInstructionId.value}`, { method: 'DELETE' })
      }
    } else if (text) {
      const { data } = await useMyFetchStrict('/instructions/global', {
        method: 'POST',
        body: {
          text,
          status: 'published',
          category: 'general',
          is_seen: true,
          can_user_toggle: true,
          load_mode: 'always',
          data_source_ids: [dsId.value],
        },
      })
      primaryInstructionId = (data as any)?.value?.id || null
    }

    if (primaryInstructionId) {
      await useMyFetchStrict(`/data_sources/${dsId.value}`, {
        method: 'PUT',
        body: { primary_instruction_id: primaryInstructionId },
      })
    }

    const created = dsId.value
    isOpen.value = false
    emit('finished', created)
  } catch (e: any) {
    toast.add({ title: 'Failed to save agent context', description: getErrorMessage(e), color: 'red' })
  } finally {
    saving.value = false
  }
}
</script>

<style scoped>
.instruction-wysiwyg {
  min-height: 280px;
}
</style>
