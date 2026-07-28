<template>
  <UModal v-model="isOpen" :ui="{ width: 'sm:max-w-5xl' }" prevent-close>
    <div class="p-5" data-testid="custom-query-modal">
      <!-- Header -->
      <div class="flex items-start justify-between mb-3">
        <div>
          <h2 class="text-lg font-semibold dark:text-white">
            {{ editing ? 'Edit custom query' : 'New custom query' }}
          </h2>
          <p class="text-xs text-gray-500 dark:text-gray-400 mt-0.5">
            Runs on a schedule and is stored locally, so agents answer from a
            cached copy instead of querying
            <span class="font-medium">{{ connectionName }}</span> every time.
          </p>
        </div>
        <button class="text-gray-400 hover:text-gray-600" @click="close">
          <UIcon name="heroicons-x-mark" class="w-5 h-5" />
        </button>
      </div>

      <!-- Connection-wide scope warning: this is created from an agent page but
           the object belongs to the connection. -->
      <div class="mb-3 flex items-start gap-2 rounded-md bg-amber-50 dark:bg-amber-900/20 border border-amber-200 dark:border-amber-800 px-3 py-2">
        <UIcon name="heroicons-information-circle" class="w-4 h-4 text-amber-600 mt-0.5 flex-shrink-0" />
        <p class="text-xs text-amber-800 dark:text-amber-200">
          This is created on the connection <b>{{ connectionName }}</b> and can be
          activated by any agent that uses it — it is not limited to this agent.
        </p>
      </div>

      <!-- Tabs -->
      <div class="flex items-center gap-1 border-b border-gray-200 dark:border-gray-800 mb-4">
        <button
          v-for="t in tabs" :key="t.key"
          :data-testid="`cq-tab-${t.key}`"
          class="px-3 py-1.5 text-sm -mb-px border-b-2 transition-colors"
          :class="tab === t.key
            ? 'border-blue-600 text-blue-600 dark:text-blue-400 font-medium'
            : 'border-transparent text-gray-500 hover:text-gray-700 dark:text-gray-400'"
          :disabled="t.disabled"
          @click="!t.disabled && (tab = t.key)"
        >
          {{ t.label }}
          <span v-if="t.disabled" class="ms-1 text-[9px] px-1 py-0.5 rounded bg-gray-100 dark:bg-gray-800 text-gray-400">soon</span>
        </button>
      </div>

      <!-- ============ QUERY ============ -->
      <div v-show="tab === 'query'">
        <div class="grid grid-cols-3 gap-3 mb-3">
          <div>
            <label class="block text-xs font-medium text-gray-700 dark:text-gray-300 mb-1">Name</label>
            <input
              v-model="form.name" data-testid="cq-name"
              placeholder="revenue_summary"
              class="w-full text-sm border border-gray-300 dark:border-gray-700 rounded-md px-2.5 py-1.5 dark:bg-gray-900 dark:text-white font-mono"
            />
            <p class="text-[10px] text-gray-400 mt-1">Agents query this as a table name.</p>
          </div>
          <div class="col-span-2">
            <label class="block text-xs font-medium text-gray-700 dark:text-gray-300 mb-1">Description <span class="text-gray-400">(optional)</span></label>
            <input
              v-model="form.description" data-testid="cq-description"
              placeholder="What this data represents — shown to the agent"
              class="w-full text-sm border border-gray-300 dark:border-gray-700 rounded-md px-2.5 py-1.5 dark:bg-gray-900 dark:text-white"
            />
          </div>
        </div>

        <label class="block text-xs font-medium text-gray-700 dark:text-gray-300 mb-1">
          SQL <span class="text-gray-400">— in {{ connectionType }} dialect</span>
        </label>
        <textarea
          v-model="form.definition_sql" data-testid="cq-sql" rows="8" spellcheck="false"
          placeholder="SELECT ..."
          class="w-full text-xs font-mono border border-gray-300 dark:border-gray-700 rounded-md px-2.5 py-2 dark:bg-gray-900 dark:text-white"
        />

        <div class="flex items-center gap-2 mt-2">
          <button
            data-testid="cq-run-preview"
            class="flex items-center gap-1.5 border border-gray-300 dark:border-gray-700 rounded-md px-3 py-1.5 text-xs text-gray-700 dark:text-gray-300 hover:bg-gray-50 dark:hover:bg-gray-800/50 disabled:opacity-50"
            :disabled="previewing || !form.definition_sql.trim()"
            @click="runPreview"
          >
            <Spinner v-if="previewing" class="w-3.5 h-3.5" />
            <UIcon v-else name="heroicons-play" class="w-3.5 h-3.5" />
            Run preview
          </button>
          <span class="text-[11px] text-gray-400">Preview is limited to {{ ROW_LIMIT }} rows.</span>
        </div>

        <!-- preview error -->
        <div v-if="previewError" data-testid="cq-preview-error"
             class="mt-3 text-xs text-red-700 dark:text-red-300 bg-red-50 dark:bg-red-900/20 border border-red-200 dark:border-red-800 rounded-md px-3 py-2 font-mono whitespace-pre-wrap">
          {{ previewError }}
        </div>

        <!-- budget refusal -->
        <div v-if="preview?.budget_error" data-testid="cq-budget-error"
             class="mt-3 text-xs text-red-700 dark:text-red-300 bg-red-50 dark:bg-red-900/20 border border-red-200 dark:border-red-800 rounded-md px-3 py-2">
          <b>Too large to cache.</b> {{ preview.budget_error }}
        </div>

        <!-- preview result -->
        <div v-if="preview && preview.columns.length" class="mt-3">
          <div class="flex items-center flex-wrap gap-x-4 gap-y-1 text-[11px] text-gray-600 dark:text-gray-400 mb-1.5">
            <span data-testid="cq-preview-count">
              Showing <b>{{ preview.rows.length }}</b> of
              <b>{{ preview.estimated_rows != null ? approx(preview.estimated_rows) : 'unknown' }}</b> rows
            </span>
            <span>{{ preview.columns.length }} columns</span>
            <span v-if="preview.estimated_bytes">Est. size {{ humanBytes(preview.estimated_bytes) }}</span>
          </div>
          <div class="border border-gray-200 dark:border-gray-800 rounded-md overflow-auto" style="max-height: 260px">
            <table class="min-w-full text-[11px]">
              <thead class="bg-gray-50 dark:bg-gray-900 sticky top-0">
                <tr>
                  <th v-for="c in preview.columns" :key="c.name"
                      class="text-start font-medium text-gray-600 dark:text-gray-300 px-2 py-1.5 whitespace-nowrap border-b border-gray-200 dark:border-gray-800">
                    {{ c.name }}
                  </th>
                </tr>
              </thead>
              <tbody>
                <tr v-for="(row, i) in preview.rows" :key="i" class="odd:bg-white even:bg-gray-50/50 dark:odd:bg-gray-950 dark:even:bg-gray-900/40">
                  <td v-for="(cell, j) in row" :key="j" class="px-2 py-1 whitespace-nowrap text-gray-700 dark:text-gray-300 font-mono">
                    {{ cell === null ? '—' : cell }}
                  </td>
                </tr>
              </tbody>
            </table>
          </div>
        </div>
      </div>

      <!-- ============ CACHE ============ -->
      <div v-show="tab === 'cache'">
        <p class="text-xs text-gray-500 dark:text-gray-400 mb-3">
          How often BOW re-runs this query against the source and refreshes the
          local copy. Between refreshes, agents read the cached data.
        </p>
        <div class="flex items-center gap-4 mb-4">
          <label class="flex items-center gap-2 text-sm dark:text-gray-200">
            <input type="radio" value="interval" v-model="form.refresh_schedule_mode" data-testid="cq-mode-interval" />
            Every
          </label>
          <select
            v-model.number="form.refresh_interval_minutes" data-testid="cq-interval"
            :disabled="form.refresh_schedule_mode !== 'interval'"
            class="text-sm border border-gray-300 dark:border-gray-700 rounded-md px-2 py-1.5 dark:bg-gray-900 dark:text-white disabled:opacity-40"
          >
            <option :value="15">15 minutes</option>
            <option :value="30">30 minutes</option>
            <option :value="60">hour</option>
            <option :value="360">6 hours</option>
            <option :value="720">12 hours</option>
            <option :value="1440">24 hours</option>
          </select>
        </div>
        <div class="flex items-center gap-4">
          <label class="flex items-center gap-2 text-sm dark:text-gray-200">
            <input type="radio" value="time" v-model="form.refresh_schedule_mode" data-testid="cq-mode-time" />
            Daily at
          </label>
          <input
            type="time" v-model="form.refresh_at_time" data-testid="cq-at-time"
            :disabled="form.refresh_schedule_mode !== 'time'"
            class="text-sm border border-gray-300 dark:border-gray-700 rounded-md px-2 py-1.5 dark:bg-gray-900 dark:text-white disabled:opacity-40"
          />
          <span class="text-[11px] text-gray-400">UTC</span>
        </div>

        <div v-if="editing && cq" class="mt-5 pt-4 border-t border-gray-100 dark:border-gray-800 text-xs text-gray-600 dark:text-gray-400 space-y-1">
          <div>Last refreshed: <b>{{ cq.last_refreshed_at ? new Date(cq.last_refreshed_at + 'Z').toLocaleString() : 'never' }}</b>
            <span v-if="cq.last_refresh_ms != null"> ({{ cq.last_refresh_ms }} ms)</span></div>
          <div>Rows cached: <b>{{ (cq.no_rows || 0).toLocaleString() }}</b></div>
          <div v-if="cq.artifact_bytes">Local size: <b>{{ humanBytes(cq.artifact_bytes) }}</b></div>
          <div v-if="cq.last_refresh_error" class="text-red-600 dark:text-red-400">Last error: {{ cq.last_refresh_error }}</div>
        </div>
      </div>

      <!-- ============ RLS (phase 2) ============ -->
      <div v-show="tab === 'rls'">
        <div class="text-sm text-gray-500 dark:text-gray-400 py-8 text-center">
          Row-level security rules for cached data are coming next.
        </div>
      </div>

      <!-- ============ DANGER ============ -->
      <div v-show="tab === 'danger'">
        <p class="text-xs text-gray-500 dark:text-gray-400 mb-3">
          Deleting removes the cached copy and takes this relation away from
          <b>{{ cq?.active_agent_count ?? 0 }}</b> agent(s) currently using it.
          The source data is not touched.
        </p>
        <button
          data-testid="cq-delete"
          class="flex items-center gap-1.5 border border-red-300 dark:border-red-800 text-red-700 dark:text-red-300 rounded-md px-3 py-1.5 text-xs hover:bg-red-50 dark:hover:bg-red-900/20 disabled:opacity-50"
          :disabled="deleting"
          @click="onDelete"
        >
          <Spinner v-if="deleting" class="w-3.5 h-3.5" />
          <UIcon v-else name="heroicons-trash" class="w-3.5 h-3.5" />
          Delete custom query
        </button>
      </div>

      <!-- Footer -->
      <div class="flex items-center justify-between mt-5 pt-4 border-t border-gray-100 dark:border-gray-800">
        <div class="text-[11px] text-gray-400">
          <span v-if="!canSave && tab === 'query'">Run a preview before saving.</span>
        </div>
        <div class="flex items-center gap-2">
          <button class="px-3 py-1.5 text-xs text-gray-600 dark:text-gray-300 hover:underline" @click="close">Cancel</button>
          <button
            v-if="editing"
            data-testid="cq-refresh-now"
            class="flex items-center gap-1.5 border border-gray-300 dark:border-gray-700 rounded-md px-3 py-1.5 text-xs text-gray-700 dark:text-gray-300 hover:bg-gray-50 dark:hover:bg-gray-800/50 disabled:opacity-50"
            :disabled="refreshingNow" @click="onRefreshNow"
          >
            <Spinner v-if="refreshingNow" class="w-3.5 h-3.5" />
            <UIcon v-else name="heroicons-arrow-path" class="w-3.5 h-3.5" />
            Refresh now
          </button>
          <button
            data-testid="cq-save"
            class="bg-blue-600 hover:bg-blue-700 text-white rounded-md px-4 py-1.5 text-xs disabled:opacity-50"
            :disabled="!canSave || saving"
            @click="onSave"
          >
            <Spinner v-if="saving" class="w-3.5 h-3.5" />
            <span v-else>{{ editing ? 'Save changes' : 'Create & cache' }}</span>
          </button>
        </div>
      </div>
    </div>
  </UModal>
</template>

<script setup lang="ts">
const ROW_LIMIT = 100

const props = defineProps<{
  modelValue: boolean
  connectionId: string
  connectionName: string
  connectionType?: string
  cq?: any | null
}>()

const emit = defineEmits<{
  (e: 'update:modelValue', v: boolean): void
  (e: 'saved'): void
  (e: 'deleted'): void
}>()

const toast = useToast()

const isOpen = computed({
  get: () => props.modelValue,
  set: (v: boolean) => emit('update:modelValue', v),
})

const editing = computed(() => !!props.cq?.id)

const tabs = computed(() => [
  { key: 'query', label: 'Query', disabled: false },
  { key: 'cache', label: 'Cache', disabled: false },
  { key: 'rls', label: 'Row-level security', disabled: true },
  { key: 'danger', label: 'Danger', disabled: !editing.value },
])

const tab = ref('query')
const previewing = ref(false)
const saving = ref(false)
const deleting = ref(false)
const refreshingNow = ref(false)
const preview = ref<any>(null)
const previewError = ref('')

const form = reactive({
  name: '',
  description: '',
  definition_sql: '',
  refresh_schedule_mode: 'interval',
  refresh_interval_minutes: 60,
  refresh_at_time: '03:00',
})

// A successful preview is what supplies the column list, so saving without one
// would create a relation with no known shape.
const canSave = computed(() =>
  !!form.name.trim()
  && !!form.definition_sql.trim()
  && !!preview.value
  && !preview.value?.budget_error
)

watch(() => props.modelValue, (open) => {
  if (!open) return
  tab.value = 'query'
  preview.value = null
  previewError.value = ''
  if (props.cq) {
    form.name = props.cq.name || ''
    form.description = props.cq.description || ''
    form.definition_sql = props.cq.definition_sql || ''
    form.refresh_schedule_mode = props.cq.refresh_schedule_mode || 'interval'
    form.refresh_interval_minutes = props.cq.refresh_interval_minutes || 60
    form.refresh_at_time = props.cq.refresh_at_time || '03:00'
    // Editing an existing relation: its columns are already known, so the
    // preview gate does not apply until the SQL is changed.
    preview.value = { columns: props.cq.columns || [], rows: [], budget_error: null, estimated_rows: props.cq.no_rows }
  } else {
    form.name = ''
    form.description = ''
    form.definition_sql = ''
    form.refresh_schedule_mode = 'interval'
    form.refresh_interval_minutes = 60
    form.refresh_at_time = '03:00'
  }
})

function close() { isOpen.value = false }

function approx(n: number) {
  if (n == null) return '—'
  return n >= 1000 ? `~${n.toLocaleString()}` : String(n)
}

function humanBytes(b: number) {
  if (!b) return '—'
  const u = ['B', 'KB', 'MB', 'GB', 'TB']
  let i = 0, v = b
  while (v >= 1024 && i < u.length - 1) { v /= 1024; i++ }
  return `${v.toFixed(v < 10 && i > 0 ? 1 : 0)} ${u[i]}`
}

async function runPreview() {
  previewing.value = true
  previewError.value = ''
  preview.value = null
  try {
    const { data, error } = await useMyFetch(
      `/connections/${props.connectionId}/custom-queries/preview`,
      { method: 'POST', body: { definition_sql: form.definition_sql } },
    )
    if (error.value) {
      previewError.value = error.value?.data?.detail || 'Query failed'
    } else {
      preview.value = data.value
    }
  } catch (e: any) {
    previewError.value = e?.message || String(e)
  } finally {
    previewing.value = false
  }
}

async function onSave() {
  saving.value = true
  try {
    const body: any = {
      name: form.name.trim(),
      definition_sql: form.definition_sql,
      description: form.description || null,
      refresh_schedule_mode: form.refresh_schedule_mode,
      refresh_interval_minutes: form.refresh_interval_minutes,
      refresh_at_time: form.refresh_schedule_mode === 'time' ? form.refresh_at_time : null,
    }
    const url = editing.value
      ? `/connections/${props.connectionId}/custom-queries/${props.cq.id}`
      : `/connections/${props.connectionId}/custom-queries`
    const { data, error } = await useMyFetch(url, { method: editing.value ? 'PUT' : 'POST', body })
    if (error.value) {
      toast.add({ title: 'Could not save', description: error.value?.data?.detail || 'Failed', color: 'red' })
      return
    }
    const saved: any = data.value
    toast.add({
      title: editing.value ? 'Custom query updated' : 'Custom query cached',
      description: `${saved.name} — ${(saved.no_rows || 0).toLocaleString()} rows cached locally`,
      color: 'green',
    })
    emit('saved')
    close()
  } finally {
    saving.value = false
  }
}

async function onRefreshNow() {
  refreshingNow.value = true
  try {
    const { data, error } = await useMyFetch(
      `/connections/${props.connectionId}/custom-queries/${props.cq.id}/refresh`,
      { method: 'POST' },
    )
    if (error.value) {
      toast.add({ title: 'Refresh failed', description: error.value?.data?.detail || 'Failed', color: 'red' })
      return
    }
    const r: any = data.value
    toast.add({ title: 'Refreshed', description: `${(r.no_rows || 0).toLocaleString()} rows in ${r.last_refresh_ms} ms`, color: 'green' })
    emit('saved')
  } finally {
    refreshingNow.value = false
  }
}

async function onDelete() {
  deleting.value = true
  try {
    const { error } = await useMyFetch(
      `/connections/${props.connectionId}/custom-queries/${props.cq.id}`,
      { method: 'DELETE' },
    )
    if (error.value) {
      toast.add({ title: 'Delete failed', description: error.value?.data?.detail || 'Failed', color: 'red' })
      return
    }
    toast.add({ title: 'Custom query deleted', color: 'green' })
    emit('deleted')
    close()
  } finally {
    deleting.value = false
  }
}
</script>
