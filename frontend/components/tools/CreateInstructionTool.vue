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
          Creating instruction...
        </span>
        <span v-else-if="isSuccess" class="text-gray-600 flex items-center">
          <Icon name="heroicons-cube" class="w-3 h-3 mr-1.5 text-green-500" />
          <span class="truncate max-w-[300px]">{{ truncatedText }}</span>
          <span v-if="category" class="ml-1.5 px-1.5 py-0.5 bg-gray-100 text-gray-600 rounded text-[10px] shrink-0">{{ category }}</span>
          <Icon
            :name="isExpanded ? 'heroicons-chevron-down' : 'heroicons-chevron-right'"
            class="w-3 h-3 ml-1 text-gray-400 shrink-0"
          />
        </span>
        <span v-else-if="isRejected" class="text-gray-600 flex items-center">
          <Icon name="heroicons-x-circle" class="w-3 h-3 mr-1.5 text-orange-500" />
          <span>Instruction rejected</span>
          <span v-if="rejectedReason" class="ml-1.5 text-orange-600 text-[10px]">({{ rejectedReason }})</span>
          <Icon
            :name="isExpanded ? 'heroicons-chevron-down' : 'heroicons-chevron-right'"
            class="w-3 h-3 ml-1 text-gray-400"
          />
        </span>
        <span v-else class="text-gray-600 flex items-center">
          <Icon name="heroicons-x-circle" class="w-3 h-3 mr-1.5 text-red-500" />
          <span>Failed to create instruction</span>
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
        <!-- Instruction card - similar to InstructionSuggestions -->
        <div class="hover:bg-gray-50 border border-gray-150 rounded-md p-3 transition-colors">
          <!-- Instruction text - click to edit -->
          <div
            v-if="instructionText"
            class="instruction-content text-[12px] text-gray-800 leading-relaxed mb-2 cursor-pointer"
            @click="currentGlobalStatus !== 'approved' ? handleEdit() : null"
          >
            <MDC :value="instructionText" class="markdown-content" />
          </div>

          <!-- Metadata row -->
          <div class="flex flex-wrap items-center gap-2 text-[10px] mb-2">
            <!-- Confidence -->
            <div v-if="confidence" class="flex items-center gap-1">
              <span class="text-gray-500">Confidence:</span>
              <span
                class="font-medium"
                :class="confidence >= 0.9 ? 'text-green-600' : confidence >= 0.7 ? 'text-yellow-600' : 'text-red-600'"
              >
                {{ Math.round(confidence * 100) }}%
              </span>
            </div>

            <!-- Load mode -->
            <div v-if="loadMode" class="flex items-center gap-1">
              <span class="text-gray-500">Load:</span>
              <span class="px-1.5 py-0.5 rounded text-[9px] font-medium"
                :class="loadMode === 'always' ? 'bg-blue-100 text-blue-700' : 'bg-gray-100 text-gray-600'"
              >
                {{ loadMode }}
              </span>
            </div>

            <!-- Tables scoped -->
            <div v-if="tableCount > 0" class="flex items-center gap-1">
              <Icon name="heroicons-table-cells" class="w-3 h-3 text-gray-400" />
              <span class="text-gray-600">{{ tableCount }} table{{ tableCount > 1 ? 's' : '' }}</span>
            </div>
          </div>

          <!-- Evidence (if available) -->
          <div v-if="evidence" class="text-[10px] text-gray-500 italic mb-2">
            <span class="font-medium">Evidence:</span> {{ evidence }}
          </div>

          <!-- Action buttons / Status display -->
          <div v-if="isSuccess && instructionId" class="flex justify-start gap-2 pt-2 border-t border-gray-200">
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
          <div v-if="errorMessage" class="text-[10px] text-red-500 bg-red-50/50 rounded px-2 py-1 mt-2">
            {{ errorMessage }}
          </div>
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
import { computed, ref } from 'vue'
import InstructionModalComponent from '~/components/InstructionModalComponent.vue'
import Spinner from '~/components/Spinner.vue'

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

// Extract from arguments_json (input)
const instructionText = computed(() => {
  const args = props.toolExecution?.arguments_json || {}
  return args.text || ''
})

const truncatedText = computed(() => {
  const text = instructionText.value
  if (!text) return 'Created instruction'
  // Get first line and truncate if needed
  const firstLine = text.split('\n')[0].replace(/^#+\s*/, '').trim()
  if (firstLine.length > 60) {
    return firstLine.substring(0, 57) + '...'
  }
  return firstLine || 'Created instruction'
})

const category = computed(() => {
  const args = props.toolExecution?.arguments_json || {}
  return args.category || ''
})

const confidence = computed(() => {
  const args = props.toolExecution?.arguments_json || {}
  return args.confidence
})

const loadMode = computed(() => {
  const args = props.toolExecution?.arguments_json || {}
  return args.load_mode || 'intelligent'
})

const evidence = computed(() => {
  const args = props.toolExecution?.arguments_json || {}
  return args.evidence || ''
})

const tableCount = computed(() => {
  const args = props.toolExecution?.arguments_json || {}
  const tableNames = args.table_names || []
  return Array.isArray(tableNames) ? tableNames.length : 0
})

// Extract from result_json (output)
const instructionId = computed(() => {
  const rj = props.toolExecution?.result_json || {}
  return rj.instruction_id || null
})

const currentGlobalStatus = computed(() => {
  // Use local state if set, otherwise use result_json
  if (localGlobalStatus.value !== null) return localGlobalStatus.value
  const rj = props.toolExecution?.result_json || {}
  return rj.global_status || null
})

const rejectedReason = computed(() => {
  const rj = props.toolExecution?.result_json || {}
  return rj.rejected_reason || ''
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

function toggleExpanded() {
  if (status.value !== 'running') {
    isExpanded.value = !isExpanded.value
  }
}

async function handleEdit() {
  if (!instructionId.value) return

  try {
    const { data, error } = await useMyFetch(`/instructions/${instructionId.value}`)
    if (!error.value && data.value) {
      editingInstruction.value = data.value
    } else {
      // Fallback to basic data from tool execution
      editingInstruction.value = {
        id: instructionId.value,
        text: instructionText.value,
        category: category.value || 'general',
        load_mode: loadMode.value
      }
    }
  } catch {
    editingInstruction.value = {
      id: instructionId.value,
      text: instructionText.value,
      category: category.value || 'general',
      load_mode: loadMode.value
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
      // Mark as deleted by setting a special status
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
  if (updated?.global_status) {
    localGlobalStatus.value = updated.global_status
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
