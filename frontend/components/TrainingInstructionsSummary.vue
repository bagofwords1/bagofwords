<template>
  <div
    v-if="shouldShow"
    class="relative"
    @mouseenter="showDropdown = true"
    @mouseleave="showDropdown = false"
  >
    <button
      class="inline-flex items-center gap-1.5 px-3 py-1.5 rounded-full border border-gray-200 bg-white text-xs text-gray-600 hover:bg-gray-50 transition-colors"
    >
      <Icon name="heroicons-academic-cap" class="w-3.5 h-3.5 text-gray-400" />
      {{ uniqueInstructions.length }} Instruction{{ uniqueInstructions.length === 1 ? '' : 's' }}
    </button>

    <!-- Dropdown -->
    <div
      v-if="showDropdown"
      class="absolute start-0 top-full mt-1 w-80 z-20"
    >
      <div class="bg-white border border-gray-200 rounded-lg shadow-lg py-1 mb-0">
        <!-- Loading -->
        <div v-if="isLoading" class="flex items-center justify-center px-3 py-2">
          <Spinner class="w-3 h-3 me-1.5" />
          <span class="text-[11px] text-gray-500">Loading...</span>
        </div>

        <!-- Instructions list -->
        <template v-else>
          <div
            v-for="inst in uniqueInstructions"
            :key="inst.instructionId"
            class="px-3 py-2 hover:bg-gray-50 cursor-pointer"
            @click="handleClick(inst)"
          >
            <div class="flex items-center gap-1.5">
              <Icon
                :name="inst.isEdit ? 'heroicons-pencil' : 'heroicons-plus-circle'"
                class="w-3 h-3 shrink-0"
                :class="inst.isEdit ? 'text-blue-500' : 'text-green-500'"
              />
              <span class="text-xs text-gray-700 truncate">{{ inst.title }}</span>
            </div>
            <div class="flex items-center gap-2 mt-0.5 ms-[18px]">
              <span v-if="inst.category" class="text-[10px] text-gray-400">{{ inst.category }}</span>
              <span v-if="inst.lineCount > 0" class="text-[10px] text-green-600">+{{ inst.lineCount }}</span>
            </div>
          </div>
        </template>
      </div>
    </div>
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
import { ref, computed, onMounted, watch } from 'vue'
import InstructionModalComponent from '~/components/InstructionModalComponent.vue'
import Spinner from '~/components/Spinner.vue'

interface Instruction {
  id: string
  text: string
  title?: string
  category: string
  status: string
  global_status?: string | null
  load_mode?: string
  data_sources?: Array<{ id: string; name: string }>
  references?: Array<{ id: string; object_type: string; display_text?: string }>
}

interface ToolExecution {
  id: string
  tool_name: string
  status: string
  result_json: any
  arguments_json: any
}

interface Report {
  id: string
  mode?: string
}

interface ChatMessage {
  id: string
  role: string
  completion_blocks?: Array<{
    tool_execution?: {
      id?: string
      tool_name: string
      status: string
      result_json?: any
      arguments_json?: any
    }
  }>
}

interface Props {
  report: Report
  isStreaming?: boolean
  messages?: ChatMessage[]
}

interface UniqueInstruction {
  instructionId: string
  title: string
  category: string
  isEdit: boolean
  lineCount: number
}

const props = defineProps<Props>()

const apiInstructions = ref<Instruction[]>([])
const isLoading = ref(false)
const showDropdown = ref(false)
const showInstructionModal = ref(false)
const editingInstruction = ref<any>(null)

const shouldShow = computed(() => {
  return props.report?.mode === 'training' && !props.isStreaming && uniqueInstructions.value.length > 0
})

// Extract tool executions from messages (both create and edit)
const extractedToolExecutions = computed<ToolExecution[]>(() => {
  if (!props.messages) return []

  const executions: ToolExecution[] = []

  for (const message of props.messages) {
    if (!message.completion_blocks) continue

    for (const block of message.completion_blocks) {
      const te = block.tool_execution
      if (te?.tool_name === 'create_instruction' && te.status === 'success') {
        const rj = te.result_json || {}
        if (rj.success === true && rj.instruction_id) {
          executions.push({
            id: te.id || `te-${rj.instruction_id}`,
            tool_name: 'create_instruction',
            status: 'success',
            result_json: rj,
            arguments_json: te.arguments_json || {}
          })
        }
      }
      if (te?.tool_name === 'edit_instruction' && te.status === 'success') {
        const rj = te.result_json || {}
        if (rj.success === true && rj.instruction_id) {
          executions.push({
            id: te.id || `te-edit-${rj.instruction_id}`,
            tool_name: 'edit_instruction',
            status: 'success',
            result_json: rj,
            arguments_json: te.arguments_json || {}
          })
        }
      }
    }
  }

  return executions
})

// Build unique instructions list (dedupe by instruction_id, keep last action)
const uniqueInstructions = computed<UniqueInstruction[]>(() => {
  const byId = new Map<string, UniqueInstruction>()

  // API instructions first
  for (const inst of apiInstructions.value) {
    byId.set(inst.id, {
      instructionId: inst.id,
      title: inst.title || inst.text.split('\n')[0].replace(/^#+\s*/, '').trim().substring(0, 60) || 'Instruction',
      category: inst.category,
      isEdit: false,
      lineCount: inst.text.split('\n').filter(l => l.trim()).length,
    })
  }

  // Overlay with extracted tool executions (later ones override)
  for (const te of extractedToolExecutions.value) {
    const instId = te.result_json.instruction_id
    if (!instId) continue
    const args = te.arguments_json || {}
    const text = args.text || ''
    const existing = byId.get(instId)
    const isEdit = te.tool_name === 'edit_instruction' || (existing?.isEdit ?? false)

    byId.set(instId, {
      instructionId: instId,
      title: existing?.title || text.split('\n')[0].replace(/^#+\s*/, '').trim().substring(0, 60) || 'Instruction',
      category: args.category || existing?.category || 'general',
      isEdit,
      lineCount: text ? text.split('\n').filter((l: string) => l.trim()).length : (existing?.lineCount ?? 0),
    })
  }

  return Array.from(byId.values())
})

async function handleClick(inst: UniqueInstruction) {
  showDropdown.value = false
  try {
    const { data, error } = await useMyFetch(`/instructions/${inst.instructionId}`)
    if (!error.value && data.value) {
      editingInstruction.value = data.value
    } else {
      editingInstruction.value = { id: inst.instructionId }
    }
  } catch {
    editingInstruction.value = { id: inst.instructionId }
  }
  showInstructionModal.value = true
}

async function fetchInstructions() {
  if (!props.report?.id || props.report?.mode !== 'training') return

  isLoading.value = true
  try {
    const { data, error } = await useMyFetch(`/reports/${props.report.id}/instructions`)
    if (!error.value && data.value) {
      apiInstructions.value = data.value as Instruction[]
    }
  } catch (e) {
    console.error('Failed to fetch training instructions:', e)
  } finally {
    isLoading.value = false
  }
}

onMounted(() => {
  if (props.report?.mode === 'training') {
    fetchInstructions()
  }
})

watch(() => props.report?.id, () => {
  if (props.report?.mode === 'training') {
    fetchInstructions()
  }
})

watch(() => props.isStreaming, (newVal, oldVal) => {
  if (oldVal === true && newVal === false && props.report?.mode === 'training') {
    fetchInstructions()
  }
})

// Emit for parent components
const emit = defineEmits<{
  (e: 'instruction-updated'): void
}>()

function handleInstructionSaved(data: any) {
  fetchInstructions()
  showInstructionModal.value = false
  emit('instruction-updated')
}
</script>
