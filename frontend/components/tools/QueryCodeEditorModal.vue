<template>
  <UModal v-model="open" :ui="{ width: 'sm:max-w-6xl', height: 'sm:h-[90vh]' }" :prevent-close="false">
    <div class="h-full flex flex-col" @click.stop>
      <div class="px-4 py-3 flex items-center justify-between flex-shrink-0">
        <div class="text-sm font-medium text-gray-800 dark:text-gray-200">Edit query — {{ title }}</div>
        <div v-if="currentStepId" class="ms-4 text-[11px] text-gray-500 dark:text-gray-400">Query ID: {{ queryId }}</div>
        <button class="px-3 py-1.5 text-xs rounded border border-gray-300 dark:border-gray-600 text-gray-700 dark:text-gray-300 hover:bg-gray-50 dark:hover:bg-gray-800" @click.stop="open = false">Close</button>
      </div>
      <div class="flex-1 flex flex-col overflow-hidden min-h-0">
        <!-- Top tabs -->
        <nav class="px-4 flex items-center space-x-4 border-b flex-shrink-0">
          <button
            v-if="canEditCode"
            class="py-2 text-xs transition-colors border-b-2 -mb-px"
            :class="activeTab === 'code' ? 'text-blue-600 border-blue-600 font-medium' : 'text-gray-500 dark:text-gray-400 border-transparent hover:text-gray-700 dark:hover:text-gray-300'"
            @click="activeTab = 'code'"
          >Query</button>
          <button
            class="py-2 text-xs transition-colors border-b-2 -mb-px"
            :class="activeTab === 'visuals' ? 'text-blue-600 border-blue-600 font-medium' : 'text-gray-500 dark:text-gray-400 border-transparent hover:text-gray-700 dark:hover:text-gray-300'"
            @click="activeTab = 'visuals'"
          >Visuals</button>

          <!-- Params live in a persistent sidebar next to the code (they ARE
               part of the query — declarations + code must stay consistent);
               this button only shows/hides the sidebar. -->
          <button
            v-if="canEditCode && activeTab === 'code'"
            class="ms-auto my-1.5 px-2.5 py-1 text-[11px] rounded border transition-colors"
            data-testid="params-tab"
            :class="paramsSidebarOpen ? 'bg-blue-600 text-white border-blue-600' : 'text-gray-600 dark:text-gray-300 border-gray-200 dark:border-gray-700 hover:bg-gray-50 dark:hover:bg-gray-800'"
            @click="paramsSidebarOpen = !paramsSidebarOpen"
          >Params<span v-if="paramSpecs.length" class="ms-1">({{ paramSpecs.length }})</span></button>
        </nav>

        <!-- Content -->
        <section class="flex-1 flex flex-col overflow-hidden min-h-0">
          <div v-if="canEditCode && activeTab === 'code'" class="h-full flex overflow-hidden">
           <div class="flex-1 min-w-0 h-full flex flex-col">
            <!-- Editor section - exactly half height, fixed and non-scrollable -->
            <div class="h-1/2 p-4 flex flex-col border-b">
              <ClientOnly>
                <div class="flex-1 min-h-0">
                  <MonacoEditor
                    v-model="editorCode"
                    lang="python"
                    :options="{ theme: 'vs', automaticLayout: false, minimap: { enabled: false }, wordWrap: 'on' }"
                    style="height: 100%"
                  />
                </div>
              </ClientOnly>
              <div v-if="errorMsg" class="mt-2 text-xs text-red-600">{{ errorMsg }}</div>
              <div class="mt-3 flex items-center justify-end space-x-2">
                <button class="px-3 py-1.5 text-xs rounded bg-gray-800 text-white hover:bg-gray-700" :disabled="running" @click="runNewStep">
                  <span v-if="running && runMode === 'save'">Saving…</span>
                  <span v-else>
                    Save</span>
                </button>
                <button class="px-3 py-1.5 text-xs rounded border border-gray-300 dark:border-gray-600 text-gray-700 dark:text-gray-300 hover:bg-gray-50 dark:hover:bg-gray-800 flex items-center" :disabled="running" @click="previewRun">
                  <span v-if="running && runMode === 'preview'">Running…</span>
                  <span v-else class="flex items-center">
                    <Icon name="heroicons-play" class="w-3 h-3 me-1.5" />
                    Run</span>
                </button>

              </div>
            </div>
            <!-- Results section - exactly half height, scrollable -->
            <div class="h-1/2 p-4 flex flex-col min-h-0">
              <div class="text-xs text-gray-600 dark:text-gray-400 mb-2 flex-shrink-0" v-if="preview?.info">Rows: {{ preview.info.total_rows?.toLocaleString?.() || preview.info.total_rows }}</div>
              <div class="flex-1 overflow-auto min-h-0">
                <div v-if="preview && preview.columns && preview.rows" class="border rounded">
                  <table class="min-w-full text-xs">
                    <thead class="bg-gray-50 dark:bg-gray-900 sticky top-0">
                      <tr>
                        <th v-for="col in preview.columns" :key="col.field" class="px-2 py-1 text-start font-medium text-gray-600 dark:text-gray-400">{{ col.headerName || col.field }}</th>
                      </tr>
                    </thead>
                    <tbody>
                      <tr v-for="(row, rIdx) in preview.rows" :key="rIdx" class="border-t">
                        <td v-for="col in preview.columns" :key="col.field" class="px-2 py-1 text-gray-800 dark:text-gray-200">
                          {{ row[col.field] }}
                        </td>
                      </tr>
                    </tbody>
                  </table>
                </div>
                <div v-else class="text-xs" :class="errorMsg ? 'text-red-600' : 'text-gray-400'">{{ errorMsg || 'No preview yet.' }}</div>
              </div>
              <!-- A strict-equality param that resolved to NULL explains a
                   0-row result better than the empty table does. -->
              <div v-if="zeroRowsHint" class="mt-2 text-[11px] text-amber-700 bg-amber-50 dark:bg-amber-900/30 dark:text-amber-200 border border-amber-200 dark:border-amber-700 rounded px-2 py-1" data-testid="zero-rows-hint">{{ zeroRowsHint }}</div>
            </div>
           </div>

           <!-- Params sidebar: declarations + test values live NEXT TO the
                code (they are one consistency unit — Save validates that the
                code reads every declared param). -->
           <aside v-if="paramsSidebarOpen" class="w-[330px] flex-shrink-0 border-s h-full overflow-y-auto p-3 space-y-3" data-testid="params-panel">
              <div class="text-[11px] text-gray-500 dark:text-gray-400">
                Typed inputs the code receives as <code class="bg-gray-100 dark:bg-gray-800 px-1 rounded">params</code>.
                <b>input</b> params are viewer-editable controls; <b>identity</b> params are locked to each viewer and bound server-side.
                Save enforces that the code reads every declared param.
              </div>
              <div v-if="paramSpecs.length === 0" class="text-xs text-gray-400 border border-dashed rounded p-3 text-center">
                No parameters declared.
              </div>
              <div v-for="(spec, idx) in paramSpecs" :key="idx" class="border border-gray-200 dark:border-gray-700 rounded p-2 space-y-1.5" :data-testid="`param-row-${spec.name || idx}`">
                <div class="flex items-center gap-1.5">
                  <input v-model="spec.name" placeholder="name" class="flex-1 min-w-0 px-2 py-1 text-xs border rounded font-mono" data-testid="param-name" />
                  <button class="text-xs text-red-500 hover:text-red-700" data-testid="param-delete" @click="paramSpecs.splice(idx, 1)">Remove</button>
                </div>
                <div class="flex items-center gap-1.5">
                  <select v-model="spec.type" class="px-1.5 py-1 text-xs border rounded" data-testid="param-type">
                    <option v-for="t in ['string','number','date','date_range','id','list']" :key="t" :value="t">{{ t }}</option>
                  </select>
                  <select v-model="spec.source" class="flex-1 min-w-0 px-1.5 py-1 text-xs border rounded" data-testid="param-source">
                    <option value="input">input (viewer-editable)</option>
                    <option value="identity">identity (locked to viewer)</option>
                    <option value="input_identity_default">input, defaults to viewer</option>
                  </select>
                  <label class="text-[11px] text-gray-500 flex items-center gap-1">
                    <input type="checkbox" v-model="spec.required" /> req
                  </label>
                </div>
                <input v-model="spec.label" placeholder="label (control caption)" class="w-full px-2 py-1 text-xs border rounded" />
                <template v-if="spec.source === 'input'">
                  <input v-model="spec.default" placeholder="default (empty = All)" class="w-full px-2 py-1 text-xs border rounded" data-testid="param-default" />
                  <!-- Control choices: a static list, or another query in this
                       report as the filter space (e.g. 'Genres' feeding the
                       genre control) — choices then never self-narrow. -->
                  <div class="flex items-center gap-1.5">
                    <span class="text-[11px] text-gray-500">choices</span>
                    <select :value="spec.options_source ? 'query' : 'static'" class="px-1.5 py-1 text-xs border rounded" :data-testid="`param-opts-mode-${spec.name || idx}`"
                      @change="setOptionsMode(spec, ($event.target as HTMLSelectElement).value)">
                      <option value="static">static list</option>
                      <option value="query">from query</option>
                    </select>
                  </div>
                  <input v-if="!spec.options_source" :value="(spec.options || []).join(', ')" @input="spec.options = parseOptions(($event.target as HTMLInputElement).value)"
                    placeholder="options, comma-separated (optional)" class="w-full px-2 py-1 text-xs border rounded" />
                  <div v-else class="space-y-1.5">
                    <select v-model="spec.options_source.query_id" class="w-full px-1.5 py-1 text-xs border rounded" :data-testid="`param-opts-query-${spec.name || idx}`">
                      <option value="" disabled>pick options query…</option>
                      <!-- A saved ref no report query matches (deleted query,
                           unresolved agent ref) must be visible, not blank. -->
                      <option v-if="spec.options_source.query_id && !optionQueryChoices.some(q => String(q.id) === String(spec.options_source.query_id))"
                        :value="spec.options_source.query_id">⚠ unknown query ({{ String(spec.options_source.query_id).slice(0, 8) }}…) — re-pick</option>
                      <option v-for="q in optionQueryChoices" :key="q.id" :value="q.id">{{ q.title }}</option>
                    </select>
                    <div class="flex items-center gap-1.5">
                      <select v-model="spec.options_source.value_column" class="flex-1 min-w-0 px-1.5 py-1 text-xs border rounded" :data-testid="`param-opts-value-${spec.name || idx}`">
                        <option value="" disabled>value column…</option>
                        <option v-for="c in columnsOfQuery(spec.options_source.query_id)" :key="c" :value="c">{{ c }}</option>
                      </select>
                      <select v-model="spec.options_source.label_column" class="flex-1 min-w-0 px-1.5 py-1 text-xs border rounded" :data-testid="`param-opts-label-${spec.name || idx}`">
                        <option :value="null">label = value</option>
                        <option v-for="c in columnsOfQuery(spec.options_source.query_id)" :key="c" :value="c">{{ c }}</option>
                      </select>
                    </div>
                  </div>
                </template>
                <template v-else>
                  <div class="flex items-center gap-1.5">
                    <select v-model="spec.identity_binding" class="flex-1 min-w-0 px-1.5 py-1 text-xs border rounded" data-testid="param-binding">
                      <option value="viewer.email">viewer.email</option>
                      <option value="viewer.user_id">viewer.user_id</option>
                      <option value="viewer.groups">viewer.groups</option>
                      <option v-if="spec.identity_binding && spec.identity_binding.startsWith('viewer.profile_attributes.')" :value="spec.identity_binding">{{ spec.identity_binding }}</option>
                    </select>
                    <span class="text-[10px] px-1.5 py-0.5 rounded bg-indigo-50 text-indigo-600 border border-indigo-200 whitespace-nowrap">viewer-scoped</span>
                  </div>
                  <input placeholder="or profile attribute key (e.g. department)" class="w-full px-2 py-1 text-xs border rounded"
                    @change="spec.identity_binding = ($event.target as HTMLInputElement).value ? ('viewer.profile_attributes.' + ($event.target as HTMLInputElement).value) : spec.identity_binding" />
                </template>
              </div>
              <button class="w-full px-3 py-1.5 text-xs rounded border border-gray-300 dark:border-gray-600 text-gray-700 dark:text-gray-300 hover:bg-gray-50 dark:hover:bg-gray-800" data-testid="param-add" @click="addParamSpec">+ Add parameter</button>

              <!-- Test values feed Run/Save; empty = the backend resolves the
                   default, or NULL when there is none. -->
              <div v-if="editableParamSpecs.length" class="border-t pt-2 space-y-1.5">
                <div class="text-xs font-medium text-gray-700 dark:text-gray-300">Test values</div>
                <div v-for="spec in editableParamSpecs" :key="spec.name" class="flex items-center gap-1.5">
                  <span class="text-[11px] text-gray-500 font-mono w-24 truncate">{{ spec.name }}=</span>
                  <input v-model="paramTestValues[spec.name]" :placeholder="testPlaceholder(spec)"
                    class="flex-1 min-w-0 px-2 py-1 text-xs border rounded" :data-testid="`param-value-${spec.name}`" />
                </div>
                <button class="px-3 py-1.5 text-xs rounded border border-gray-300 dark:border-gray-600 text-gray-700 dark:text-gray-300 hover:bg-gray-50 dark:hover:bg-gray-800 flex items-center" :disabled="running" data-testid="param-preview-run" @click="previewRun">
                  <span v-if="running && runMode === 'preview'">Running…</span>
                  <span v-else class="flex items-center"><Icon name="heroicons-play" class="w-3 h-3 me-1.5" />Run with values</span>
                </button>
              </div>

              <!-- What the last run actually bound — makes '0 rows' legible. -->
              <div v-if="appliedParams && Object.keys(appliedParams).length" class="border-t pt-2 space-y-1" data-testid="applied-params">
                <div class="text-xs font-medium text-gray-700 dark:text-gray-300">Last run used</div>
                <div v-for="(v, k) in appliedParams" :key="k" class="text-[11px] font-mono text-gray-600 dark:text-gray-400">
                  {{ k }} = <span :class="v === null ? 'text-amber-600' : ''">{{ v === null ? 'NULL' : JSON.stringify(v) }}</span>
                </div>
              </div>

              <div v-if="errorMsg" class="text-xs text-red-600" data-testid="params-error">{{ errorMsg }}</div>
              <div class="border-t pt-2">
                <button class="w-full px-3 py-1.5 text-xs rounded bg-gray-800 text-white hover:bg-gray-700" :disabled="running" data-testid="params-save" @click="runNewStep">
                  <span v-if="running && runMode === 'save'">Saving…</span>
                  <span v-else>Save parameters &amp; run</span>
                </button>
              </div>
           </aside>
          </div>

          <div v-else class="flex-1 overflow-auto">
            <div v-if="visualizations.length > 0" class="h-full">
              <!-- Visualization Cards -->
              <div class="space-y-4 p-4">
                <div v-for="viz in visualizations" :key="viz.id" class="border border-gray-200 dark:border-gray-700 rounded-lg bg-white dark:bg-gray-900 overflow-hidden">
                  <!-- Header -->
                  <div class="px-3 py-2 border-b border-gray-100 dark:border-gray-800 bg-gray-50 dark:bg-gray-900">
                    <div class="flex items-center justify-between">
                      <div class="flex items-center space-x-2">
                        <h4 class="text-sm font-medium text-gray-800 dark:text-gray-200">{{ viz.title || 'Untitled' }}</h4>
                      </div>
                    </div>
                  </div>

                  <!-- Content: Visualization (60%) + Config (40%) -->
                  <div class="flex h-[32rem]">
                    <!-- Left: Visualization Rendering (60%) -->
                    <div class="w-[65%] border-e border-gray-100 dark:border-gray-800">
                      <div v-if="shouldShowVisual(viz)" class="h-full bg-gray-50 dark:bg-gray-900 p-2">
                        <!-- Dynamic component rendering -->
                        <div v-if="getResolvedCompEl(viz) && shouldShowVisual(viz)" class="h-full">
                          <component
                            :is="getResolvedCompEl(viz)"
                            :key="getChartKey(viz)"
                            :widget="{ id: viz.id, title: viz.title } as any"
                            :data="viz.step?.data"
                            :data_model="getDataModelWithView(viz)"
                            :step="viz.step"
                            :view="wrapView(viz.view)"
                            :reportThemeName="reportThemeName"
                            :reportOverrides="reportOverrides"
                          />
                        </div>
                        <!-- Fallback rendering -->
                        <div v-else-if="chartVisualTypes.has(viz.view?.type || viz.step?.data_model?.type)" class="h-full">
                          <RenderVisual
                            :key="getChartKey(viz)"
                            :widget="{ id: viz.id, title: viz.title } as any"
                            :data="viz.step?.data"
                            :data_model="getDataModelWithView(viz)"
                            :view="wrapView(viz.view)"
                          />
                        </div>
                        <div v-else-if="(viz.view?.type || viz.step?.data_model?.type) === 'count'" class="h-full flex items-center justify-center">
                          <RenderCount
                            :widget="{ id: viz.id, title: viz.title } as any"
                            :data="viz.step?.data"
                            :data_model="getDataModelWithView(viz)"
                          />
                        </div>
                        <div v-else class="h-full flex items-center justify-center text-gray-400 text-sm">No preview available</div>
                      </div>
                      <div v-else class="h-full bg-gray-50 dark:bg-gray-900 p-2">
                        <div v-if="String(viz.view?.type || viz.step?.data_model?.type).toLowerCase() === 'table'" class="h-full">
                          <RenderTable
                            :widget="{ id: viz.id, title: viz.title } as any"
                            :step="{ ...(viz.step || {}), data_model: getDataModelWithView(viz) } as any"
                          />
                        </div>
                        <div v-else class="h-full flex items-center justify-center text-gray-400 text-sm">No visual representation</div>
                      </div>
                    </div>

                    <!-- Right: Configuration (40%) -->
                    <div class="w-[35%] p-3 flex flex-col overflow-x-hidden">
                      <VisualizationConfigEditor
                        :viz="viz"
                        :step="viz.step"
                        @apply="onVizApply(viz, $event)"
                        @saved="onVizSaved"
                      />
                    </div>
                  </div>
                </div>
              </div>
            </div>

            <div v-else class="flex-1 flex items-center justify-center">
              <div class="text-center py-8">
                <div v-if="loadingVisualizations" class="flex flex-col items-center">
                  <Spinner class="w-4 h-4 mb-2" />
                  <div class="text-gray-400 text-sm">Loading visualizations...</div>
                </div>
                <div v-else>
                  <div class="text-gray-400 text-sm">No visualizations found for this query</div>
                  <div v-if="!queryId" class="text-xs text-gray-400 mt-1">Query ID not available</div>
                </div>
              </div>
            </div>
          </div>
        </section>
      </div>
    </div>
  </UModal>
</template>

<script setup lang="ts">
import { ref, watch, onMounted, computed, defineAsyncComponent } from 'vue'
import { useMyFetch } from '~/composables/useMyFetch'
import RenderVisual from '../RenderVisual.vue'
import RenderCount from '../RenderCount.vue'
import RenderTable from '../RenderTable.vue'
import Spinner from '../Spinner.vue'
import { resolveEntryByType } from '@/components/dashboard/registry'
import VisualizationConfigEditor from './VisualizationConfigEditor.vue'
import { useOrgSettings } from '~/composables/useOrgSettings'

interface Props {
  visible: boolean
  queryId?: string | null
  initialCode: string
  title: string
  stepId?: string | null
  toolExecutionId?: string | null
}

const props = defineProps<Props>()
const route = useRoute()
const emit = defineEmits(['close', 'stepCreated'])

const editorCode = ref('')
const running = ref(false)
const errorMsg = ref('')
const preview = ref<any | null>(null)
const queryId = ref<string | null>(null)
const currentStepId = ref<string | null>(null)
const queryData = ref<any | null>(null)
const visualizations = ref<any[]>([])
const loadingVisualizations = ref(false)
const reportThemeName = ref<string | null>(null)
const reportOverrides = ref<Record<string, any> | null>(null)

const open = computed({
  get: () => props.visible,
  set: (v: boolean) => {
    if (!v) emit('close')
  }
})

const { canEditCode } = useOrgSettings()

const activeTab = ref<'code' | 'visuals'>(canEditCode.value ? 'code' : 'visuals')

// ── Query parameters ────────────────────────────────────────────────────────
// Declared ParamSpec rows (persisted on the Query via Save) + test values for
// preview runs. Identity params carry no test value — the server binds them.
// They render in a sidebar NEXT TO the code: declarations and code are one
// consistency unit, and a run's resolved values must be visible in place.
const paramSpecs = ref<any[]>([])
const paramTestValues = ref<Record<string, any>>({})
const paramsSidebarOpen = ref(true)
// The param values the last Run actually bound (from the backend response) —
// shown in the sidebar so `genre = NULL → 0 rows` explains itself.
const appliedParams = ref<Record<string, any> | null>(null)
// Report queries available as an options source (the filter-space pattern).
const reportQueriesList = ref<any[]>([])

async function fetchReportQueries() {
  // The query's own report_id is authoritative — the modal can open from
  // pages whose route param is not the report id (or none at all).
  const reportId = (queryData.value as any)?.report_id || (route.params as any)?.id
  if (!reportId) return
  try {
    const { data } = await useMyFetch(`/api/queries?report_id=${reportId}`)
    reportQueriesList.value = Array.isArray(data.value) ? (data.value as any[]) : []
  } catch { reportQueriesList.value = [] }
}

const optionQueryChoices = computed(() =>
  reportQueriesList.value.filter((q: any) => String(q.id) !== String(queryId.value || '')))

function columnsOfQuery(qid: string | null | undefined): string[] {
  if (!qid) return []
  const q = reportQueriesList.value.find((x: any) => String(x.id) === String(qid))
  const cols = q?.default_step?.data?.columns || []
  return cols.map((c: any) => c.field || c.headerName).filter(Boolean)
}

function setOptionsMode(spec: any, mode: string) {
  if (mode === 'query') {
    spec.options = null
    spec.options_source = spec.options_source || { query_id: '', value_column: '', label_column: null }
  } else {
    spec.options_source = null
  }
}

// Does the code treat NULL for this param as "All"? Heuristics for the SQL
// optional pattern and the python guard — drives honest placeholders and the
// 0-rows hint (a strict `= :name` with NULL matches nothing).
function usesOptionalPattern(name: string): boolean {
  const code = editorCode.value || ''
  if (!name) return false
  try {
    return new RegExp(':' + name + '\\s+is\\s+null', 'i').test(code)
      || new RegExp("params\\.get\\((['\"])" + name + "\\1\\)").test(code)
      || new RegExp('\\b' + name + '\\b\\s+is\\s+None').test(code)
  } catch { return false }
}

function testPlaceholder(spec: any): string {
  if (spec.default != null && spec.default !== '') return String(spec.default)
  return usesOptionalPattern(spec.name) ? '(All)' : 'empty = no rows'
}

const zeroRowsHint = computed(() => {
  const rows = preview.value?.rows
  if (!Array.isArray(rows) || rows.length !== 0) return null
  const applied = appliedParams.value || {}
  const culprit = editableParamSpecs.value.find((s: any) =>
    (applied[s.name] === null || applied[s.name] === undefined)
    && Object.prototype.hasOwnProperty.call(applied, s.name)
    && !usesOptionalPattern(s.name))
  if (!culprit) return null
  return `'${culprit.name}' was empty and resolved to NULL — a strict '= :${culprit.name}' filter matches nothing. ` +
    `Set a test value, or use the '(:${culprit.name} IS NULL OR col = :${culprit.name})' pattern for an All behavior.`
})

const editableParamSpecs = computed(() =>
  paramSpecs.value.filter((s: any) => s.source !== 'identity' && s.name))

function addParamSpec() {
  paramSpecs.value.push({
    name: '', type: 'string', label: '', source: 'input',
    default: null, required: false, identity_binding: 'viewer.email',
    options: null, options_source: null,
  })
}

function parseOptions(raw: string): string[] | null {
  const items = raw.split(',').map(s => s.trim()).filter(Boolean)
  return items.length ? items : null
}

function cleanedParamSpecs(): any[] {
  return paramSpecs.value
    .filter((s: any) => s.name)
    .map((s: any) => ({
      name: s.name,
      type: s.type || 'string',
      label: s.label || null,
      source: s.source || 'input',
      default: s.default === '' ? null : s.default,
      required: !!s.required,
      identity_binding: (s.source === 'identity' || s.source === 'input_identity_default')
        ? (s.identity_binding || 'viewer.email') : null,
      options: Array.isArray(s.options) && s.options.length ? s.options : null,
      options_source: (s.options_source && s.options_source.query_id && s.options_source.value_column)
        ? {
            query_id: s.options_source.query_id,
            value_column: s.options_source.value_column,
            label_column: s.options_source.label_column || null,
          }
        : null,
    }))
}

function collectedTestValues(): Record<string, any> {
  const out: Record<string, any> = {}
  for (const spec of editableParamSpecs.value) {
    const v = paramTestValues.value[spec.name]
    if (v !== undefined && v !== '') out[spec.name] = v
  }
  return out
}

watch(canEditCode, (v) => {
  if (!v) activeTab.value = 'visuals'
})

// Keep internal queryId in sync with prop while modal is open
watch(() => props.queryId, (v) => {
  if (props.visible && v) {
    queryId.value = v
  }
})

// Reload query data when queryId changes
watch(() => queryId.value, async (newQueryId) => {
  if (newQueryId && props.visible) {
    await loadQueryData()
  }
})

async function syncQueryIdOnOpen() {
  queryId.value = props.queryId || null
  if (!queryId.value && props.stepId) {
    try {
      const s: any = await useMyFetch(`/api/steps/${props.stepId}`)
      const step = s?.data?.value
      if (step?.query_id) queryId.value = step.query_id
    } catch {
      // ignore
    }
  }
}

async function loadQueryData() {
  if (!queryId.value) return

  loadingVisualizations.value = true
  try {
    const { data, error } = await useMyFetch(`/api/queries/${queryId.value}`)
    if (error.value) throw error.value

    queryData.value = data.value
    // Hydrate declared parameters from the Query
    try {
      const declared = (data.value as any)?.parameters
      paramSpecs.value = Array.isArray(declared)
        ? declared.map((p: any) => ({ ...p }))
        : []
    } catch { paramSpecs.value = [] }
    // Load theme from owning report (queries themselves do not carry theme)
    try {
      const rid = (data.value as any)?.report_id
      if (rid) {
        const { data: rdata, error: rerr } = await useMyFetch(`/api/reports/${rid}`)
        if (!rerr.value) {
          const rpt: any = rdata.value
          reportThemeName.value = rpt?.report_theme_name || rpt?.theme_name || null
          reportOverrides.value = rpt?.theme_overrides || null
        }
      }
    } catch {}
    const vizList = (data.value as any)?.visualizations || []
    const defaultStep = (data.value as any)?.default_step

    // Attach the default step to each visualization
    const vizWithSteps = vizList.map((viz: any) => ({
      ...viz,
      step: defaultStep
    }))

    visualizations.value = vizWithSteps
  } catch (e) {
    console.error('Failed to load query data:', e)
    queryData.value = null
    visualizations.value = []
  } finally {
    loadingVisualizations.value = false
  }
}

// Ensure we have a query id; create one if missing
async function ensureQueryId() {
  if (queryId.value) return
  // Try to sync from props/step first
  await syncQueryIdOnOpen()
  if (queryId.value) return
  // Create a new query with the provided title
  const resp: any = await useMyFetch('/api/queries', {
    method: 'POST',
    body: { title: props.title, report_id: (route?.params as any)?.id || null }
  })
  const q = resp?.data?.value
  if (!q?.id) {
    throw new Error('Failed to create query.')
  }
  queryId.value = q.id
}

watch(() => props.visible, async (v) => {
  if (v) {
    editorCode.value = props.initialCode || ''
    errorMsg.value = ''
    preview.value = null
    currentStepId.value = props.stepId || null
    if (!canEditCode.value) activeTab.value = 'visuals'
    appliedParams.value = null
    await syncQueryIdOnOpen()
    await loadQueryData()
    await loadInitialStepOrDefault()
    await fetchReportQueries()
    // Listen for theme changes to update editor visuals
    try {
      const handler = (ev: any) => {
        const d = (ev && ev.detail) || {}
        if (!d) return
        reportThemeName.value = d.themeName || reportThemeName.value
        reportOverrides.value = d.overrides || reportOverrides.value
      }
      window.addEventListener('dashboard:theme_changed', handler as any, { once: true })
    } catch {}
  }
})

onMounted(async () => {
  if (props.visible) {
    editorCode.value = props.initialCode || ''
    currentStepId.value = props.stepId || null
    await syncQueryIdOnOpen()
    await loadQueryData()
    await loadInitialStepOrDefault()
    await fetchReportQueries()
  }
})

async function loadInitialStepOrDefault() {
  try {
    let loadedStep: any = null
    // If a stepId is provided, load it first
    if (props.stepId) {
      const s: any = await useMyFetch(`/api/steps/${props.stepId}`)
      loadedStep = s?.data?.value || null
    }
    // If we have a queryId, fetch the current default step
    let defaultStep: any = null
    if (queryId.value) {
      const resp: any = await useMyFetch(`/api/queries/${queryId.value}/default_step`)
      defaultStep = resp?.data?.value?.step || null
    }
    const step = defaultStep || loadedStep
    if (step) {
      currentStepId.value = step.id
      if (step.query_id && !queryId.value) queryId.value = step.query_id
      if (step.data) preview.value = step.data
      // The modal edits the QUERY, so its CURRENT version wins: the default
      // step's code (which matches the query's declared parameters) replaces
      // the tool-execution snapshot the card passed in — that snapshot goes
      // stale the moment the query advances (params tab save, another edit).
      if (defaultStep?.code) editorCode.value = defaultStep.code
      else if (!editorCode.value && step.code) editorCode.value = step.code
    }
  } catch (e) {
    // swallow
  }
}

const runMode = ref<'preview' | 'save' | null>(null)

// Visualization rendering logic (similar to ToolWidgetPreview)
const chartVisualTypes = new Set<string>([
  'pie_chart',
  'line_chart',
  'bar_chart',
  'area_chart',
  'heatmap',
  'scatter_plot',
  'map',
  'candlestick',
  'treemap',
  'radar_chart'
])

// Dashboard registry-driven dynamic component
const compCache = new Map<string, any>()
function getCompForType(type?: string | null) {
  const t = (type || '').toLowerCase()
  if (!t) return null
  if (compCache.has(t)) return compCache.get(t)
  const entry = resolveEntryByType(t)
  if (!entry) return null
  const comp = defineAsyncComponent(entry.load)
  compCache.set(t, comp)
  return comp
}

// Get the resolved component for a visualization
function getResolvedCompEl(viz: any) {
  const vType = viz?.view?.type
  const dmType = viz?.step?.data_model?.type
  return getCompForType(String(vType || dmType || ''))
}

// Check if visualization should show visual rendering
function shouldShowVisual(viz: any) {
  const t = viz?.view?.type || viz?.step?.data_model?.type
  if (!t) return false
  const entry = resolveEntryByType(String(t).toLowerCase())
  if (entry) {
    // treat table as data-only; everything else is a visual
    return entry.componentKey !== 'table.aggrid'
  }
  return chartVisualTypes.has(String(t)) || String(t) === 'count'
}

// Helper to wrap flat view for RenderVisual which expects { view: {...} }
function wrapView(view: any) {
  if (!view) return null
  // If already wrapped (has .view property with type), return as-is
  if (view.view?.type) return view
  // Wrap flat view
  return { view, version: 'v2' }
}

// Helper to merge view type into data_model for rendering
function getDataModelWithView(viz: any) {
  const dm = viz.step?.data_model || {}
  const viewType = viz.view?.type
  if (viewType && viewType !== dm.type) {
    return { ...dm, type: viewType }
  }
  return dm
}

// Generate a key for the chart component that changes when view settings change
function getChartKey(viz: any): string {
  const v = viz.view || {}
  // Include all view fields that affect rendering to force re-render when they change
  return `${viz.id}-${v.type || ''}-${v.x || ''}-${JSON.stringify(v.y || '')}-${v.category || ''}-${v.value || ''}-${JSON.stringify(v.encoding || {})}-${JSON.stringify(v.legend || {})}-${JSON.stringify(v.axisX || {})}-${JSON.stringify(v.axisY || {})}-${v.showGrid}-${v.stacked}-${v.smooth}-${v.horizontal}-${v.donut}`
}

function onVizApply(viz: any, nextView: any) {
  // Replace the view entirely (not merge) to ensure all fields are from the new payload
  const updated = { ...(nextView || {}) }

  // Force reactivity update on the visualizations array by replacing the object
  const idx = visualizations.value.findIndex((v: any) => v.id === viz.id)
  if (idx >= 0) {
    // Create a completely new object to ensure Vue detects the change
    visualizations.value[idx] = {
      ...visualizations.value[idx],
      view: updated
    }
    // Also update the original viz reference (for v-for iteration)
    viz.view = updated
  }
}

function onVizSaved(updated: any) {
  if (!updated?.id) return
  const idx = visualizations.value.findIndex(v => v.id === updated.id)
  if (idx >= 0) {
    // Preserve attached step for preview while merging new view/title/status
    const prev = visualizations.value[idx]
    visualizations.value[idx] = { ...prev, ...updated, step: prev.step }
  }
  // Broadcast so dashboard can update the rendered tile immediately
  try {
    window.dispatchEvent(new CustomEvent('visualization:updated', { detail: { id: updated.id, visualization: updated } }))
  } catch {}
}

async function previewRun() {
  running.value = true
  runMode.value = 'preview'
  errorMsg.value = ''
  try {
    // Ensure we have or create a query id
    await ensureQueryId()
    const resp: any = await useMyFetch(`/api/queries/${queryId.value}/preview`, {
      method: 'POST',
      body: {
        code: editorCode.value, title: props.title, type: 'table',
        tool_execution_id: props.toolExecutionId || null,
        parameters: cleanedParamSpecs(),
        params: collectedTestValues(),
      }
    })
    if (resp?.error?.value) {
      const err: any = resp.error.value
      errorMsg.value = err?.data?.detail || err?.message || 'Failed to run'
      preview.value = null
      return
    }
    const payload = resp?.data?.value
    if (payload?.error) {
      errorMsg.value = payload.error
      preview.value = null
      return
    }
    preview.value = payload?.preview || null
    if (payload && 'applied_params' in payload) appliedParams.value = payload.applied_params || null
  } catch (e: any) {
    errorMsg.value = e?.data?.detail || e?.message || 'Failed to run'
  } finally {
    running.value = false
    runMode.value = null
  }
}

async function runNewStep() {
  running.value = true
  runMode.value = 'save'
  errorMsg.value = ''
  try {
    // Ensure we have or create a query id
    await ensureQueryId()
    const resp: any = await useMyFetch(`/api/queries/${queryId.value}/run`, {
      method: 'POST',
      body: {
        code: editorCode.value,
        title: props.title,
        type: 'table',
        tool_execution_id: props.toolExecutionId || null,
        parameters: cleanedParamSpecs(),
        params: collectedTestValues(),
      }
    })
    if (resp?.error?.value) {
      const err: any = resp.error.value
      errorMsg.value = err?.data?.detail || err?.message || 'Failed to run'
      preview.value = null
      return
    }
    const payload = resp?.data?.value
    // Show backend error message if execution failed
    if (payload?.error) {
      errorMsg.value = payload.error
      preview.value = null
      return
    }
    preview.value = payload?.step?.data || null
    if (payload?.step && 'applied_params' in payload.step) {
      appliedParams.value = payload.step.applied_params || null
    }
    if (payload?.step) {
      currentStepId.value = payload.step.id
      emit('stepCreated', payload.step)
      // Broadcast the new default step so dashboard can refresh the tile immediately
      try {
        const step: any = payload.step
        const qid = step?.query_id || queryId.value
        if (qid) {
          window.dispatchEvent(new CustomEvent('query:default_step_changed', { detail: { query_id: qid, step } }))
          // Also instruct the originating chat preview (if any) to rebind to this query id
          if (props.toolExecutionId) {
            window.dispatchEvent(new CustomEvent('tool_preview:rebind', { detail: { tool_execution_id: props.toolExecutionId, query_id: qid } }))
          }
        }
      } catch {}
    }
  } catch (e: any) {
    errorMsg.value = e?.data?.detail || e?.message || 'Failed to run'
  } finally {
    running.value = false
    runMode.value = null
  }
}
</script>

<style scoped>
.fade-enter-active,
.fade-leave-active {
  transition: opacity 0.3s ease;
}
.fade-enter-from,
.fade-leave-to {
  opacity: 0;
}
</style>


