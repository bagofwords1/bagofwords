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
        <!-- Same row shape as ReadInstructionTool: chevron first, cube icon,
             "Edit instruction · TITLE" — the two block kinds read as one
             family in the transcript. -->
        <span v-else-if="isSuccess" class="text-gray-600 dark:text-gray-400 flex items-center">
          <Icon
            :name="isExpanded ? 'heroicons-chevron-down' : 'heroicons-chevron-right'"
            class="w-3 h-3 me-1 text-gray-400 flex-shrink-0 rtl-flip"
          />
          <Icon name="heroicons-cube" class="w-3 h-3 me-1 text-indigo-400 flex-shrink-0" />
          <span dir="auto" class="text-gray-700 dark:text-gray-300 truncate max-w-[300px]">
            {{ $t('tools.editInstruction.editLabel') }}<template v-if="headerTitle"> · {{ headerTitle }}</template>
          </span>
          <span v-if="versionNumber" class="ms-1.5 px-1.5 py-0.5 bg-blue-100 text-blue-700 rounded text-[10px] shrink-0">v{{ versionNumber }}</span>
          <!-- Grouped card: this card stands for every edit call of one
               (instruction, build) run; the count streams up in real time as
               later calls land, like other actions' live counters. -->
          <span v-if="(editGroupCount || 0) > 1" class="ms-1.5 px-1.5 py-0.5 bg-gray-100 dark:bg-gray-800 text-gray-600 dark:text-gray-300 rounded text-[10px] shrink-0">{{ $t('tools.editInstruction.editCount', { n: editGroupCount }) }}</span>
          <span v-if="linesAdded > 0" class="ms-1.5 text-[10px] text-green-600 shrink-0">+{{ linesAdded }}</span>
          <span v-if="linesRemoved > 0" class="ms-0.5 text-[10px] text-red-500 shrink-0">-{{ linesRemoved }}</span>
        </span>
        <span v-else-if="isRejected" class="text-gray-600 dark:text-gray-400 flex items-center">
          <Icon
            :name="isExpanded ? 'heroicons-chevron-down' : 'heroicons-chevron-right'"
            class="w-3 h-3 me-1 text-gray-400 flex-shrink-0 rtl-flip"
          />
          <Icon name="heroicons-exclamation-triangle" class="w-3 h-3 me-1 text-amber-500 flex-shrink-0" />
          <span>{{ $t('tools.editInstruction.rejected') }}</span>
          <span v-if="rejectedReason" class="ms-1.5 text-amber-600 dark:text-amber-500 text-[10px]">({{ prettyRejectedReason }})</span>
        </span>
        <span v-else class="text-gray-600 dark:text-gray-400 flex items-center">
          <Icon
            :name="isExpanded ? 'heroicons-chevron-down' : 'heroicons-chevron-right'"
            class="w-3 h-3 me-1 text-gray-400 flex-shrink-0 rtl-flip"
          />
          <Icon name="heroicons-x-circle" class="w-3 h-3 me-1 text-red-500 flex-shrink-0" />
          <span>{{ $t('tools.editInstruction.failed') }}</span>
        </span>
      </div>
    </Transition>

    <!-- Expandable content -->
    <Transition name="slide">
      <div v-if="isExpanded && status !== 'running'" class="mt-2 space-y-2">
        <!-- ONE spinner until this card's FINAL presentation is known, then a
             single render. Anything shown earlier repaints later — verdicts
             land, hunks load, streamed calls mutate the data — and every
             repaint is the reported flicker. So: spinner while the turn
             streams, while the verdict is unresolved, and while a pending
             card's review panel is still loading (it mounts invisibly below).
             Only then does content appear, and it never changes again. -->
        <div v-if="turnActive || awaitingFinal" class="flex items-center justify-center py-4 border border-gray-150 dark:border-gray-800 rounded-md">
          <Spinner class="w-4 h-4 me-2" />
          <span class="text-[11px] text-gray-500 dark:text-gray-400">{{ $t('tools.editInstruction.loadingDiff') }}</span>
        </div>

        <!-- Pending: per-hunk tracked-changes review (inline accept/reject,
             collapsed to changed regions). Same component as the editor.
             Mounts as soon as the verdict reads pending, but stays hidden
             (v-show) behind the spinner above until its first load lands. -->
        <div v-if="!turnActive && canResolve && instructionId" v-show="panelReady" class="border border-gray-150 dark:border-gray-800 rounded-md overflow-hidden">
          <InstructionTrackedChanges
            ref="trackedChangesRef"
            :instruction-id="instructionId"
            :build-id="buildId || undefined"
            :can-approve="canManageInstruction"
            compact
            collapse-context
            @changed="onInlineResolved"
            @empty="resolution = resolution || 'accepted'"
            @loaded="panelReady = true"
          />
        </div>

        <!-- Resolved / read-only: show the version diff -->
        <div v-else-if="!turnActive && !awaitingFinal && hasTextDiff && previousText !== null" class="border border-gray-150 dark:border-gray-800 rounded-md overflow-hidden">
          <div class="px-3 py-1.5 bg-gray-50 dark:bg-gray-900 border-b border-gray-150 dark:border-gray-800 flex items-center justify-between">
            <span class="text-[10px] text-gray-600 dark:text-gray-400 font-medium">{{ $t('tools.editInstruction.textChanges') }}</span>
            <!-- Only the resulting version is known. The parent is NOT
                 versionNumber - 1: that counter is bumped by every path, so
                 the version before this one by number is often not the one
                 this edit was based on. -->
            <span v-if="versionNumber" class="text-[10px] text-gray-500 dark:text-gray-400">v{{ versionNumber }}</span>
          </div>
          <!-- Same clamp as the pending review panel above (its compact mode is
               `px-3 py-2 max-h-80`), on purpose: resolving a suggestion swaps
               this body in for that one, and any other number would make the
               card jump height at that moment. Until this, the resolved branches
               were the only ones with no bound — a long instruction rendered in
               full, so a transcript of edits was a transcript of whole
               documents. Clamped, never truncated: the diff is only meaningful
               whole, so the rest scrolls. -->
          <div class="px-3 py-2 bg-white dark:bg-gray-900 max-h-80 overflow-y-auto">
            <TrackedChangesView :diff-ops="diffOps" />
          </div>
        </div>

        <!-- Instruction card for non-text changes or when no diff. Requires the
             text: the wrapper used to render whenever there was no diff, so a
             card whose only child is `v-if="displayText"` drew an empty bordered
             box whenever that text wasn't there — a rejected edit (no
             new_text), or any card whose instruction fetch hadn't landed. -->
        <div v-else-if="!turnActive && !awaitingFinal && displayText && !isUnsuccessful" class="hover:bg-gray-50 dark:hover:bg-gray-800/50 border border-gray-150 dark:border-gray-800 rounded-md p-3 transition-colors">
          <!-- Instruction text - click to edit -->
          <div
            dir="auto"
            class="instruction-content text-[12px] text-gray-800 dark:text-gray-200 leading-relaxed mb-2 cursor-pointer max-h-80 overflow-y-auto"
            @click="handleEdit()"
          >
            <InstructionText :text="displayText" :markdown="true" />
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

        <!-- Status row (accepted / rejected / staged). When still resolvable the
             embedded per-hunk review shows the state + inline actions instead. -->
        <div v-if="isSuccess && instructionId && !canResolve" class="flex items-center gap-1.5 pt-2 border-t border-gray-100 dark:border-gray-800 px-1">
          <template v-if="resolution === 'accepted'">
            <Icon name="heroicons:check-circle" class="w-3 h-3 text-green-500" />
            <span class="text-[10px] font-medium text-gray-600 dark:text-gray-400">{{ $t('tools.editInstruction.accepted', 'Accepted') }}</span>
          </template>
          <template v-else-if="resolution === 'rejected'">
            <Icon name="heroicons:x-circle" class="w-3 h-3 text-gray-400 dark:text-gray-500" />
            <span class="text-[10px] text-gray-400 dark:text-gray-500">{{ $t('tools.editInstruction.rejectedLabel', 'Rejected') }}</span>
          </template>
          <template v-else-if="canResolve">
            <!-- Accept/reject is handled inline per-hunk by the embedded review. -->
          </template>
          <template v-else>
            <Icon name="heroicons:clock" class="w-3 h-3 text-gray-400 dark:text-gray-500" />
            <span class="text-[10px] font-medium text-gray-500 dark:text-gray-400">
              {{ $t('tools.editInstruction.stagedInBuild', 'Staged in draft build') }}
            </span>
          </template>
        </div>

        <!-- Resolved evals for this agent, pinned to the draft build this edit
             was staged into (training-mode self-check). -->
        <div v-if="isSuccess && instructionId && buildId" class="mt-1.5 px-1">
          <ResolvedEvalStrip :instruction-id="instructionId" :build-id="buildId" :agent-ids="reportAgentIds" :report-id="reportId" />
        </div>

        <!-- Reason. Amber for a rejection — the edit was refused, nothing
             broke and nothing changed; red stays for an actual tool error. -->
        <div
          v-if="errorMessage"
          class="text-[11px] rounded px-2 py-1.5 leading-relaxed"
          :class="isRejected
            ? 'text-amber-800 dark:text-amber-200 bg-amber-50 dark:bg-amber-500/10 border border-amber-200 dark:border-amber-500/20'
            : 'text-red-600 dark:text-red-300 bg-red-50/50 dark:bg-red-500/10 border border-red-100 dark:border-red-500/20'"
        >
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
import ResolvedEvalStrip from '~/components/instructions/ResolvedEvalStrip.vue'
import InstructionText from '~/components/instructions/InstructionText.vue'
import TrackedChangesView from '~/components/instructions/TrackedChangesView.vue'
import InstructionTrackedChanges from '~/components/instructions/InstructionTrackedChanges.vue'
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
  /** Grouped card: number of edit calls this card stands for (>=2 when the
      earlier sibling cards of the same (instruction, build) run are folded
      behind this one). Live — grows as later calls stream in. */
  editGroupCount?: number
  /** new_text / version_number of the run's LAST member. The FIRST card
      anchors the group (so the mounted card never changes identity while
      calls stream in) and these carry the run's final state to it: the
      read-only diff spans first base -> last result, and the version chip
      shows where the run ended. Live — updated as later calls land. */
  editGroupLastNewText?: string
  editGroupLastVersion?: number
  /** True while this card's turn is still streaming (or its blocks are the
      streamed objects not yet replaced by the post-run refetch). The card
      then renders ONLY a spinner box — no diff, no verdict fetch, no review
      panel — so nothing on screen can change shape mid-run. The final state
      renders exactly once, from the refetched blocks. */
  turnActive?: boolean
  /** The conversation's agents. Only used to scope the eval strip when the
      edited instruction is global (it then has no agents of its own). */
  dataSources?: any[]
  /** The conversation, so a run started from the eval strip reports its
      result back into this thread when it finishes. */
  reportId?: string | null
}

const props = defineProps<Props>()
const emit = defineEmits<{
  (e: 'instruction-updated'): void
  (e: 'openInstruction', id: string, opts?: { initialVersionNumber?: number | null }): void
}>()

// Agents of the conversation, for the eval strip's global-instruction fallback.
const reportAgentIds = computed<string[]>(() =>
  (props.dataSources || []).map((d: any) => String(d?.id || '')).filter(Boolean)
)

const isExpanded = ref(true)
// Set once the reader clicks this card's header: from then on its open/closed
// state is theirs, and nothing auto-collapses it out from under them.
const userToggled = ref(false)
const localGlobalStatus = ref<string | null>(null)
const isLoadingVersions = ref(false)
const fetchedInstruction = ref<any>(null)

// The instruction text this edit was written against, and what it produced.
// BOTH come from the tool's own result — it is the only thing that knows.
// They used to be re-derived on the client: `previousText` by fetching the
// version numbered `versionNumber - 1`, and the "after" side from
// `arguments_json.text`. Both were wrong.
//
// version_number is a per-instruction counter bumped by EVERY path — other
// sessions' proposals (accepted or not), direct user edits, accept promotions
// — so "the version before mine" by number is routinely not the version my
// edit was based on. And `arguments_json.text` is the INPUT: with anchored
// edits it is a snippet, not a document, so diffing a whole previous version
// against it rendered the entire instruction as deleted.
const previousText = computed<string | null>(() => {
  // The anchor is the group's FIRST member, so its own previous_text is
  // already the base the whole run started from.
  const rj = props.toolExecution?.result_json || {}
  return typeof rj.previous_text === 'string' ? rj.previous_text : null
})

// The resulting instruction text after this edit — for a grouped card, after
// the run's LAST edit, so the read-only diff spans the whole run.
const resultText = computed<string | null>(() => {
  if ((props.editGroupCount || 0) > 1 && typeof props.editGroupLastNewText === 'string') {
    return props.editGroupLastNewText
  }
  const rj = props.toolExecution?.result_json || {}
  return typeof rj.new_text === 'string' ? rj.new_text : null
})
const isAccepting = ref(false)
const isRejecting = ref(false)
const resolution = ref<'accepted' | 'rejected' | null>(null)
const isCheckingResolution = ref(false)
const toast = useToast()

async function handleAccept() {
  if (!canManageInstruction.value || !buildId.value || !instructionId.value || isAccepting.value) return
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

// A hunk was accepted/rejected inline via the embedded review — refresh the
// resolution state and tell the panel to refresh its instruction list.
async function onInlineResolved() {
  emit('instruction-updated')
  // Broadcast, like handleReject does. Resolving a hunk inside this panel moves
  // main, which invalidates every OTHER pending suggestion's rebased diff on
  // this instruction — without this they keep rendering a comparison against a
  // version that no longer exists.
  dispatchInstructionResolved({
    instructionId: instructionId.value,
    buildId: buildId.value,
    action: 'accept',
  })
  await refreshResolutionState()
}

async function handleReject() {
  if (!canManageInstruction.value || !buildId.value || !instructionId.value || isRejecting.value) return
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
const trackedChangesRef = ref<any>(null)

// A later edit call of this run landed (the group count grew): the staged
// suggestion gained hunks the mounted panel hasn't seen. Refresh them
// silently — an in-place data swap, not the blanking 'Loading…' reload.
watch(() => props.editGroupCount, (n, old) => {
  if ((n || 0) > (old || 0) && panelReady.value) {
    trackedChangesRef.value?.reload?.({ silent: true })
  }
})

// Something elsewhere resolved a change on this instruction. Two things go
// stale here, and only the first was being refreshed:
//   - whether THIS suggestion is now resolved, and
//   - the hunks this panel is showing. They are rebased against main, so once
//     main moves every "before" side is wrong — the panel keeps offering a
//     comparison against a version that no longer exists, and accepting from
//     it would overwrite whatever landed in between.
function onExternalResolution(e: Event) {
  const detail = (e as CustomEvent).detail
  if (!detail || !instructionId.value) return
  if (detail.instructionId !== instructionId.value) return
  if (resolution.value === null) refreshResolutionState()
  // Skip our own build: the panel already reloaded itself after that action.
  if (detail.buildId && buildId.value && detail.buildId === buildId.value) return
  trackedChangesRef.value?.reload?.()
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

// The detail fetch carries the instruction's complete agent scope. Permission
// checks must use that scope: a per-agent manager may review their instruction,
// while a shared instruction still requires authority over every attached
// agent and a global instruction remains org-admin only.
const canManageInstruction = computed(() =>
  useCanManageInstruction(fetchedInstruction.value)
)

const status = computed<string>(() => props.toolExecution?.status || '')

const isSuccess = computed(() => {
  const rj = props.toolExecution?.result_json || {}
  return status.value === 'success' && rj.success === true
})

const isRejected = computed(() => {
  const rj = props.toolExecution?.result_json || {}
  return status.value === 'success' && rj.success === false && rj.rejected_reason
})

// A rejected or failed edit changed nothing, so its body is noise: there is no
// diff to read and no action to take, only the reason — which the header
// already carries. It opens collapsed, and a card that fails mid-stream
// collapses when the verdict lands rather than leaving a spent block open.
const isUnsuccessful = computed(() => !!isRejected.value || status.value === 'error')
watch(isUnsuccessful, (bad) => {
  if (bad && !userToggled.value) isExpanded.value = false
}, { immediate: true })

// Extract from arguments_json (input) - these are the updates applied.
// NOTE: there is deliberately no `updatedText` here. `arguments_json.text` is
// the edit's INPUT — with an anchored edit a snippet, not a document — so it
// must never be used as the "after" side of a diff or compared to live text.
// Use `resultText` (result_json.new_text) for that.
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

// The interactive review panel mounts ONLY after the server confirmed this
// build is still pending. It used to mount by default (resolution starts
// null), so on a report with many historical edit cards EVERY card briefly
// rendered a loading review panel, the verdict then landed and swapped each
// body to the read-only diff — a cascade of visible content swaps a second or
// two after the page looked settled. Now the stable read-only diff (rendered
// straight from result_json, no fetch) is the default for every card, and the
// one genuinely pending card upgrades to the panel once — an addition of
// buttons, never content vanishing. 'unknown' (resolved before verdicts were
// recorded) deliberately stays read-only instead of probing hunks to find out.
const serverVerdict = ref<'pending' | 'accepted' | 'rejected' | 'unknown' | null>(null)
// True once the interactive panel has content on screen. Until then the card
// keeps its previous stable view (read-only diff / spinner) — one swap total.
const panelReady = ref(false)

const canResolve = computed(() =>
  !!buildId.value && !!instructionId.value && resolution.value === null
  && serverVerdict.value === 'pending'
)

// The card's final presentation is not yet decidable: the verdict fetch is
// still out, or the verdict said pending and the review panel hasn't loaded.
// The template shows one spinner for the whole window — never intermediate
// content that a later answer would repaint. Verdict-less cards (failed edits,
// rows with no build) are decidable immediately and skip this entirely.
const awaitingFinal = computed(() => {
  if (!isSuccess.value || !instructionId.value || !buildId.value) return false
  if (resolution.value !== null) return false
  if (serverVerdict.value === null) return true
  return serverVerdict.value === 'pending' && !panelReady.value
})

// Ask the server what a reviewer actually decided, so refreshes don't show
// stale buttons — and, more importantly, so the label is a RECORD rather than
// a guess. This used to be inferred: pending if our build was still in
// /pending-builds, otherwise "does live instruction text equal our proposal?"
// with any mismatch meaning rejected. That reported "rejected" for a
// suggestion nobody had reviewed yet, for one accepted before main moved on,
// and — because a failed fetch fell through to the same branch — for a network
// blip. 'unknown' is now a real answer: a build resolved before verdicts were
// recorded has none, and leaving the label off beats inventing one.
async function refreshResolutionState() {
  if (!instructionId.value || !buildId.value) return
  isCheckingResolution.value = true
  try {
    const { data, error } = await useMyFetch(
      `/instructions/${instructionId.value}/builds/${buildId.value}/verdict`
    )
    if (error.value || !data.value) {
      // A failed fetch must still resolve the card — 'unknown' renders the
      // read-only diff. Leaving null would hold the awaiting-final spinner
      // forever.
      serverVerdict.value = serverVerdict.value || 'unknown'
      return
    }
    const status = (data.value as any).status
    serverVerdict.value = status || 'unknown'
    if (status === 'accepted' || status === 'rejected') {
      resolution.value = status
    }
  } finally {
    isCheckingResolution.value = false
  }
}

watch([instructionId, () => props.turnActive], ([id, active]) => {
  // No verdict traffic while the turn is streaming: the answer would be
  // stale one call later, and its arrival is what used to mount the panel
  // mid-run. The fetch fires once, when the card settles (turnActive off).
  if (id && !active && resolution.value === null && serverVerdict.value === null) {
    refreshResolutionState()
  }
}, { immediate: true })

const versionNumber = computed(() => {
  // Grouped card: show where the run ENDED, not the first call's version.
  if ((props.editGroupCount || 0) > 1 && props.editGroupLastVersion) {
    return props.editGroupLastVersion
  }
  const rj = props.toolExecution?.result_json || {}
  return rj.version_number || null
})

const rejectedReason = computed(() => {
  const rj = props.toolExecution?.result_json || {}
  return rj.rejected_reason || ''
})

// `ambiguous_anchor` is a code for the model; the header is read by a person.
const prettyRejectedReason = computed(() => rejectedReason.value.replace(/_/g, ' '))

const currentGlobalStatus = computed(() => {
  if (localGlobalStatus.value !== null) return localGlobalStatus.value
  return fetchedInstruction.value?.global_status || null
})

// Line diff counts for summary
const linesAdded = computed(() => {
  if (resultText.value === null || previousText.value === null) return 0
  const newLines = resultText.value.split('\n')
  const oldLines = (previousText.value || '').split('\n')
  return Math.max(0, newLines.length - oldLines.length)
})

const linesRemoved = computed(() => {
  if (resultText.value === null || previousText.value === null) return 0
  const newLines = resultText.value.split('\n')
  const oldLines = (previousText.value || '').split('\n')
  return Math.max(0, oldLines.length - newLines.length)
})

// Check if text was changed
const hasTextDiff = computed(() => {
  return resultText.value !== null && previousText.value !== null
})

// Current text (after edit)
const currentText = computed(() => {
  return resultText.value ?? fetchedInstruction.value?.text ?? ''
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
  return fetchedInstruction.value?.text || resultText.value || ''
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

// Header identity, mirroring ReadInstructionTool: prefer the instruction's
// TITLE over a text excerpt; fall back to the first line of the edited text
// for old/untitled rows.
const headerTitle = computed(() => {
  const title = props.toolExecution?.result_json?.title
  return (typeof title === 'string' && title.trim()) ? title.trim() : truncatedText.value
})

// The rejection message is written for the MODEL: it restates how to retry and
// then quotes the entire current instruction text so the next call can anchor on
// it. Printing that verbatim turned a one-line "your anchor wasn't unique" into
// a wall of red containing the whole instruction — which the card renders above
// anyway. Keep the actionable sentence, drop the quoted document and the
// "Edit rejected:" prefix the header already says.
const stripEchoedText = (message: string) => {
  const cut = message.split(/\s*Current instruction text:/)[0]
  return cut.replace(/^\s*Edit rejected:\s*/, '').trim()
}

const errorMessage = computed(() => {
  if (status.value === 'error') {
    const rj = props.toolExecution?.result_json || {}
    return stripEchoedText(rj.error || rj.message || t('tools.editInstruction.errorOccurred'))
  }
  if (isRejected.value) {
    const rj = props.toolExecution?.result_json || {}
    return stripEchoedText(rj.message || '')
  }
  return ''
})

// Watch instructionId too — it lands after mount with result_json, so a single watch on isExpanded misses it.
watch([isExpanded, instructionId], async ([expanded, id]) => {
  if (expanded && id && fetchedInstruction.value === null) {
    await fetchInstruction()
  }
}, { immediate: true })

// Only the live instruction is fetched now — for the metadata card and the
// resolution check. The diff needs no fetching at all: both sides come from
// the tool's own result_json. This used to walk the version list to find
// `versionNumber - 1` and fetch its full text (two extra round trips per
// expanded block) to reconstruct a baseline the backend had already recorded,
// and got it wrong whenever that counter had been bumped by anything else.
async function fetchInstruction() {
  if (!instructionId.value) return

  isLoadingVersions.value = true
  try {
    const { data: instructionData, error: instructionError } = await useMyFetch(`/instructions/${instructionId.value}`)
    if (!instructionError.value && instructionData.value) {
      fetchedInstruction.value = instructionData.value
    }
  } catch (e) {
    console.error('Failed to fetch instruction:', e)
  } finally {
    isLoadingVersions.value = false
  }
}

function toggleExpanded() {
  if (status.value === 'running') return
  userToggled.value = true
  isExpanded.value = !isExpanded.value
  // A first fetch that failed (or ran before the id landed) left this null and
  // the guard below never retried it — the body then rendered empty for the
  // rest of the session. Reopening the card is the natural retry.
  if (isExpanded.value && instructionId.value && fetchedInstruction.value === null) {
    fetchInstruction()
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
