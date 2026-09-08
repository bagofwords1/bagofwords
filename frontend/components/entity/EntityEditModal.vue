<!--
  One form for a saved query, laid out as a workbench: the metadata (title,
  agents, status, description) as a strip on top, the code with its declared
  parameters beside it in the middle, the results across the bottom. The same
  frame the report's query editor uses, so create and edit look alike:

  - create (`entityId` null, `dsId` set): the agent panel's "New query". Run
    previews through the stateless POST /entities/preview; Save mints the row
    with POST /entities and runs it once so it lands with data. Same two
    tiers as the report: entity managers publish, anyone with access to the
    agents suggests (the backend decides; the footer only says which).
  - edit (`entityId` + `detail`): the query's Edit button. Run previews
    through POST /entities/{id}/preview; Save is PUT /entities/{id}, plus a
    run when the code changed so the snapshot follows it.

  Parameters travel with the code: Run sends the declarations and the test
  values so what it shows is what the saved query will run.
-->
<template>
  <UModal v-model="open" :ui="{ width: 'sm:max-w-6xl', height: 'sm:h-[90vh]' }">
    <div class="h-full flex flex-col bg-white dark:bg-gray-900" data-testid="entity-edit-modal" @keydown="onKeydown">
      <!-- Header: what this is and where it lives -->
      <div class="px-4 py-2.5 border-b border-gray-200 dark:border-gray-700 flex items-center gap-2 flex-shrink-0">
        <div class="text-sm font-medium text-gray-800 dark:text-gray-200" data-testid="entity-edit-title">
          {{ isCreate ? (canCreateEntities ? $t('queries.newQuery') : $t('queries.suggestQuery')) : $t('queries.editQuery') }}
        </div>
        <template v-if="selectedAgents.length">
          <span class="text-gray-300 dark:text-gray-600">·</span>
          <span
            v-for="ds in selectedAgents"
            :key="ds.id"
            class="inline-flex items-center gap-1 bg-blue-50 dark:bg-blue-900/40 text-blue-700 dark:text-blue-300 text-[11px] font-medium px-1.5 py-0.5 rounded"
            data-testid="entity-edit-agent-chip"
          >
            <DataSourceIcon :type="ds.type" :icon="ds.icon" class="h-3" />
            {{ ds.name }}
          </span>
        </template>
        <button
          type="button"
          class="ms-auto text-gray-400 hover:text-gray-600 dark:hover:text-gray-300 rounded p-1"
          :aria-label="$t('entityCreate.close')"
          @click="open = false"
        >
          <Icon name="heroicons-x-mark" class="w-4 h-4" />
        </button>
      </div>

      <!-- Metadata strip: title, description, agents, status on one row -->
      <div class="px-4 py-2.5 border-b border-gray-200 dark:border-gray-700 flex-shrink-0">
        <EntityForm v-model="form" :show-status="canCreateEntities" compact />
      </div>

      <!-- Workbench: code (+ params) over results -->
      <div class="flex-1 min-h-0 flex flex-col">
        <div class="flex-1 min-h-0 flex" :style="{ flexBasis: '55%' }">
          <section class="flex-1 min-w-0 flex flex-col">
            <div class="h-9 px-4 flex items-center gap-2 border-b border-gray-200 dark:border-gray-700 bg-gray-50 dark:bg-gray-800/60 flex-shrink-0">
              <span class="text-[11px] font-medium uppercase tracking-wide text-gray-600 dark:text-gray-300">{{ $t('queries.codeLabel') }}</span>
              <span class="text-[11px] font-mono text-gray-400 dark:text-gray-500">generate_df · {{ editorLang || 'python' }}</span>
              <button
                type="button"
                data-testid="entity-edit-params-toggle"
                class="ms-auto inline-flex items-center gap-1.5 px-2.5 py-1 text-[11px] rounded border transition-colors"
                :class="paramsOpen
                  ? 'bg-blue-50 dark:bg-blue-900/40 border-blue-200 dark:border-blue-800 text-blue-700 dark:text-blue-300'
                  : 'border-gray-200 dark:border-gray-700 text-gray-600 dark:text-gray-300 hover:bg-gray-100 dark:hover:bg-gray-800'"
                :aria-pressed="paramsOpen"
                @click="paramsOpen = !paramsOpen"
              >
                <Icon name="heroicons-adjustments-horizontal" class="w-3 h-3" />
                {{ $t('queries.params') }}<span v-if="declaredCount" class="tabular-nums opacity-80">({{ declaredCount }})</span>
              </button>
              <button
                type="button"
                data-testid="entity-edit-run"
                class="inline-flex items-center gap-1.5 px-2.5 py-1 text-[11px] rounded border border-gray-300 dark:border-gray-600 bg-white dark:bg-gray-900 text-gray-700 dark:text-gray-200 hover:bg-gray-50 dark:hover:bg-gray-800 disabled:opacity-50"
                :disabled="running || !code.trim()"
                :title="$t('queries.runShortcut')"
                @click="run"
              >
                <Icon v-if="running" name="heroicons-arrow-path" class="w-3 h-3 animate-spin" />
                <Icon v-else name="heroicons-play" class="w-3 h-3" />
                <span>{{ running ? $t('queries.running') : $t('queries.run') }}</span>
                <kbd class="font-mono text-[10px] text-gray-400 dark:text-gray-500">{{ shortcutLabel }}</kbd>
              </button>
            </div>
            <ClientOnly>
              <div class="flex-1 min-h-0">
                <MonacoEditor
                  v-model="code"
                  :lang="editorLang || 'python'"
                  :options="{ theme: 'vs', automaticLayout: true, minimap: { enabled: false }, wordWrap: 'on', fontSize: 13, scrollBeyondLastLine: false, padding: { top: 8 } }"
                  style="height: 100%"
                />
              </div>
            </ClientOnly>
          </section>

          <!-- Parameters, beside the code that reads them -->
          <QueryParamsPanel
            v-if="paramsOpen"
            class="w-[300px] flex-shrink-0 border-s border-gray-200 dark:border-gray-700 bg-gray-50/60 dark:bg-gray-800/40"
            data-testid="entity-edit-params"
            :specs="paramSpecs"
            :test-values="paramTestValues"
            :code="code"
            :applied-params="appliedParams"
            :running="running"
            run-mode="preview"
            @run="run"
          />
        </div>

        <!-- Results, across the full width -->
        <section class="min-h-0 flex flex-col border-t border-gray-200 dark:border-gray-700" :style="{ flexBasis: '45%' }" data-testid="entity-edit-results">
          <div class="h-9 px-4 flex items-center gap-2 border-b border-gray-200 dark:border-gray-700 bg-gray-50 dark:bg-gray-800/60 text-[11px] text-gray-600 dark:text-gray-300 flex-shrink-0">
            <span class="font-medium uppercase tracking-wide">{{ $t('queries.resultsHeading') }}</span>
            <span v-if="lastRunAt" class="text-gray-400 dark:text-gray-500">{{ $t('queries.ranAt', { when: lastRunAt }) }}</span>
            <span v-if="result" class="ms-auto tabular-nums" data-testid="entity-edit-result-count">
              {{ $t('queries.previewRows', { n: result.info?.total_rows ?? result.rows?.length ?? 0 }) }}
              · {{ $t('queries.previewColumns', { n: result.columns?.length ?? 0 }) }}
            </span>
          </div>
          <div class="flex-1 min-h-0 overflow-auto">
            <div v-if="runError" class="p-4 text-xs text-red-600 dark:text-red-400 whitespace-pre-wrap" data-testid="entity-edit-run-error">{{ runError }}</div>
            <table v-else-if="result && result.columns" class="min-w-full text-xs" data-testid="entity-edit-result">
              <thead class="bg-white dark:bg-gray-900 sticky top-0">
                <tr>
                  <th v-for="col in result.columns" :key="col.field" class="px-4 py-1.5 text-start text-[11px] font-medium text-gray-600 dark:text-gray-300 border-b border-gray-200 dark:border-gray-700 whitespace-nowrap">{{ col.headerName || col.field }}</th>
                </tr>
              </thead>
              <tbody>
                <tr v-for="(row, i) in (result.rows || []).slice(0, 200)" :key="i" class="border-b border-gray-100 dark:border-gray-800 hover:bg-gray-50 dark:hover:bg-gray-800/60">
                  <td v-for="col in result.columns" :key="col.field" class="px-4 py-1 text-gray-800 dark:text-gray-200 whitespace-nowrap tabular-nums">{{ row[col.field] }}</td>
                </tr>
                <tr v-if="!(result.rows || []).length">
                  <td :colspan="result.columns.length" class="px-4 py-6 text-center text-gray-400 dark:text-gray-500">{{ $t('queries.noRows') }}</td>
                </tr>
              </tbody>
            </table>
            <div v-else class="h-full flex items-center justify-center text-xs text-gray-400 dark:text-gray-500" data-testid="entity-edit-empty">
              {{ $t('queries.runToPreview') }}
            </div>
          </div>
          <div v-if="zeroRowsMessage" class="mx-4 mb-2 text-[11px] text-amber-700 bg-amber-50 dark:bg-amber-900/30 dark:text-amber-200 border border-amber-200 dark:border-amber-700 rounded px-2 py-1" data-testid="zero-rows-hint">{{ zeroRowsMessage }}</div>
        </section>
      </div>

      <!-- Footer: what saving does, then the actions -->
      <div class="px-4 py-2.5 border-t border-gray-200 dark:border-gray-700 flex items-center gap-2 flex-shrink-0">
        <span v-if="errorMsg" class="text-xs text-red-600 dark:text-red-400 truncate" data-testid="entity-edit-error">{{ $t('entityCreate.saveFailed') }}: {{ errorMsg }}</span>
        <span v-else class="text-xs text-gray-500 dark:text-gray-400" data-testid="entity-edit-tier">{{ tierNote }}</span>
        <button
          type="button"
          class="ms-auto px-3 py-1.5 text-xs rounded-lg text-gray-600 dark:text-gray-300 hover:bg-gray-100 dark:hover:bg-gray-800"
          @click="open = false"
        >{{ $t('entityCreate.cancel') }}</button>
        <button
          type="button"
          data-testid="entity-edit-save"
          class="text-white text-xs font-medium py-1.5 px-3 rounded-lg disabled:opacity-50"
          :class="(!isCreate || canCreateEntities) ? 'bg-blue-500 hover:bg-blue-600' : 'bg-amber-500 hover:bg-amber-600'"
          :disabled="saving || !canSave"
          @click="onSave"
        >
          <span v-if="saving">{{ (!isCreate || canCreateEntities) ? $t('entityCreate.saving') : $t('entityCreate.submitting') }}</span>
          <span v-else-if="!isCreate">{{ $t('queries.save') }}</span>
          <span v-else>{{ canCreateEntities ? $t('queries.createQuery') : $t('queries.suggestQuery') }}</span>
        </button>
      </div>
    </div>
  </UModal>
</template>

<script setup lang="ts">
import { ref, computed, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { useMyFetch } from '~/composables/useMyFetch'
import { useCanAll, useCan } from '~/composables/usePermissions'
import {
  cleanParamSpecs, collectTestValues, zeroRowsHint, type ParamSpecDraft,
} from '~/composables/useQueryParams'
import EntityForm from './EntityForm.vue'
import DataSourceIcon from '~/components/DataSourceIcon.vue'
import QueryParamsPanel from '~/components/tools/QueryParamsPanel.vue'

const { t } = useI18n()
const toast = useToast()

type MinimalDS = { id: string; name?: string; type?: string; icon?: string | null }
type EntityDetail = {
  id: string
  type: string
  title: string
  slug: string
  description?: string | null
  data?: any
  status?: string
  data_sources?: MinimalDS[]
  code?: string
  parameters?: any[] | null
  applied_params?: Record<string, any> | null
  private_status?: string | null
  global_status?: string | null
  owner_id?: string
  [key: string]: any
}

const props = defineProps<{
  modelValue: boolean
  detail?: EntityDetail | null
  entityId?: string | null
  editorLang?: string
  // create mode: the agent whose Queries panel opened the form
  dsId?: string | null
}>()
const emit = defineEmits<{
  (e: 'update:modelValue', v: boolean): void
  (e: 'saved', entity?: any): void
}>()

const open = computed({
  get: () => props.modelValue,
  set: (v: boolean) => emit('update:modelValue', v),
})
const isCreate = computed(() => !props.entityId)

const form = ref<{
  type: string
  title: string
  description: string | null
  status: string
  data_source_ids?: string[]
  global_status?: string | null
}>({ type: 'model', title: '', description: null, status: 'published', data_source_ids: [], global_status: null })

const code = ref('')
const savedCode = ref('')
const result = ref<any | null>(null)
const runError = ref('')
const errorMsg = ref('')
const running = ref(false)
const saving = ref(false)
const lastRunAt = ref('')

// Declared parameters + test values, edited in the panel beside the code.
const paramSpecs = ref<ParamSpecDraft[]>([])
const paramTestValues = ref<Record<string, any>>({})
const appliedParams = ref<Record<string, any> | null>(null)
const paramsOpen = ref(false)
const declaredCount = computed(() => paramSpecs.value.filter(s => s && s.name).length)
const zeroRowsMessage = computed(() =>
  zeroRowsHint(result.value?.rows, appliedParams.value, paramSpecs.value, code.value))

// Agents, for the header chips (the form's select owns the choice).
const agents = ref<MinimalDS[]>([])
const selectedIds = computed(() => form.value.data_source_ids || [])
const selectedAgents = computed(() =>
  selectedIds.value.map(id => agents.value.find(a => String(a.id) === String(id))).filter(Boolean) as MinimalDS[])
async function fetchAgents() {
  try {
    const { data } = await useMyFetch<MinimalDS[]>('/api/data_sources/active', { method: 'GET' })
    agents.value = Array.isArray(data.value) ? data.value : []
  } catch { agents.value = [] }
}

// Same rule as the report's Save Query: publishing / full editing needs
// per-agent `create_entities` on EVERY selected agent (org `manage_entities`
// and full admins via implication). Agent-less queries are org-wide and stay
// an org-admin capability.
const canCreateEntities = computed(() =>
  selectedIds.value.length ? useCanAll('create_entities', 'data_source', selectedIds.value) : useCan('manage_entities'))
const canSave = computed(() => !!form.value.title.trim() && !!code.value.trim())

const tierNote = computed(() => {
  if (!isCreate.value) return t('queries.tierEdit')
  return canCreateEntities.value ? t('queries.tierPublish') : t('queries.tierSuggest')
})

const isMac = typeof navigator !== 'undefined' && /Mac|iPhone|iPad/.test(navigator.platform || '')
const shortcutLabel = isMac ? '⌘↵' : 'Ctrl+↵'
function onKeydown(e: KeyboardEvent) {
  if ((e.metaKey || e.ctrlKey) && e.key === 'Enter') {
    e.preventDefault()
    if (!running.value && code.value.trim()) run()
  }
}

// Entity code is `generate_df(ds_clients, excel_files)` reaching an agent
// through `ds_clients["<agent name>:<connection name>"]`. Seed a new query
// with the opening agent's real key so the user only has to replace the SQL.
function template(key?: string): string {
  const client = key ? `ds_clients[${JSON.stringify(key)}]` : 'ds_clients["<agent>:<connection>"]'
  return `def generate_df(ds_clients, excel_files):\n    return ${client}.execute_query("""\n        SELECT 1 AS example\n    """)\n`
}

async function reset() {
  result.value = null
  runError.value = ''
  errorMsg.value = ''
  running.value = false
  saving.value = false
  lastRunAt.value = ''
  appliedParams.value = null
  paramTestValues.value = {}
  if (!isCreate.value && props.detail) {
    const d = props.detail
    form.value = {
      type: d.type || 'model',
      title: d.title || '',
      description: d.description || null,
      status: d.status || 'draft',
      data_source_ids: (d.data_sources || []).map(ds => ds.id),
      global_status: d.global_status || null,
    }
    code.value = d.code || ''
    savedCode.value = code.value
    result.value = d.data || null
    paramSpecs.value = Array.isArray(d.parameters) ? d.parameters.map((p: any) => ({ ...p })) : []
    appliedParams.value = d.applied_params || null
    paramsOpen.value = paramSpecs.value.length > 0
    return
  }
  form.value = {
    type: 'model', title: '', description: null, status: 'published',
    data_source_ids: props.dsId ? [props.dsId] : [], global_status: null,
  }
  savedCode.value = ''
  paramSpecs.value = []
  paramsOpen.value = false
  code.value = template()
  if (!props.dsId) return
  try {
    const { data } = await useMyFetch<any>(`/api/data_sources/${props.dsId}`, { method: 'GET' })
    const ds: any = data.value
    const conn = (ds?.connections || []).find((c: any) => c?.is_active !== false) || (ds?.connections || [])[0]
    if (ds?.name && conn?.name) code.value = template(`${ds.name}:${conn.name}`)
  } catch { /* the generic template stays */ }
}

watch(() => props.modelValue, (v) => { if (v) { fetchAgents(); reset() } }, { immediate: true })

function stamp() {
  lastRunAt.value = new Date().toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' })
}

async function run() {
  running.value = true
  runError.value = ''
  try {
    const body: any = {
      code: code.value,
      parameters: cleanParamSpecs(paramSpecs.value),
      params: collectTestValues(paramSpecs.value, paramTestValues.value),
    }
    if (isCreate.value) body.data_source_ids = selectedIds.value
    const { data, error } = isCreate.value
      ? await useMyFetch<any>('/api/entities/preview', { method: 'POST', body })
      : await useMyFetch<any>(`/api/entities/${props.entityId}/preview`, { method: 'POST', body })
    if (error.value) throw error.value
    const payload: any = data.value
    stamp()
    if (payload && 'applied_params' in payload) appliedParams.value = payload.applied_params || null
    if (payload?.error) { runError.value = payload.error; result.value = null; return }
    result.value = payload?.data || null
  } catch (e: any) {
    runError.value = e?.data?.detail || e?.message || t('queries.runFailed')
    result.value = null
  } finally {
    running.value = false
  }
}

async function onSave() {
  saving.value = true
  errorMsg.value = ''
  try {
    const status = form.value.status === 'published' ? 'published' : (form.value.status || 'draft')
    const parameters = cleanParamSpecs(paramSpecs.value)
    if (isCreate.value) {
      const body = {
        type: form.value.type || 'model',
        title: form.value.title.trim(),
        description: form.value.description || null,
        code: code.value,
        data: {},
        status,
        data_source_ids: selectedIds.value,
        parameters,
      }
      const { data, error } = await useMyFetch<any>('/api/entities', { method: 'POST', body })
      if (error.value) throw error.value
      const saved: any = data.value
      // Fill the snapshot so the row opens with data; a failed run still
      // leaves a saved query the user can fix here.
      try { await useMyFetch(`/api/entities/${saved.id}/run`, { method: 'POST', body: { code: code.value } }) } catch { /* see above */ }
      toast.add({
        title: saved?.global_status === 'approved' && saved?.status === 'published'
          ? t('entityCreate.publishedToast') : t('entityCreate.suggestedToast'),
        color: 'green',
      })
      emit('saved', saved)
    } else {
      const body: any = {
        type: form.value.type || 'model',
        title: form.value.title.trim(),
        description: form.value.description || null,
        code: code.value,
        data_source_ids: selectedIds.value,
        parameters,
      }
      // Status is only offered to entity managers; leaving it out keeps the
      // owner tier from being refused for a field they never saw.
      if (canCreateEntities.value) body.status = status
      const { data, error } = await useMyFetch<any>(`/api/entities/${props.entityId}`, { method: 'PUT', body })
      if (error.value) throw error.value
      if (code.value !== savedCode.value) {
        try { await useMyFetch(`/api/entities/${props.entityId}/run`, { method: 'POST', body: { code: code.value } }) } catch { /* snapshot refresh is best-effort */ }
      }
      emit('saved', data.value)
    }
    open.value = false
  } catch (e: any) {
    errorMsg.value = e?.data?.detail || e?.message || t('entityCreate.saveFailed')
    toast.add({ title: t('entityCreate.saveFailed'), description: errorMsg.value, color: 'red' })
  } finally {
    saving.value = false
  }
}
</script>
