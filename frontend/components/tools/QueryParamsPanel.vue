<!--
  Parameter declarations + test values, rendered NEXT TO the code that reads
  them (declarations and code are one consistency unit — Save validates that
  the code reads every declared param). Shared by the report's query editor
  and the catalog's saved-query form; the host owns the specs and the values
  (v-model) and decides what Run / Save do.

  `optionQueries` lists the queries a param may take its choices from (the
  filter-space pattern); leave it empty where no such queries exist — the
  "from query" mode is then not offered at all.
-->
<template>
  <aside class="h-full overflow-y-auto p-3 space-y-3" data-testid="params-panel">
    <div class="text-[11px] text-gray-500 dark:text-gray-400">
      Typed inputs the code receives as <code class="bg-gray-100 dark:bg-gray-800 px-1 rounded">params</code>.
      <b>input</b> params are viewer-editable controls; <b>identity</b> params are locked to each viewer and bound server-side.
      Save enforces that the code reads every declared param.
    </div>
    <div v-if="specs.length === 0" class="text-xs text-gray-400 border border-dashed rounded p-3 text-center">
      No parameters declared.
    </div>
    <div v-for="(spec, idx) in specs" :key="idx" class="border border-gray-200 dark:border-gray-700 rounded p-2 space-y-1.5" :data-testid="`param-row-${spec.name || idx}`">
      <div class="flex items-center gap-1.5">
        <input v-model="spec.name" placeholder="name" class="flex-1 min-w-0 px-2 py-1 text-xs border rounded font-mono" data-testid="param-name" />
        <button class="text-xs text-red-500 hover:text-red-700" data-testid="param-delete" @click="specs.splice(idx, 1)">Remove</button>
      </div>
      <div class="flex items-center gap-1.5">
        <select v-model="spec.type" class="px-1.5 py-1 text-xs border rounded" data-testid="param-type">
          <option v-for="t in PARAM_TYPES" :key="t" :value="t">{{ t }}</option>
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
        <!-- Control choices: a static list, or another query as the filter
             space (e.g. 'Genres' feeding the genre control) — choices then
             never self-narrow. -->
        <div v-if="optionQueries.length" class="flex items-center gap-1.5">
          <span class="text-[11px] text-gray-500">choices</span>
          <select :value="spec.options_source ? 'query' : 'static'" class="px-1.5 py-1 text-xs border rounded" :data-testid="`param-opts-mode-${spec.name || idx}`"
            @change="setOptionsMode(spec, ($event.target as HTMLSelectElement).value)">
            <option value="static">static list</option>
            <option value="query">from query</option>
          </select>
        </div>
        <input v-if="!spec.options_source" :value="(spec.options || []).join(', ')" @input="spec.options = parseParamOptions(($event.target as HTMLInputElement).value)"
          placeholder="options, comma-separated (optional)" class="w-full px-2 py-1 text-xs border rounded" />
        <div v-else class="space-y-1.5">
          <select v-model="spec.options_source.query_id" class="w-full px-1.5 py-1 text-xs border rounded" :data-testid="`param-opts-query-${spec.name || idx}`">
            <option value="" disabled>pick options query…</option>
            <!-- A saved ref no query matches (deleted query, unresolved agent
                 ref) must be visible, not blank. -->
            <option v-if="spec.options_source.query_id && !optionQueries.some(q => String(q.id) === String(spec.options_source!.query_id))"
              :value="spec.options_source.query_id">⚠ unknown query ({{ String(spec.options_source.query_id).slice(0, 8) }}…) — re-pick</option>
            <option v-for="q in optionQueries" :key="q.id" :value="q.id">{{ q.title }}</option>
          </select>
          <div class="flex items-center gap-1.5">
            <select v-model="spec.options_source.value_column" class="flex-1 min-w-0 px-1.5 py-1 text-xs border rounded" :data-testid="`param-opts-value-${spec.name || idx}`">
              <option value="" disabled>value column…</option>
              <option v-for="c in columnsOf(spec.options_source.query_id)" :key="c" :value="c">{{ c }}</option>
            </select>
            <select v-model="spec.options_source.label_column" class="flex-1 min-w-0 px-1.5 py-1 text-xs border rounded" :data-testid="`param-opts-label-${spec.name || idx}`">
              <option :value="null">label = value</option>
              <option v-for="c in columnsOf(spec.options_source.query_id)" :key="c" :value="c">{{ c }}</option>
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
    <button class="w-full px-3 py-1.5 text-xs rounded border border-gray-300 dark:border-gray-600 text-gray-700 dark:text-gray-300 hover:bg-gray-50 dark:hover:bg-gray-800" data-testid="param-add" @click="specs.push(newParamSpec())">+ Add parameter</button>

    <!-- Test values feed Run/Save; empty = the backend resolves the
         default, or NULL when there is none. -->
    <div v-if="editable.length" class="border-t pt-2 space-y-1.5">
      <div class="text-xs font-medium text-gray-700 dark:text-gray-300">Test values</div>
      <div v-for="spec in editable" :key="spec.name" class="flex items-center gap-1.5">
        <span class="text-[11px] text-gray-500 font-mono w-24 truncate">{{ spec.name }}=</span>
        <input v-model="testValues[spec.name]" :placeholder="testValuePlaceholder(code, spec)"
          class="flex-1 min-w-0 px-2 py-1 text-xs border rounded" :data-testid="`param-value-${spec.name}`" />
      </div>
      <button class="px-3 py-1.5 text-xs rounded border border-gray-300 dark:border-gray-600 text-gray-700 dark:text-gray-300 hover:bg-gray-50 dark:hover:bg-gray-800 flex items-center" :disabled="running" data-testid="param-preview-run" @click="$emit('run')">
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
    <div v-if="showSave" class="border-t pt-2">
      <button class="w-full px-3 py-1.5 text-xs rounded bg-gray-800 text-white hover:bg-gray-700" :disabled="running" data-testid="params-save" @click="$emit('save')">
        <span v-if="running && runMode === 'save'">Saving…</span>
        <span v-else>Save parameters &amp; run</span>
      </button>
    </div>
  </aside>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import {
  PARAM_TYPES, newParamSpec, editableParamSpecs, parseParamOptions, testValuePlaceholder,
  type ParamSpecDraft,
} from '~/composables/useQueryParams'

export type OptionQuery = { id: string; title: string; columns: string[] }

const props = withDefaults(defineProps<{
  /** Declared specs — mutated in place (rows are edited field by field). */
  specs: ParamSpecDraft[]
  /** Test values by param name — mutated in place. */
  testValues: Record<string, any>
  /** The code the params feed; drives the placeholders' All/no-rows honesty. */
  code: string
  appliedParams?: Record<string, any> | null
  optionQueries?: OptionQuery[]
  running?: boolean
  runMode?: 'preview' | 'save' | null
  errorMsg?: string
  /** Offer "Save parameters & run" (the report editor persists a new step from here). */
  showSave?: boolean
}>(), {
  appliedParams: null,
  optionQueries: () => [],
  running: false,
  runMode: null,
  errorMsg: '',
  showSave: false,
})

defineEmits<{ (e: 'run'): void; (e: 'save'): void }>()

const editable = computed(() => editableParamSpecs(props.specs))

function columnsOf(qid: string | null | undefined): string[] {
  if (!qid) return []
  return props.optionQueries.find(q => String(q.id) === String(qid))?.columns || []
}

function setOptionsMode(spec: ParamSpecDraft, mode: string) {
  if (mode === 'query') {
    spec.options = null
    spec.options_source = spec.options_source || { query_id: '', value_column: '', label_column: null }
  } else {
    spec.options_source = null
  }
}
</script>
