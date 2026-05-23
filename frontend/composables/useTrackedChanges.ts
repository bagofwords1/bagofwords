import { ref, computed, watch, type Ref } from 'vue'
import DiffMatchPatch from 'diff-match-patch'

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

  async function accept() {
    const build = currentBuild.value
    const id = instructionId.value
    if (!build || !id || isResolving.value) return false
    isResolving.value = true
    try {
      const { error } = await useMyFetch(`/builds/${build.build_id}/publish`, {
        method: 'POST',
        body: { instruction_ids: [id] },
      })
      if (error.value) return false
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
      const { error } = await useMyFetch(
        `/builds/${build.build_id}/contents/${id}`,
        { method: 'DELETE' },
      )
      if (error.value) return false
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
