<template>
  <div class="h-full overflow-y-auto bg-white dark:bg-gray-900">
    <!-- Header -->
    <div class="px-4 pt-4 pb-6 bg-gradient-to-b from-amber-50 dark:from-amber-900/15 to-white dark:to-gray-900 flex items-center gap-2">
      <button v-if="showClose" @click="$emit('close')" class="hover:bg-gray-100 dark:hover:bg-gray-700 p-1 rounded">
        <Icon name="heroicons:x-mark" class="w-4 h-4 text-gray-500 dark:text-gray-400" />
      </button>
      <h2 class="text-sm font-semibold text-gray-900 dark:text-white">{{ $t('reportView.tabSummary') }}</h2>
    </div>

    <div class="max-w-xl mx-auto px-4 py-4 space-y-6">

      <!-- Empty state -->
      <div v-if="!hasAnything" class="flex flex-col items-center justify-center h-64 text-gray-400">
        <Icon name="heroicons-document-text" class="w-8 h-8 mb-2" />
        <span class="text-sm">{{ $t('chatSummary.empty') }}</span>
      </div>

      <!-- Scheduled Tasks -->
      <section v-if="scheduledPrompts.length > 0">
        <h3 class="text-xs font-medium text-gray-400 uppercase tracking-wide mb-2">{{ $t('chatSummary.scheduledTasks') }}</h3>
        <ul class="space-y-1.5">
          <li v-for="sp in scheduledPrompts" :key="sp.id">
            <ScheduledTaskCard :scheduled-prompt="sp" @click="emit('editScheduledPrompt', sp)" />
          </li>
        </ul>
      </section>

      <!-- Artifacts -->
      <section v-if="artifactList.length > 0">
        <h3 class="text-xs font-medium text-gray-400 uppercase tracking-wide mb-2">{{ $t('chatSummary.artifacts') }}</h3>
        <ul class="space-y-1.5">
          <li
            v-for="group in visibleArtifactGroups"
            :key="group.latest.id"
            class="px-3 py-2.5 rounded-lg bg-white dark:bg-gray-900 border border-gray-100 dark:border-gray-800 shadow-sm hover:shadow transition-all"
          >
            <div class="flex items-center gap-2.5 cursor-pointer" @click="emit('openArtifact', { artifactId: group.latest.id })">
              <Icon
                :name="artifactIcon(group.latest.mode)"
                class="w-4 h-4 flex-shrink-0"
                :class="artifactIconClass(group.latest.mode)"
              />
              <div class="flex-1 min-w-0">
                <div class="flex items-center gap-1.5">
                  <span class="text-sm text-gray-700 dark:text-gray-300 truncate">{{ group.latest.title || $t('prompt.untitled') }}</span>
                  <span v-if="group.latest.version" class="text-[10px] font-medium text-gray-400 flex-shrink-0">v{{ group.latest.version }}</span>
                  <span v-if="group.latest.id === defaultArtifactId" class="inline-flex items-center text-[10px] font-medium text-blue-600 bg-blue-50 dark:bg-blue-950 px-1.5 py-0.5 rounded flex-shrink-0">{{ $t('chatSummary.default') }}</span>
                </div>
                <div v-if="group.latest.mode" class="text-[11px] text-gray-400 mt-0.5">{{ group.latest.mode === 'doc' ? $t('chatSummary.document') : group.latest.mode }}</div>
              </div>
            </div>
            <!-- Older versions of this same artifact, as number-only chips: the
                 kind keeps one row, but no version becomes unreachable. Digits
                 need no translation, so this adds no untranslated UI text. -->
            <div v-if="group.older.length" class="flex flex-wrap items-center gap-1 mt-1.5 ps-6">
              <button
                v-for="old in group.older"
                :key="old.id"
                class="text-[10px] font-medium text-gray-400 hover:text-gray-700 dark:hover:text-gray-200 bg-gray-50 dark:bg-gray-800 hover:bg-gray-100 dark:hover:bg-gray-700 px-1.5 py-0.5 rounded transition-colors"
                @click.stop="emit('openArtifact', { artifactId: old.id })"
              >
                v{{ old.version }}
              </button>
            </div>
          </li>
        </ul>
        <button
          v-if="artifactGroups.length > 3 && !showAllArtifacts"
          class="mt-2 text-xs text-gray-400 hover:text-gray-600 transition-colors"
          @click="showAllArtifacts = true"
        >
          {{ $t('chatSummary.showMore', { count: artifactGroups.length - 3 }) }}
        </button>
      </section>

      <!-- Lists (Agent Lists this conversation wrote to; per-record detail
           stays on the chat card — the numbered chips scroll to each call) -->
      <section v-if="listItems.length > 0">
        <h3 class="text-xs font-medium text-gray-400 uppercase tracking-wide mb-2">{{ $t('chatSummary.lists') }}</h3>
        <ul class="space-y-1.5">
          <li
            v-for="item in visibleListItems"
            :key="item.list_id"
            class="px-3 py-2.5 rounded-lg bg-white dark:bg-gray-900 border border-gray-100 dark:border-gray-800 shadow-sm hover:shadow transition-all"
            data-testid="summary-list-item"
          >
            <NuxtLink :to="`/agents/${item.data_source_id}/lists/${item.list_id}`" class="flex items-center gap-2.5">
              <Icon name="heroicons-list-bullet" class="w-4 h-4 flex-shrink-0 text-emerald-500" />
              <div class="flex-1 min-w-0">
                <div class="text-sm text-gray-700 dark:text-gray-300 truncate"><bdi>{{ item.list_name }}</bdi></div>
                <div class="text-[11px] text-gray-400 mt-0.5 truncate">
                  <bdi v-if="item.agent_name">{{ item.agent_name }}</bdi>
                  <span v-for="c in listCounts(item)" :key="c"> · {{ c }}</span>
                </div>
              </div>
            </NuxtLink>
            <div v-if="item.submissions.length > 1" class="flex flex-wrap items-center gap-1 mt-1.5 ps-6">
              <button
                v-for="(sub, idx) in item.submissions"
                :key="sub.tool_execution_id"
                class="text-[10px] font-medium text-gray-400 hover:text-gray-700 dark:hover:text-gray-200 bg-gray-50 dark:bg-gray-800 hover:bg-gray-100 dark:hover:bg-gray-700 px-1.5 py-0.5 rounded transition-colors"
                @click="emit('scrollToMessage', sub.message_id)"
              >
                #{{ idx + 1 }}
              </button>
            </div>
          </li>
        </ul>
        <button
          v-if="listItems.length > 3 && !showAllLists"
          class="mt-2 text-xs text-gray-400 hover:text-gray-600 transition-colors"
          @click="showAllLists = true"
        >
          {{ $t('chatSummary.showMore', { count: listItems.length - 3 }) }}
        </button>
      </section>

      <!-- Notes (agent scratchpad) -->
      <!-- Sub-agents (delegated background investigations) -->
      <section v-if="subAgentItems.length > 0" data-testid="summary-sub-agents">
        <h3 class="text-xs font-medium text-gray-400 uppercase tracking-wide mb-2">{{ $t('chatSummary.subAgents') }}</h3>
        <p class="text-[11px] text-gray-400 mb-2" dir="ltr">{{ $t('subAgents.headerStrip', { total: subAgentItems.length, tokens: subAgentTokensLabel }) }}<span v-if="subAgentOutcomeSummary"> · {{ subAgentOutcomeSummary }}</span></p>
        <ul class="space-y-1">
          <li
            v-for="sr in subAgentItems"
            :key="sr.sub_agent_id"
            class="flex items-center gap-2 text-xs px-2 py-1.5 rounded border border-gray-100 dark:border-gray-800 hover:border-gray-200 dark:hover:border-gray-700 cursor-pointer min-w-0"
            :data-testid="`summary-sub-agent-${sr.sub_agent_id}`"
            @click="emit('scrollToMessage', sr.message_id)"
          >
            <Icon :name="subAgentIcon(sr)" class="w-3.5 h-3.5 flex-shrink-0" :class="subAgentIconClass(sr)" />
            <span class="truncate flex-1 text-gray-700 dark:text-gray-200" :title="sr.summary || sr.title">{{ sr.title }}</span>
            <span v-if="sr.outcome" class="px-1.5 py-0.5 rounded text-[10px] font-medium flex-shrink-0" :class="subAgentOutcomeClass(sr.outcome)">
              {{ $t(`tools.delegate.outcomes.${sr.outcome}`, sr.outcome) }}<template v-if="typeof sr.confidence === 'number'"> · {{ Math.round(sr.confidence * 100) }}%</template>
            </span>
            <span v-else class="text-[10px] text-gray-400 flex-shrink-0">{{ $t(`tools.delegate.status.${subAgentStatusKey(sr)}`, sr.status) }}</span>
            <span class="text-[10px] text-gray-400 flex-shrink-0 tabular-nums" dir="ltr">
              <template v-if="sr.steps_used !== null && sr.steps_used !== undefined">{{ sr.steps_used }} {{ $t('tools.delegate.steps') }}</template>
              <template v-if="tokensOf(sr)"> · {{ tokensOf(sr) }}</template>
            </span>
          </li>
        </ul>
      </section>

      <section v-if="notes.length > 0">
        <h3 class="text-xs font-medium text-gray-400 uppercase tracking-wide mb-2">{{ $t('notes.heading') }}</h3>
        <ul class="space-y-1.5">
          <li
            v-for="note in notes"
            :key="note.id"
            class="flex items-start gap-2.5 px-3 py-2.5 rounded-lg bg-white dark:bg-gray-900 border border-gray-100 dark:border-gray-800 shadow-sm hover:shadow cursor-pointer transition-all"
            @click="openNote(note)"
          >
            <Icon name="heroicons:pencil-square" class="w-4 h-4 flex-shrink-0 text-amber-500 mt-0.5" />
            <div class="flex-1 min-w-0">
              <div class="text-sm text-gray-700 dark:text-gray-300 truncate">{{ note.title || $t('tools.createNote.untitled') }}</div>
              <div dir="auto" class="text-[11px] text-gray-400 mt-0.5 line-clamp-2 whitespace-pre-line">{{ notePreview(note.content) }}</div>
            </div>
          </li>
        </ul>
      </section>

      <!-- Queries -->
      <section v-if="queryExecutions.length > 0">
        <h3 class="text-xs font-medium text-gray-400 uppercase tracking-wide mb-2">{{ $t('chatSummary.queries') }}</h3>
        <div class="space-y-2">
          <div v-for="te in queryExecutions" :key="te.id">
            <!-- A query created inside a sub-agent keeps its provenance visible -->
            <p v-if="subAgentForExecution(te.agent_execution_id)" class="text-[10px] text-gray-400 mb-0.5 truncate" :data-testid="`via-sub-agent-${te.id}`">
              {{ $t('subAgents.viaSubAgent', { title: subAgentForExecution(te.agent_execution_id)?.title }) }}
            </p>
            <ToolWidgetPreview
              :tool-execution="te"
              :readonly="true"
              :initial-collapsed="true"
              :can-expand="canExpand"
              @openDataPanel="$emit('openDataPanel', $event)"
            />
          </div>
        </div>
      </section>

      <!-- Instructions (historical: created in this report, regardless of accept state) -->
      <section v-if="instructionsList.length > 0">
        <h3 class="text-xs font-medium text-gray-400 uppercase tracking-wide mb-2">{{ $t('chatSummary.instructions') }}</h3>
        <ul class="space-y-1.5">
          <li
            v-for="inst in instructionsList"
            :key="inst.id"
            class="flex items-center gap-2.5 px-3 py-2.5 rounded-lg bg-white dark:bg-gray-900 border border-gray-100 dark:border-gray-800 shadow-sm hover:shadow transition-all"
          >
            <Icon
              name="heroicons-cube"
              class="w-4 h-4 flex-shrink-0 text-blue-400"
            />
            <div class="flex-1 min-w-0">
              <div class="text-sm text-gray-700 dark:text-gray-300 truncate">{{ inst.title || $t('chatSummary.untitledInstruction') }}</div>
              <div class="flex items-center gap-2 mt-0.5">
                <span v-if="inst.category" class="text-[11px] text-gray-400">{{ inst.category }}</span>
                <span
                  v-if="inst.state === 'pending'"
                  class="inline-flex items-center gap-1 text-[11px] text-amber-600"
                >
                  <span class="w-1.5 h-1.5 rounded-full bg-amber-400" />
                  {{ $t('chatSummary.pending') }}
                </span>
                <span
                  v-else
                  class="inline-flex items-center gap-1 text-[11px] text-green-600"
                >
                  <Icon name="heroicons:check-circle" class="w-3 h-3" />
                  {{ $t('chatSummary.accepted') }}
                </span>
              </div>
            </div>
          </li>
        </ul>
      </section>

      <!-- Webhooks -->
      <section v-if="webhooks.length > 0">
        <h3 class="text-xs font-medium text-gray-400 uppercase tracking-wide mb-2">{{ $t('chatSummary.webhooks') }}</h3>
        <ul class="space-y-1.5">
          <li
            v-for="w in webhooks"
            :key="w.id"
            class="flex items-center gap-2.5 px-3 py-2.5 rounded-lg bg-white dark:bg-gray-900 border border-gray-100 dark:border-gray-800 shadow-sm hover:shadow cursor-pointer transition-all"
            @click="showWebhookModal = true"
          >
            <Icon :name="webhookIcon(w.source)" class="w-4 h-4 flex-shrink-0 text-gray-400" />
            <div class="flex-1 min-w-0">
              <div class="text-sm text-gray-700 dark:text-gray-300 truncate">{{ w.name }}</div>
              <div class="flex items-center gap-2 mt-0.5">
                <span class="text-[11px] text-gray-400">{{ w.source }} · {{ w.auth_mode }}</span>
                <span v-if="w.classify_enabled" class="inline-flex items-center gap-1 text-[11px] text-blue-500">
                  <Icon name="heroicons-sparkles" class="w-3 h-3" /> AI
                </span>
              </div>
            </div>
          </li>
        </ul>
      </section>

    </div>

    <!-- Configure webhook (pinned at the bottom) -->
    <div v-if="reportId" class="max-w-xl mx-auto px-4 pb-6">
      <button
        @click="showWebhookModal = true"
        class="w-full flex items-center justify-center gap-1.5 px-3 py-2 text-xs text-gray-400 hover:text-gray-600 rounded-lg border border-dashed border-gray-200 dark:border-gray-700 hover:border-gray-300 transition-colors"
      >
        <Icon name="heroicons-plus" class="w-3.5 h-3.5" />
        {{ $t('chatSummary.configureWebhook') }}
      </button>
    </div>

    <WebhookConfigModal
      v-if="reportId"
      v-model="showWebhookModal"
      :report-id="reportId"
      @changed="loadWebhooks"
    />

    <NoteDetailsModal v-model="showNoteModal" :note="selectedNote" />
  </div>
</template>

<script setup lang="ts">
import { computed, ref, watch, onMounted } from 'vue'
import ToolWidgetPreview from '~/components/tools/ToolWidgetPreview.vue'
import WebhookConfigModal from '~/components/report/WebhookConfigModal.vue'
import NoteDetailsModal from '~/components/report/NoteDetailsModal.vue'
import { subAgentStatusKey } from '~/utils/subAgentStatus'

const props = defineProps<{
  /** Host page has a side panel the query cards can open into. */
  canExpand?: boolean
  scheduledPrompts: any[]
  artifactList: any[]
  queryList: any[]
  queryExecutions: any[]
  // Agent Lists this conversation wrote to (GET /reports/{id}/summary → lists).
  listSubmissions?: any[]
  // Delegated sub-agents (GET /reports/{id}/summary → sub_agents).
  subAgents?: any[]
  // Pending-only: drives session-pill state. Kept for backwards compat with
  // existing callers but no longer rendered directly in the Instructions
  // section — we use reportInstructions for that.
  trainingInstructions: any[]
  // Historical list of instructions created in this report (all states).
  reportInstructions?: any[]
  // The current pending build id, used to mark items as Pending vs Accepted.
  pendingBuildId?: string | null
  showClose?: boolean
  // Report id — enables the Webhooks section + Configure webhook modal.
  reportId?: string
}>()

// ---- Webhooks ----
const showWebhookModal = ref(false)
const webhooks = ref<any[]>([])

function webhookIcon(source: string): string {
  switch ((source || '').toLowerCase()) {
    case 'github': return 'heroicons-code-bracket-square'
    case 'jira': return 'heroicons-bug-ant'
    default: return 'heroicons-bolt'
  }
}

async function loadWebhooks() {
  if (!props.reportId) return
  try {
    const { data } = await useMyFetch(`/reports/${props.reportId}/webhooks`)
    webhooks.value = (data.value as any[]) || []
  } catch { webhooks.value = [] }
}

onMounted(loadWebhooks)
watch(() => props.reportId, loadWebhooks)

// ---- Notes (agent scratchpad) ----
const notes = ref<any[]>([])
const showNoteModal = ref(false)
const selectedNote = ref<any>(null)

async function loadNotes() {
  if (!props.reportId) { notes.value = []; return }
  try {
    const { data } = await useMyFetch(`/reports/${props.reportId}/notes`)
    notes.value = (data.value as any[]) || []
  } catch { notes.value = [] }
}

function openNote(note: any) {
  selectedNote.value = note
  showNoteModal.value = true
}

function notePreview(content: string): string {
  return (content || '').replace(/[#*`>_-]/g, '').replace(/\s+/g, ' ').trim().slice(0, 160)
}

onMounted(loadNotes)
watch(() => props.reportId, loadNotes)
// Reload notes when the report's activity changes (a run created/edited notes) —
// query executions / artifacts update as tools complete, so they proxy "activity".
watch(() => [props.queryExecutions.length, props.artifactList.length], loadNotes)
defineExpose({ reloadNotes: loadNotes })

const showAllArtifacts = ref(false)

// ---- Lists ----
const { t } = useI18n()
const showAllLists = ref(false)
const listItems = computed(() => props.listSubmissions || [])
const visibleListItems = computed(() =>
  showAllLists.value ? listItems.value : listItems.value.slice(0, 3)
)
// Same wording as the chat card's header.
function listCounts(item: any): string[] {
  const out: string[] = []
  if (item.inserted) out.push(t('tools.submitList.added', { n: item.inserted }))
  if (item.updated) out.push(t('tools.submitList.updated', { n: item.updated }))
  return out
}

// The artifact the report page actually opens by default: the newest
// dashboard/deck row (docs never take the default slot — same rule as the
// backend's get_latest_by_report and the public share page).
const defaultArtifactId = computed(() => {
  const rows = props.artifactList || []
  return (rows.find((a: any) => a?.mode !== 'doc') || rows[0])?.id
})

// One row per ARTIFACT, not per version — and not per kind either. Grouping
// by mode (the previous rule) merged two different dashboards of the same
// report into one row with interleaved version numbers; grouping by version
// would let a repeatedly-rebuilt deck push everything past the 3-row cut-off.
// `artifact_id` (the parent identity every version carries) is the real key.
const artifactGroups = computed(() => {
  // Pass 1: bucket by parent artifact id — versions of the same
  // dashboard/deck/doc stay together whatever their titles say.
  const rows = props.artifactList || []
  const byArtifact = new Map<string, any[]>()
  for (const art of rows) {
    const key = String(art?.artifact_id || art?.id)
    if (!byArtifact.has(key)) byArtifact.set(key, [])
    byArtifact.get(key)!.push(art)
  }
  // Pass 2: pre-migration history has one parent PER VERSION (the backfill
  // deliberately refused to guess lineage), so artifact_id alone would
  // explode an old 7-edit dashboard into 7 rows. Backfilled parents reuse
  // their original version's id (migration artver01) — new parents never do —
  // so a group holding a version whose id IS the artifact_id is old history:
  // those merge by kind+title, even after a post-migration edit has grown
  // one of them into a chain. Everything else keeps its own identity, so two
  // new dashboards that happen to share a title stay two rows.
  const merged = new Map<string, any[]>()
  for (const [artifactId, versions] of byArtifact) {
    const backfilled = versions.some((v: any) => String(v?.id) === artifactId)
    const key = backfilled
      ? `legacy:${versions[0]?.mode || 'unknown'}::${versions[0]?.title || ''}`
      : `artifact:${artifactId}`
    if (!merged.has(key)) merged.set(key, [])
    merged.get(key)!.push(...versions)
  }
  // props.artifactList is created_at DESC. Merging legacy groups can
  // interleave their versions, so restore that order inside each bucket;
  // its first entry is then its newest version, and Map insertion order
  // leaves the most-recently-touched artifact first.
  const position = new Map(rows.map((a: any, i: number) => [a, i]))
  for (const versions of merged.values()) {
    versions.sort((a, b) => position.get(a)! - position.get(b)!)
  }
  return Array.from(merged.values()).map((versions) => ({
    latest: versions[0],
    older: versions.slice(1),
  }))
})

const visibleArtifactGroups = computed(() =>
  showAllArtifacts.value ? artifactGroups.value : artifactGroups.value.slice(0, 3)
)

// A distinct glyph and colour per kind. Only `doc` used to get its own icon, so
// a dashboard and a slide deck rendered identically — same glyph, same colour,
// and (the agent tends to name them alike) the same title. Mirrors the mapping
// ArtifactFrame already uses in its artifact picker.
const artifactIcon = (mode: string): string =>
  mode === 'doc' ? 'heroicons:document-text'
    : mode === 'slides' ? 'heroicons:presentation-chart-bar'
      : 'heroicons:squares-2x2'
const artifactIconClass = (mode: string): string =>
  mode === 'doc' ? 'text-emerald-500'
    : mode === 'slides' ? 'text-amber-500'
      : 'text-blue-500'

const emit = defineEmits([
  'editScheduledPrompt',
  'openArtifact',
  'openDataPanel',
  'scrollToMessage',
  'close',
])

// Render-ready instruction list: prefer the historical list (so accepted
// instructions don't disappear after approval). Pending state is derived
// from membership in the current pending training build, looked up via the
// `trainingInstructions` (pending-only) list passed alongside.
const pendingIdSet = computed(() => {
  const set = new Set<string>()
  for (const t of (props.trainingInstructions || [])) {
    if (t?.instructionId) set.add(String(t.instructionId))
  }
  return set
})
const instructionsList = computed(() => {
  const raw = Array.isArray(props.reportInstructions) ? props.reportInstructions : []
  const seen = new Set<string>()
  const out: { id: string; title: string; category: string; state: string }[] = []
  for (const i of raw) {
    if (!i || !i.id) continue
    const id = String(i.id)
    if (seen.has(id)) continue
    seen.add(id)
    out.push({
      id,
      title: i.title || '',
      category: i.category || '',
      state: pendingIdSet.value.has(id) ? 'pending' : 'accepted',
    })
  }
  return out
})

// ---- Sub-agents ----
const subAgentItems = computed<any[]>(() => props.subAgents || [])
function tokenTotal(sr: any): number {
  const tk = sr?.tokens || {}
  return Number(tk.total_tokens ?? ((tk.prompt_tokens || 0) + (tk.completion_tokens || 0))) || 0
}
function fmtTokens(n: number): string {
  if (!n) return ''
  return n >= 1000 ? `${(n / 1000).toFixed(n >= 10000 ? 0 : 1)}k` : String(n)
}
function tokensOf(sr: any): string { return fmtTokens(tokenTotal(sr)) }
function subAgentForExecution(executionId?: string | null): any | null {
  if (!executionId) return null
  return subAgentItems.value.find((sr) => sr.child_execution_id === executionId) || null
}
const subAgentTokensLabel = computed(() => fmtTokens(subAgentItems.value.reduce((n, sr) => n + tokenTotal(sr), 0)) || '0')
const subAgentOutcomeSummary = computed(() => {
  const counts: Record<string, number> = {}
  for (const sr of subAgentItems.value) {
    const k = sr.outcome || sr.status
    if (!k) continue
    counts[k] = (counts[k] || 0) + 1
  }
  return Object.entries(counts).map(([k, n]) => `${n} ${t(`tools.delegate.outcomes.${k}`, k)}`).join(' · ')
})
function subAgentIcon(sr: any): string {
  switch (sr.status) {
    case 'running': case 'pending': return 'heroicons-arrow-path'
    case 'finished': return sr.outcome === 'refuted' ? 'heroicons-x-circle' : 'heroicons-check-circle'
    case 'failed': return 'heroicons-exclamation-circle'
    case 'timed_out': return 'heroicons-clock'
    default: return 'heroicons-stop-circle'
  }
}
function subAgentIconClass(sr: any): string {
  switch (sr.status) {
    case 'finished': return sr.outcome === 'refuted' ? 'text-gray-400' : 'text-green-500'
    case 'failed': return 'text-red-400'
    case 'timed_out': return 'text-amber-500'
    default: return 'text-gray-400'
  }
}
function subAgentOutcomeClass(outcome: string): string {
  switch (outcome) {
    case 'supported': case 'done': return 'bg-green-50 text-green-700 dark:bg-green-900/30 dark:text-green-300'
    case 'refuted': return 'bg-gray-100 text-gray-600 dark:bg-gray-800 dark:text-gray-300'
    case 'inconclusive': case 'partial': return 'bg-amber-50 text-amber-700 dark:bg-amber-900/30 dark:text-amber-300'
    case 'blocked': return 'bg-red-50 text-red-700 dark:bg-red-900/30 dark:text-red-300'
    default: return 'bg-gray-100 text-gray-600'
  }
}

const hasAnything = computed(() =>
  subAgentItems.value.length > 0 ||
  props.scheduledPrompts.length > 0 ||
  props.artifactList.length > 0 ||
  props.queryExecutions.length > 0 ||
  listItems.value.length > 0 ||
  notes.value.length > 0 ||
  instructionsList.value.length > 0
)

</script>
