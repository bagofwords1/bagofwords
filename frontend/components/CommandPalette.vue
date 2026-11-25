<template>
  <UModal
    v-model="isOpen"
    :ui="{ width: 'sm:max-w-2xl', container: 'items-start', margin: 'sm:mt-[12vh]' }"
  >
    <!-- The category tabs sit between the search input and the results, but
         UCommandPalette has no slot there. Its root is display:contents
         (palette-root), so the input, the tab row and the list are all flex
         items of palette-shell and CSS `order` stacks them. -->
    <div class="palette-shell flex flex-col" @keydown.capture="onTabKeys">
    <UCommandPalette
      ref="paletteRef"
      :groups="groups"
      :placeholder="reportContext ? t('commandPalette.placeholderInReport') : t('commandPalette.placeholder')"
      :empty-state="{ icon: 'i-heroicons-magnifying-glass', label: t('commandPalette.emptyLabel'), queryLabel: t('commandPalette.emptyQueryLabel') }"
      icon="i-heroicons-magnifying-glass"
      :loading="loading"
      class="palette-root !h-auto"
      :ui="{
        wrapper: 'contents',
        input: { size: 'sm:text-sm' },
        group: {
          container: 'text-sm text-gray-700 dark:text-gray-200',
          label: 'px-2.5 my-1.5 text-2xs font-semibold text-gray-700 dark:text-white',
          command: { icon: { base: 'flex-shrink-0 w-4 h-4' } }
        }
      }"
      @update:model-value="onSelect"
      @close="close"
    >
      <!-- In-report hits: the snippet, with the matched term emphasized -->
      <template #inReport-command="{ command }">
        <span v-if="command.parts" class="truncate" dir="auto">
          <template v-for="(part, i) in command.parts" :key="i">
            <mark v-if="part.hit" class="bg-transparent font-semibold text-ink">{{ part.text }}</mark>
            <template v-else>{{ part.text }}</template>
          </template>
        </span>
        <span v-else class="truncate text-ink-muted">{{ command.label }}</span>
        <span v-if="command.suffix" class="flex-shrink-0 text-ink-muted">{{ command.suffix }}</span>
      </template>
      <!-- Agents use the real data-source logo for their type -->
      <template #agents-icon="{ command }">
        <DataSourceIcon :icon-token="command.dsIconToken" class="h-4 w-4 flex-shrink-0" />
      </template>
    </UCommandPalette>

    <div
      role="tablist"
      :aria-label="t('commandPalette.tabsLabel')"
      class="palette-tabs flex items-center gap-1 px-3 py-2 border-y border-gray-100 dark:border-gray-800 overflow-x-auto"
    >
      <button
        v-for="tab in PALETTE_TABS"
        :key="tab"
        type="button"
        role="tab"
        tabindex="-1"
        :aria-selected="activeTab === tab"
        :data-tab="tab"
        class="px-2.5 py-1 rounded-md text-xs whitespace-nowrap transition-colors"
        :class="activeTab === tab
          ? 'bg-gray-100 dark:bg-gray-800 text-ink font-medium'
          : 'text-ink-muted hover:text-ink hover:bg-gray-50 dark:hover:bg-gray-800/60'"
        @mousedown.prevent
        @click="setTab(tab)"
      >
        {{ t(`commandPalette.tabs.${tab}`) }}
      </button>
    </div>
    </div>

    <!-- Footer keyboard hints -->
    <div class="flex items-center gap-4 px-4 py-2 border-t border-gray-100 dark:border-gray-800 text-2xs text-ink-muted bg-gray-50/60 dark:bg-gray-900 rounded-b-lg">
      <span><kbd class="font-sans">↑↓</kbd> navigate</span>
      <span><kbd class="font-sans">↵</kbd> open</span>
      <span><kbd class="font-sans">esc</kbd> close</span>
      <span><kbd class="font-sans">←→</kbd> {{ t('commandPalette.tabsHint') }}</span>
    </div>
  </UModal>

  <!-- Instruction create / view — reused for "New instruction" action and opening a result.
       The modal itself decides global-create vs. suggestion based on permission. -->
  <InstructionModalComponent
    v-if="showInstructionModal"
    v-model="showInstructionModal"
    :instruction="instructionModalInstruction"
    :initial-text="instructionInitialText"
    @instructionSaved="onInstructionSaved"
  />

  <!-- Parameter fill for a selected prompt (only when it has parameters). -->
  <PromptParametersModal
    v-if="showPromptParams"
    v-model="showPromptParams"
    :prompt="promptForParams"
    @confirm="onPromptParamsConfirm"
    @cancel="showPromptParams = false"
  />
</template>

<script setup lang="ts">
import InstructionModalComponent from '~/components/InstructionModalComponent.vue'
import DataSourceIcon from '~/components/DataSourceIcon.vue'
import PromptParametersModal from '~/components/prompt/PromptParametersModal.vue'
import { useCan, useCanAny } from '~/composables/usePermissions'
import { usePromptFill } from '~/composables/usePromptFill'
import { parseReportScope, snippetParts } from '~/utils/commandPaletteScope'
import {
  PALETTE_TABS, groupsForTab, fetchesForTab, actionsForTab, limitFor, stepTab,
  type PaletteTab, type PaletteFetch,
} from '~/utils/commandPaletteTabs'

const { t } = useI18n()
const router = useRouter()
const { isOpen, close, reportContext } = useCommandPalette()

// Global ⌘K / Ctrl+K. usingInput:true so it also fires while a text field is focused.
defineShortcuts({
  meta_k: {
    usingInput: true,
    handler: () => { isOpen.value = !isOpen.value },
  },
})

// --- live query (owned by UCommandPalette, mirrored here for echo + filtering) ---
const paletteRef = ref<any>(null)
const queryStr = ref('')
watch(() => paletteRef.value?.query, (q: string) => { queryStr.value = q ?? '' })

// --- data ---
const loading = ref(false)
const reportItems = ref<any[]>([])
const agentItemsRaw = ref<any[]>([])
const instructionItems = ref<any[]>([])
const promptItems = ref<any[]>([])

// --- category tab (reset to "All" on every open) ---
const activeTab = ref<PaletteTab>('all')

// Each fetcher returns its rows; runFetches() decides whether they are still
// current before writing them.
async function fetchReports(q: string): Promise<any[]> {
  try {
    const qs = `/reports?filter=my&limit=${limitFor(activeTab.value, 'reports')}${q ? `&search=${encodeURIComponent(q)}` : ''}`
    const res: any = await useMyFetch(qs, { method: 'GET' })
    return res?.data?.value?.reports ?? []
  } catch { return [] }
}

async function fetchInstructions(q: string): Promise<any[]> {
  try {
    const qs = `/instructions?limit=${limitFor(activeTab.value, 'instructions')}&include_drafts=true${q ? `&search=${encodeURIComponent(q)}` : ''}`
    const res: any = await useMyFetch(qs, { method: 'GET' })
    return res?.data?.value?.items ?? []
  } catch { return [] }
}

async function fetchPrompts(q: string): Promise<any[]> {
  try {
    const qs = `/prompts?limit=${limitFor(activeTab.value, 'prompts')}${q ? `&search=${encodeURIComponent(q)}` : ''}`
    const res: any = await useMyFetch(qs, { method: 'GET' })
    return res?.data?.value?.prompts ?? []
  } catch { return [] }
}

// Dashboards (artifacts) the user can open: the dashboards page's own list,
// mine + shared with me (the endpoint takes one at a time), newest edit first.
const dashboardItems = ref<any[]>([])
async function fetchDashboards(q: string): Promise<any[]> {
  const limit = limitFor(activeTab.value, 'dashboards')
  try {
    const page = (filter: string) => useMyFetch(
      `/artifacts?filter=${filter}&sort=edited&limit=${limit}${q ? `&search=${encodeURIComponent(q)}` : ''}`,
      { method: 'GET' },
    )
    const [mine, shared]: any[] = await Promise.all([page('my'), page('shared')])
    const seen = new Set<string>()
    const when = (d: any) => new Date(d.edited_at || d.updated_at || d.created_at || 0).getTime()
    return [...(mine?.data?.value?.dashboards ?? []), ...(shared?.data?.value?.dashboards ?? [])]
      .filter((d: any) => !seen.has(d.id) && seen.add(d.id))
      .sort((a: any, b: any) => when(b) - when(a))
      .slice(0, limit)
  } catch { return [] }
}

async function fetchAgents() {
  try {
    // Reuse the shared agent list (deduped/cached in useAgent) instead of
    // firing another GET /data_sources — the layout already loads it.
    await initAgent()
    const list: any[] = (sharedAgents.value as any[]) ?? []
    // newest first for the "recent" default view
    agentItemsRaw.value = [...list].sort((a: any, b: any) =>
      new Date(b.created_at || 0).getTime() - new Date(a.created_at || 0).getTime())
  } catch { agentItemsRaw.value = [] }
}

// --- "In this report" (only while a report page has registered itself) ---
// `report:` limits the search to this report; the English prefix always works,
// a locale may add an alias (commandPalette.reportPrefix).
const scopePrefixes = computed(() => Array.from(new Set(['report:', t('commandPalette.reportPrefix')])))
const scope = computed(() => reportContext.value
  ? parseReportScope(queryStr.value, scopePrefixes.value)
  : { scoped: false, term: queryStr.value.trim() })

const inReportHits = ref<any[]>([])
async function fetchInReport(term: string): Promise<any[]> {
  const ctx = reportContext.value
  if (!ctx || !term) return []
  try {
    const res: any = await useMyFetch(
      `/reports/${ctx.id}/completions/search?limit=8&q=${encodeURIComponent(term)}`,
      { method: 'GET' },
    )
    // An answer about another report (navigated meanwhile) is dropped.
    if (reportContext.value?.id !== ctx.id) return []
    return Array.isArray(res?.data?.value) ? res.data.value : []
  } catch { return [] }
}

// What the active tab shows, given the report context and a `report:` scope.
const tabContext = computed(() => ({ inReport: !!reportContext.value, scoped: scope.value.scoped }))

// Run only the server searches the active tab shows; agents filter client-side.
// Opening, typing and switching tabs each start a run; only the newest may
// write results or clear the spinner, so a slow answer from an older run
// (another query, another tab's limit) never overwrites a newer one.
let runSeq = 0
async function runFetches() {
  const run = ++runSeq
  const { term } = scope.value
  const q = queryStr.value
  const jobs: Record<PaletteFetch, [() => Promise<any[]>, (rows: any[]) => void]> = {
    inReport: [() => fetchInReport(term), rows => { inReportHits.value = rows }],
    reports: [() => fetchReports(q), rows => { reportItems.value = rows }],
    prompts: [() => fetchPrompts(q), rows => { promptItems.value = rows }],
    instructions: [() => fetchInstructions(q), rows => { instructionItems.value = rows }],
    dashboards: [() => fetchDashboards(q), rows => { dashboardItems.value = rows }],
  }
  loading.value = true
  await Promise.all(fetchesForTab(activeTab.value, tabContext.value).map(async (f) => {
    const [load, store] = jobs[f]
    const rows = await load()
    if (run === runSeq) store(rows)
  }))
  if (run === runSeq) loading.value = false
}

let debounceT: any = null
watch(queryStr, () => {
  clearTimeout(debounceT)
  loading.value = true
  debounceT = setTimeout(runFetches, 200)
})

function setTab(tab: PaletteTab) {
  if (tab === activeTab.value) return
  activeTab.value = tab
  clearTimeout(debounceT)
  runFetches()
}

// ←/→ switch tabs while the caret is at the start of the query (or it's
// empty), so they never steal cursor movement inside typed text. The row is
// mirrored in RTL, so the arrows follow what the reader sees.
function onTabKeys(e: KeyboardEvent) {
  if (e.key !== 'ArrowLeft' && e.key !== 'ArrowRight') return
  if (e.metaKey || e.ctrlKey || e.altKey || e.shiftKey) return
  const input = e.target as HTMLInputElement | null
  if (!input || input.tagName !== 'INPUT') return
  if (input.value && (input.selectionStart !== 0 || input.selectionEnd !== 0)) return
  e.preventDefault()
  e.stopPropagation()
  const rtl = getComputedStyle(input).direction === 'rtl'
  setTab(stepTab(activeTab.value, e.key, rtl))
}

// On open: "All", a clear query, recents.
watch(isOpen, async (open) => {
  if (!open) return
  await nextTick()
  activeTab.value = 'all'
  paletteRef.value?.updateQuery?.('')
  queryStr.value = ''
  inReportHits.value = []
  await Promise.all([runFetches(), fetchAgents()])
})

// --- helpers ---
const { relativeTime: relTime } = useRelativeTime()
function truncate(s?: string, n = 60): string {
  if (!s) return ''
  return s.length > n ? s.slice(0, n).trimEnd() + '…' : s
}

const canCreateInstructions = computed(() => useCanAny('manage_instructions', 'data_source'))

// --- groups (all static: we search server-side ourselves) ---
const inReportGroup = computed(() => {
  const { term } = scope.value
  const commands: any[] = inReportHits.value.map((h: any) => ({
    id: `in-report-${h.completion_id}`,
    label: h.snippet,
    parts: snippetParts(h.snippet || '', term),
    icon: h.role === 'user' ? 'i-heroicons-user-circle' : 'i-heroicons-sparkles',
    suffix: relTime(h.created_at),
    jumpTo: h.completion_id,
  }))
  if (!term) {
    // Nothing typed yet: teach the scope prefix.
    commands.push({
      id: 'in-report-hint',
      label: t('commandPalette.reportScopeHint'),
      icon: 'i-heroicons-magnifying-glass',
      disabled: true,
    })
  }
  return { key: 'inReport', label: t('commandPalette.inThisReport'), static: true, commands }
})

const actionsGroup = computed(() => {
  const q = scope.value.term
  const commands: any[] = [
    {
      id: 'action-new-report',
      label: q ? t('commandPalette.newReportWithQuery', { query: q }) : t('commandPalette.newReport'),
      icon: 'i-heroicons-plus-circle',
      shortcuts: ['↵'],
      click: () => createReport(q),
    },
    {
      id: 'action-new-instruction',
      label: canCreateInstructions.value
        ? (q ? t('commandPalette.newInstructionWithQuery', { query: q }) : t('commandPalette.newInstruction'))
        : (q ? t('commandPalette.suggestInstructionWithQuery', { query: q }) : t('commandPalette.suggestInstruction')),
      icon: 'i-heroicons-pencil-square',
      suffix: canCreateInstructions.value ? undefined : t('commandPalette.suggestionHint'),
      click: () => openNewInstruction(q),
    },
  ]
  const allowed = actionsForTab(activeTab.value)
  const kept = commands.filter(c => allowed.includes(c.id === 'action-new-report' ? 'newReport' : 'newInstruction'))
  return { key: 'actions', label: t('commandPalette.actions'), static: true, commands: kept }
})

const reportsGroup = computed(() => ({
  key: 'reports',
  label: t('commandPalette.reports'),
  static: true,
  commands: reportItems.value.map((r: any) => ({
    id: `report-${r.id}`,
    label: r.title || 'Untitled report',
    icon: 'i-heroicons-document-text',
    suffix: relTime(r.created_at),
    to: `/reports/${r.id}`,
  })),
}))

const agentsGroup = computed(() => {
  const q = queryStr.value.trim().toLowerCase()
  const filtered = (q
    ? agentItemsRaw.value.filter((a: any) => (a.name || '').toLowerCase().includes(q))
    : agentItemsRaw.value
  ).slice(0, limitFor(activeTab.value, 'agents'))
  return {
    key: 'agents',
    label: t('commandPalette.agents'),
    static: true,
    commands: filtered.map((a: any) => ({
      id: `agent-${a.id}`,
      label: a.name,
      dsIconToken: a.icon_token,
      suffix: a.status === 'active' ? 'active' : 'inactive',
      to: `/agents/${a.id}`,
    })),
  }
})

const promptsGroup = computed(() => ({
  key: 'prompts',
  label: t('commandPalette.prompts'),
  static: true,
  commands: promptItems.value.map((p: any) => ({
    id: `prompt-${p.id}`,
    label: p.title || truncate(p.text),
    icon: 'i-heroicons-book-open',
    suffix: (p.parameters && p.parameters.length) ? `${p.parameters.length} ${p.parameters.length === 1 ? 'param' : 'params'}` : undefined,
    prompt: p,
  })),
}))

const instructionsGroup = computed(() => ({
  key: 'instructions',
  label: t('commandPalette.instructions'),
  static: true,
  commands: instructionItems.value.map((i: any) => ({
    id: `instruction-${i.id}`,
    label: i.title || truncate(i.text),
    icon: 'i-heroicons-chat-bubble-bottom-center-text',
    suffix: i.category || undefined,
    instruction: i,
  })),
}))

const dashboardsGroup = computed(() => ({
  key: 'dashboards',
  label: t('commandPalette.dashboards'),
  static: true,
  commands: dashboardItems.value.map((d: any) => ({
    id: `dashboard-${d.id}`,
    label: d.title || t('commandPalette.untitledDashboard'),
    icon: 'i-heroicons-chart-bar-square',
    suffix: relTime(d.edited_at || d.updated_at || d.created_at),
    to: `/d/${d.slug || d.id}`,
  })),
}))

const groupsByKey = computed<Record<string, any>>(() => ({
  inReport: inReportGroup.value,
  actions: actionsGroup.value,
  reports: reportsGroup.value,
  prompts: promptsGroup.value,
  agents: agentsGroup.value,
  instructions: instructionsGroup.value,
  dashboards: dashboardsGroup.value,
}))

const groups = computed(() => groupsForTab(activeTab.value, tabContext.value)
  .map(key => groupsByKey.value[key])
  .filter(g => g && g.commands.length))

// --- selection handling ---
function onSelect(option: any) {
  if (!option) return
  if (option.jumpTo) {
    const ctx = reportContext.value
    close()
    if (ctx) nextTick(() => ctx.jumpTo(option.jumpTo))
    return
  }
  if (option.click) { close(); option.click(); return }
  if (option.prompt) { selectPrompt(option.prompt); return }
  if (option.instruction) { openInstruction(option.instruction); return }
  if (option.to) { close(); router.push(option.to) }
}

// --- prompts ---
const { substitute, mergeMentions } = usePromptFill()
const showPromptParams = ref(false)
const promptForParams = ref<any>(null)

function selectPrompt(prompt: any) {
  if (prompt?.parameters && prompt.parameters.length) {
    // Fill parameters BEFORE creating the report. Close the palette first so
    // the parameter modal isn't trapped behind it.
    promptForParams.value = prompt
    close()
    nextTick(() => { showPromptParams.value = true })
    return
  }
  close()
  createReport(prompt.text || '', mergeMentions([], prompt.mentions || []))
}

function onPromptParamsConfirm(values: Record<string, any>) {
  const prompt = promptForParams.value
  showPromptParams.value = false
  if (!prompt) return
  const text = substitute(prompt.text || '', values)
  const mentions = mergeMentions([], prompt.mentions || [])
  close()
  createReport(text, mentions)
}

// --- actions ---
const { initAgent, selectedAgentObjects, agents: sharedAgents } = useAgent()
const { activeProjectId, newReportPayload } = useNewReportProjectContext()
const creatingReport = ref(false)
async function createReport(initialPrompt: string, mentions: any[] = []) {
  if (creatingReport.value) return
  creatingReport.value = true
  try {
    // Attach any data-source mentions carried by a consumed prompt so the new
    // report has the right context (dedup against the agent selector's ids).
    const promptDsIds = (mentions || [])
      .filter((g: any) => g?.name === 'DATA SOURCES')
      .flatMap((g: any) => (g.items || []).map((it: any) => it.id))
    // Inside a project the workspace-wide agent selection is dropped so the
    // project's default agents land on the report; a prompt's own data-source
    // mentions still win, since those are an explicit pick for this report.
    const dataSourceIds = activeProjectId.value
      ? []
      : selectedAgentObjects.value.map((a: any) => a.id)
    const allDsIds = Array.from(new Set([...dataSourceIds, ...promptDsIds]))
    const res: any = await useMyFetch('/reports', {
      method: 'POST',
      body: JSON.stringify({ title: 'untitled report', files: [], ...newReportPayload(allDsIds) }),
    })
    const data: any = res?.data?.value
    if (data?.id) {
      const query: any = {}
      if (initialPrompt) query.prompt = initialPrompt
      if (mentions && mentions.length) query.mentions = encodeURIComponent(JSON.stringify(mentions))
      router.push({ path: `/reports/${data.id}`, query: Object.keys(query).length ? query : undefined })
    }
  } finally {
    creatingReport.value = false
  }
}

// --- instruction modal ---
const showInstructionModal = ref(false)
const instructionModalInstruction = ref<any>(null)
const instructionInitialText = ref('')

function openNewInstruction(initialText: string) {
  close()
  instructionModalInstruction.value = null
  instructionInitialText.value = initialText
  nextTick(() => { showInstructionModal.value = true })
}
function openInstruction(instruction: any) {
  close()
  instructionInitialText.value = ''
  instructionModalInstruction.value = instruction
  nextTick(() => { showInstructionModal.value = true })
}
function onInstructionSaved() {
  showInstructionModal.value = false
}

</script>

<style scoped>
/* Stack input → tabs → results (see the palette-shell comment). */
.palette-shell :deep(.palette-root > *) { order: 3; }
.palette-shell :deep(.palette-root > :first-child) { order: 1; }
.palette-shell > .palette-tabs { order: 2; }
</style>
