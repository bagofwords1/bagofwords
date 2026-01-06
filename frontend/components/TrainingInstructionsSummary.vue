<template>
  <div v-if="shouldShow" class="mt-1">
    <!-- Minimal header row - cursor style -->
    <div
      class="flex items-center text-xs text-gray-500 cursor-pointer hover:text-gray-700"
      @click="toggleExpanded"
    >
      <Icon name="heroicons-academic-cap" class="w-3 h-3 mr-1.5 text-gray-400" />
      <span class="text-gray-600">Training summary</span>
      <span v-if="instructions.length > 0" class="text-gray-400 ml-1.5">
        {{ instructions.length }} instruction{{ instructions.length === 1 ? '' : 's' }}
      </span>
      <Icon
        :name="isExpanded ? 'heroicons-chevron-down' : 'heroicons-chevron-right'"
        class="w-3 h-3 ml-1 text-gray-400"
      />
    </div>

    <!-- Expandable content -->
    <Transition name="slide">
      <div v-if="isExpanded" class="mt-2 space-y-2">
        <!-- Loading -->
        <div v-if="isLoading" class="flex items-center text-[11px] text-gray-500">
          <Spinner class="w-3 h-3 mr-1.5" />
          Loading...
        </div>

        <!-- Empty state -->
        <div v-else-if="instructions.length === 0" class="text-[11px] text-gray-400 italic">
          No instructions created
        </div>

        <!-- Instructions list - card style like InstructionSuggestions -->
        <template v-else>
          <div
            v-for="instruction in instructions"
            :key="instruction.id"
            class="hover:bg-gray-50 border border-gray-150 rounded-md p-3 transition-colors"
          >
            <!-- Instruction text with MDC -->
            <div class="instruction-content text-[12px] text-gray-800 leading-relaxed mb-2">
              <MDC :value="instruction.text" class="markdown-content" />
            </div>

            <!-- Metadata row: category, load mode, tables -->
            <div class="flex flex-wrap items-center gap-2 text-[10px] mb-2">
              <span class="px-1.5 py-0.5 rounded bg-gray-100 text-gray-600">
                {{ instruction.category }}
              </span>
              <span class="text-gray-400">
                {{ instruction.load_mode || 'intelligent' }}
              </span>
              <span v-if="instruction.references?.length" class="text-gray-400">
                {{ instruction.references.length }} table{{ instruction.references.length > 1 ? 's' : '' }}
              </span>
            </div>

            <!-- Action buttons -->
            <div class="flex justify-start gap-2 pt-2 border-t border-gray-200">
              <button
                @click="handleEdit(instruction)"
                class="flex items-center text-[10px] font-medium text-gray-700 bg-gray-50 hover:bg-gray-100 rounded transition-colors"
              >
                <Icon name="heroicons:pencil" class="w-3 h-3 text-blue-600 mr-1" />
                <span>Edit</span>
              </button>
              <button
                @click="handleDelete(instruction)"
                class="flex items-center text-[10px] font-medium text-gray-700 bg-gray-50 hover:bg-gray-100 rounded transition-colors"
                :disabled="isDeleting === instruction.id"
              >
                <Spinner
                  v-if="isDeleting === instruction.id"
                  class="w-3 h-3 text-red-600 mr-1"
                />
                <Icon
                  v-else
                  name="heroicons:trash"
                  class="w-3 h-3 text-red-600 mr-1"
                />
                <span>{{ isDeleting === instruction.id ? 'Deleting...' : 'Delete' }}</span>
              </button>
            </div>
          </div>
        </template>
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

interface Report {
  id: string
  mode?: string
}

interface Props {
  report: Report
  isStreaming?: boolean
}

const props = defineProps<Props>()

const toast = useToast()
const instructions = ref<Instruction[]>([])
const isLoading = ref(false)
const isExpanded = ref(false)
const showInstructionModal = ref(false)
const editingInstruction = ref<Instruction | null>(null)
const isDeleting = ref<string | null>(null)

const shouldShow = computed(() => {
  // Only show for training mode reports when not streaming
  return props.report?.mode === 'training' && !props.isStreaming
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
      instructions.value = data.value as Instruction[]
    }
  } catch (e) {
    console.error('Failed to fetch training instructions:', e)
  } finally {
    isLoading.value = false
  }
}

async function handleEdit(instruction: Instruction) {
  try {
    const { data, error } = await useMyFetch(`/instructions/${instruction.id}`)
    if (!error.value && data.value) {
      editingInstruction.value = data.value as Instruction
    } else {
      editingInstruction.value = instruction
    }
  } catch {
    editingInstruction.value = instruction
  }
  showInstructionModal.value = true
}

async function handleDelete(instruction: Instruction) {
  if (!confirm('Delete this instruction?')) return

  isDeleting.value = instruction.id
  try {
    const { error } = await useMyFetch(`/instructions/${instruction.id}`, { method: 'DELETE' })
    if (!error.value) {
      instructions.value = instructions.value.filter(i => i.id !== instruction.id)
      toast.add({ title: 'Deleted', description: 'Instruction deleted', color: 'orange' })
    } else {
      throw new Error('Failed to delete')
    }
  } catch (e) {
    console.error('Failed to delete instruction:', e)
    toast.add({ title: 'Error', description: 'Failed to delete instruction', color: 'red' })
  } finally {
    isDeleting.value = null
  }
}

function handleInstructionSaved(data: any) {
  const updated = data?.data || data
  if (updated?.id) {
    const idx = instructions.value.findIndex(i => i.id === updated.id)
    if (idx !== -1) {
      instructions.value[idx] = { ...instructions.value[idx], ...updated }
    }
  }
  showInstructionModal.value = false
  toast.add({ title: 'Success', description: 'Instruction saved', color: 'green' })
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
  max-height: 500px;
}

/* Markdown content styling for instructions */
.instruction-content :deep(.markdown-content) {
  font-size: 10px;
  line-height: 1.5;
}

.instruction-content :deep(.markdown-content p) {
  margin: 0 0 0.5em 0;
}

.instruction-content :deep(.markdown-content p:last-child) {
  margin-bottom: 0;
}

.instruction-content :deep(.markdown-content code) {
  font-size: 9px;
  padding: 0.1em 0.3em;
  background: rgba(0, 0, 0, 0.05);
  border-radius: 3px;
}

.instruction-content :deep(.markdown-content pre) {
  font-size: 9px;
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
