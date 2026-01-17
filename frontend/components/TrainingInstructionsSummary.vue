<template>
  <div v-if="shouldShow" class="mt-1">
    <!-- Minimal header row - cursor style -->
    <div
      class="flex items-center text-xs text-gray-500 cursor-pointer hover:text-gray-700"
      @click="toggleExpanded"
    >
      <Icon name="heroicons-academic-cap" class="w-3 h-3 mr-1.5 text-gray-400" />
      <span class="text-gray-600">Training summary</span>
      <span v-if="toolExecutions.length > 0" class="text-gray-400 ml-1.5">
        {{ toolExecutions.length }} instruction{{ toolExecutions.length === 1 ? '' : 's' }}
      </span>
      <Icon
        :name="isExpanded ? 'heroicons-chevron-down' : 'heroicons-chevron-right'"
        class="w-3 h-3 ml-1 text-gray-400"
      />
    </div>

    <!-- Expandable content -->
    <Transition name="slide">
      <div v-if="isExpanded" class="mt-2 space-y-1">
        <!-- Loading -->
        <div v-if="isLoading" class="flex items-center text-[11px] text-gray-500">
          <Spinner class="w-3 h-3 mr-1.5" />
          Loading...
        </div>

        <!-- Empty state -->
        <div v-else-if="toolExecutions.length === 0" class="text-[11px] text-gray-400 italic">
          No instructions created
        </div>

        <!-- Instructions list using appropriate tool component -->
        <template v-else>
          <template v-for="te in toolExecutions" :key="te.id">
            <EditInstructionTool
              v-if="te.tool_name === 'edit_instruction'"
              :tool-execution="te"
              @instruction-updated="handleInstructionUpdated"
            />
            <CreateInstructionTool
              v-else
              :tool-execution="te"
              @instruction-updated="handleInstructionUpdated"
            />
          </template>
        </template>
      </div>
    </Transition>
  </div>
</template>

<script setup lang="ts">
import { ref, computed, onMounted, watch } from 'vue'
import CreateInstructionTool from '~/components/tools/CreateInstructionTool.vue'
import EditInstructionTool from '~/components/tools/EditInstructionTool.vue'
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

const props = defineProps<Props>()

const apiInstructions = ref<Instruction[]>([])
const isLoading = ref(false)
const isExpanded = ref(false)

const shouldShow = computed(() => {
  // Only show for training mode reports when not streaming AND has instructions
  return props.report?.mode === 'training' && !props.isStreaming && toolExecutions.value.length > 0
})

// Extract tool executions from messages (both create and edit)
const extractedToolExecutions = computed<ToolExecution[]>(() => {
  if (!props.messages) return []

  const executions: ToolExecution[] = []

  for (const message of props.messages) {
    if (!message.completion_blocks) continue

    for (const block of message.completion_blocks) {
      const te = block.tool_execution
      // Handle create_instruction
      if (te?.tool_name === 'create_instruction' && te.status === 'success') {
        const rj = te.result_json || {}
        // Only include successfully created instructions
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
      // Handle edit_instruction
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

// Convert API instructions to tool execution format
const apiToolExecutions = computed<ToolExecution[]>(() => {
  return apiInstructions.value.map(inst => ({
    id: `api-${inst.id}`,
    tool_name: 'create_instruction',
    status: 'success',
    result_json: {
      success: true,
      instruction_id: inst.id,
      global_status: inst.global_status,
      status: inst.status
    },
    arguments_json: {
      text: inst.text,
      category: inst.category,
      load_mode: inst.load_mode || 'intelligent',
      table_names: inst.references?.map(r => r.display_text) || []
    }
  }))
})

// Combine extracted and API tool executions (dedupe by instruction_id)
const toolExecutions = computed<ToolExecution[]>(() => {
  const byInstructionId = new Map<string, ToolExecution>()

  // API instructions take precedence (more complete data)
  for (const te of apiToolExecutions.value) {
    const instId = te.result_json.instruction_id
    if (instId) {
      byInstructionId.set(instId, te)
    }
  }

  // Add extracted ones if not already present
  for (const te of extractedToolExecutions.value) {
    const instId = te.result_json.instruction_id
    if (instId && !byInstructionId.has(instId)) {
      byInstructionId.set(instId, te)
    }
  }

  return Array.from(byInstructionId.values())
})

function toggleExpanded() {
  isExpanded.value = !isExpanded.value
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
  if (shouldShow.value) {
    fetchInstructions()
  }
})

watch(() => props.report?.id, () => {
  if (shouldShow.value) {
    fetchInstructions()
  }
})

watch(() => props.isStreaming, (newVal, oldVal) => {
  if (oldVal === true && newVal === false && shouldShow.value) {
    fetchInstructions()
  }
})

// Emit for parent components
const emit = defineEmits<{
  (e: 'instruction-updated'): void
}>()

function handleInstructionUpdated() {
  // Refresh the instructions list when any instruction is updated
  fetchInstructions()
  // Propagate the event up if needed
  emit('instruction-updated')
}
</script>

<style scoped>
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
  max-height: 1000px;
}
</style>
