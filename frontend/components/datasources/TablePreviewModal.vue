<template>
  <UModal v-model="isOpen" :ui="{ width: 'sm:max-w-6xl' }">
    <div class="flex flex-col max-h-[90vh]" data-testid="table-preview-modal">
      <!-- Header -->
      <div class="flex items-start justify-between gap-3 px-5 pt-4 pb-3">
        <div class="min-w-0">
          <h2 class="text-base font-semibold text-gray-900 dark:text-gray-100 truncate" dir="ltr">{{ tableName }}</h2>
          <p class="text-xs text-ink-muted mt-0.5 truncate">{{ t('tablePreview.subtitle', { connection: connectionName }) }}</p>
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

      <!-- Query: always visible, compact. Editable where custom tables can be
           authored (an edited query runs through the custom-table preview
           route); read-only elsewhere. -->
      <div v-if="sqlDraft !== null" class="px-5 pb-3">
        <SqlEditor
          v-model="sqlDraft"
          :language="preview?.language || 'sql'"
          :read-only="!canEdit"
          :connection-id="canEdit ? connectionId : undefined"
          :min-height="44"
          :max-height="200"
          testid="table-preview-sql-editor"
          @run="runQuery"
        />
        <div class="flex flex-wrap items-center gap-2 mt-2">
          <template v-if="canEdit">
            <UButton
              size="2xs"
              icon="i-heroicons-play"
              :loading="loading"
              :disabled="!sqlDraft.trim()"
              data-testid="table-preview-run"
              @click="runQuery"
            >
              {{ t('tablePreview.run') }}
            </UButton>
            <UButton
              v-if="edited"
              color="gray"
              variant="ghost"
              size="2xs"
              icon="i-heroicons-arrow-uturn-left"
              :disabled="loading"
              data-testid="table-preview-reset"
              @click="resetQuery"
            >
              {{ t('tablePreview.resetQuery') }}
            </UButton>
            <span class="text-2xs text-ink-muted">{{ t('tablePreview.runHint') }}</span>
          </template>
          <span v-else class="text-2xs text-ink-muted">{{ t('tablePreview.readOnlyHint') }}</span>
        </div>
      </div>

      <!-- Result -->
      <div class="px-5 min-h-0 flex-1 flex flex-col">
        <div
          v-if="preview && !needsSignIn && !errorMessage"
          class="flex flex-wrap items-center gap-x-2 text-2xs text-ink-muted mb-1.5"
          data-testid="table-preview-count"
        >
          <span>{{ t('tablePreview.showing', { shown: preview.rows.length }) }}</span>
          <span v-if="!edited && preview.total_rows">· {{ t('tablePreview.ofTotal', { total: formatCount(preview.total_rows) }) }}</span>
          <span>· {{ t('tablePreview.columns', { count: preview.columns.length }) }}</span>
          <span v-if="preview.sampled_from === 'storage' && !edited">· {{ t('tablePreview.storageNote') }}</span>
        </div>

        <div v-if="loading && !preview" class="flex items-center justify-center gap-2 py-12 text-xs text-ink-muted">
          <Spinner class="w-3.5 h-3.5" />
          {{ t('tablePreview.loading') }}
        </div>

        <!-- Per-user connection and no personal sign-in yet: the rows are the
             caller's own to see, so the answer is a sign-in, not an error. -->
        <div
          v-else-if="needsSignIn"
          data-testid="table-preview-sign-in"
          class="flex flex-wrap items-center justify-between gap-2 rounded-md bg-blue-50 dark:bg-blue-500/10 px-3 py-2"
        >
          <span class="text-xs text-blue-700 dark:text-blue-300">{{ t('tablePreview.signInRequired') }}</span>
          <UButton size="2xs" icon="i-heroicons-key" @click="emit('sign-in', connectionId)">
            {{ t('tablePreview.signIn') }}
          </UButton>
        </div>

        <div
          v-else-if="errorMessage"
          data-testid="table-preview-error"
          class="flex items-start justify-between gap-3 text-xs text-red-600 dark:text-red-400 bg-red-50 dark:bg-red-900/20 rounded-md px-3 py-2"
        >
          <span class="whitespace-pre-wrap break-words min-w-0">{{ errorMessage }}</span>
          <button
            type="button"
            class="shrink-0 font-medium hover:underline"
            data-testid="table-preview-retry"
            @click="rerun"
          >
            {{ t('tablePreview.retry') }}
          </button>
        </div>

        <template v-else-if="preview">
          <div v-if="!preview.rows.length" class="text-xs text-ink-muted py-10 text-center" data-testid="table-preview-empty">
            {{ t('tablePreview.empty') }}
          </div>
          <!-- Column order is the source's, so the grid is always LTR. Each
               value aligns by its own first strong character (a Hebrew name
               reads right-to-left), as grid cells do in assets/css/rtl.css. -->
          <div
            v-else
            dir="ltr"
            class="border border-gray-200 dark:border-gray-800 rounded-md overflow-auto min-h-0"
            :class="{ 'opacity-60': loading }"
            style="max-height: 52vh"
          >
            <table class="min-w-full text-xs nums" data-testid="table-preview-grid">
              <thead class="bg-gray-50 dark:bg-gray-900 sticky top-0 z-[1]">
                <tr>
                  <th
                    v-for="c in preview.columns"
                    :key="c.name"
                    class="text-start font-medium text-gray-500 dark:text-gray-400 px-2.5 py-1.5 whitespace-nowrap border-b border-gray-200 dark:border-gray-800"
                    :title="c.dtype || ''"
                  >
                    {{ c.name }}
                  </th>
                </tr>
              </thead>
              <tbody class="divide-y divide-gray-100 dark:divide-gray-800">
                <tr v-for="(row, i) in preview.rows" :key="i" class="hover:bg-gray-50/70 dark:hover:bg-gray-900/50">
                  <td
                    v-for="(cell, j) in row"
                    :key="j"
                    class="px-2.5 py-1 whitespace-nowrap font-mono text-gray-700 dark:text-gray-300 max-w-xs truncate text-start"
                    style="unicode-bidi: plaintext"
                    :title="cell === null ? '' : String(cell)"
                  >
                    <span v-if="cell === null" class="text-ink-muted">NULL</span>
                    <template v-else>{{ cell }}</template>
                  </td>
                </tr>
              </tbody>
            </table>
          </div>
        </template>
      </div>

      <!-- Footer -->
      <div class="flex items-center justify-between gap-2 px-5 py-3 mt-3 border-t border-gray-100 dark:border-gray-800">
        <UButton
          color="gray"
          variant="ghost"
          size="xs"
          icon="i-heroicons-arrow-path"
          :disabled="loading"
          data-testid="table-preview-reload"
          @click="rerun"
        >
          {{ t('tablePreview.reload') }}
        </UButton>
        <UButton
          v-if="canCreateCustomTable"
          size="xs"
          icon="i-heroicons-bolt"
          :disabled="loading || !(sqlDraft || '').trim()"
          data-testid="table-preview-create-custom"
          @click="onCreateCustomTable"
        >
          {{ t('tablePreview.createCustomTable') }}
        </UButton>
      </div>
    </div>
  </UModal>
</template>

<script setup lang="ts">
type PreviewResponse = {
  table_id: string
  table_name: string
  language?: string
  sql: string
  // The same query without its row bound (absent for an edited query).
  base_sql?: string
  sampled_from?: 'query' | 'storage'
  columns: { name: string; dtype?: string }[]
  rows: unknown[][]
  row_limit: number
  truncated: boolean
  // The table's row count from indexing; only meaningful for the unedited
  // table query, so it is never filled from an edited query's estimate.
  total_rows: number | null
}

const props = defineProps<{
  modelValue: boolean
  connectionId: string
  connectionName: string
  tableId: string
  tableName: string
  // Where a custom table can be authored on this connection (feature on,
  // connection supports it, caller manages it). Also gates editing, since an
  // edited query runs through the custom-table preview route.
  canCreateCustomTable?: boolean
}>()

const emit = defineEmits<{
  (e: 'update:modelValue', v: boolean): void
  (e: 'create-custom-table', payload: { connectionId: string; tableName: string; sql: string }): void
  (e: 'sign-in', connectionId: string): void
}>()

const { t, locale } = useI18n()
const { getErrorMessage } = useErrorMessage()

const isOpen = computed({
  get: () => props.modelValue,
  set: (v: boolean) => emit('update:modelValue', v),
})

const loading = ref(false)
const errorMessage = ref('')
const needsSignIn = ref(false)
const preview = ref<PreviewResponse | null>(null)
// The server-built query for this table, and the admin's edit of it. null
// until the first preview lands.
const tableSql = ref<string | null>(null)
// The same query without its row bound, for building a custom table.
const tableBaseSql = ref<string | null>(null)
const sqlDraft = ref<string | null>(null)
// Which table the state above belongs to. Compared on every open, so a
// different table never inherits the last one's edit — whether or not the
// modal was open when the parent switched tables.
const loadedFor = ref('')
// A slow source can answer after a newer request; only the newest may write.
let requestSeq = 0

const canEdit = computed(() => !!props.canCreateCustomTable)
const edited = computed(() =>
  sqlDraft.value !== null && tableSql.value !== null && sqlDraft.value.trim() !== tableSql.value.trim())

// A per-user connection the caller has not signed in to answers 403 with this
// sentence (CredentialsRequiredError); anything else is a real failure.
function isSignInRequired(err: any): boolean {
  const status = err?.statusCode ?? err?.status ?? err?.response?.status
  const detail = String(err?.data?.detail ?? '')
  return status === 403 && /credentials required|connect required/i.test(detail)
}

function begin(): number {
  const seq = ++requestSeq
  loading.value = true
  errorMessage.value = ''
  needsSignIn.value = false
  return seq
}

function fail(seq: number, err: any) {
  if (seq !== requestSeq) return
  if (isSignInRequired(err)) needsSignIn.value = true
  else errorMessage.value = getErrorMessage(err, t('tablePreview.failed'))
}

async function load() {
  if (!props.connectionId || !props.tableId) return
  const seq = begin()
  try {
    const { data, error } = await useMyFetch(
      `/connections/${props.connectionId}/tables/${props.tableId}/preview`,
      { method: 'GET' },
    )
    if (seq !== requestSeq) return
    if (error.value) {
      fail(seq, error.value)
      preview.value = null
    } else {
      preview.value = data.value as PreviewResponse
      tableSql.value = preview.value.sql
      tableBaseSql.value = preview.value.base_sql || null
      sqlDraft.value = preview.value.sql
    }
  } catch (e) {
    fail(seq, e)
  } finally {
    if (seq === requestSeq) loading.value = false
  }
}

// An edited query runs through the custom-table preview route: the same
// read-only guard and row bound the custom-table editor uses.
async function runQuery() {
  const sql = (sqlDraft.value || '').trim()
  if (!sql || loading.value) return
  if (!edited.value) return load()
  const seq = begin()
  try {
    const { data, error } = await useMyFetch(
      `/connections/${props.connectionId}/custom-queries/preview`,
      { method: 'POST', body: { definition_sql: sql } },
    )
    if (seq !== requestSeq) return
    if (error.value) {
      fail(seq, error.value)
      preview.value = null
    } else {
      const r = data.value as any
      preview.value = {
        table_id: props.tableId,
        table_name: props.tableName,
        language: preview.value?.language,
        sql,
        columns: r.columns || [],
        rows: r.rows || [],
        row_limit: r.row_limit,
        truncated: !!r.truncated,
        total_rows: null,
      }
    }
  } catch (e) {
    fail(seq, e)
  } finally {
    if (seq === requestSeq) loading.value = false
  }
}

function rerun() {
  return edited.value ? runQuery() : load()
}

function resetQuery() {
  sqlDraft.value = tableSql.value
  load()
}

watch(
  () => [props.modelValue, props.connectionId, props.tableId] as const,
  ([open, connectionId, tableId]) => {
    if (!open || !connectionId || !tableId) return
    const key = `${connectionId}:${tableId}`
    if (loadedFor.value === key && (preview.value || loading.value)) return
    loadedFor.value = key
    tableSql.value = null
    tableBaseSql.value = null
    sqlDraft.value = null
    preview.value = null
    load()
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
  const draft = (sqlDraft.value || '').trim()
  const sql = !edited.value && tableBaseSql.value ? tableBaseSql.value : draft
  if (!sql) return
  emit('create-custom-table', {
    connectionId: props.connectionId,
    tableName: props.tableName,
    sql,
  })
  isOpen.value = false
}

// The parent reopens the preview after a sign-in; a fresh load then follows.
function reload() {
  loadedFor.value = `${props.connectionId}:${props.tableId}`
  preview.value = null
  load()
}
defineExpose({ reload })
</script>
