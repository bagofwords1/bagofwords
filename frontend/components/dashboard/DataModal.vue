<template>
  <UTooltip :text="$t('artifactFrame.viewData')">
    <button @click="openModal" class="text-lg items-center flex gap-1 hover:bg-gray-100 dark:hover:bg-gray-700 px-2 py-1 rounded">
      <Icon name="heroicons:circle-stack" class="w-3.5 h-3.5 text-gray-500 dark:text-gray-400" />
    </button>
  </UTooltip>

  <UModal v-model="isOpen" :ui="{ width: 'sm:max-w-4xl', height: 'h-[90vh]' }">
    <div class="h-full flex flex-col">
      <!-- Header -->
      <div class="p-3 flex justify-between items-center border-b bg-white dark:bg-gray-900">
        <span class="text-sm font-medium text-gray-700 dark:text-gray-300">{{ $t('artifactFrame.dataTitle') }}</span>
        <div class="flex items-center gap-2">
          <!-- Add an existing report query to the dashboard -->
          <UPopover v-if="canManage" :popper="{ placement: 'bottom-end' }" :ui="{ ring: '', shadow: 'shadow-md' }">
            <button
              type="button"
              :disabled="isMutating"
              class="inline-flex items-center gap-1 h-7 px-2.5 rounded-md text-xs font-medium text-blue-600 bg-blue-50 hover:bg-blue-100 dark:text-blue-400 dark:bg-blue-900/30 dark:hover:bg-blue-900/50 transition-colors disabled:opacity-50"
            >
              <Spinner v-if="isMutating" class="w-3.5 h-3.5" />
              <Icon v-else name="heroicons:plus" class="w-3.5 h-3.5" />
              {{ $t('artifactFrame.dataAddQuery') }}
              <Icon name="heroicons:chevron-down" class="w-2.5 h-2.5 opacity-60" />
            </button>
            <template #panel="{ close }">
              <div class="p-1 w-64 max-h-72 overflow-auto">
                <p v-if="availableOptions.length === 0" class="px-2 py-3 text-xs text-gray-400 text-center">
                  {{ $t('artifactFrame.dataNoQueriesToAdd') }}
                </p>
                <button
                  v-for="opt in availableOptions"
                  :key="opt.id"
                  :title="opt.reason || ''"
                  class="w-full flex items-center gap-2 px-2 py-1.5 rounded hover:bg-gray-50 dark:hover:bg-gray-800/50 text-start text-[13px] text-gray-700 dark:text-gray-200"
                  @click="close(); addQuery(opt.id)"
                >
                  <span
                    class="w-2 h-2 rounded-full shrink-0"
                    :class="opt.status === 'error' ? 'bg-red-500' : opt.status === 'success' ? 'bg-green-500' : 'bg-gray-300 dark:bg-gray-600'"
                  />
                  <span class="truncate">{{ opt.label }}</span>
                  <Icon name="heroicons:plus" class="w-3.5 h-3.5 ms-auto text-gray-300 shrink-0" />
                </button>
              </div>
            </template>
          </UPopover>
          <UButton color="gray" variant="ghost" icon="i-heroicons-x-mark-20-solid" @click="isOpen = false" />
        </div>
      </div>

      <!-- Dashboard query list -->
      <div class="flex-1 min-h-0 overflow-y-auto bg-gray-50 dark:bg-gray-900 p-4">
        <div v-if="isLoading" class="flex items-center justify-center h-full">
          <Spinner class="w-5 h-5 text-gray-400" />
        </div>
        <div v-else-if="dashboardQueries.length === 0" class="flex items-center justify-center h-full text-gray-400">
          <p>{{ $t('artifactFrame.dataEmpty') }}</p>
        </div>
        <div v-else>
          <!-- ToolWidgetPreview is the whole row: its own header is the single
               collapse level; remove rides in header-actions, and last-run
               status + usage in the header-subtitle line -->
          <ToolWidgetPreview
            v-for="q in dashboardQueries"
            :key="q.id"
            :tool-execution="toToolExecution(q)"
            :readonly="true"
            :initial-collapsed="true"
            :can-edit="canManage"
            @editQuery="openQueryEditor"
          >
            <template #header-actions>
              <!-- Remove from dashboard -->
              <UTooltip v-if="canManage" :text="$t('artifactFrame.dataRemove')">
                <button
                  @click.stop="confirmTarget = q"
                  :disabled="removingIds[q.id] || isMutating"
                  class="p-1 rounded hover:bg-red-50 dark:hover:bg-red-900/30 text-gray-400 hover:text-red-500 transition-colors disabled:opacity-50 shrink-0"
                >
                  <Spinner v-if="removingIds[q.id]" class="w-3.5 h-3.5" />
                  <Icon v-else name="heroicons:trash" class="w-3.5 h-3.5" />
                </button>
              </UTooltip>
            </template>

            <!-- Second line: last-run status, error reason, artifact usage -->
            <template #header-subtitle>
              <div class="mt-1 ps-5 flex items-center gap-2 text-[11px] text-gray-400 min-w-0">
                <template v-if="q.last_run">
                  <span
                    class="w-2 h-2 rounded-full shrink-0"
                    :class="q.last_run.status === 'error' ? 'bg-red-500' : 'bg-green-500'"
                  />
                  <span class="whitespace-nowrap shrink-0">
                    {{ $t('artifactFrame.dataLastRun', { time: relativeTime(q.last_run.ran_at) }) }}
                  </span>
                  <span
                    v-if="q.last_run.status === 'error' && q.last_run.status_reason"
                    class="text-red-500 truncate min-w-0"
                    :title="q.last_run.status_reason"
                  >
                    {{ q.last_run.status_reason }}
                  </span>
                </template>
                <template v-else>
                  <span class="w-2 h-2 rounded-full bg-gray-300 dark:bg-gray-600 shrink-0" />
                  <span class="whitespace-nowrap shrink-0">{{ $t('artifactFrame.dataNeverRun') }}</span>
                </template>

                <!-- In use / not used: whether the artifact code renders this viz.
                     Hidden until the artifact's usage set has loaded. -->
                <UTooltip
                  v-if="usedVizIds !== null"
                  :text="isInUse(q) ? $t('artifactFrame.dataInUseHint') : $t('artifactFrame.dataNotUsedHint')"
                  class="ms-auto shrink-0"
                >
                  <span
                    class="px-1.5 py-0.5 rounded text-[10px] font-medium"
                    :class="isInUse(q)
                      ? 'bg-green-50 text-green-600 dark:bg-green-900/30 dark:text-green-400'
                      : 'bg-amber-50 text-amber-600 dark:bg-amber-900/30 dark:text-amber-400'"
                  >
                    {{ isInUse(q) ? $t('artifactFrame.dataInUse') : $t('artifactFrame.dataNotUsed') }}
                  </span>
                </UTooltip>
              </div>
            </template>
          </ToolWidgetPreview>
        </div>
      </div>
    </div>
  </UModal>

  <!-- Remove confirmation -->
  <UModal :model-value="!!confirmTarget" @update:model-value="confirmTarget = null" :ui="{ width: 'sm:max-w-sm' }">
    <div class="p-5">
      <div class="flex items-start gap-3">
        <div class="shrink-0 w-9 h-9 rounded-full bg-red-50 dark:bg-red-900/30 flex items-center justify-center">
          <Icon name="heroicons:trash" class="w-4 h-4 text-red-500" />
        </div>
        <div class="min-w-0">
          <h3 class="text-sm font-semibold text-gray-800 dark:text-gray-100">
            {{ $t('artifactFrame.dataRemove') }}
          </h3>
          <p class="mt-0.5 text-sm font-medium text-gray-600 dark:text-gray-300 truncate">
            {{ confirmTarget?.title || $t('artifactFrame.dataUntitledQuery') }}
          </p>
          <p class="mt-2 text-xs text-gray-500 dark:text-gray-400">
            {{ $t('artifactFrame.dataRemoveConfirm') }}
          </p>
        </div>
      </div>
      <div class="mt-5 flex justify-end gap-2">
        <UButton color="gray" variant="ghost" size="sm" @click="confirmTarget = null">
          {{ $t('common.cancel') }}
        </UButton>
        <UButton color="red" size="sm" @click="onConfirmRemove">
          {{ $t('artifactFrame.dataRemove') }}
        </UButton>
      </div>
    </div>
  </UModal>

  <!-- Query code editor: saving runs the code on the SAME query and promotes
       the result to its default step — no new query/viz, so the artifact's
       vizById() binding keeps working and tiles refresh via the
       query:default_step_changed broadcast the editor emits. -->
  <QueryCodeEditorModal
    :visible="showQueryEditor"
    :query-id="queryEditorProps.queryId"
    :step-id="queryEditorProps.stepId"
    :initial-code="queryEditorProps.initialCode"
    :title="queryEditorProps.title"
    @close="showQueryEditor = false"
    @stepCreated="onEditorStepCreated"
  />
</template>

<script setup lang="ts">
import { ref, reactive, computed } from 'vue'
import { useMyFetch } from '~/composables/useMyFetch'
import ToolWidgetPreview from '~/components/tools/ToolWidgetPreview.vue'
import QueryCodeEditorModal from '~/components/tools/QueryCodeEditorModal.vue'
import Spinner from '~/components/Spinner.vue'

const props = defineProps<{
  reportId: string
  artifactId?: string | null
  artifactVizIds?: string[]
  artifactMode?: string | null
}>()

const { t } = useI18n()
const toast = useToast()
const { relativeTime } = useRelativeTime()

const isOpen = ref(false)
const isLoading = ref(false)
const isMutating = ref(false)
const allQueries = ref<any[]>([])
const removingIds = reactive<Record<string, boolean>>({})
// Query pending removal — set by the trash button, cleared by the dialog
const confirmTarget = ref<any | null>(null)
// Track the artifact locally: add/remove create a new version, and the
// parent's props only catch up after it refetches.
const currentArtifactId = ref<string | null>(null)
const currentVizIds = ref<string[]>([])
// Which viz ids the artifact code actually renders (used_visualization_ids,
// computed server-side from content.code). null = not loaded yet, so the
// in-use badges stay hidden instead of guessing.
const usedVizIds = ref<string[] | null>(null)

// Query code editor state
const showQueryEditor = ref(false)
const queryEditorProps = ref<{
  queryId: string | null
  stepId: string | null
  initialCode: string
  title: string
}>({ queryId: null, stepId: null, initialCode: '', title: '' })

// Only page dashboards support add/remove (docs hold markdown, slides only
// render slide sections) — the modal is read-only elsewhere
const canManage = computed(() => !props.artifactMode || props.artifactMode === 'page')

function openModal() {
  currentArtifactId.value = props.artifactId || null
  currentVizIds.value = [...(props.artifactVizIds || [])]
  usedVizIds.value = null
  isOpen.value = true
  loadQueries()
  loadArtifactUsage()
}

// Fetch the artifact to learn which viz ids its code actually renders. Also
// refreshes membership — the server copy beats possibly-stale props.
async function loadArtifactUsage() {
  if (!currentArtifactId.value) {
    usedVizIds.value = []
    return
  }
  const { data, error } = await useMyFetch(`/api/artifacts/${currentArtifactId.value}`)
  if (error.value || !data.value) return
  const artifact = data.value as any
  usedVizIds.value = [...(artifact.used_visualization_ids || [])]
  const vizIds = (artifact.content || {}).visualization_ids
  if (Array.isArray(vizIds)) currentVizIds.value = [...vizIds]
}

// "In use" = at least one of the query's on-dashboard visualizations is
// actually referenced by the artifact code
function isInUse(q: any): boolean {
  const used = new Set(usedVizIds.value || [])
  return (q.visualizations || []).some((v: any) => used.has(v.id))
}

async function loadQueries() {
  isLoading.value = true
  try {
    const { data, error } = await useMyFetch(`/api/queries?report_id=${props.reportId}`)
    allQueries.value = !error.value && Array.isArray(data.value) ? (data.value as any[]) : []
  } finally {
    isLoading.value = false
  }
}

// The dashboard's queries: derived from the report-wide list + the
// artifact's viz-id membership the modal already tracks — one fetch
// serves both this list and the add-menu, and they can't disagree
const dashboardQueries = computed(() => {
  const onDashboard = new Set(currentVizIds.value)
  return allQueries.value.filter(q =>
    (q.visualizations || []).some((v: any) => onDashboard.has(v.id))
  )
})

// Report queries not on the dashboard yet. Failing ones stay listed with
// their status — attempting to add surfaces the backend's exact reason.
const availableOptions = computed(() => {
  const onDashboard = new Set(currentVizIds.value)
  return allQueries.value
    .filter(q =>
      (q.visualizations || []).length > 0 &&
      !(q.visualizations || []).some((v: any) => onDashboard.has(v.id))
    )
    .map(q => {
      const viz = (q.visualizations || []).find((v: any) => v.status === 'success') || q.visualizations[0]
      const status = q.last_run?.status || (q.default_step?.status ?? null)
      return {
        id: viz.id,
        label: q.title || t('artifactFrame.dataUntitledQuery'),
        status,
        reason: q.last_run?.status === 'error' ? (q.last_run.status_reason || null) : null,
      }
    })
})

// ToolWidgetPreview expects a tool-execution shape; wrap the query's default
// step so it renders as the Chart/Table/Code card. A never-run query has no
// default_step — the stub still carries query_id so the card can hydrate a
// fresh step from the server when expanded.
function toToolExecution(q: any) {
  const step = q.default_step || {}
  return {
    id: q.id,
    tool_name: 'query',
    status: 'success',
    created_step: {
      id: step.id,
      query_id: q.id,
      title: q.title || t('artifactFrame.dataUntitledQuery'),
      data: step.data || {},
      data_model: step.data_model || { type: 'table' },
      code: step.code || ''
    },
    created_visualizations: (q.visualizations || []).map((v: any) => ({
      id: v.id,
      query_id: q.id,
      title: v.title || q.title,
      view: v.view,
      status: v.status || 'success'
    }))
  }
}

// A successful add/remove returns a NEW artifact version — adopt it locally
// and broadcast the same events ToolWidgetPreview's "Add to dashboard" does,
// so ArtifactFrame switches to the new version and refetches its data.
function adoptArtifact(artifact: any) {
  currentArtifactId.value = artifact.id
  currentVizIds.value = [...((artifact.content || {}).visualization_ids || [])]
  usedVizIds.value = [...(artifact.used_visualization_ids || [])]
  window.dispatchEvent(new CustomEvent('artifact:created', { detail: { report_id: props.reportId, artifact_id: artifact.id } }))
  window.dispatchEvent(new CustomEvent('artifact:open', { detail: { artifact_id: artifact.id } }))
}

async function addQuery(vizId: string) {
  if (!vizId || isMutating.value) return
  isMutating.value = true
  try {
    const { data, error } = await useMyFetch(
      `/api/artifacts/report/${props.reportId}/add-visualization`,
      // artifact_id: operate on the artifact the user is looking at, not
      // whatever happens to be the report's newest one
      { method: 'POST', body: { visualization_id: vizId, artifact_id: currentArtifactId.value || undefined } },
    )
    if (error.value || !(data.value as any)?.id) {
      // Surface the backend's reason (no data model, already added, …)
      const detail = (error.value as any)?.data?.detail
      toast.add({
        title: t('artifactFrame.dataAddFailed'),
        description: typeof detail === 'string' ? detail : undefined,
        color: 'red',
        timeout: 8000,
      })
      return
    }
    adoptArtifact(data.value)
    await loadQueries()
  } finally {
    isMutating.value = false
  }
}

function openQueryEditor(payload: { queryId: string; stepId: string | null; initialCode: string; title: string }) {
  queryEditorProps.value = {
    queryId: payload.queryId,
    stepId: payload.stepId,
    initialCode: payload.initialCode,
    title: payload.title,
  }
  showQueryEditor.value = true
}

// The editor already broadcasts query:default_step_changed (each card
// refreshes itself); reload the list so last_run catches up too.
async function onEditorStepCreated() {
  await loadQueries()
}

function onConfirmRemove() {
  const q = confirmTarget.value
  confirmTarget.value = null
  if (q) removeQuery(q)
}

async function removeQuery(q: any) {
  const onDashboard = new Set(currentVizIds.value)
  const vizIds = (q.visualizations || []).map((v: any) => v.id).filter((id: string) => onDashboard.has(id))
  if (vizIds.length === 0) return
  removingIds[q.id] = true
  try {
    let lastArtifact: any = null
    // Each removal creates a new version — the next one must build on it,
    // not on the artifact we started from
    let baseId = currentArtifactId.value
    for (const vizId of vizIds) {
      const { data, error } = await useMyFetch(
        `/api/artifacts/report/${props.reportId}/remove-visualization`,
        { method: 'POST', body: { visualization_id: vizId, artifact_id: baseId || undefined } },
      )
      if (error.value) {
        const detail = (error.value as any)?.data?.detail
        toast.add({
          title: t('artifactFrame.dataRemoveFailed'),
          description: typeof detail === 'string' ? detail : undefined,
          color: 'red',
          timeout: 8000,
        })
        break
      }
      lastArtifact = data.value
      baseId = (lastArtifact as any)?.id || baseId
    }
    if (lastArtifact) {
      adoptArtifact(lastArtifact)
      await loadQueries()
    }
  } finally {
    removingIds[q.id] = false
  }
}
</script>
