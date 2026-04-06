<template>
  <div v-if="isActive" class="mt-3">
    <!-- Status header -->
    <div
      class="flex items-center text-xs text-gray-500 cursor-pointer hover:text-gray-700"
      @click="toggleExpanded"
    >
      <span v-if="isLoading" class="flex items-center flex-wrap gap-1">
        <Spinner class="w-3 h-3 mr-1 text-gray-400 shrink-0" />
        <span class="knowledge-shimmer">Reviewing Knowledge</span>
        <template v-if="currentActivity">
          <span class="text-gray-300">·</span>
          <Transition name="knowledge-fade" mode="out-in">
            <span :key="currentActivity" class="knowledge-shimmer">{{ currentActivity }}</span>
          </Transition>
        </template>
      </span>
      <span v-else class="text-gray-600 flex items-center flex-wrap gap-1">
        <Icon
          :name="isExpanded ? 'heroicons-chevron-down' : 'heroicons-chevron-right'"
          class="w-3 h-3 mr-1 text-gray-400"
        />
        <span>Knowledge</span>
        <span class="text-gray-300">·</span>
        <span>{{ changes.length }} {{ changes.length === 1 ? 'change' : 'changes' }}</span>
        <span v-if="stepCount > 0" class="text-gray-400">in {{ stepCount }} {{ stepCount === 1 ? 'step' : 'steps' }}</span>
        <span v-if="totalAdded > 0" class="text-[10px] font-mono text-green-600">+{{ totalAdded }}</span>
        <span v-if="totalRemoved > 0" class="text-[10px] font-mono text-red-500">−{{ totalRemoved }}</span>
        <span v-if="isBuildPublished" class="text-[10px] text-green-600 flex items-center">
          <Icon name="heroicons:check-circle-solid" class="w-3 h-3 mr-0.5" />Published
        </span>
      </span>
    </div>

    <!-- Expanded body -->
    <Transition name="slide">
      <div v-if="isExpanded && !isLoading" class="mt-2 ml-5 space-y-1.5">
        <div v-if="steps.length > 0" class="space-y-0.5 mb-2">
          <div
            v-for="s in steps"
            :key="s.id"
            class="flex items-center gap-1 text-[11px] text-gray-500"
          >
            <span class="w-1 h-1 rounded-full bg-gray-300 shrink-0 mx-1"></span>
            <span class="truncate">{{ s.label }}</span>
          </div>
        </div>

        <div v-if="changes.length === 0" class="text-[11px] text-gray-400 italic">
          No changes captured from this session.
        </div>

        <div
          v-for="ch in changes"
          :key="ch.id"
          :class="[
            'flex items-start gap-2 py-1 px-1.5 -mx-1.5 rounded hover:bg-gray-50 cursor-pointer',
            !isBuildPublished && !selectedIds.has(ch.id) ? 'opacity-50' : ''
          ]"
          @click="handleEdit(ch)"
        >
          <UCheckbox
            v-if="!isBuildPublished"
            :model-value="selectedIds.has(ch.id)"
            color="blue"
            @update:model-value="toggleSelection(ch.id, $event)"
            @click.stop
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
              <span class="text-[12px] text-gray-700 truncate hover:text-gray-900">
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

interface KnowledgeHarnessBuild {
  id: string
  build_number?: number
  status: 'draft' | 'pending_approval' | 'approved' | 'rejected'
  is_main: boolean
}

interface Props {
  blocks: HarnessBlock[]
  harnessRunning?: boolean
  knowledgeHarnessBuild?: KnowledgeHarnessBuild | null
}

const props = defineProps<Props>()
const emit = defineEmits<{
  (e: 'open-instruction', id: string): void
  (e: 'published'): void
}>()

const isExpanded = ref(true)
const showInstructionModal = ref(false)
const editingInstruction = ref<any>(null)
const isPublishingBuild = ref(false)
const selectedIds = ref<Set<string>>(new Set())

const toast = useToast()

const canCreateInstructions = computed(() => useCan('create_instructions'))

const stepCount = computed(() => props.blocks.filter(b => !!b.tool_execution).length)

const isHarnessRunning = computed(() => !!props.harnessRunning)

const TOOL_LABELS: Record<string, string> = {
  search_instructions: 'Searching instructions',
  describe_tables: 'Reading tables',
  inspect_data: 'Inspecting data',
  create_instruction: 'Creating instruction',
  edit_instruction: 'Editing instruction',
}

interface Step {
  id: string
  label: string
}

const steps = computed<Step[]>(() => {
  const out: Step[] = []
  for (const b of props.blocks || []) {
    const te = b.tool_execution
    if (!te?.tool_name) continue
    const args = te.arguments_json || {}
    const rj = te.result_json || {}
    let label = ''
    switch (te.tool_name) {
      case 'search_instructions': {
        const q = args.search || args.query || ''
        label = q ? `Searched "${q}"` : 'Searched instructions'
        break
      }
      case 'describe_tables': {
        const q = args.query
        const names = Array.isArray(q) ? q.join(', ') : (q || '')
        label = names ? `Read ${names}` : 'Read tables'
        break
      }
      case 'inspect_data':
        label = 'Inspected data'
        break
      case 'create_instruction': {
        const title = rj.title || args.title || String(args.text || '').split('\n')[0].replace(/^#+\s*/, '').trim()
        label = `Created ${title || 'instruction'}`
        break
      }
      case 'edit_instruction': {
        const title = rj.title || args.title || String(rj.new_text || args.text || '').split('\n')[0].replace(/^#+\s*/, '').trim()
        label = `Edited ${title || 'instruction'}`
        break
      }
      default:
        label = te.tool_name
    }
    out.push({ id: b.id, label })
  }
  return out
})

const currentActivity = computed<string>(() => {
  const blocks = props.blocks || []
  // Prefer a currently-running tool
  for (let i = blocks.length - 1; i >= 0; i--) {
    const te = blocks[i]?.tool_execution
    if (te && (te.status === 'running' || te.status === 'in_progress')) {
      return TOOL_LABELS[te.tool_name] || te.tool_name
    }
  }
  // Otherwise the most recent tool in the stream
  for (let i = blocks.length - 1; i >= 0; i--) {
    const te = blocks[i]?.tool_execution
    if (te?.tool_name) return TOOL_LABELS[te.tool_name] || te.tool_name
  }
  return ''
})

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
      const resolvedTitle = String(rj.title || args.title || firstLine || 'New instruction').trim()
      out.push({
        id: b.id,
        type: 'create',
        title: resolvedTitle.length > 80 ? resolvedTitle.slice(0, 77) + '…' : resolvedTitle,
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
      const resolvedTitle = String(rj.title || args.title || firstLine || 'Edit instruction').trim()
      out.push({
        id: b.id,
        type: 'edit',
        title: resolvedTitle.length > 80 ? resolvedTitle.slice(0, 77) + '…' : resolvedTitle,
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
  if (isHarnessRunning.value && changes.value.length === 0) return true
  return false
})

const totalAdded = computed(() => changes.value.reduce((s, c) => s + c.added, 0))
const totalRemoved = computed(() => changes.value.reduce((s, c) => s + c.removed, 0))

const isActive = computed(() => isHarnessRunning.value || props.blocks.length > 0)

// Derive build_id from the first change (all harness instructions share one draft build)
const buildId = computed(() => {
  for (const c of changes.value) {
    if (c.buildId) return c.buildId
  }
  return null
})

const isBuildPublished = computed(() => {
  const b = props.knowledgeHarnessBuild
  if (!b) return false
  return b.is_main === true || b.status === 'approved'
})

const publishButtonText = computed(() => {
  const n = selectedIds.value.size
  if (n === 0) return 'Publish Changes'
  if (n === 1) return 'Publish 1 Change'
  return `Publish ${n} Changes`
})

const toggleExpanded = () => {
  if (!isLoading.value) isExpanded.value = !isExpanded.value
}

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

const handleEdit = (ch: Change) => {
  if (!ch.instructionId) return
  emit('open-instruction', ch.instructionId)
}

const handlePublishBuild = async () => {
  const targetBuildId = buildId.value
  const selectedInstructionIds = changes.value
    .filter(c => selectedIds.value.has(c.id) && c.instructionId)
    .map(c => c.instructionId as string)

  if (!targetBuildId) {
    // Harness contract says all changes in a session share one draft build;
    // missing means the tool result never carried build_id — a real bug.
    console.error('Publish aborted: no build_id found on any harness change.')
    toast.add({ title: 'Error', description: 'Cannot publish: draft build id missing', color: 'red' })
    return
  }

  // Sanity check: every change in this harness session should share the same build.
  const distinctBuildIds = new Set(changes.value.map(c => c.buildId).filter(Boolean))
  if (distinctBuildIds.size > 1) {
    console.error('Publish aborted: harness changes span multiple builds', Array.from(distinctBuildIds))
    toast.add({ title: 'Error', description: 'Inconsistent build ids in changes', color: 'red' })
    return
  }

  if (selectedInstructionIds.length === 0) return

  isPublishingBuild.value = true
  try {
    const response = await useMyFetch(`/builds/${targetBuildId}/publish`, {
      method: 'POST',
      body: { instruction_ids: selectedInstructionIds },
    })
    if (response.status.value === 'success') {
      toast.add({ title: 'Success', description: 'Changes published', color: 'green' })
      emit('published')
    } else {
      throw new Error('Failed to publish build')
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
.knowledge-shimmer {
  background: linear-gradient(90deg, #888 0%, #999 25%, #ccc 50%, #999 75%, #888 100%);
  background-size: 200% 100%;
  -webkit-background-clip: text;
  background-clip: text;
  color: transparent;
  animation: knowledge-shimmer-anim 2s linear infinite;
}
@keyframes knowledge-shimmer-anim {
  0% { background-position: -100% 0; }
  100% { background-position: 100% 0; }
}
.knowledge-fade-enter-active,
.knowledge-fade-leave-active {
  transition: opacity 0.25s ease;
}
.knowledge-fade-enter-from,
.knowledge-fade-leave-to {
  opacity: 0;
}

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
