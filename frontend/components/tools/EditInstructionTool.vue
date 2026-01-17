<template>
  <div class="mt-1">
    <!-- Status header -->
    <Transition name="fade" appear>
      <div
        class="flex items-center text-xs text-gray-500 cursor-pointer hover:text-gray-700"
        @click="toggleExpanded"
      >
        <span v-if="status === 'running'" class="tool-shimmer flex items-center">
          <Icon name="heroicons-cube" class="w-3 h-3 mr-1.5 text-gray-400" />
          Editing instruction...
        </span>
        <span v-else-if="isSuccess" class="text-gray-600 flex items-center">
          <Icon name="heroicons-cube" class="w-3 h-3 mr-1.5 text-blue-500" />
          <span class="truncate max-w-[300px]">Edited: {{ truncatedText }}</span>
          <span v-if="versionNumber" class="ml-1.5 px-1.5 py-0.5 bg-blue-100 text-blue-700 rounded text-[10px] shrink-0">v{{ versionNumber }}</span>
          <Icon
            :name="isExpanded ? 'heroicons-chevron-down' : 'heroicons-chevron-right'"
            class="w-3 h-3 ml-1 text-gray-400 shrink-0"
          />
        </span>
        <span v-else-if="isRejected" class="text-gray-600 flex items-center">
          <Icon name="heroicons-x-circle" class="w-3 h-3 mr-1.5 text-orange-500" />
          <span>Edit rejected</span>
          <span v-if="rejectedReason" class="ml-1.5 text-orange-600 text-[10px]">({{ rejectedReason }})</span>
          <Icon
            :name="isExpanded ? 'heroicons-chevron-down' : 'heroicons-chevron-right'"
            class="w-3 h-3 ml-1 text-gray-400"
          />
        </span>
        <span v-else class="text-gray-600 flex items-center">
          <Icon name="heroicons-x-circle" class="w-3 h-3 mr-1.5 text-red-500" />
          <span>Failed to edit instruction</span>
          <Icon
            :name="isExpanded ? 'heroicons-chevron-down' : 'heroicons-chevron-right'"
            class="w-3 h-3 ml-1 text-gray-400"
          />
        </span>
      </div>
    </Transition>

    <!-- Expandable content -->
    <Transition name="slide">
      <div v-if="isExpanded && status !== 'running'" class="mt-2 space-y-2">
        <!-- Loading state while fetching versions -->
        <div v-if="isLoadingVersions" class="flex items-center justify-center py-4">
          <Spinner class="w-4 h-4 mr-2" />
          <span class="text-[11px] text-gray-500">Loading diff...</span>
        </div>

        <!-- Diff view when text was changed -->
        <div v-else-if="hasTextDiff && previousText !== null" class="border border-gray-150 rounded-md overflow-hidden">
          <div class="px-3 py-1.5 bg-gray-50 border-b border-gray-150 flex items-center justify-between">
            <span class="text-[10px] text-gray-600 font-medium">Text changes</span>
            <span v-if="versionNumber" class="text-[10px] text-gray-500">v{{ versionNumber - 1 }} → v{{ versionNumber }}</span>
          </div>
          <ClientOnly>
            <MonacoDiffEditor
              :original="previousText"
              :modified="currentText"
              height="180px"
              language="plaintext"
            />
          </ClientOnly>
        </div>

        <!-- Instruction card for non-text changes or when no diff -->
        <div v-else class="hover:bg-gray-50 border border-gray-150 rounded-md p-3 transition-colors">
          <!-- Instruction text - click to edit -->
          <div
            v-if="displayText"
            class="instruction-content text-[12px] text-gray-800 leading-relaxed mb-2 cursor-pointer"
            @click="currentGlobalStatus !== 'approved' ? handleEdit() : null"
          >
            <MDC :value="displayText" class="markdown-content" />
          </div>
        </div>

        <!-- Metadata changes summary -->
        <div v-if="metadataChanges.length > 0" class="text-[10px] text-gray-500 px-1">
          <span class="font-medium">Other changes:</span>
          {{ metadataChanges.join(', ') }}
        </div>

        <!-- Metadata row -->
        <div class="flex flex-wrap items-center gap-2 text-[10px] px-1">
          <!-- Category -->
          <span v-if="displayCategory" class="px-1.5 py-0.5 bg-gray-100 text-gray-600 rounded text-[10px]">
            {{ displayCategory }}
          </span>

          <!-- Confidence -->
          <div v-if="displayConfidence" class="flex items-center gap-1">
            <span class="text-gray-500">Confidence:</span>
            <span
              class="font-medium"
              :class="displayConfidence >= 0.9 ? 'text-green-600' : displayConfidence >= 0.7 ? 'text-yellow-600' : 'text-red-600'"
            >
              {{ Math.round(displayConfidence * 100) }}%
            </span>
          </div>

          <!-- Load mode -->
          <div v-if="displayLoadMode" class="flex items-center gap-1">
            <span class="text-gray-500">Load:</span>
            <span class="px-1.5 py-0.5 rounded text-[9px] font-medium"
              :class="displayLoadMode === 'always' ? 'bg-blue-100 text-blue-700' : 'bg-gray-100 text-gray-600'"
            >
              {{ displayLoadMode }}
            </span>
          </div>

          <!-- Tables scoped -->
          <div v-if="displayTableCount > 0" class="flex items-center gap-1">
            <Icon name="heroicons-table-cells" class="w-3 h-3 text-gray-400" />
            <span class="text-gray-600">{{ displayTableCount }} table{{ displayTableCount > 1 ? 's' : '' }}</span>
          </div>
        </div>

        <!-- Action buttons / Status display -->
        <div v-if="isSuccess && instructionId" class="flex justify-start gap-2 pt-2 border-t border-gray-100 px-1">
          <!-- Show status for published instructions -->
          <div v-if="currentGlobalStatus === 'approved'" class="flex items-center">
            <Icon name="heroicons:check-circle" class="w-3 h-3 text-gray-500 mr-1" />
            <span class="text-[10px] font-medium text-gray-500">Published</span>
          </div>

          <!-- Show action buttons for draft/suggested instructions -->
          <template v-else>
            <button
              v-if="canCreateInstructions"
              @click.stop="handlePublish"
              class="flex items-center px-2 py-1 text-[10px] font-medium text-gray-700 bg-gray-50 hover:bg-gray-100 rounded transition-colors"
              :disabled="isPublishing"
            >
              <Spinner
                v-if="isPublishing"
                class="w-3 h-3 text-green-600 mr-1"
              />
              <Icon
                v-else
                name="heroicons:check"
                class="w-3 h-3 text-green-600 mr-1"
              />
              <span>{{ isPublishing ? 'Publishing...' : 'Publish' }}</span>
            </button>
            <button
              @click.stop="handleEdit"
              class="flex items-center px-2 py-1 text-[10px] font-medium text-gray-700 bg-gray-50 hover:bg-gray-100 rounded transition-colors"
            >
              <Icon name="heroicons:pencil" class="w-3 h-3 text-blue-600 mr-1" />
              <span>Edit</span>
            </button>
            <button
              @click.stop="handleDelete"
              class="flex items-center px-2 py-1 text-[10px] font-medium text-gray-700 bg-gray-50 hover:bg-gray-100 rounded transition-colors"
              :disabled="isDeleting"
            >
              <Spinner
                v-if="isDeleting"
                class="w-3 h-3 text-red-600 mr-1"
              />
              <Icon
                v-else
                name="heroicons:trash"
                class="w-3 h-3 text-red-600 mr-1"
              />
              <span>{{ isDeleting ? 'Deleting...' : 'Delete' }}</span>
            </button>
          </template>
        </div>

        <!-- Error message -->
        <div v-if="errorMessage" class="text-[10px] text-red-500 bg-red-50/50 rounded px-2 py-1">
          {{ errorMessage }}
        </div>
      </div>
    </Transition>

    <!-- Instruction Modal -->
    <InstructionModalComponent
      v-model="showInstructionModal"
      :instruction="editingInstruction"
      :initial-type="'global'"
      @instruction-saved="handleInstructionSaved"
    />
  </div>
</template>

<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import InstructionModalComponent from '~/components/InstructionModalComponent.vue'
import Spinner from '~/components/Spinner.vue'
import MonacoDiffEditor from '~/components/MonacoDiffEditor.vue'

interface ToolExecution {
  id: string
  tool_name: string
  tool_action?: string
  status: string
  result_summary?: string
  result_json?: any
  arguments_json?: any
  duration_ms?: number
}

interface Props {
  toolExecution: ToolExecution
}

const props = defineProps<Props>()
const emit = defineEmits<{
  (e: 'instruction-updated'): void
}>()

const toast = useToast()
const isExpanded = ref(false)
const showInstructionModal = ref(false)
const editingInstruction = ref<any>(null)
const isPublishing = ref(false)
const isDeleting = ref(false)
const localGlobalStatus = ref<string | null>(null)
const isLoadingVersions = ref(false)
const fetchedInstruction = ref<any>(null)
const previousText = ref<string | null>(null)

const canCreateInstructions = computed(() => {
  return useCan('create_instructions')
})

const status = computed<string>(() => props.toolExecution?.status || '')

const isSuccess = computed(() => {
  const rj = props.toolExecution?.result_json || {}
  return status.value === 'success' && rj.success === true
})

const isRejected = computed(() => {
  const rj = props.toolExecution?.result_json || {}
  return status.value === 'success' && rj.success === false && rj.rejected_reason
})

// Extract from arguments_json (input) - these are the updates applied
const updatedText = computed(() => {
  const args = props.toolExecution?.arguments_json || {}
  return args.text || null
})

const updatedCategory = computed(() => {
  const args = props.toolExecution?.arguments_json || {}
  return args.category || null
})

const updatedConfidence = computed(() => {
  const args = props.toolExecution?.arguments_json || {}
  return args.confidence ?? null
})

const updatedLoadMode = computed(() => {
  const args = props.toolExecution?.arguments_json || {}
  return args.load_mode || null
})

const updatedTableNames = computed(() => {
  const args = props.toolExecution?.arguments_json || {}
  return args.table_names || null
})

const updatedEvidence = computed(() => {
  const args = props.toolExecution?.arguments_json || {}
  return args.evidence || null
})

// Extract from result_json (output)
const instructionId = computed(() => {
  const rj = props.toolExecution?.result_json || {}
  return rj.instruction_id || null
})

const versionNumber = computed(() => {
  const rj = props.toolExecution?.result_json || {}
  return rj.version_number || null
})

const rejectedReason = computed(() => {
  const rj = props.toolExecution?.result_json || {}
  return rj.rejected_reason || ''
})

const currentGlobalStatus = computed(() => {
  if (localGlobalStatus.value !== null) return localGlobalStatus.value
  return fetchedInstruction.value?.global_status || null
})

// Check if text was changed
const hasTextDiff = computed(() => {
  return updatedText.value !== null && versionNumber.value !== null
})

// Current text (after edit)
const currentText = computed(() => {
  return updatedText.value || fetchedInstruction.value?.text || ''
})

// Display values - prefer fetched instruction, fall back to args
const displayText = computed(() => {
  return fetchedInstruction.value?.text || updatedText.value || ''
})

const displayCategory = computed(() => {
  return fetchedInstruction.value?.category || updatedCategory.value || ''
})

const displayConfidence = computed(() => {
  return updatedConfidence.value ?? fetchedInstruction.value?.confidence ?? null
})

const displayLoadMode = computed(() => {
  return fetchedInstruction.value?.load_mode || updatedLoadMode.value || 'intelligent'
})

const displayTableCount = computed(() => {
  if (fetchedInstruction.value?.references) {
    return fetchedInstruction.value.references.length
  }
  if (updatedTableNames.value) {
    return Array.isArray(updatedTableNames.value) ? updatedTableNames.value.length : 0
  }
  return 0
})

// Metadata changes (non-text changes)
const metadataChanges = computed(() => {
  const changes: string[] = []
  if (updatedCategory.value) changes.push(`category → ${updatedCategory.value}`)
  if (updatedConfidence.value !== null) changes.push(`confidence → ${Math.round(updatedConfidence.value * 100)}%`)
  if (updatedLoadMode.value) changes.push(`load mode → ${updatedLoadMode.value}`)
  if (updatedTableNames.value) changes.push(`tables updated`)
  if (updatedEvidence.value) changes.push(`evidence updated`)
  return changes
})

const truncatedText = computed(() => {
  const text = displayText.value
  if (!text) return 'Edited instruction'
  const firstLine = text.split('\n')[0].replace(/^#+\s*/, '').trim()
  if (firstLine.length > 60) {
    return firstLine.substring(0, 57) + '...'
  }
  return firstLine || 'Edited instruction'
})

const errorMessage = computed(() => {
  if (status.value === 'error') {
    const rj = props.toolExecution?.result_json || {}
    return rj.error || rj.message || 'An error occurred'
  }
  if (isRejected.value) {
    const rj = props.toolExecution?.result_json || {}
    return rj.message || ''
  }
  return ''
})

// Fetch instruction and previous version when expanded
watch(isExpanded, async (expanded) => {
  if (expanded && instructionId.value && previousText.value === null) {
    await fetchVersionsForDiff()
  }
})

async function fetchVersionsForDiff() {
  if (!instructionId.value) return

  isLoadingVersions.value = true
  try {
    // Fetch current instruction
    const { data: instructionData, error: instructionError } = await useMyFetch(`/instructions/${instructionId.value}`)
    if (!instructionError.value && instructionData.value) {
      fetchedInstruction.value = instructionData.value
    }

    // If text was changed and we have a version number, fetch previous version
    if (hasTextDiff.value && versionNumber.value && versionNumber.value > 1) {
      const { data: versionsData, error: versionsError } = await useMyFetch(
        `/instructions/${instructionId.value}/versions?limit=50`
      )
      if (!versionsError.value && versionsData.value) {
        const versions = (versionsData.value as any).items || []
        // Find the previous version (version_number - 1)
        const prevVersionNumber = versionNumber.value - 1
        const prevVersionMeta = versions.find((v: any) => v.version_number === prevVersionNumber)

        if (prevVersionMeta) {
          // Fetch full previous version to get text
          const { data: prevVersionData, error: prevVersionError } = await useMyFetch(
            `/instructions/${instructionId.value}/versions/${prevVersionMeta.id}`
          )
          if (!prevVersionError.value && prevVersionData.value) {
            previousText.value = (prevVersionData.value as any).text || ''
          }
        }
      }
    }

    // If no previous version found, set to empty to indicate we tried
    if (previousText.value === null) {
      previousText.value = ''
    }
  } catch (e) {
    console.error('Failed to fetch versions:', e)
    previousText.value = ''
  } finally {
    isLoadingVersions.value = false
  }
}

function toggleExpanded() {
  if (status.value !== 'running') {
    isExpanded.value = !isExpanded.value
  }
}

async function handleEdit() {
  if (!instructionId.value) return

  if (fetchedInstruction.value) {
    editingInstruction.value = fetchedInstruction.value
  } else {
    editingInstruction.value = {
      id: instructionId.value,
      text: displayText.value,
      category: displayCategory.value || 'general',
      load_mode: displayLoadMode.value
    }
  }
  showInstructionModal.value = true
}

async function handlePublish() {
  if (!instructionId.value) return

  isPublishing.value = true
  try {
    const { error } = await useMyFetch(`/instructions/${instructionId.value}`, {
      method: 'PUT',
      body: {
        status: 'published',
        global_status: 'approved'
      }
    })
    if (!error.value) {
      localGlobalStatus.value = 'approved'
      toast.add({ title: 'Success', description: 'Instruction published', color: 'green' })
      emit('instruction-updated')
    } else {
      throw new Error('Failed to publish')
    }
  } catch (e) {
    console.error('Failed to publish instruction:', e)
    toast.add({ title: 'Error', description: 'Failed to publish instruction', color: 'red' })
  } finally {
    isPublishing.value = false
  }
}

async function handleDelete() {
  if (!instructionId.value) return
  if (!confirm('Delete this instruction?')) return

  isDeleting.value = true
  try {
    const { error } = await useMyFetch(`/instructions/${instructionId.value}`, { method: 'DELETE' })
    if (!error.value) {
      localGlobalStatus.value = 'deleted'
      toast.add({ title: 'Deleted', description: 'Instruction deleted', color: 'orange' })
      emit('instruction-updated')
    } else {
      throw new Error('Failed to delete')
    }
  } catch (e) {
    console.error('Failed to delete instruction:', e)
    toast.add({ title: 'Error', description: 'Failed to delete instruction', color: 'red' })
  } finally {
    isDeleting.value = false
  }
}

function handleInstructionSaved(data: any) {
  const updated = data?.data || data
  if (updated) {
    fetchedInstruction.value = updated
    if (updated.global_status) {
      localGlobalStatus.value = updated.global_status
    }
  }
  showInstructionModal.value = false
  toast.add({ title: 'Success', description: 'Instruction saved', color: 'green' })
  emit('instruction-updated')
}
</script>

<style scoped>
.tool-shimmer {
  animation: shimmer 1.6s linear infinite;
  background: linear-gradient(90deg, rgba(0,0,0,0) 0%, rgba(160,160,160,0.15) 50%, rgba(0,0,0,0) 100%);
  background-size: 300% 100%;
  background-clip: text;
}

@keyframes shimmer {
  0% { background-position: 0% 0; }
  100% { background-position: 100% 0; }
}

.fade-enter-active, .fade-leave-active {
  transition: opacity 0.2s ease;
}
.fade-enter-from, .fade-leave-to {
  opacity: 0;
}

.slide-enter-active, .slide-leave-active {
  transition: all 0.15s ease;
  overflow: hidden;
}
.slide-enter-from, .slide-leave-to {
  opacity: 0;
  max-height: 0;
}
.slide-enter-to, .slide-leave-from {
  opacity: 1;
  max-height: 500px;
}

/* Markdown content styling for instructions */
.instruction-content :deep(.markdown-content) {
  font-size: 12px;
  line-height: 1.5;
}

.instruction-content :deep(.markdown-content p) {
  margin: 0 0 0.5em 0;
}

.instruction-content :deep(.markdown-content p:last-child) {
  margin-bottom: 0;
}

.instruction-content :deep(.markdown-content code) {
  font-size: 10px;
  padding: 0.1em 0.3em;
  background: rgba(0, 0, 0, 0.05);
  border-radius: 3px;
}

.instruction-content :deep(.markdown-content pre) {
  font-size: 10px;
  padding: 0.5em;
  background: rgba(0, 0, 0, 0.03);
  border-radius: 4px;
  overflow-x: auto;
  margin: 0.5em 0;
}

.instruction-content :deep(.markdown-content ul),
.instruction-content :deep(.markdown-content ol) {
  margin: 0.5em 0;
  padding-left: 1.5em;
}

.instruction-content :deep(.markdown-content li) {
  margin: 0.2em 0;
}
</style>
