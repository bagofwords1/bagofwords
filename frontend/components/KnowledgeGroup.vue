<template>
  <div v-if="isActive" class="mt-3">
    <!-- Status header -->
    <div
      class="flex items-center text-xstext-gray-500 cursor-pointer hover:text-gray-700"
      @click="isExpanded = !isExpanded"
    >
      <Icon name="heroicons-light-bulb" class="w-3 h-3 mr-1.5 text-gray-400 shrink-0" />
      <span v-if="isLoading" class="tool-shimmer flex items-center">
        Reviewing knowledge…
      </span>
      <span v-else class="text-gray-600 flex items-center gap-1.5 min-w-0">
        <span>Knowledge</span>
        <span class="text-gray-400">·</span>
        <span class="text-gray-500">{{ changes.length }} {{ changes.length === 1 ? 'change' : 'changes' }}</span>
        <span v-if="totalAdded > 0" class="text-[10px] font-mono text-green-600">+{{ totalAdded }}</span>
        <span v-if="totalRemoved > 0" class="text-[10px] font-mono text-red-500">−{{ totalRemoved }}</span>
        <span v-if="isBuildPublished" class="text-[10px] text-green-600 flex items-center">
          <Icon name="heroicons:check-circle-solid" class="w-3 h-3 mr-0.5" />Published
        </span>
        <Icon
          :name="isExpanded ? 'heroicons-chevron-down' : 'heroicons-chevron-right'"
          class="w-3 h-3 ml-0.5 text-gray-400 shrink-0"
        />
      </span>
    </div>

    <!-- Expanded body -->
    <Transition name="slide">
      <div v-if="isExpanded && !isLoading" class="mt-2 ml-5 space-y-1.5">
        <div v-if="changes.length === 0" class="text-[11px] text-gray-400 italic">
          No changes captured from this session.
        </div>

        <div
          v-for="ch in changes"
          :key="ch.id"
          :class="[
            'flex items-start gap-2 py-1',
            !isBuildPublished && !selectedIds.has(ch.id) ? 'opacity-50' : ''
          ]"
        >
          <UCheckbox
            v-if="!isBuildPublished"
            :model-value="selectedIds.has(ch.id)"
            color="blue"
            @update:model-value="toggleSelection(ch.id, $event)"
            class="mt-0.5"
          />
          <div class="flex-1 min-w-0">
            <div class="flex items-center gap-1.5">
              <span
                :class="[
                  'text-[9px] font-mono font-semibold uppercase tracking-wide',
                  ch.type === 'create' ? 'text-green-600' : 'text-blue-600'
                ]"
              >
                {{ ch.type === 'create' ? 'new' : 'edit' }}
              </span>
              <span
                class="text-[12px] text-gray-700 truncate cursor-pointer hover:text-gray-900"
                @click="!isBuildPublished ? handleEdit(ch) : null"
              >
                {{ ch.title }}
              </span>
              <span v-if="ch.added > 0" class="text-[10px] font-mono text-green-600 shrink-0">+{{ ch.added }}</span>
              <span v-if="ch.removed > 0" class="text-[10px] font-mono text-red-500 shrink-0">−{{ ch.removed }}</span>
            </div>
            <div class="text-[11px] text-gray-400 line-clamp-1 mt-0.5">
              {{ ch.preview }}
            </div>
          </div>
        </div>

        <!-- Publish button -->
        <div v-if="changes.length > 0 && !isBuildPublished && canCreateInstructions" class="pt-1">
          <button
            class="flex items-center px-2 py-1 text-[10px] font-medium text-gray-700 bg-gray-50 hover:bg-gray-100 rounded transition-colors disabled:opacity-50"
            :disabled="isPublishingBuild || selectedIds.size === 0"
            @click="handlePublishBuild"
          >
            <Spinner v-if="isPublishingBuild" class="w-3 h-3 text-green-600 mr-1" />
            <Icon v-else name="heroicons:check" class="w-3 h-3 text-green-600 mr-1" />
            <span>{{ publishButtonText }}</span>
          </button>
        </div>
      </div>
    </Transition>

    <!-- Instruction Modal -->
    <InstructionModalComponent
      v-model="showInstructionModal"
      :instruction="editingInstruction"
      :initial-type="'global'"
      :is-suggestion="true"
      :target-build-id="buildId"
      @instruction-saved="handleInstructionSaved"
    />
  </div>
</template>

<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import InstructionModalComponent from '~/components/InstructionModalComponent.vue'
import Spinner from '~/components/Spinner.vue'

interface ToolExecution {
  id: string
  tool_name: string
  status: string
  arguments_json?: any
  result_json?: any
}

interface HarnessBlock {
  id: string
  phase?: string | null
  status: string
  tool_execution?: ToolExecution
}

interface Props {
  blocks: HarnessBlock[]
  completionStatus?: string
}

const props = defineProps<Props>()

const isExpanded = ref(true)
const showInstructionModal = ref(false)
const editingInstruction = ref<any>(null)
const isPublishingBuild = ref(false)
const localPublishOverride = ref(false)
const selectedIds = ref<Set<string>>(new Set())

const toast = useToast()

const canCreateInstructions = computed(() => useCan('create_instructions'))

const stepCount = computed(() => props.blocks.filter(b => !!b.tool_execution).length)

const isParentStreaming = computed(() => props.completionStatus === 'in_progress')

interface Change {
  id: string
  type: 'create' | 'edit'
  title: string
  preview: string
  added: number
  removed: number
  instructionId: string | null
  text: string
  buildId: string | null
}

const changes = computed<Change[]>(() => {
  const out: Change[] = []
  for (const b of props.blocks) {
    const te = b.tool_execution
    if (!te) continue
    const rj = te.result_json || {}
    const args = te.arguments_json || {}
    if (te.tool_name === 'create_instruction' && rj.success === true) {
      const text = String(args.text || '')
      const firstLine = text.split('\n')[0].replace(/^#+\s*/, '').trim()
      out.push({
        id: b.id,
        type: 'create',
        title: firstLine.length > 80 ? firstLine.slice(0, 77) + '…' : (firstLine || 'New instruction'),
        preview: text,
        added: text.length,
        removed: 0,
        instructionId: rj.instruction_id || null,
        text,
        buildId: rj.build_id || null,
      })
    } else if (te.tool_name === 'edit_instruction' && rj.success === true) {
      const prev = String(rj.previous_text || '')
      const next = String(rj.new_text ?? args.text ?? '')
      const added = Math.max(next.length - prev.length, 0)
      const removed = Math.max(prev.length - next.length, 0)
      const firstLine = next.split('\n')[0].replace(/^#+\s*/, '').trim()
      out.push({
        id: b.id,
        type: 'edit',
        title: firstLine.length > 80 ? firstLine.slice(0, 77) + '…' : (firstLine || 'Edit instruction'),
        preview: next || prev,
        added,
        removed,
        instructionId: rj.instruction_id || null,
        text: next,
        buildId: rj.build_id || null,
      })
    }
  }
  return out
})

const isLoading = computed(() => {
  if (props.blocks.some(b =>
    b.status === 'in_progress' ||
    b.tool_execution?.status === 'running' ||
    b.tool_execution?.status === 'in_progress'
  )) return true
  // Parent completion is still streaming and harness hasn't produced a change yet:
  // treat as loading so we don't flash "0 changes · No changes captured" mid-stream.
  if (isParentStreaming.value && changes.value.length === 0) return true
  return false
})

const totalAdded = computed(() => changes.value.reduce((s, c) => s + c.added, 0))
const totalRemoved = computed(() => changes.value.reduce((s, c) => s + c.removed, 0))

const isActive = computed(() => props.blocks.length > 0)

// Derive build_id from the first change (all harness instructions share one draft build)
const buildId = computed(() => {
  for (const c of changes.value) {
    if (c.buildId) return c.buildId
  }
  return null
})

const isBuildPublished = computed(() => {
  if (localPublishOverride.value) return true
  return false
})

const publishButtonText = computed(() => {
  const n = selectedIds.value.size
  if (n === 0) return 'Publish Changes'
  if (n === 1) return 'Publish 1 Change'
  return `Publish ${n} Changes`
})

const toggleSelection = (id: string, checked: boolean) => {
  const next = new Set(selectedIds.value)
  if (checked) next.add(id)
  else next.delete(id)
  selectedIds.value = next
}

// Select all new changes by default
watch(changes, (newCh, oldCh) => {
  const oldIds = new Set((oldCh || []).map(c => c.id))
  const next = new Set(selectedIds.value)
  for (const c of newCh) {
    if (!oldIds.has(c.id)) next.add(c.id)
  }
  for (const id of selectedIds.value) {
    if (!newCh.some(c => c.id === id)) next.delete(id)
  }
  selectedIds.value = next
}, { immediate: true })

const handleEdit = async (ch: Change) => {
  if (!ch.instructionId) return
  try {
    const { data, error } = await useMyFetch(`/instructions/${ch.instructionId}`)
    if (!error.value && data.value) {
      editingInstruction.value = data.value
    } else {
      editingInstruction.value = { id: ch.instructionId, text: ch.text }
    }
  } catch {
    editingInstruction.value = { id: ch.instructionId, text: ch.text }
  }
  showInstructionModal.value = true
}

const handlePublishBuild = async () => {
  isPublishingBuild.value = true
  try {
    let targetBuildId = buildId.value
    const selectedInstructionIds = changes.value
      .filter(c => selectedIds.value.has(c.id) && c.instructionId)
      .map(c => c.instructionId as string)

    if (!targetBuildId && selectedInstructionIds[0]) {
      const { data } = await useMyFetch<any>(`/instructions/${selectedInstructionIds[0]}`)
      if (data.value?.current_build_id) targetBuildId = data.value.current_build_id
    }

    if (targetBuildId) {
      for (const iid of selectedInstructionIds) {
        await useMyFetch(`/instructions/${iid}`, {
          method: 'PUT',
          body: { status: 'published', target_build_id: targetBuildId },
        })
      }
      const response = await useMyFetch(`/builds/${targetBuildId}/publish`, {
        method: 'POST',
        body: { instruction_ids: selectedInstructionIds },
      })
      if (response.status.value === 'success') {
        localPublishOverride.value = true
        toast.add({ title: 'Success', description: 'Changes published', color: 'green' })
      } else {
        throw new Error('Failed to publish build')
      }
    } else {
      for (const iid of selectedInstructionIds) {
        await useMyFetch(`/instructions/${iid}`, {
          method: 'PUT',
          body: { status: 'published' },
        })
      }
      localPublishOverride.value = true
      toast.add({ title: 'Success', description: 'Changes published', color: 'green' })
    }
  } catch (e) {
    console.error('Error publishing knowledge changes:', e)
    toast.add({ title: 'Error', description: 'Failed to publish changes', color: 'red' })
  } finally {
    isPublishingBuild.value = false
  }
}

const handleInstructionSaved = () => {
  showInstructionModal.value = false
  toast.add({ title: 'Success', description: 'Instruction saved', color: 'green' })
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
</style>
