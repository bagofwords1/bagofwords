<!--
  One form for a saved query, in two modes — the report's "Save Query" layout
  (title, description, agents, status) plus the code, since there is no chat
  step to take it from:

  - create (`entityId` null, `dsId` set): the agent panel's "New query". Run
    previews through the stateless POST /entities/preview; Save mints the row
    with POST /entities and runs it once so it lands with data. Same two
    tiers as the report: entity managers publish, anyone with access to the
    agents suggests (the backend decides; the form only says which).
  - edit (`entityId` + `detail`): the query's Edit button. Run previews
    through POST /entities/{id}/preview; Save is PUT /entities/{id}, plus a
    run when the code changed so the snapshot follows it.
-->
<template>
  <UModal v-model="open" :ui="{ width: 'sm:max-w-2xl', height: 'sm:h-[80vh]' }">
    <div class="h-full flex flex-col bg-gray-50 dark:bg-gray-900" data-testid="entity-edit-modal">
      <!-- Header -->
      <div class="px-4 py-3 bg-white dark:bg-gray-900 border-b flex items-center justify-between flex-shrink-0">
        <div class="text-sm font-medium text-gray-800 dark:text-gray-200" data-testid="entity-edit-title">
          {{ isCreate ? (canCreateEntities ? $t('queries.newQuery') : $t('entityCreate.suggestQuery')) : $t('queries.editQuery') }}
        </div>
        <button class="text-xs text-gray-500 dark:text-gray-400 hover:text-gray-700 dark:hover:text-gray-300" @click="open = false">{{ $t('entityCreate.close') }}</button>
      </div>

      <div class="flex-1 flex overflow-hidden min-h-0">
        <section class="flex-1 flex flex-col overflow-hidden min-h-0">
          <div class="flex-1 overflow-auto">
            <div class="bg-white dark:bg-gray-900 rounded-lg p-3">
              <!-- Tier banners (create only): same wording as the report's Save Query -->
              <template v-if="isCreate">
                <div v-if="canCreateEntities" class="mb-4 p-3 bg-green-50 dark:bg-green-950 border border-green-200 rounded-lg text-xs text-green-800" data-testid="entity-edit-banner-publish">
                  <div class="font-medium mb-1">{{ $t('entityCreate.adminHeading') }}</div>
                  <div>{{ $t('entityCreate.adminBody') }}</div>
                </div>
                <div v-else class="mb-4 p-3 bg-blue-50 dark:bg-blue-950 border border-blue-200 rounded-lg text-xs text-blue-800" data-testid="entity-edit-banner-suggest">
                  <div class="font-medium mb-1">{{ $t('entityCreate.suggestHeading') }}</div>
                  <div>{{ $t('entityCreate.suggestBody') }}</div>
                </div>
              </template>
              <!-- Save failure: surface the backend's reason instead of failing silently -->
              <div v-if="errorMsg" class="mb-4 p-3 bg-red-50 dark:bg-red-950 border border-red-200 rounded-lg text-xs text-red-800" data-testid="entity-edit-error">
                <div class="font-medium mb-1">{{ $t('entityCreate.saveFailed') }}</div>
                <div>{{ errorMsg }}</div>
              </div>

              <EntityForm v-model="form" :show-status="canCreateEntities" />

              <!-- Code: what the saved query runs. Same field group width as the form. -->
              <div class="max-w-2xl mx-auto mt-4">
                <label class="text-xs font-medium text-gray-700 dark:text-gray-300 mb-1 block">{{ $t('queries.codeLabel') }}</label>
                <ClientOnly>
                  <div class="h-56 rounded-lg overflow-hidden border border-gray-300 dark:border-gray-600">
                    <MonacoEditor
                      v-model="code"
                      :lang="editorLang || 'python'"
                      :options="{ theme: 'vs-dark', automaticLayout: true, minimap: { enabled: false }, wordWrap: 'on', fontSize: 13, scrollBeyondLastLine: false }"
                      style="height: 100%"
                    />
                  </div>
                </ClientOnly>
                <div class="mt-2 flex items-center gap-2">
                  <button
                    type="button"
                    data-testid="entity-edit-run"
                    class="bg-white dark:bg-gray-900 border border-gray-300 dark:border-gray-600 rounded-lg px-3 py-1.5 text-xs hover:bg-gray-50 dark:hover:bg-gray-800 flex items-center gap-1.5 disabled:opacity-50"
                    :disabled="running || !code.trim()"
                    @click="run"
                  >
                    <Icon v-if="running" name="heroicons-arrow-path" class="w-3 h-3 animate-spin" />
                    <Icon v-else name="heroicons-play" class="w-3 h-3" />
                    <span>{{ running ? $t('queries.running') : $t('queries.run') }}</span>
                  </button>
                  <span v-if="runError" class="text-xs text-red-600 dark:text-red-400" data-testid="entity-edit-run-error">{{ runError }}</span>
                </div>

                <div v-if="result" class="mt-3 rounded-lg border border-gray-200 dark:border-gray-700 overflow-hidden" data-testid="entity-edit-result">
                  <div class="px-3 py-2 text-xs text-gray-600 dark:text-gray-400 border-b bg-gray-50 dark:bg-gray-900 flex items-center justify-between">
                    <span>{{ $t('queries.resultsHeading') }}</span>
                    <span>{{ $t('queries.previewRows', { n: result.info?.total_rows ?? result.rows?.length ?? 0 }) }}</span>
                  </div>
                  <div class="overflow-auto max-h-48">
                    <table class="min-w-full text-xs">
                      <thead class="bg-gray-50 dark:bg-gray-900 sticky top-0 border-b">
                        <tr>
                          <th v-for="col in result.columns" :key="col.field" class="px-3 py-2 text-start text-xs font-medium text-gray-700 dark:text-gray-300">{{ col.headerName || col.field }}</th>
                        </tr>
                      </thead>
                      <tbody>
                        <tr v-for="(row, i) in (result.rows || []).slice(0, 100)" :key="i" class="border-b hover:bg-gray-50 dark:hover:bg-gray-800">
                          <td v-for="col in result.columns" :key="col.field" class="px-3 py-2 text-gray-800 dark:text-gray-200 whitespace-nowrap">{{ row[col.field] }}</td>
                        </tr>
                      </tbody>
                    </table>
                  </div>
                </div>
              </div>
            </div>
          </div>

          <!-- Footer Actions -->
          <div class="px-4 py-3 bg-white dark:bg-gray-900 border-t flex items-center justify-end gap-2 flex-shrink-0">
            <button class="bg-white dark:bg-gray-900 border border-gray-300 dark:border-gray-600 rounded-lg px-3 py-1.5 text-xs hover:bg-gray-50 dark:hover:bg-gray-800" @click="open = false">{{ $t('entityCreate.cancel') }}</button>
            <button
              data-testid="entity-edit-save"
              class="text-white text-xs font-medium py-1.5 px-3 rounded-lg disabled:opacity-50"
              :class="(!isCreate || canCreateEntities) ? 'bg-blue-500 hover:bg-blue-600' : 'bg-amber-500 hover:bg-amber-600'"
              :disabled="saving || !canSave"
              @click="onSave"
            >
              <span v-if="saving">{{ (!isCreate || canCreateEntities) ? $t('entityCreate.saving') : $t('entityCreate.submitting') }}</span>
              <span v-else-if="!isCreate">{{ $t('queries.save') }}</span>
              <span v-else>{{ canCreateEntities ? $t('entityCreate.saveEntity') : $t('entityCreate.suggestEntity') }}</span>
            </button>
          </div>
        </section>
      </div>
    </div>
  </UModal>
</template>

<script setup lang="ts">
import { ref, computed, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { useMyFetch } from '~/composables/useMyFetch'
import { useCanAll, useCan } from '~/composables/usePermissions'
import EntityForm from './EntityForm.vue'

const { t } = useI18n()
const toast = useToast()

type MinimalDS = { id: string; name?: string; type?: string }
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

// Same rule as the report's Save Query: publishing / full editing needs
// per-agent `create_entities` on EVERY selected agent (org `manage_entities`
// and full admins via implication). Agent-less queries are org-wide and stay
// an org-admin capability.
const selectedIds = computed(() => form.value.data_source_ids || [])
const canCreateEntities = computed(() =>
  selectedIds.value.length ? useCanAll('create_entities', 'data_source', selectedIds.value) : useCan('manage_entities'))
const canSave = computed(() => !!form.value.title.trim() && !!code.value.trim())

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
    return
  }
  form.value = { type: 'model', title: '', description: null, status: 'published', data_source_ids: props.dsId ? [props.dsId] : [], global_status: null }
  code.value = template()
  savedCode.value = ''
  if (!props.dsId) return
  try {
    const { data } = await useMyFetch<any>(`/api/data_sources/${props.dsId}`, { method: 'GET' })
    const ds: any = data.value
    const conn = (ds?.connections || []).find((c: any) => c?.is_active !== false)
    if (ds?.name && conn?.name && code.value === template()) code.value = template(`${ds.name}:${conn.name}`)
  } catch { /* the placeholder key is still a valid starting point */ }
}

watch(() => props.modelValue, (v) => { if (v) reset() })

async function run() {
  running.value = true
  runError.value = ''
  try {
    const { data, error } = isCreate.value
      ? await useMyFetch<any>('/api/entities/preview', { method: 'POST', body: { code: code.value, data_source_ids: selectedIds.value } })
      : await useMyFetch<any>(`/api/entities/${props.entityId}/preview`, { method: 'POST', body: { code: code.value } })
    if (error.value) throw error.value
    const payload: any = data.value
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
    if (isCreate.value) {
      const body = {
        type: form.value.type || 'model',
        title: form.value.title.trim(),
        description: form.value.description || null,
        code: code.value,
        data: {},
        status,
        data_source_ids: selectedIds.value,
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
