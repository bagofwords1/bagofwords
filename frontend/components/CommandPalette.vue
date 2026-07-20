<template>
  <UModal
    v-model="isOpen"
    :ui="{ width: 'sm:max-w-2xl', container: 'items-start', margin: 'sm:mt-[12vh]' }"
  >
    <UCommandPalette
      ref="paletteRef"
      :groups="groups"
      :placeholder="t('commandPalette.placeholder')"
      :empty-state="{ icon: 'i-heroicons-magnifying-glass', label: t('commandPalette.emptyLabel'), queryLabel: t('commandPalette.emptyQueryLabel') }"
      icon="i-heroicons-magnifying-glass"
      :loading="loading"
      class="!h-auto"
      :ui="{
        input: { size: 'sm:text-[13px]' },
        group: {
          container: 'text-[13px] text-gray-700 dark:text-gray-200',
          label: 'px-2.5 my-1.5 text-[11px] font-semibold text-gray-700 dark:text-white',
          command: { icon: { base: 'flex-shrink-0 w-4 h-4' } }
        }
      }"
      @update:model-value="onSelect"
      @close="close"
    >
      <!-- Agents use the real data-source logo for their type -->
      <template #agents-icon="{ command }">
        <DataSourceIcon :type="command.dsType" :icon="command.dsIcon" class="h-4 w-4 flex-shrink-0" />
      </template>
    </UCommandPalette>

    <!-- Footer keyboard hints -->
    <div class="flex items-center gap-4 px-4 py-2 border-t border-gray-100 dark:border-gray-800 text-[11px] text-gray-400 bg-gray-50/60 dark:bg-gray-900 rounded-b-lg">
      <span><kbd class="font-sans">↑↓</kbd> navigate</span>
      <span><kbd class="font-sans">↵</kbd> open</span>
      <span><kbd class="font-sans">esc</kbd> close</span>
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

const { t } = useI18n()
const router = useRouter()
const { isOpen, close } = useCommandPalette()

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

async function fetchReports(q: string) {
  try {
    const qs = `/reports?filter=my&limit=3${q ? `&search=${encodeURIComponent(q)}` : ''}`
    const res: any = await useMyFetch(qs, { method: 'GET' })
    reportItems.value = res?.data?.value?.reports ?? []
  } catch { reportItems.value = [] }
}

async function fetchInstructions(q: string) {
  try {
    const qs = `/instructions?limit=3&include_drafts=true${q ? `&search=${encodeURIComponent(q)}` : ''}`
    const res: any = await useMyFetch(qs, { method: 'GET' })
    instructionItems.value = res?.data?.value?.items ?? []
  } catch { instructionItems.value = [] }
}

async function fetchPrompts(q: string) {
  try {
    const qs = `/prompts?limit=5${q ? `&search=${encodeURIComponent(q)}` : ''}`
    const res: any = await useMyFetch(qs, { method: 'GET' })
    promptItems.value = res?.data?.value?.prompts ?? []
  } catch { promptItems.value = [] }
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

// Debounced server-side search for reports + instructions; agents filter client-side.
let debounceT: any = null
watch(queryStr, (q) => {
  clearTimeout(debounceT)
  loading.value = true
  debounceT = setTimeout(async () => {
    await Promise.all([fetchReports(q), fetchInstructions(q), fetchPrompts(q)])
    loading.value = false
  }, 200)
})

// On open: clear stale query and load recents.
watch(isOpen, async (open) => {
  if (!open) return
  await nextTick()
  paletteRef.value?.updateQuery?.('')
  queryStr.value = ''
  loading.value = true
  await Promise.all([fetchReports(''), fetchInstructions(''), fetchPrompts(''), fetchAgents()])
  loading.value = false
})

// --- helpers ---
const _df = useFormatDate()
function relTime(iso?: string): string {
  if (!iso) return ''
  const d = new Date(iso).getTime()
  if (Number.isNaN(d)) return ''
  const s = Math.floor((Date.now() - d) / 1000)
  if (s < 60) return 'just now'
  const m = Math.floor(s / 60); if (m < 60) return `${m}m ago`
  const h = Math.floor(m / 60); if (h < 24) return `${h}h ago`
  const days = Math.floor(h / 24); if (days < 7) return `${days}d ago`
  return _df.formatDate(iso)
}
function truncate(s?: string, n = 60): string {
  if (!s) return ''
  return s.length > n ? s.slice(0, n).trimEnd() + '…' : s
}

const canCreateInstructions = computed(() => useCanAny('manage_instructions', 'data_source'))

// --- groups (all static: we search server-side ourselves) ---
const actionsGroup = computed(() => {
  const q = queryStr.value.trim()
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
  return { key: 'actions', label: t('commandPalette.actions'), static: true, commands }
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
  ).slice(0, 6)
  return {
    key: 'agents',
    label: t('commandPalette.agents'),
    static: true,
    commands: filtered.map((a: any) => ({
      id: `agent-${a.id}`,
      label: a.name,
      dsType: a.type,
      dsIcon: a.icon,
      suffix: a.status === 'active' ? 'active' : 'inactive',
      to: `/agents/${a.id}`,
    })),
  }
})

const promptsGroup = computed(() => ({
  key: 'prompts',
  label: t('commandPalette.prompts'),
  static: true,
  commands: promptItems.value.slice(0, 5).map((p: any) => ({
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

const groups = computed(() => {
  const g: any[] = [actionsGroup.value]
  if (reportsGroup.value.commands.length) g.push(reportsGroup.value)
  if (promptsGroup.value.commands.length) g.push(promptsGroup.value)
  if (agentsGroup.value.commands.length) g.push(agentsGroup.value)
  if (instructionsGroup.value.commands.length) g.push(instructionsGroup.value)
  return g
})

// --- selection handling ---
function onSelect(option: any) {
  if (!option) return
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
const creatingReport = ref(false)
async function createReport(initialPrompt: string, mentions: any[] = []) {
  if (creatingReport.value) return
  creatingReport.value = true
  try {
    const dataSourceIds = selectedAgentObjects.value.map((a: any) => a.id)
    // Attach any data-source mentions carried by a consumed prompt so the new
    // report has the right context (dedup against the agent selector's ids).
    const promptDsIds = (mentions || [])
      .filter((g: any) => g?.name === 'DATA SOURCES')
      .flatMap((g: any) => (g.items || []).map((it: any) => it.id))
    const allDsIds = Array.from(new Set([...dataSourceIds, ...promptDsIds]))
    const res: any = await useMyFetch('/reports', {
      method: 'POST',
      body: JSON.stringify({ title: 'untitled report', files: [], data_sources: allDsIds }),
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
