<template>
  <UModal v-model="isOpen" :ui="{ width: 'sm:max-w-5xl' }">
    <div class="p-5" data-testid="table-preview-modal">
      <!-- Header -->
      <div class="flex items-start justify-between gap-3 mb-3">
        <div class="min-w-0">
          <h2 class="text-lg font-semibold text-ink truncate" dir="ltr">{{ tableName }}</h2>
          <p class="text-xs text-ink-muted mt-0.5">
            {{ t('tablePreview.subtitle', { connection: connectionName }) }}
          </p>
        </div>
        <UButton
          color="gray"
          variant="ghost"
          size="xs"
          icon="i-heroicons-x-mark"
          :aria-label="t('tablePreview.close')"
          @click="isOpen = false"
        />
      </div>

      <!-- Where custom tables can be authored, the query is editable and runs
           through the custom-table preview route (read-only guard, row bound,
           manage_connection). Elsewhere it is shown as the query that ran. -->
      <div v-if="canCreateCustomTable && sqlDraft !== null" class="mb-3">
        <textarea
          v-model="sqlDraft"
          dir="ltr"
          rows="3"
          spellcheck="false"
          data-testid="table-preview-sql-editor"
          :aria-label="t('tablePreview.sqlLabel')"
          class="w-full text-xs font-mono text-ink-2 bg-surface-subtle border border-line rounded-md px-2 py-1.5 focus:outline-none focus:border-blue-500 resize-y"
          @keydown.meta.enter.prevent="runQuery"
          @keydown.ctrl.enter.prevent="runQuery"
        />
        <div class="flex items-center gap-2 mt-1.5">
          <UButton
            size="xs"
            icon="i-heroicons-play"
            :loading="loading"
            :disabled="!sqlDraft.trim()"
            data-testid="table-preview-run"
            @click="runQuery"
          >
            {{ t('tablePreview.run') }}
          </UButton>
          <UButton
            v-if="sqlDraft !== tableSql"
            color="gray"
            variant="ghost"
            size="xs"
            icon="i-heroicons-arrow-uturn-left"
            :disabled="loading"
            data-testid="table-preview-reset"
            @click="resetQuery"
          >
            {{ t('tablePreview.resetQuery') }}
          </UButton>
          <span class="text-2xs text-ink-muted">{{ t('tablePreview.runHint') }}</span>
        </div>
      </div>
      <pre
        v-else-if="preview?.sql"
        dir="ltr"
        data-testid="table-preview-sql"
        class="text-2xs font-mono text-ink-2 bg-surface-subtle border border-line rounded-md px-2 py-1.5 mb-3 overflow-x-auto"
      >{{ preview.sql }}</pre>

      <div v-if="loading" class="flex items-center justify-center gap-2 py-12 text-sm text-ink-muted">
        <Spinner class="w-4 h-4" />
        {{ t('tablePreview.loading') }}
      </div>

      <div
        v-else-if="errorMessage"
        data-testid="table-preview-error"
        class="text-xs text-red-600 dark:text-red-400 bg-red-50 dark:bg-red-900/20 rounded-md px-3 py-2 whitespace-pre-wrap break-words"
      >
        {{ errorMessage }}
      </div>

      <template v-else-if="preview">
        <div class="flex items-center gap-3 text-2xs text-ink-muted mb-1.5">
          <span data-testid="table-preview-count">
            {{ preview.total_rows
              ? t('tablePreview.showingOf', { shown: preview.rows.length, total: formatCount(preview.total_rows) })
              : t('tablePreview.showing', { shown: preview.rows.length }) }}
          </span>
          <span>{{ t('tablePreview.columns', { count: preview.columns.length }) }}</span>
        </div>
        <div v-if="!preview.rows.length" class="text-sm text-ink-muted py-8 text-center" data-testid="table-preview-empty">
          {{ t('tablePreview.empty') }}
        </div>
        <!-- Column order is the source's, so the grid is always LTR. Each
             value aligns by its own first strong character (a Hebrew name
             reads right-to-left), as grid cells do in assets/css/rtl.css. -->
        <div v-else dir="ltr" class="border border-line rounded-md overflow-auto" style="max-height: 420px">
          <table class="min-w-full text-2xs nums" data-testid="table-preview-grid">
            <thead class="bg-surface-subtle sticky top-0">
              <tr>
                <th
                  v-for="c in preview.columns"
                  :key="c.name"
                  class="text-start font-medium text-ink-2 px-2 py-1.5 whitespace-nowrap border-b border-line"
                  :title="c.dtype || ''"
                >
                  {{ c.name }}
                </th>
              </tr>
            </thead>
            <tbody>
              <tr
                v-for="(row, i) in preview.rows"
                :key="i"
                class="odd:bg-white even:bg-gray-50/50 dark:odd:bg-gray-950 dark:even:bg-gray-900/40"
              >
                <td
                  v-for="(cell, j) in row"
                  :key="j"
                  class="px-2 py-1 whitespace-nowrap font-mono text-ink-2 max-w-xs truncate text-start"
                  style="unicode-bidi: plaintext"
                  :title="cell === null ? '' : String(cell)"
                >
                  <span v-if="cell === null" class="text-ink-subtle">NULL</span>
                  <template v-else>{{ cell }}</template>
                </td>
              </tr>
            </tbody>
          </table>
        </div>
      </template>

      <!-- Footer -->
      <!-- The row bound is written into the query above (TOP / LIMIT /
           ROWNUM), so there is no separate "limited to N rows" note. -->
      <div class="flex items-center justify-end gap-2 mt-5 pt-4 border-t border-line">
        <div class="flex items-center gap-2">
          <UButton
            v-if="!canCreateCustomTable"
            color="gray"
            variant="ghost"
            size="xs"
            icon="i-heroicons-arrow-path"
            :disabled="loading"
            data-testid="table-preview-reload"
            @click="load"
          >
            {{ t('tablePreview.reload') }}
          </UButton>
          <UButton
            v-if="canCreateCustomTable"
            size="xs"
            icon="i-heroicons-bolt"
            :disabled="loading || !(sqlDraft || preview?.sql || '').trim()"
            data-testid="table-preview-create-custom"
            @click="onCreateCustomTable"
          >
            {{ t('tablePreview.createCustomTable') }}
          </UButton>
        </div>
      </div>
    </div>
  </UModal>
</template>

<script setup lang="ts">
type PreviewResponse = {
  table_id: string
  table_name: string
  sql: string
  // The same query without its row bound (absent for an edited query).
  base_sql?: string
  columns: { name: string; dtype?: string }[]
  rows: unknown[][]
  row_limit: number
  truncated: boolean
  total_rows: number | null
}

const props = defineProps<{
  modelValue: boolean
  connectionId: string
  connectionName: string
  tableId: string
  tableName: string
  // Shown only where a custom table can actually be authored on this
  // connection (feature on, connection supports it, caller manages it).
  canCreateCustomTable?: boolean
}>()

const emit = defineEmits<{
  (e: 'update:modelValue', v: boolean): void
  (e: 'create-custom-table', payload: { connectionId: string; tableName: string; sql: string }): void
}>()

const { t, locale } = useI18n()
const { getErrorMessage } = useErrorMessage()

const isOpen = computed({
  get: () => props.modelValue,
  set: (v: boolean) => emit('update:modelValue', v),
})

const loading = ref(false)
const errorMessage = ref('')
const preview = ref<PreviewResponse | null>(null)
// The server-built SELECT for this table, and the admin's edit of it. null
// until the first preview lands.
const tableSql = ref<string | null>(null)
// The same SELECT without its TOP/LIMIT, for building a custom table.
const tableBaseSql = ref<string | null>(null)
const sqlDraft = ref<string | null>(null)
// A slow source can answer after the user has moved to another table; only the
// newest request may write the result.
let requestSeq = 0

async function load() {
  if (!props.connectionId || !props.tableId) return
  const seq = ++requestSeq
  loading.value = true
  errorMessage.value = ''
  preview.value = null
  try {
    const { data, error } = await useMyFetch(
      `/connections/${props.connectionId}/tables/${props.tableId}/preview`,
      { method: 'GET' },
    )
    if (seq !== requestSeq) return
    if (error.value) {
      errorMessage.value = getErrorMessage(error.value, t('tablePreview.failed'))
    } else {
      preview.value = data.value as PreviewResponse
      tableSql.value = preview.value.sql
      tableBaseSql.value = preview.value.base_sql || null
      sqlDraft.value = preview.value.sql
    }
  } catch (e) {
    if (seq === requestSeq) errorMessage.value = getErrorMessage(e, t('tablePreview.failed'))
  } finally {
    if (seq === requestSeq) loading.value = false
  }
}

// An edited query runs through the custom-table preview route: the same
// read-only guard and row bound the custom-table editor uses. The unedited
// table query keeps using the table route, which needs no beta flag.
async function runQuery() {
  const sql = (sqlDraft.value || '').trim()
  if (!sql || loading.value) return
  if (sql === tableSql.value) return load()
  const seq = ++requestSeq
  loading.value = true
  errorMessage.value = ''
  try {
    const { data, error } = await useMyFetch(
      `/connections/${props.connectionId}/custom-queries/preview`,
      { method: 'POST', body: { definition_sql: sql } },
    )
    if (seq !== requestSeq) return
    if (error.value) {
      errorMessage.value = getErrorMessage(error.value, t('tablePreview.failed'))
      preview.value = null
    } else {
      const r = data.value as any
      preview.value = {
        table_id: props.tableId,
        table_name: props.tableName,
        sql,
        columns: r.columns || [],
        rows: r.rows || [],
        row_limit: r.row_limit,
        truncated: !!r.truncated,
        total_rows: r.estimated_rows ?? null,
      }
    }
  } catch (e) {
    if (seq === requestSeq) errorMessage.value = getErrorMessage(e, t('tablePreview.failed'))
  } finally {
    if (seq === requestSeq) loading.value = false
  }
}

function resetQuery() {
  sqlDraft.value = tableSql.value
  load()
}

watch(
  () => [props.modelValue, props.connectionId, props.tableId] as const,
  ([open], prev) => {
    if (!open) return
    // A different table starts from its own query, not the last one's edit.
    if (!prev || prev[2] !== props.tableId || prev[1] !== props.connectionId) {
      tableSql.value = null
      tableBaseSql.value = null
      sqlDraft.value = null
    }
    if (sqlDraft.value === null || sqlDraft.value === tableSql.value) load()
    else runQuery()
  },
  { immediate: true },
)

function formatCount(n: number): string {
  return new Intl.NumberFormat(locale.value).format(n)
}

function onCreateCustomTable() {
  // Unedited, the draft is the bounded preview query; a custom table built
  // from it would hold only the preview's rows, so start from the whole table.
  // An edited query is the admin's own and goes over as written.
  const draft = (sqlDraft.value || preview.value?.sql || '').trim()
  const sql = draft === (tableSql.value || '').trim() && tableBaseSql.value
    ? tableBaseSql.value
    : draft
  if (!sql) return
  emit('create-custom-table', {
    connectionId: props.connectionId,
    tableName: props.tableName,
    sql,
  })
  isOpen.value = false
}
</script>
