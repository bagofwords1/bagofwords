<template>
  <div class="mt-1">
    <!-- Status header -->
    <Transition name="fade" appear>
      <div
        class="flex items-center text-xs text-gray-500 dark:text-gray-400 cursor-pointer hover:text-gray-700 dark:hover:text-gray-300"
        @click="toggleExpanded"
      >
        <span v-if="status === 'running'" class="tool-shimmer flex items-center">
          <Icon name="heroicons-cube" class="w-3 h-3 me-1.5 text-gray-400 dark:text-gray-500" />
          {{ $t('tools.editInstruction.editing') }}
        </span>
        <span v-else-if="isSuccess" class="text-gray-600 dark:text-gray-400 flex items-center">
          <Icon name="heroicons-cube" class="w-3 h-3 me-1.5 text-blue-500" />
          <span dir="auto" class="truncate max-w-[300px]">{{ $t('tools.editInstruction.editedPrefix', { text: truncatedText }) }}</span>
          <span v-if="versionNumber" class="ms-1.5 px-1.5 py-0.5 bg-blue-100 text-blue-700 rounded text-[10px] shrink-0">v{{ versionNumber }}</span>
          <span v-if="linesAdded > 0" class="ms-1.5 text-[10px] text-green-600 shrink-0">+{{ linesAdded }}</span>
          <span v-if="linesRemoved > 0" class="ms-0.5 text-[10px] text-red-500 shrink-0">-{{ linesRemoved }}</span>
          <Icon
            :name="isExpanded ? 'heroicons-chevron-down' : 'heroicons-chevron-right'"
            class="w-3 h-3 ms-1 text-gray-400 dark:text-gray-500 shrink-0 rtl-flip"
          />
        </span>
        <span v-else-if="isRejected" class="text-gray-600 dark:text-gray-400 flex items-center">
          <Icon name="heroicons-x-circle" class="w-3 h-3 me-1.5 text-orange-500" />
          <span>{{ $t('tools.editInstruction.rejected') }}</span>
          <span v-if="rejectedReason" class="ms-1.5 text-orange-600 text-[10px]">({{ rejectedReason }})</span>
          <Icon
            :name="isExpanded ? 'heroicons-chevron-down' : 'heroicons-chevron-right'"
            class="w-3 h-3 ms-1 text-gray-400 dark:text-gray-500 rtl-flip"
          />
        </span>
        <span v-else class="text-gray-600 dark:text-gray-400 flex items-center">
          <Icon name="heroicons-x-circle" class="w-3 h-3 me-1.5 text-red-500" />
          <span>{{ $t('tools.editInstruction.failed') }}</span>
          <Icon
            :name="isExpanded ? 'heroicons-chevron-down' : 'heroicons-chevron-right'"
            class="w-3 h-3 ms-1 text-gray-400 dark:text-gray-500 rtl-flip"
          />
        </span>
      </div>
    </Transition>

    <!-- Expandable content -->
    <Transition name="slide">
      <div v-if="isExpanded && status !== 'running'" class="mt-2 space-y-2">
        <!-- Loading state while fetching versions -->
        <div v-if="isLoadingVersions" class="flex items-center justify-center py-4">
          <Spinner class="w-4 h-4 me-2" />
          <span class="text-[11px] text-gray-500 dark:text-gray-400">{{ $t('tools.editInstruction.loadingDiff') }}</span>
        </div>

        <!-- Diff view when text was changed -->
        <div v-else-if="hasTextDiff && previousText !== null" class="border border-gray-150 rounded-md overflow-hidden">
          <div class="px-3 py-1.5 bg-gray-50 dark:bg-gray-900 border-b border-gray-150 flex items-center justify-between">
            <span class="text-[10px] text-gray-600 dark:text-gray-400 font-medium">{{ $t('tools.editInstruction.textChanges') }}</span>
            <span v-if="versionNumber" class="text-[10px] text-gray-500 dark:text-gray-400">v{{ versionNumber - 1 }} → v{{ versionNumber }}</span>
          </div>
          <div class="px-3 py-2 bg-white dark:bg-gray-900">
            <TrackedChangesView :diff-ops="diffOps" />
          </div>
        </div>

        <!-- Instruction card for non-text changes or when no diff -->
        <div v-else class="hover:bg-gray-50 dark:hover:bg-gray-800 border border-gray-150 rounded-md p-3 transition-colors">
          <!-- Instruction text - click to edit -->
          <div
            v-if="displayText"
            dir="auto"
            class="instruction-content text-[12px] text-gray-800 dark:text-gray-200 leading-relaxed mb-2 cursor-pointer"
            @click="handleEdit()"
          >
            <MDC :value="displayText" class="markdown-content" />
          </div>
        </div>

        <!-- Metadata changes summary -->
        <div v-if="metadataChanges.length > 0" class="text-[10px] text-gray-500 dark:text-gray-400 px-1">
          <span class="font-medium">{{ $t('tools.editInstruction.otherChanges') }}</span>
          {{ metadataChanges.join(', ') }}
        </div>

        <!-- Metadata row -->
        <div class="flex flex-wrap items-center gap-2 text-[10px] px-1">
          <!-- Category -->
          <span v-if="displayCategory" class="px-1.5 py-0.5 bg-gray-100 dark:bg-gray-800 text-gray-600 dark:text-gray-400 rounded text-[10px]">
            {{ displayCategory }}
          </span>

          <!-- Confidence -->
          <div v-if="displayConfidence" class="flex items-center gap-1">
            <span class="text-gray-500 dark:text-gray-400">{{ $t('tools.editInstruction.confidence') }}</span>
            <span
              class="font-medium"
              :class="displayConfidence >= 0.9 ? 'text-green-600' : displayConfidence >= 0.7 ? 'text-yellow-600' : 'text-red-600'"
            >
              {{ Math.round(displayConfidence * 100) }}%
            </span>
          </div>

          <!-- Load mode -->
          <div v-if="displayLoadMode" class="flex items-center gap-1">
            <span class="text-gray-500 dark:text-gray-400">{{ $t('tools.editInstruction.load') }}</span>
            <span class="px-1.5 py-0.5 rounded text-[9px] font-medium"
              :class="displayLoadMode === 'always' ? 'bg-blue-100 text-blue-700' : 'bg-gray-100 dark:bg-gray-800 text-gray-600 dark:text-gray-400'"
            >
              {{ displayLoadMode }}
            </span>
          </div>

          <!-- Tables scoped -->
          <div v-if="displayTableCount > 0" class="flex items-center gap-1">
            <Icon name="heroicons-table-cells" class="w-3 h-3 text-gray-400 dark:text-gray-500" />
            <span class="text-gray-600 dark:text-gray-400">{{ displayTableCount === 1 ? $t('tools.editInstruction.tableSingular', { n: displayTableCount }) : $t('tools.editInstruction.tablePlural', { n: displayTableCount }) }}</span>
          </div>
        </div>

        <!-- Status + Accept/Reject actions -->
        <div v-if="isSuccess && instructionId" class="flex items-center gap-1.5 pt-2 border-t border-gray-100 dark:border-gray-800 px-1">
          <template v-if="resolution === 'accepted'">
            <Icon name="heroicons:check-circle" class="w-3 h-3 text-green-500" />
            <span class="text-[10px] font-medium text-gray-600 dark:text-gray-400">{{ $t('tools.editInstruction.accepted', 'Accepted') }}</span>
          </template>
          <template v-else-if="resolution === 'rejected'">
            <Icon name="heroicons:x-circle" class="w-3 h-3 text-gray-400 dark:text-gray-500" />
            <span class="text-[10px] text-gray-400 dark:text-gray-500">{{ $t('tools.editInstruction.rejectedLabel', 'Rejected') }}</span>
          </template>
          <template v-else-if="canResolve">
            <button
              class="inline-flex items-center gap-1 px-2 py-0.5 text-[11px] font-medium text-green-700 bg-green-50 dark:bg-green-950 border border-green-200 rounded hover:bg-green-100 transition-colors disabled:opacity-50"
              :disabled="isAccepting || isRejecting"
              @click.stop="handleAccept"
            >
              <Spinner v-if="isAccepting" class="w-2.5 h-2.5 text-green-600" />
              <Icon v-else name="heroicons:check" class="w-2.5 h-2.5" />
              {{ $t('tools.editInstruction.accept', 'Accept') }}
            </button>
            <button
              class="inline-flex items-center gap-1 px-2 py-0.5 text-[11px] font-medium text-gray-600 dark:text-gray-400 bg-white dark:bg-gray-900 border border-gray-200 dark:border-gray-700 rounded hover:bg-gray-50 dark:hover:bg-gray-800 transition-colors disabled:opacity-50"
              :disabled="isAccepting || isRejecting"
              @click.stop="handleReject"
            >
              <Spinner v-if="isRejecting" class="w-2.5 h-2.5 text-gray-400 dark:text-gray-500" />
              <Icon v-else name="heroicons:x-mark" class="w-2.5 h-2.5" />
              {{ $t('tools.editInstruction.reject', 'Reject') }}
            </button>
          </template>
          <template v-else>
            <Icon name="heroicons:clock" class="w-3 h-3 text-gray-400 dark:text-gray-500" />
            <span class="text-[10px] font-medium text-gray-500 dark:text-gray-400">
              {{ $t('tools.editInstruction.stagedInBuild', 'Staged in draft build') }}
            </span>
          </template>
        </div>

        <!-- Error message -->
        <div v-if="errorMessage" class="text-[10px] text-red-500 bg-red-50/50 dark:bg-red-950 rounded px-2 py-1">
          {{ errorMessage }}
        </div>
      </div>
    </Transition>
  </div>
</template>

<script setup lang="ts">
import { computed, ref, watch, onMounted, onBeforeUnmount } from 'vue'
import { useI18n } from 'vue-i18n'
import DiffMatchPatch from 'diff-match-patch'
import Spinner from '~/components/Spinner.vue'
import TrackedChangesView from '~/components/instructions/TrackedChangesView.vue'
import {
  dispatchInstructionResolved,
  INSTRUCTION_RESOLVED_EVENT,
  type DiffOp,
  type DiffOpType,
} from '~/composables/useTrackedChanges'

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
  (e: 'openInstruction', id: string, opts?: { initialVersionNumber?: number | null }): void
}>()

const isExpanded = ref(true)
const localGlobalStatus = ref<string | null>(null)
const isLoadingVersions = ref(false)
const fetchedInstruction = ref<any>(null)
const previousText = ref<string | null>(null)
const isAccepting = ref(false)
const isRejecting = ref(false)
const resolution = ref<'accepted' | 'rejected' | null>(null)
const isCheckingResolution = ref(false)
const toast = useToast()

async function handleAccept() {
  if (!buildId.value || !instructionId.value || isAccepting.value) return
  isAccepting.value = true
  try {
    const { error } = await useMyFetch(`/builds/${buildId.value}/publish`, {
      method: 'POST',
      body: { instruction_ids: [instructionId.value] },
    })
    if (!error.value) {
      resolution.value = 'accepted'
      localGlobalStatus.value = 'approved'
      dispatchInstructionResolved({
        instructionId: instructionId.value,
        buildId: buildId.value,
        action: 'accept',
      })
      toast.add({ title: t('tools.editInstruction.acceptedToast', 'Change accepted'), color: 'green' })
      emit('instruction-updated')
    } else {
      toast.add({ title: t('tools.editInstruction.acceptFailed', 'Failed to accept'), color: 'red' })
    }
  } finally {
    isAccepting.value = false
  }
}

async function handleReject() {
  if (!buildId.value || !instructionId.value || isRejecting.value) return
  isRejecting.value = true
  try {
    const { error } = await useMyFetch(
      `/builds/${buildId.value}/contents/${instructionId.value}`,
      { method: 'DELETE' },
    )
    if (!error.value) {
      resolution.value = 'rejected'
      dispatchInstructionResolved({
        instructionId: instructionId.value,
        buildId: buildId.value,
        action: 'reject',
      })
      toast.add({ title: t('tools.editInstruction.rejectedToast', 'Change rejected'), color: 'gray' })
      emit('instruction-updated')
    } else {
      toast.add({ title: t('tools.editInstruction.rejectFailed', 'Failed to reject'), color: 'red' })
    }
  } finally {
    isRejecting.value = false
  }
}

// Stay in sync if someone else (modal, pill, another tool card) resolves
// the same instruction.
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

const buildId = computed<string | null>(() => {
  const rj = props.toolExecution?.result_json || {}
  return rj.build_id || null
})

const canResolve = computed(() =>
  !!buildId.value && !!instructionId.value && resolution.value === null && !isCheckingResolution.value
)

// Derive resolution from server on mount / id change so refreshes don't show stale buttons.
// Pending = our build_id is still in /pending-builds for this instruction.
// Else: compare current instruction.text to the tool's updated text — match = accepted, mismatch = rejected.
async function refreshResolutionState() {
  if (!instructionId.value || !buildId.value) return
  isCheckingResolution.value = true
  try {
    const { data: pendingData } = await useMyFetch(`/instructions/${instructionId.value}/pending-builds`)
    const builds = Array.isArray(pendingData.value) ? pendingData.value : []
    const stillPending = builds.some((b: any) => b.build_id === buildId.value)
    if (stillPending) return
    const { data: instData, error: instErr } = await useMyFetch(`/instructions/${instructionId.value}`)
    if (instErr.value || !instData.value) {
      resolution.value = 'rejected'
      return
    }
    const liveText = ((instData.value as any).text || '').trim()
    const proposedText = (updatedText.value || '').trim()
    resolution.value = proposedText && liveText === proposedText ? 'accepted' : 'rejected'
  } finally {
    isCheckingResolution.value = false
  }
}

watch(instructionId, (id) => {
  if (id && resolution.value === null) refreshResolutionState()
}, { immediate: true })

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

// Line diff counts for summary
const linesAdded = computed(() => {
  if (!updatedText.value || previousText.value === null) return 0
  const newLines = updatedText.value.split('\n')
  const oldLines = (previousText.value || '').split('\n')
  return Math.max(0, newLines.length - oldLines.length)
})

const linesRemoved = computed(() => {
  if (!updatedText.value || previousText.value === null) return 0
  const newLines = updatedText.value.split('\n')
  const oldLines = (previousText.value || '').split('\n')
  return Math.max(0, oldLines.length - newLines.length)
})

// Check if text was changed
const hasTextDiff = computed(() => {
  return updatedText.value !== null && versionNumber.value !== null
})

// Current text (after edit)
const currentText = computed(() => {
  return updatedText.value || fetchedInstruction.value?.text || ''
})

// Inline diff ops (previousText → currentText) for TrackedChangesView.
const diffOps = computed<DiffOp[]>(() => {
  const base = previousText.value || ''
  const next = currentText.value || ''
  if (base === next) return [{ type: 0 as DiffOpType, text: base }]
  const dmp = new DiffMatchPatch()
  const ops = dmp.diff_main(base, next)
  dmp.diff_cleanupSemantic(ops)
  return ops.map(([type, text]) => ({ type: type as DiffOpType, text }))
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
  if (updatedCategory.value) changes.push(t('tools.editInstruction.changeCategory', { value: updatedCategory.value }))
  if (updatedConfidence.value !== null) changes.push(t('tools.editInstruction.changeConfidence', { value: Math.round(updatedConfidence.value * 100) }))
  if (updatedLoadMode.value) changes.push(t('tools.editInstruction.changeLoadMode', { value: updatedLoadMode.value }))
  if (updatedTableNames.value) changes.push(t('tools.editInstruction.changeTables'))
  if (updatedEvidence.value) changes.push(t('tools.editInstruction.changeEvidence'))
  return changes
})

const truncatedText = computed(() => {
  const text = displayText.value
  if (!text) return t('tools.editInstruction.editedFallback')
  const firstLine = text.split('\n')[0].replace(/^#+\s*/, '').trim()
  if (firstLine.length > 60) {
    return firstLine.substring(0, 57) + '...'
  }
  return firstLine || t('tools.editInstruction.editedFallback')
})

const errorMessage = computed(() => {
  if (status.value === 'error') {
    const rj = props.toolExecution?.result_json || {}
    return rj.error || rj.message || t('tools.editInstruction.errorOccurred')
  }
  if (isRejected.value) {
    const rj = props.toolExecution?.result_json || {}
    return rj.message || ''
  }
  return ''
})

// Watch instructionId too — it lands after mount with result_json, so a single watch on isExpanded misses it.
watch([isExpanded, instructionId], async ([expanded, id]) => {
  if (expanded && id && previousText.value === null) {
    await fetchVersionsForDiff()
  }
}, { immediate: true })

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
  // Open the right-pane instruction view with this tool's edited version
  // preselected so the user immediately sees a diff against the current
  // version. The pane resolves the version_number to a version_id from its
  // already-loaded version list.
  emit('openInstruction', instructionId.value, {
    initialVersionNumber: versionNumber.value ?? null,
  })
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
