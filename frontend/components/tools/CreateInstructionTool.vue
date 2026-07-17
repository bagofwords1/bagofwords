<template>
  <div class="mt-1">
    <!-- Status header -->
    <Transition name="fade" appear>
      <div
        class="flex items-center text-xs text-gray-500 dark:text-gray-400 cursor-pointer hover:text-gray-700 dark:hover:text-gray-300"
        @click="toggleExpanded"
      >
        <span v-if="status === 'running'" class="tool-shimmer flex items-center">
          <Icon name="heroicons-cube" class="w-3 h-3 me-1.5 text-gray-400" />
          <span v-if="instructionText" dir="auto" class="truncate max-w-[300px]">{{ $t('tools.createInstruction.creatingPrefix', { text: truncatedText }) }}</span>
          <span v-else>{{ $t('tools.createInstruction.creating') }}</span>
          <span v-if="category" class="ms-1.5 px-1.5 py-0.5 bg-gray-100 dark:bg-gray-800 text-gray-600 dark:text-gray-400 rounded text-[10px] shrink-0">{{ category }}</span>
          <Icon
            v-if="instructionText"
            :name="isExpanded ? 'heroicons-chevron-down' : 'heroicons-chevron-right'"
            class="w-3 h-3 ms-1 text-gray-400 shrink-0 rtl-flip"
          />
        </span>
        <span v-else-if="isSuccess" class="text-gray-600 dark:text-gray-400 flex items-center">
          <Icon name="heroicons-cube" class="w-3 h-3 me-1.5 text-green-500" />
          <span dir="auto" class="truncate max-w-[300px]">{{ truncatedText }}</span>
          <span v-if="category" class="ms-1.5 px-1.5 py-0.5 bg-gray-100 dark:bg-gray-800 text-gray-600 dark:text-gray-400 rounded text-[10px] shrink-0">{{ category }}</span>
          <span v-if="lineCount > 0" class="ms-1.5 text-[10px] text-green-600 shrink-0">+{{ lineCount }}</span>
          <Icon
            :name="isExpanded ? 'heroicons-chevron-down' : 'heroicons-chevron-right'"
            class="w-3 h-3 ms-1 text-gray-400 shrink-0 rtl-flip"
          />
        </span>
        <span v-else-if="isRejected" class="text-gray-600 dark:text-gray-400 flex items-center">
          <Icon name="heroicons-x-circle" class="w-3 h-3 me-1.5 text-orange-500" />
          <span>{{ $t('tools.createInstruction.rejected') }}</span>
          <span v-if="rejectedReason" class="ms-1.5 text-orange-600 text-[10px]">({{ rejectedReason }})</span>
          <Icon
            :name="isExpanded ? 'heroicons-chevron-down' : 'heroicons-chevron-right'"
            class="w-3 h-3 ms-1 text-gray-400 rtl-flip"
          />
        </span>
        <span v-else class="text-gray-600 dark:text-gray-400 flex items-center">
          <Icon name="heroicons-x-circle" class="w-3 h-3 me-1.5 text-red-500" />
          <span>{{ $t('tools.createInstruction.failed') }}</span>
          <Icon
            :name="isExpanded ? 'heroicons-chevron-down' : 'heroicons-chevron-right'"
            class="w-3 h-3 ms-1 text-gray-400 rtl-flip"
          />
        </span>
      </div>
    </Transition>

    <!-- Expandable content -->
    <Transition name="slide">
      <div v-if="isExpanded && (status !== 'running' || instructionText)" class="mt-2 space-y-2">
        <!-- Instruction card - similar to InstructionSuggestions -->
        <div class="hover:bg-gray-50 dark:hover:bg-gray-800 border border-gray-150 rounded-md p-3 transition-colors">
          <!-- Instruction text - click to edit -->
          <div
            v-if="instructionText"
            dir="auto"
            class="instruction-content text-[12px] text-gray-800 dark:text-gray-200 leading-relaxed mb-2 cursor-pointer"
            @click="currentGlobalStatus !== 'approved' ? handleEdit() : null"
          >
            <MDC :value="instructionText" class="markdown-content" />
          </div>

          <!-- Metadata row -->
          <div class="flex flex-wrap items-center gap-2 text-[10px] mb-2">
            <!-- Confidence -->
            <div v-if="confidence" class="flex items-center gap-1">
              <span class="text-gray-500 dark:text-gray-400">{{ $t('tools.createInstruction.confidence') }}</span>
              <span
                class="font-medium"
                :class="confidence >= 0.9 ? 'text-green-600' : confidence >= 0.7 ? 'text-yellow-600' : 'text-red-600'"
              >
                {{ Math.round(confidence * 100) }}%
              </span>
            </div>

            <!-- Load mode -->
            <div v-if="loadMode" class="flex items-center gap-1">
              <span class="text-gray-500 dark:text-gray-400">{{ $t('tools.createInstruction.load') }}</span>
              <span class="px-1.5 py-0.5 rounded text-[9px] font-medium"
                :class="loadMode === 'always' ? 'bg-blue-100 dark:bg-blue-900/50 text-blue-700' : 'bg-gray-100 dark:bg-gray-800 text-gray-600 dark:text-gray-400'"
              >
                {{ loadMode }}
              </span>
            </div>

            <!-- Tables scoped -->
            <div v-if="tableCount > 0" class="flex items-center gap-1">
              <Icon name="heroicons-table-cells" class="w-3 h-3 text-gray-400" />
              <span class="text-gray-600 dark:text-gray-400">{{ tableCount === 1 ? $t('tools.createInstruction.tableSingular', { n: tableCount }) : $t('tools.createInstruction.tablePlural', { n: tableCount }) }}</span>
            </div>
          </div>

          <!-- Evidence (if available) -->
          <div v-if="evidence" dir="auto" class="text-[10px] text-gray-500 dark:text-gray-400 italic mb-2">
            <span class="font-medium">{{ $t('tools.createInstruction.evidence') }}</span> {{ evidence }}
          </div>

          <!-- Status + Accept/Reject actions -->
          <div v-if="isSuccess && instructionId" class="flex items-center gap-1.5 pt-2 border-t border-gray-200 dark:border-gray-700">
            <template v-if="resolution === 'accepted'">
              <Icon name="heroicons:check-circle" class="w-3 h-3 text-green-500" />
              <span class="text-[10px] font-medium text-gray-600 dark:text-gray-400">{{ $t('tools.createInstruction.accepted', 'Accepted') }}</span>
            </template>
            <template v-else-if="resolution === 'rejected'">
              <Icon name="heroicons:x-circle" class="w-3 h-3 text-gray-400" />
              <span class="text-[10px] text-gray-400">{{ $t('tools.createInstruction.rejectedLabel', 'Rejected') }}</span>
            </template>
            <template v-else-if="canResolve">
              <button
                class="inline-flex items-center gap-1 px-2 py-0.5 text-[11px] font-medium text-green-700 bg-green-50 dark:bg-green-950 border border-green-200 rounded hover:bg-green-100 dark:hover:bg-green-900/50 transition-colors disabled:opacity-50"
                :disabled="isAccepting || isRejecting"
                @click.stop="handleAccept"
              >
                <Spinner v-if="isAccepting" class="w-2.5 h-2.5 text-green-600" />
                <Icon v-else name="heroicons:check" class="w-2.5 h-2.5" />
                {{ $t('tools.createInstruction.accept', 'Accept') }}
              </button>
              <button
                class="inline-flex items-center gap-1 px-2 py-0.5 text-[11px] font-medium text-gray-600 dark:text-gray-400 bg-white dark:bg-gray-900 border border-gray-200 dark:border-gray-700 rounded hover:bg-gray-50 dark:hover:bg-gray-800 transition-colors disabled:opacity-50"
                :disabled="isAccepting || isRejecting"
                @click.stop="handleReject"
              >
                <Spinner v-if="isRejecting" class="w-2.5 h-2.5 text-gray-400" />
                <Icon v-else name="heroicons:x-mark" class="w-2.5 h-2.5" />
                {{ $t('tools.createInstruction.reject', 'Reject') }}
              </button>
            </template>
            <template v-else>
              <Icon name="heroicons:clock" class="w-3 h-3 text-gray-400" />
              <span class="text-[10px] font-medium text-gray-500 dark:text-gray-400">
                {{ $t('tools.createInstruction.stagedInBuild', 'Staged in draft build') }}
              </span>
            </template>
            <!-- Resolved evals for this agent, pinned to the draft build -->
            <ResolvedEvalStrip v-if="buildId" :instruction-id="instructionId" :build-id="buildId" class="ms-auto" />
          </div>

          <!-- Error message -->
          <div v-if="errorMessage" class="text-[10px] text-red-500 bg-red-50/50 dark:bg-red-950 rounded px-2 py-1 mt-2">
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
import { computed, ref, watch, onMounted, onBeforeUnmount } from 'vue'
import { useI18n } from 'vue-i18n'
import InstructionModalComponent from '~/components/InstructionModalComponent.vue'
import Spinner from '~/components/Spinner.vue'
import ResolvedEvalStrip from '~/components/instructions/ResolvedEvalStrip.vue'
import { dispatchInstructionResolved, INSTRUCTION_RESOLVED_EVENT } from '~/composables/useTrackedChanges'

const { t } = useI18n()

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
const isExpanded = ref(true)
const showInstructionModal = ref(false)
const editingInstruction = ref<any>(null)
const isPublishing = ref(false)
const isDeleting = ref(false)
const localGlobalStatus = ref<string | null>(null)
const isAccepting = ref(false)
const isRejecting = ref(false)
const resolution = ref<'accepted' | 'rejected' | null>(null)
const isCheckingResolution = ref(false)

async function handleAccept() {
  if (!buildId.value || !instructionId.value || isAccepting.value) return
  isAccepting.value = true
  try {
    // Promote just this staged instruction as a build-of-one; the shared
    // training draft (which holds the other create_instruction suggestions from
    // the same completion) stays a draft so its siblings remain acceptable.
    // NOT /builds/{id}/publish — that prunes the siblings and finalizes the
    // whole build, so the next accept 400s ("Build is already published").
    const { error } = await useMyFetch(`/instructions/${instructionId.value}/accept-staged`, {
      method: 'POST',
      body: { build_id: buildId.value },
    })
    if (!error.value) {
      resolution.value = 'accepted'
      localGlobalStatus.value = 'approved'
      dispatchInstructionResolved({
        instructionId: instructionId.value,
        buildId: buildId.value,
        action: 'accept',
      })
      toast.add({ title: t('tools.createInstruction.acceptedToast', 'Change accepted'), color: 'green' })
      emit('instruction-updated')
    } else {
      toast.add({ title: t('tools.createInstruction.acceptFailed', 'Failed to accept'), color: 'red' })
    }
  } finally {
    isAccepting.value = false
  }
}

async function handleReject() {
  if (!buildId.value || !instructionId.value || isRejecting.value) return
  isRejecting.value = true
  try {
    // Rejecting a brand-new instruction discards it entirely (it was never in
    // main). Deleting the instruction — rather than only detaching it from the
    // shared draft — keeps the resolved-state derivation honest: a refresh sees
    // "instruction gone → rejected" instead of "no longer pending → accepted".
    const { error } = await useMyFetch(
      `/instructions/${instructionId.value}`,
      { method: 'DELETE' },
    )
    if (!error.value) {
      resolution.value = 'rejected'
      dispatchInstructionResolved({
        instructionId: instructionId.value,
        buildId: buildId.value,
        action: 'reject',
      })
      toast.add({ title: t('tools.createInstruction.rejectedToast', 'Change rejected'), color: 'gray' })
      emit('instruction-updated')
    } else {
      toast.add({ title: t('tools.createInstruction.rejectFailed', 'Failed to reject'), color: 'red' })
    }
  } finally {
    isRejecting.value = false
  }
}

function onExternalResolution(e: Event) {
  const detail = (e as CustomEvent).detail
  if (!detail || !instructionId.value) return
  if (detail.instructionId === instructionId.value && resolution.value === null) {
    refreshResolutionState()
  }
}
onMounted(() => {
  if (typeof window !== 'undefined') {
    window.addEventListener(INSTRUCTION_RESOLVED_EVENT, onExternalResolution)
  }
})
onBeforeUnmount(() => {
  if (typeof window !== 'undefined') {
    window.removeEventListener(INSTRUCTION_RESOLVED_EVENT, onExternalResolution)
  }
})

const canCreateInstructions = computed(() => {
  return useCan('manage_instructions')
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
  if (!text) return t('tools.createInstruction.createdFallback')
  // Get first line and truncate if needed
  const firstLine = text.split('\n')[0].replace(/^#+\s*/, '').trim()
  if (firstLine.length > 60) {
    return firstLine.substring(0, 57) + '...'
  }
  return firstLine || t('tools.createInstruction.createdFallback')
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

const lineCount = computed(() => {
  const text = instructionText.value
  if (!text) return 0
  return text.split('\n').filter((l: string) => l.trim()).length
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

const buildId = computed<string | null>(() => {
  const rj = props.toolExecution?.result_json || {}
  return rj.build_id || null
})

const canResolve = computed(() => !!buildId.value && resolution.value === null && !isCheckingResolution.value)

// Derive resolution from server on mount so refresh doesn't show stale Accept/Reject.
// If our build_id isn't in pending-builds for this instruction, it's resolved.
// Distinguish by whether the instruction still exists (reject = deleted).
async function refreshResolutionState() {
  if (!instructionId.value || !buildId.value) return
  isCheckingResolution.value = true
  try {
    const { data: instData, error: instErr } = await useMyFetch(`/instructions/${instructionId.value}`)
    if (instErr.value || !instData.value) {
      resolution.value = 'rejected'
      return
    }
    const { data: pendingData } = await useMyFetch(`/instructions/${instructionId.value}/pending-builds`)
    const builds = Array.isArray(pendingData.value) ? pendingData.value : []
    const stillPending = builds.some((b: any) => b.build_id === buildId.value)
    if (!stillPending) resolution.value = 'accepted'
  } finally {
    isCheckingResolution.value = false
  }
}

watch(instructionId, (id) => {
  if (id && resolution.value === null) refreshResolutionState()
}, { immediate: true })

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
    return rj.error || rj.message || t('tools.createInstruction.errorOccurred')
  }
  if (isRejected.value) {
    const rj = props.toolExecution?.result_json || {}
    return rj.message || ''
  }
  return ''
})

function toggleExpanded() {
  if (status.value !== 'running' || instructionText.value) {
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
      toast.add({ title: t('tools.createInstruction.toastSuccess'), description: t('tools.createInstruction.toastPublished'), color: 'green' })
      emit('instruction-updated')
    } else {
      throw new Error('Failed to publish')
    }
  } catch (e) {
    console.error('Failed to publish instruction:', e)
    toast.add({ title: t('tools.createInstruction.toastError'), description: t('tools.createInstruction.toastFailedPublish'), color: 'red' })
  } finally {
    isPublishing.value = false
  }
}

async function handleDelete() {
  if (!instructionId.value) return
  if (!confirm(t('tools.createInstruction.confirmDelete'))) return

  isDeleting.value = true
  try {
    const { error } = await useMyFetch(`/instructions/${instructionId.value}`, { method: 'DELETE' })
    if (!error.value) {
      // Mark as deleted by setting a special status
      localGlobalStatus.value = 'deleted'
      toast.add({ title: t('tools.createInstruction.toastDeleted'), description: t('tools.createInstruction.toastDeletedDesc'), color: 'orange' })
      emit('instruction-updated')
    } else {
      throw new Error('Failed to delete')
    }
  } catch (e) {
    console.error('Failed to delete instruction:', e)
    toast.add({ title: t('tools.createInstruction.toastError'), description: t('tools.createInstruction.toastFailedDelete'), color: 'red' })
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
  toast.add({ title: t('tools.createInstruction.toastSuccess'), description: t('tools.createInstruction.toastSaved'), color: 'green' })
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
