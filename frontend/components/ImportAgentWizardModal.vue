<template>
  <!-- Import an agent from the zip "Export agent" downloads. Same shell and
       stepper as NewAgentWizardModal; the Tables step reuses the same
       AgentKnowledgeTabs once the agent exists. -->
  <UModal v-model="isOpen" :transition="false" :ui="{ width: expanded ? 'sm:max-w-[calc(100vw-2rem)]' : 'sm:max-w-3xl', margin: 'sm:my-4' }" :prevent-close="busy || step === 'tables'">
    <div class="p-5 overflow-y-auto" :class="step === 'tables' ? 'h-[calc(100dvh-2rem)] flex flex-col' : 'max-h-[calc(100dvh-2rem)]'" data-testid="import-agent-wizard">
      <div v-show="!expanded" class="flex items-center justify-between mb-1">
        <h3 class="text-lg font-semibold text-ink">{{ $t('agentImport.title') }}</h3>
        <button class="text-ink-muted hover:text-ink-2" :aria-label="$t('common.close')" @click="close">
          <UIcon name="heroicons-x-mark" class="w-5 h-5" />
        </button>
      </div>
      <p v-show="!expanded" class="text-sm text-ink-muted">{{ $t('agentImport.subtitle') }}</p>

      <!-- Stepper -->
      <nav v-show="!expanded" class="w-full my-5">
        <ol class="flex justify-center items-center gap-4 text-xs">
          <li v-for="(s, idx) in steps" :key="s.key" class="flex items-center gap-2">
            <span class="flex items-center gap-2">
              <span :class="circleClass(s.key)" class="w-5 h-5 rounded-full flex items-center justify-center">
                <UIcon v-if="isDone(s.key)" name="heroicons-check" class="w-3.5 h-3.5" />
                <span v-else>{{ idx + 1 }}</span>
              </span>
              <span :class="s.key === step ? 'text-ink' : 'text-ink-muted'">{{ s.label }}</span>
            </span>
            <span v-if="idx < steps.length - 1" class="mx-2 w-6 h-px bg-gray-200 dark:bg-gray-800"></span>
          </li>
        </ol>
      </nav>

      <div v-if="errorMessage" class="p-3 mb-4 rounded-lg text-sm bg-red-50 text-red-700 dark:bg-red-500/10 dark:text-red-400" data-testid="import-error">
        {{ errorMessage }}
      </div>

      <!-- ── Step 1: Upload ─────────────────────────────────────── -->
      <div v-if="step === 'upload'">
        <label
          class="flex flex-col items-center justify-center gap-2 py-12 px-4 rounded-lg border-2 border-dashed cursor-pointer transition-colors"
          :class="dragging ? 'border-blue-400 bg-blue-50/60 dark:bg-blue-500/10' : 'border-line hover:border-line-strong bg-surface-subtle'"
          @dragover.prevent="dragging = true"
          @dragleave.prevent="dragging = false"
          @drop.prevent="onDrop"
        >
          <Spinner v-if="busy" class="h-5 w-5 text-ink-muted" />
          <UIcon v-else name="i-heroicons-arrow-up-tray" class="w-6 h-6 text-ink-muted" />
          <span class="text-sm font-medium text-ink">{{ busy ? $t('agentImport.reading') : $t('agentImport.dropHere') }}</span>
          <span class="text-xs text-ink-muted">{{ $t('agentImport.dropHint') }}</span>
          <input ref="fileInput" type="file" accept=".zip,application/zip" class="sr-only" data-testid="import-file-input" :disabled="busy" @change="onPick" />
        </label>
        <div class="flex justify-start pt-4">
          <button class="text-sm text-ink-muted hover:text-ink-2" @click="close">{{ $t('common.cancel') }}</button>
        </div>
      </div>

      <!-- ── Step 2: Map connections ────────────────────────────── -->
      <div v-else-if="step === 'connections' && preview">
        <BundleSummary :preview="preview" :file-name="file?.name" />

        <h4 class="text-sm font-semibold text-ink mt-5 mb-1">{{ $t('agentImport.mapTitle') }}</h4>
        <p class="text-xs text-ink-muted mb-3">{{ $t('agentImport.mapHint') }}</p>

        <div class="rounded-lg border border-line divide-y divide-gray-100 dark:divide-gray-800">
          <div v-for="c in preview.connections" :key="c.name" class="p-3 flex flex-col sm:flex-row sm:items-center gap-3" :data-testid="`map-row-${c.name}`">
            <div class="sm:w-2/5 min-w-0">
              <div class="text-sm font-medium text-ink truncate">{{ c.name }}</div>
              <div class="text-2xs text-ink-muted">
                {{ $t('agentImport.tablesInBundle', { n: c.tables_total }) }}<template v-if="c.tools_total"> · {{ $t('agentImport.toolsInBundle', { n: c.tools_total }) }}</template>
              </div>
            </div>
            <UIcon name="i-heroicons-arrow-right" class="hidden sm:block w-4 h-4 text-ink-subtle shrink-0 rtl:rotate-180" />
            <div class="flex-1 min-w-0">
              <USelectMenu
                :model-value="mapping[c.name] ?? SKIP"
                :options="connectionOptions"
                value-attribute="id"
                option-attribute="name"
                searchable
                :searchable-placeholder="$t('agentImport.searchConnections')"
                size="sm"
                :disabled="busy"
                :data-testid="`map-select-${c.name}`"
                @update:model-value="(v: string) => setMapping(c.name, v)"
              >
                <template #label>
                  <span v-if="selectedConnection(c.name)" class="flex items-center gap-1.5 min-w-0">
                    <DataSourceIcon :type="selectedConnection(c.name)!.type" :connector-key="selectedConnection(c.name)!.connector_key" class="h-3.5 shrink-0" />
                    <span class="truncate">{{ selectedConnection(c.name)!.name }}</span>
                  </span>
                  <span v-else class="text-ink-muted">{{ $t('agentImport.skipConnection') }}</span>
                </template>
                <template #option="{ option }">
                  <span class="flex items-center gap-2 min-w-0">
                    <DataSourceIcon v-if="option.type" :type="option.type" :connector-key="option.connector_key" class="h-4 shrink-0" />
                    <UIcon v-else name="i-heroicons-no-symbol" class="w-4 h-4 text-ink-muted shrink-0" />
                    <span class="truncate">{{ option.name }}</span>
                  </span>
                </template>
              </USelectMenu>
              <!-- Match signal for the chosen connection -->
              <div class="mt-1 text-2xs" :data-testid="`map-status-${c.name}`">
                <span v-if="!mapping[c.name]" class="text-ink-muted">{{ $t('agentImport.willSkip') }}</span>
                <span v-else-if="c.error" class="text-red-600 dark:text-red-400">{{ $t('agentImport.noAccess') }}</span>
                <span v-else-if="c.indexing_pending" class="text-amber-600 dark:text-amber-400">{{ $t('agentImport.indexingPending') }}</span>
                <span v-else-if="c.tables_found === c.tables_total" class="text-green-600 dark:text-green-400">
                  <UIcon name="i-heroicons-check-circle" class="w-3.5 h-3.5 align-text-bottom" /> {{ $t('agentImport.allTablesFound', { n: c.tables_total }) }}
                </span>
                <span v-else class="text-amber-600 dark:text-amber-400">
                  {{ $t('agentImport.someTablesFound', { found: c.tables_found ?? 0, total: c.tables_total }) }}
                  <UTooltip v-if="c.missing_tables.length" :text="c.missing_tables.join(', ')">
                    <span class="underline decoration-dotted cursor-help">{{ $t('agentImport.showMissing') }}</span>
                  </UTooltip>
                </span>
                <span v-if="c.suggested && mapping[c.name]" class="text-ink-muted"> · {{ $t('agentImport.autoMatched') }}</span>
              </div>
            </div>
          </div>
        </div>

        <button v-if="canCreateConnection" type="button" class="mt-2 inline-flex items-center gap-1.5 text-xs text-blue-600 hover:text-blue-700" :disabled="busy" @click="showAddConnection = true">
          <UIcon name="heroicons-plus-circle" class="h-3.5 w-3.5" />
          <span>{{ $t('agentImport.createConnection') }}</span>
        </button>

        <div class="flex justify-start rtl:flex-row-reverse gap-3 items-center pt-4 mt-4 border-t border-line">
          <UButton color="blue" size="xs" :disabled="!anyMapped || busy" :loading="busy" data-testid="import-continue" @click="step = 'review'">
            {{ $t('agentImport.continue') }}
          </UButton>
          <button class="text-sm text-ink-muted hover:text-ink-2" :disabled="busy" @click="reset">{{ $t('agentImport.back') }}</button>
          <span v-if="!anyMapped" class="text-xs text-ink-muted">{{ $t('agentImport.mapAtLeastOne') }}</span>
        </div>
      </div>

      <!-- ── Step 3: Review & import ────────────────────────────── -->
      <div v-else-if="step === 'review' && preview">
        <div class="mb-4">
          <label class="block text-sm font-medium text-ink-2 mb-1">{{ $t('agentImport.nameLabel') }} <span class="text-red-500">*</span></label>
          <UInput v-model="agentName" size="lg" :disabled="busy" data-testid="import-name" />
          <p v-if="preview.name_taken && agentName.trim() === preview.agent_name" class="mt-1 text-xs text-amber-600 dark:text-amber-400">
            {{ $t('agentImport.nameTaken', { name: preview.agent_name }) }}
          </p>
          <p v-else-if="preview.name_taken" class="mt-1 text-xs text-ink-muted">{{ $t('agentImport.nameRenamed', { name: preview.agent_name }) }}</p>
        </div>

        <h4 class="text-sm font-semibold text-ink mb-2">{{ $t('agentImport.whatToImport') }}</h4>
        <div class="rounded-lg border border-line divide-y divide-gray-100 dark:divide-gray-800">
          <div class="p-3 flex items-center justify-between gap-3">
            <span class="text-sm text-ink-2">{{ $t('agentImport.tablesLine', { n: mappedTables, conns: mappedCount }) }}</span>
            <UIcon name="i-heroicons-check" class="w-4 h-4 text-green-600" />
          </div>
          <label v-for="opt in includeRows" :key="opt.key" class="p-3 flex items-center justify-between gap-3 cursor-pointer" :class="opt.count ? '' : 'opacity-50'">
            <span class="text-sm text-ink-2">{{ opt.label }}</span>
            <UToggle v-model="include[opt.key]" :disabled="!opt.count || busy" size="xs" color="blue" />
          </label>
        </div>

        <label class="mt-3 flex items-start gap-2 cursor-pointer">
          <UToggle v-model="publishInstructions" size="xs" color="blue" class="mt-0.5" :disabled="busy" data-testid="import-publish" />
          <span>
            <span class="block text-sm text-ink-2">{{ $t('agentImport.publishLabel') }}</span>
            <span class="block text-xs text-ink-muted">{{ $t('agentImport.publishHint') }}</span>
          </span>
        </label>

        <div v-if="preview.members_unresolved.length || skippedNames.length" class="mt-4 p-3 rounded-lg bg-amber-50 dark:bg-amber-500/10 text-xs text-amber-800 dark:text-amber-300 space-y-1">
          <div v-if="skippedNames.length">{{ $t('agentImport.skippedConnections', { names: skippedNames.join(', ') }) }}</div>
          <div v-if="preview.members_unresolved.length">{{ $t('agentImport.membersLeftOut', { n: preview.members_unresolved.length }) }}</div>
        </div>

        <div class="flex justify-start rtl:flex-row-reverse gap-3 items-center pt-4 mt-4 border-t border-line">
          <UButton color="blue" size="xs" :loading="busy" :disabled="!agentName.trim()" data-testid="import-submit" @click="runImport">
            {{ $t('agentImport.importButton') }}
          </UButton>
          <button class="text-sm text-ink-muted hover:text-ink-2" :disabled="busy" @click="step = 'connections'">{{ $t('agentImport.back') }}</button>
        </div>
      </div>

      <!-- ── Step 4: Review tables on the created agent ─────────── -->
      <div v-else-if="step === 'tables' && result" class="flex-1 min-h-0 flex flex-col">
        <div v-show="!expanded" class="mb-3 p-3 rounded-lg bg-green-50 dark:bg-green-500/10 text-sm text-green-800 dark:text-green-300" data-testid="import-result">
          <div class="font-medium">{{ $t('agentImport.doneTitle', { name: result.name }) }}</div>
          <div class="text-xs mt-0.5">{{ resultLine }}</div>
          <ul v-if="result.warnings.length" class="mt-2 text-xs text-amber-800 dark:text-amber-300 list-disc ps-4 space-y-0.5 max-h-24 overflow-y-auto">
            <li v-for="(w, i) in result.warnings" :key="i">{{ w }}</li>
          </ul>
        </div>
        <div class="flex-1 min-h-0 flex flex-col bg-white dark:bg-gray-900 rounded-lg">
          <AgentKnowledgeTabs :ds-id="result.data_source_id" fill fullscreen-in-place :continue-label="$t('agentImport.finish')" @fullscreen-change="expanded = $event" @saved="finish">
            <template #continue-actions>
              <button type="button" class="text-sm text-ink-muted hover:text-ink-2" data-testid="import-open-agent" @click="finish">{{ $t('agentImport.openAgent') }}</button>
            </template>
          </AgentKnowledgeTabs>
        </div>
      </div>
    </div>

    <AddConnectionModal v-model="showAddConnection" :skip-success-step="true" @created="onConnectionCreated" />
  </UModal>
</template>

<script setup lang="ts">
import Spinner from '~/components/Spinner.vue'
import AgentKnowledgeTabs from '@/components/datasources/AgentKnowledgeTabs.vue'
import AddConnectionModal from '~/components/AddConnectionModal.vue'
import DataSourceIcon from '~/components/DataSourceIcon.vue'
import BundleSummary from '~/components/agents/AgentBundleSummary.vue'
import { useErrorMessage } from '~/composables/useErrorMessage'

const props = defineProps<{ modelValue: boolean, folderId?: string | null }>()
const emit = defineEmits<{
  (e: 'update:modelValue', value: boolean): void
  (e: 'finished', dsId: string): void
}>()

const { t } = useI18n()
const { getErrorMessage } = useErrorMessage()

const isOpen = computed({ get: () => props.modelValue, set: (v) => emit('update:modelValue', v) })

// ── Types (mirror app/schemas/agent_bundle_import_schema.py) ─────────────
interface BundleConnection {
  name: string; tables_total: number; tools_total: number
  mapped_connection_id: string | null; suggested: boolean
  tables_found: number | null; indexing_pending: boolean
  missing_tables: string[]; error: string | null
}
interface Preview {
  agent_name: string; suggested_name: string; name_taken: boolean
  description: string | null
  connections: BundleConnection[]
  counts: { instructions: number, folders: number, evals: number, eval_cases: number, queries: number, conversation_starters: number, members: number }
  members_unresolved: string[]; warnings: string[]
}
interface ImportResult {
  data_source_id: string; name: string
  instructions_created: number; instructions_failed: number; folders_created: number
  references_resolved: number; references_unresolved: number
  evals_created: number; queries_created: number
  tables_active: number; tables_missing: number; warnings: string[]
}
interface Connection { id: string, name: string, type: string, connector_key?: string | null }

// ── Steps ─────────────────────────────────────────────────────────────────
type Step = 'upload' | 'connections' | 'review' | 'tables'
const order: Step[] = ['upload', 'connections', 'review', 'tables']
const steps = computed(() => [
  { key: 'upload', label: t('agentImport.stepUpload') },
  { key: 'connections', label: t('agentImport.stepConnections') },
  { key: 'review', label: t('agentImport.stepReview') },
  { key: 'tables', label: t('agentImport.stepTables') },
])
const step = ref<Step>('upload')
const isDone = (key: string) => order.indexOf(key as Step) < order.indexOf(step.value)
function circleClass(key: string) {
  if (isDone(key)) return 'bg-green-100 text-green-600 dark:bg-green-500/10 dark:text-green-400'
  if (key === step.value) return 'bg-gray-900 text-white dark:bg-white dark:text-gray-900'
  return 'bg-gray-100 text-gray-500 dark:bg-gray-800 dark:text-gray-400'
}

// ── State ─────────────────────────────────────────────────────────────────
const SKIP = '__skip__'
const expanded = ref(false)
const busy = ref(false)
const dragging = ref(false)
const errorMessage = ref('')
const file = ref<File | null>(null)
const fileInput = ref<HTMLInputElement | null>(null)
const preview = ref<Preview | null>(null)
const result = ref<ImportResult | null>(null)
const connections = ref<Connection[]>([])
// bundle connection name -> connection id (null = skip)
const mapping = reactive<Record<string, string | null>>({})
const agentName = ref('')
const publishInstructions = ref(false)
const include = reactive({ instructions: true, evals: true, queries: true, starters: true })
const showAddConnection = ref(false)
const canCreateConnection = computed(() => useCan('create_connections'))

function reset() {
  step.value = 'upload'
  expanded.value = false
  busy.value = false
  errorMessage.value = ''
  file.value = null
  preview.value = null
  result.value = null
  for (const k of Object.keys(mapping)) delete mapping[k]
  agentName.value = ''
  publishInstructions.value = false
  Object.assign(include, { instructions: true, evals: true, queries: true, starters: true })
  if (fileInput.value) fileInput.value.value = ''
}

watch(isOpen, (open) => { if (open) { reset(); loadConnections() } })

function close() {
  // Once the agent exists, closing still lands the user on it.
  if (result.value) return finish()
  isOpen.value = false
}

async function loadConnections() {
  const { data } = await useMyFetch('/connections', { method: 'GET' })
  connections.value = ((data.value || []) as Connection[])
}

const connectionOptions = computed(() => [
  { id: SKIP, name: t('agentImport.skipConnection'), type: '' },
  ...connections.value,
])
const selectedConnection = (bundleName: string) =>
  connections.value.find(c => c.id === mapping[bundleName]) || null

// ── Upload + preview ──────────────────────────────────────────────────────
function onPick(e: Event) {
  const f = (e.target as HTMLInputElement).files?.[0]
  if (f) start(f)
}
function onDrop(e: DragEvent) {
  dragging.value = false
  const f = e.dataTransfer?.files?.[0]
  if (f) start(f)
}

async function start(f: File) {
  file.value = f
  const ok = await runPreview()
  if (!ok) { file.value = null; if (fileInput.value) fileInput.value.value = ''; return }
  // Adopt the server's suggestions as the starting mapping.
  for (const c of preview.value!.connections) mapping[c.name] = c.mapped_connection_id
  agentName.value = preview.value!.suggested_name
  include.instructions = preview.value!.counts.instructions > 0
  include.evals = preview.value!.counts.evals > 0
  include.queries = preview.value!.counts.queries > 0
  include.starters = preview.value!.counts.conversation_starters > 0
  step.value = 'connections'
}

function formFor(options: Record<string, any>) {
  const fd = new FormData()
  fd.append('file', file.value as File)
  fd.append('options', JSON.stringify(options))
  return fd
}

async function runPreview(): Promise<boolean> {
  if (!file.value) return false
  busy.value = true
  errorMessage.value = ''
  try {
    const hasMapping = Object.keys(mapping).length > 0
    const { data, error } = await useMyFetch('/agents/import/preview', {
      method: 'POST',
      body: formFor(hasMapping ? { connection_map: { ...mapping } } : {}),
    })
    if (error.value) { errorMessage.value = getErrorMessage(error.value, t('agentImport.previewFailed')); return false }
    preview.value = data.value as Preview
    return true
  } finally {
    busy.value = false
  }
}

async function setMapping(bundleName: string, id: string) {
  mapping[bundleName] = id === SKIP ? null : id
  await runPreview()
}

async function onConnectionCreated(conn: any) {
  try { await useAuth().getSession() } catch {}
  await loadConnections()
  // Map the first bundle connection still unmapped to the new one.
  const target = preview.value?.connections.find(c => !mapping[c.name])
  if (conn?.id && target) await setMapping(target.name, conn.id)
}

const anyMapped = computed(() => Object.values(mapping).some(Boolean))
const mappedCount = computed(() => Object.values(mapping).filter(Boolean).length)
const skippedNames = computed(() => Object.entries(mapping).filter(([, v]) => !v).map(([k]) => k))
const mappedTables = computed(() =>
  (preview.value?.connections || []).filter(c => mapping[c.name]).reduce((n, c) => n + (c.tables_found ?? c.tables_total), 0))

const includeRows = computed(() => {
  const c = preview.value?.counts
  if (!c) return []
  return [
    { key: 'instructions' as const, count: c.instructions, label: t('agentImport.includeInstructions', { n: c.instructions, folders: c.folders }) },
    { key: 'evals' as const, count: c.evals, label: t('agentImport.includeEvals', { n: c.evals, cases: c.eval_cases }) },
    { key: 'queries' as const, count: c.queries, label: t('agentImport.includeQueries', { n: c.queries }) },
    { key: 'starters' as const, count: c.conversation_starters, label: t('agentImport.includeStarters', { n: c.conversation_starters }) },
  ]
})

// ── Import ────────────────────────────────────────────────────────────────
async function runImport() {
  if (!file.value || busy.value) return
  busy.value = true
  errorMessage.value = ''
  try {
    const { data, error } = await useMyFetch('/agents/import', {
      method: 'POST',
      body: formFor({
        name: agentName.value.trim(),
        connection_map: { ...mapping },
        include_instructions: include.instructions,
        include_evals: include.evals,
        include_queries: include.queries,
        include_conversation_starters: include.starters,
        publish_instructions: publishInstructions.value,
        folder_id: props.folderId || null,
      }),
    })
    if (error.value) { errorMessage.value = getErrorMessage(error.value, t('agentImport.importFailed')); return }
    result.value = data.value as ImportResult
    // The importer now owns the agent — refresh per-agent permissions.
    try { await useAuth().getSession() } catch {}
    step.value = 'tables'
  } finally {
    busy.value = false
  }
}

const resultLine = computed(() => {
  const r = result.value
  if (!r) return ''
  return t('agentImport.doneLine', {
    tables: r.tables_active, instructions: r.instructions_created, evals: r.evals_created, queries: r.queries_created,
  })
})

function finish() {
  const id = result.value?.data_source_id
  isOpen.value = false
  if (id) emit('finished', id)
}
</script>
