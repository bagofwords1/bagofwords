import { ref, computed, watch, onMounted, onBeforeUnmount, type Ref } from 'vue'
import DiffMatchPatch from 'diff-match-patch'

// Global cross-component sync: any accept/reject anywhere dispatches this
// event so other open views (pill, modal, tool cards) refetch their state.
// Detail shape: { instructionId, buildId, action: 'accept' | 'reject' }
export const INSTRUCTION_RESOLVED_EVENT = 'instruction:resolved'
export function dispatchInstructionResolved(detail: {
  instructionId: string
  buildId: string
  action: 'accept' | 'reject'
}) {
  if (typeof window === 'undefined') return
  window.dispatchEvent(new CustomEvent(INSTRUCTION_RESOLVED_EVENT, { detail }))
}

export interface PendingBuild {
  build_id: string
  build_number: number
  status: string
  source: string
  created_at: string | null
  created_by: { id: string; name: string | null } | null
  pending_version_id: string
  pending_version_number: number | null
  pending_text: string
  pending_title: string | null
  // false = a create suggestion (the instruction isn't in main yet). Those are
  // resolved by publishing/discarding the build; edits go through the per-hunk
  // cherry-pick endpoints so they stay consistent with the /agents review.
  in_main?: boolean
}

export type DiffOpType = -1 | 0 | 1
export interface DiffOp { type: DiffOpType; text: string }

export function useTrackedChanges(
  instructionId: Ref<string | null | undefined>,
  liveText: Ref<string>,
) {
  const pendingBuilds = ref<PendingBuild[]>([])
  const currentIndex = ref(0)
  const isLoading = ref(false)
  const isResolving = ref(false)

  const currentBuild = computed<PendingBuild | null>(() => {
    const list = pendingBuilds.value
    if (!list.length) return null
    const i = Math.min(currentIndex.value, list.length - 1)
    return list[i] || null
  })

  const hasPending = computed(() => pendingBuilds.value.length > 0)
  const pendingCount = computed(() => pendingBuilds.value.length)

  const diffOps = computed<DiffOp[]>(() => {
    const build = currentBuild.value
    if (!build) return []
    const base = liveText.value || ''
    const next = build.pending_text || ''
    if (base === next) return [{ type: 0, text: base }]
    const dmp = new DiffMatchPatch()
    const ops = dmp.diff_main(base, next)
    dmp.diff_cleanupSemantic(ops)
    return ops.map(([type, text]) => ({ type: type as DiffOpType, text }))
  })

  async function refresh() {
    const id = instructionId.value
    if (!id) {
      pendingBuilds.value = []
      return
    }
    isLoading.value = true
    try {
      const { data, error } = await useMyFetch(`/instructions/${id}/pending-builds`)
      if (!error.value && Array.isArray(data.value)) {
        pendingBuilds.value = data.value as PendingBuild[]
        if (currentIndex.value >= pendingBuilds.value.length) {
          currentIndex.value = 0
        }
      } else {
        pendingBuilds.value = []
      }
    } finally {
      isLoading.value = false
    }
  }

  // Edit suggestions (in_main === true) go through the per-hunk cherry-pick
  // endpoints (scoped to the suggestion's build) — the same resolution model as
  // the /agents review, so hunks rejected there can't be re-published from here.
  // Create suggestions (in_main === false) have no hunks against main, so they
  // resolve per-instruction too: accept promotes the staged version as a
  // build-of-one (accept-staged) and reject deletes the never-published
  // instruction. Both leave sibling creates in a shared draft untouched — unlike
  // whole-build publish/discard, which broke every accept after the first.
  async function accept() {
    const build = currentBuild.value
    const id = instructionId.value
    if (!build || !id || isResolving.value) return false
    isResolving.value = true
    try {
      const { error } = build.in_main === false
        ? await useMyFetch(`/instructions/${id}/accept-staged`, {
            method: 'POST',
            body: { build_id: build.build_id },
          })
        : await useMyFetch(`/instructions/${id}/hunks/accept-all`, {
            method: 'POST',
            body: { build_id: build.build_id },
          })
      if (error.value) return false
      dispatchInstructionResolved({ instructionId: id, buildId: build.build_id, action: 'accept' })
      await refresh()
      return true
    } finally {
      isResolving.value = false
    }
  }

  async function reject() {
    const build = currentBuild.value
    const id = instructionId.value
    if (!build || !id || isResolving.value) return false
    isResolving.value = true
    try {
      const { error } = build.in_main === false
        ? await useMyFetch(`/instructions/${id}`, { method: 'DELETE' })
        : await useMyFetch(`/instructions/${id}/hunks/reject-all`, {
            method: 'POST',
            body: { build_id: build.build_id },
          })
      if (error.value) return false
      dispatchInstructionResolved({ instructionId: id, buildId: build.build_id, action: 'reject' })
      await refresh()
      return true
    } finally {
      isResolving.value = false
    }
  }

  function next() {
    if (currentIndex.value < pendingBuilds.value.length - 1) currentIndex.value++
  }
  function prev() {
    if (currentIndex.value > 0) currentIndex.value--
  }

  watch(
    () => instructionId.value,
    () => {
      currentIndex.value = 0
      refresh()
    },
    { immediate: true },
  )

  // Refresh when anyone else (tool card, pill, another modal) resolves
  // a change for this same instruction.
  function onExternalResolution(e: Event) {
    const detail = (e as CustomEvent).detail
    if (!detail || !instructionId.value) return
    if (detail.instructionId === instructionId.value) refresh()
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

  return {
    pendingBuilds,
    currentBuild,
    currentIndex,
    hasPending,
    pendingCount,
    diffOps,
    isLoading,
    isResolving,
    accept,
    reject,
    next,
    prev,
    refresh,
  }
}
