<template>
  <div class="-mx-5 min-h-0 overflow-y-auto border-t border-line text-sm text-ink" role="tabpanel">
    <p v-if="!hasFeature('rls')" class="p-6">{{ t('errors.license.enterprise_required') }}</p>
    <div v-else class="px-5 py-5 sm:px-7">
      <div v-if="screen === 'catalog'" class="space-y-5">
        <header class="flex flex-wrap items-start justify-between gap-3">
          <div><h3 class="heading">{{ t('accessEditor.tables') }}</h3><p class="hint">{{ t('accessEditor.chooseTable') }}</p></div>
          <UButton color="white" icon="i-heroicons-adjustments-horizontal" @click="openMapping">{{ t('accessEditor.connectionSetup') }}</UButton>
        </header>
        <div class="overflow-hidden rounded-lg border border-line">
          <div class="flex flex-wrap items-center gap-2 border-b border-line p-3">
            <UInput v-model="search" class="min-w-48 flex-1" icon="i-heroicons-magnifying-glass" :placeholder="t('liveAccess.search')" :aria-label="t('liveAccess.search')" />
            <select v-model="filter" class="control w-auto" :aria-label="t('accessEditor.status')">
              <option v-for="s in statuses" :key="s" :value="s">{{ t('liveAccess.' + s) }} ({{ coverage?.counts[s] ?? '…' }})</option>
            </select>
            <UButton color="gray" variant="ghost" icon="i-heroicons-arrow-up-tray" @click="screen = 'import'">{{ t('accessEditor.import') }}</UButton>
          </div>
          <div v-if="draft.inventory.length" class="border-b border-line px-4 py-2"><label class="flex items-center gap-2"><input v-model="inventoryOnly" type="checkbox">{{ t('liveAccess.inventory') }}</label></div>
          <p v-if="coverage?.unresolved_total" class="p-3 text-xs">{{ t('liveAccess.missing') }} ({{ coverage.unresolved_total }}) · {{ coverage.unresolved.join(', ') }}</p>
          <div class="catalog-row bg-surface-subtle text-xs font-medium text-ink-muted"><span>{{ t('fabricCompany.table') }}</span><span>{{ t('accessEditor.status') }}</span><span /></div>
          <button v-for="row in coverage?.tables" :key="row.table" class="catalog-row w-full border-t border-line text-start hover:bg-surface-subtle" @click="row.table === draft.grants_table ? openMapping() : openTable(row.table)">
            <span class="flex min-w-0 items-center gap-3"><UIcon name="i-heroicons-table-cells" class="size-4 shrink-0 text-ink-muted" /><span class="truncate font-medium">{{ row.table }}</span></span>
            <span class="flex items-center gap-2 text-xs"><i class="size-2 rounded-full" :class="row.pending ? 'bg-amber-500' : row.protected ? 'bg-emerald-500' : 'bg-ink-subtle'" />{{ row.table === draft.grants_table ? t('fabricCompany.grants_table') : t('liveAccess.' + (row.pending ? 'pending' : row.protected ? 'protected' : 'missing')) }}</span>
            <UIcon name="i-heroicons-chevron-right" class="size-4 text-ink-muted rtl:rotate-180" />
          </button>
          <p v-if="coverage && !coverage.tables.length" class="p-5 text-ink-muted">{{ t('accessEditor.noTables') }}</p>
        </div>
        <div class="flex items-center justify-between gap-2">
          <div class="flex gap-2">
            <UButton v-if="offset" color="white" @click="offset -= pageSize">{{ t('liveAccess.previous') }}</UButton>
            <UButton v-if="coverage && offset + pageSize < coverage.counts[filter]" color="white" @click="offset += pageSize">{{ t('liveAccess.next') }}</UButton>
          </div>
          <UButton v-if="coverage?.activation_enabled && saved && hasPending" color="white" @click="openReview">{{ t('liveAccess.review') }}</UButton>
        </div>
      </div>
      <template v-else>
        <button class="mb-3 inline-flex items-center gap-1 text-xs text-ink-muted hover:text-ink" @click="screen = 'catalog'">
          <UIcon name="i-heroicons-arrow-left" class="size-3.5 rtl:rotate-180" />{{ t('accessEditor.tables') }}
        </button>
        <template v-if="screen === 'editor'">
          <h3 class="heading flex items-center gap-2"><UIcon name="i-heroicons-table-cells" class="size-5 text-ink-muted" />{{ selected }}</h3>
          <div class="mt-4 grid gap-5 md:grid-cols-[minmax(0,1.4fr)_minmax(0,1fr)]">
            <section class="min-w-0">
              <div class="inline-flex rounded-lg bg-surface-muted p-1">
                <button v-for="mode in modes" :key="mode" :aria-pressed="tab === mode" class="rounded-md px-3 py-1.5 text-xs font-medium" :class="tab === mode ? 'bg-surface text-ink shadow-sm' : 'text-ink-muted'" @click="tab = mode">{{ t('accessEditor.' + mode) }}</button>
              </div>
              <div v-if="!rule" class="mt-4"><UButton @click="addRule">{{ t('fabricCompany.add') }}</UButton></div>
              <div v-else-if="tab === 'rows'" class="mt-4 space-y-4">
                <label class="label">{{ t('grantMapping.target') }}<select v-if="physicalColumns.length" v-model="rule.company_column" class="control"><option disabled value="">{{ t('grantMapping.chooseColumn') }}</option><option v-for="name in [...new Set([rule.company_column, ...physicalColumns].filter(Boolean))]" :key="name" :value="name">{{ name }}</option></select><input v-else v-model.trim="rule.company_column" class="control" /></label>
                <label class="label">{{ t('accessEditor.grantVia') }}
                  <select :value="rule.row_attribute != null ? 'property' : 'table'" class="control" @change="setRowMode(($event.target as HTMLSelectElement).value)">
                    <option value="table">{{ t('fabricCompany.grants_table') }}</option><option value="property">{{ t('accessEditor.property') }}</option>
                  </select>
                </label>
                <template v-if="rule.row_attribute == null">
                  <label v-if="draft.source_column" class="label">{{ t('grantMapping.resourceTypeValue') }}<input v-model.trim="rule.source" class="control" /></label>
                  <button class="flex max-w-full items-center gap-1.5 text-xs text-ink-muted underline" @click="openMapping"><UIcon name="i-heroicons-link" class="size-3.5" />{{ draft.grants_table || t('accessEditor.connectionSetup') }}</button>
                </template>
                <label v-else class="label">{{ t('accessEditor.property') }}<select v-model="rule.row_attribute" class="control"><option v-for="prop in propertyOptions(rule.row_attribute)" :key="prop" :value="prop">{{ propertyLabel(prop) }}</option></select></label>
              </div>
              <div v-else class="mt-4 space-y-3">
                <label class="flex items-center gap-2 font-medium"><input type="checkbox" :checked="rule.columns != null" @change="toggleColumns(($event.target as HTMLInputElement).checked)" />{{ t('liveAccess.restrict') }}</label>
                <p class="hint">{{ t(rule.columns == null ? 'accessEditor.allColumns' : 'accessEditor.selectedColumns') }}</p>
                <template v-if="rule.columns != null">
                  <div v-for="column in columnChoices" :key="column" class="rounded-lg border border-line p-3">
                    <label class="flex items-center gap-2 font-medium"><input type="checkbox" :checked="Object.hasOwn(rule.columns, column)" @change="setVisible(column, ($event.target as HTMLInputElement).checked)" />{{ column }}</label>
                    <div v-if="Object.hasOwn(rule.columns, column)" class="mt-2 space-y-2">
                      <select class="control" :aria-label="t('accessEditor.audience')" :value="rule.columns[column] == null ? 'all' : 'match'" @change="setAudience(column, ($event.target as HTMLSelectElement).value)">
                        <option value="all">{{ t('accessEditor.everyReader') }}</option><option value="match">{{ t('accessEditor.match') }}</option>
                      </select>
                      <div v-if="rule.columns[column] != null" class="grid gap-2 sm:grid-cols-2">
                        <label class="label">{{ t('accessEditor.property') }}<select class="control" :value="matchFor(column).source" @change="setMatch(column, 'source', ($event.target as HTMLSelectElement).value)"><option v-for="prop in propertyOptions(matchFor(column).source)" :key="prop" :value="prop">{{ propertyLabel(prop) }}</option></select></label>
                        <label class="label">{{ t('accessEditor.values') }}<input class="control" :value="matchFor(column).values.join(', ')" @change="setMatch(column, 'values', ($event.target as HTMLInputElement).value)" /></label>
                      </div>
                    </div>
                  </div>
                  <form v-if="!physicalColumns.length" class="flex items-center gap-2" @submit.prevent="addColumn"><input v-model.trim="newColumn" class="control" :placeholder="t('accessEditor.columnName')" :aria-label="t('accessEditor.columnName')" /><UButton type="submit" color="white" :disabled="!newColumn">{{ t('accessEditor.add') }}</UButton></form>
                </template>
              </div>
              <div class="mt-5 flex items-center justify-between gap-2 border-t border-line pt-3">
                <UButton :disabled="busy || loadFailed || saved || !draft.tables.length" @click="save">{{ t('fabricCompany.save') }}</UButton>
                <UButton v-if="rule && draft.tables.length > 1" color="gray" variant="ghost" size="xs" @click="removeRule">{{ t('accessEditor.removeRule') }}</UButton>
              </div>
            </section>
            <aside class="self-start rounded-lg border border-line bg-surface-subtle p-4">
              <h4 class="flex items-center gap-2 font-semibold"><UIcon name="i-heroicons-eye" class="size-4 text-ink-muted" />{{ t('accessEditor.previewAs') }}</h4>
              <p class="hint">{{ t(saved ? 'accessEditor.previewHint' : 'accessEditor.saveFirst') }}</p>
              <div class="mt-3 space-y-2">
                <USelectMenu v-if="canChooseMember" v-model="reader" searchable :options="memberOptions" option-attribute="label" value-attribute="value" :aria-label="t('accessEditor.previewAs')" />
                <UButton size="xs" color="white" :loading="previewing" :disabled="busy || !saved || !rule" @click="preview()">{{ t('accessEditor.runPreview') }}</UButton>
                <div v-if="sample" class="overflow-x-auto rounded border border-line bg-surface p-2 text-xs"><strong>{{ t('liveAccess.' + sample.access_status) }}</strong>
                  <table v-if="sample.data.length" class="mt-2 w-full"><thead><tr><th v-for="col in sample.columns" :key="col">{{ col }}</th></tr></thead><tbody><tr v-for="(row,j) in sample.data" :key="j"><td v-for="(cell,k) in row" :key="k">{{ cell }}</td></tr></tbody></table>
                </div>
              </div>
              <div v-if="coverage?.activation_enabled && saved && hasPending" class="mt-4 border-t border-line pt-3"><UButton size="xs" color="white" @click="openReview">{{ t('liveAccess.review') }}</UButton></div>
            </aside>
          </div>
        </template>
        <section v-if="screen === 'mapping'" class="space-y-4">
          <h3 class="heading">{{ t('accessEditor.connectionSetup') }}</h3>
          <div class="grid max-w-2xl gap-4 sm:grid-cols-2">
            <label class="label">{{ t('liveAccess.identity') }}<select v-model="draft.reader_source" class="control"><option value="entra">Microsoft Entra</option><option value="verified_email">{{ t('liveAccess.email') }}</option></select></label>
            <label v-if="draft.reader_source === 'entra'" class="label">{{ t('fabricCompany.tenant_id') }}<input v-model.trim="draft.tenant_id" class="control" /></label>
          </div>
          <div class="max-w-2xl space-y-3 border-t border-line pt-4">
            <label class="label">{{ t('fabricCompany.grants_table') }}<input v-model.trim="draft.grants_table" list="grant-tables" class="control" @input="searchGrantTables" /></label>
            <datalist id="grant-tables"><option v-for="name in grantTables" :key="name" :value="name" /></datalist>
            <div class="grid gap-3 sm:grid-cols-2">
              <label class="label">{{ t('grantMapping.principal') }}
                <select v-model="draft.grant_principal" class="control" :aria-label="t('grantMapping.principal')"><option v-for="prop in propertyOptions(draft.grant_principal)" :key="prop" :value="prop">{{ propertyLabel(prop) }}</option></select>
              </label>
              <label class="label">{{ t('grantMapping.principalColumn') }}<input v-model.trim="draft.employee_column" class="control" list="grant-columns" /></label>
              <label class="label">{{ t('grantMapping.valueColumn') }}<input v-model.trim="draft.company_column" class="control" list="grant-columns" /></label>
            </div>
            <label class="flex items-center gap-2"><input type="checkbox" :checked="draft.source_column != null" @change="toggleResourceType(($event.target as HTMLInputElement).checked)" />{{ t('grantMapping.useType') }}</label>
            <label v-if="draft.source_column != null" class="label">{{ t('grantMapping.typeColumn') }}<input v-model.trim="draft.source_column" class="control" list="grant-columns" /></label>
            <datalist id="grant-columns"><option v-for="name in grantColumns" :key="name" :value="name" /></datalist>
          </div>
          <UButton color="white" @click="screen = mappingReturn">{{ t('accessEditor.applySetup') }}</UButton>
          <UButton v-if="draft.tables.length && !saved" class="ms-2" :disabled="busy || loadFailed" @click="save">{{ t('fabricCompany.save') }}</UButton>
        </section>
        <section v-if="screen === 'import'" class="max-w-xl space-y-3"><h3 class="heading">{{ t('liveAccess.bulk') }}</h3><label class="label">{{ t(draft.source_column ? 'grantMapping.importTyped' : 'grantMapping.importSimple') }}<textarea v-model="bulkText" class="control" rows="5" /></label><UButton @click="applyBulk">{{ t('liveAccess.apply') }}</UButton></section>
        <section v-if="screen === 'review'" class="max-w-xl space-y-4">
          <h3 class="heading">{{ t('liveAccess.review') }}</h3>
          <p class="text-ink-2">{{ t('liveAccess.impact') }}</p>
          <div class="rounded-lg border border-line p-4"><strong>{{ t('liveAccess.pending') }} · {{ pendingCount }}</strong><p v-for="name in pendingNames" :key="name" class="mt-1 text-ink-2">{{ name }}</p></div>
          <UButton :disabled="busy || !saved" @click="activate">{{ t('liveAccess.activate') }}</UButton>
        </section>
      </template>
      <div v-if="screen === 'catalog' && !saved && draft.tables.length" class="mt-3"><UButton :disabled="busy || loadFailed" @click="save">{{ t('fabricCompany.save') }}</UButton></div>
      <p v-if="message" role="status" class="mt-3 text-xs text-ink-2">{{ message }}</p>
    </div>
  </div>
</template>
<script setup lang="ts">
import { useEnterprise } from '~/ee/composables/useEnterprise'
import { useCan } from '~/composables/usePermissions'

type Screen = 'catalog' | 'editor' | 'mapping' | 'review' | 'import'
type Match = { source: string, values: string[] }
type Rule = {
  table: string, company_column: string, source: string | null, row_attribute: string | null,
  columns: Record<string, null | string[] | Match> | null
}
const statuses = ['all', 'protected', 'missing', 'pending'] as const
type Status = typeof statuses[number]
type Coverage = {
  counts: Record<Status, number>, properties: string[], activation_enabled: boolean,
  pending_total: number, pending_tables: string[], unresolved_total: number, unresolved: string[], tables: {table: string, protected: boolean, pending: boolean}[]
}
type Sample = { columns: string[], data: unknown[][], access_status: string }

const props = defineProps<{ connectionId: string }>()
const root = '/connections/' + encodeURIComponent(props.connectionId) + '/company-policy'
const { hasFeature } = useEnterprise()
const { organization } = useOrganization()
const canChooseMember = computed(() => useCan('full_admin_access'))
const { t } = useI18n()
const screen = ref<Screen>('catalog')
const mappingReturn = ref<Screen>('catalog')
const modes = ['rows', 'columns'] as const
const tab = ref<typeof modes[number]>('rows')
const draft = reactive({
  grant_principal: 'user.email', reader_source: 'verified_email', tenant_id: null as string | null, grants_table: null as string | null,
  employee_column: null as string | null, company_column: null as string | null, source_column: null as string | null,
  inventory: [] as string[], tables: [] as Rule[]
})
const coverage = ref<Coverage | null>(null)
const filter = ref<Status>('all'), search = ref(''), offset = ref(0), inventoryOnly = ref(false)
const pageSize = 12
const selected = ref(''), physicalColumns = ref<string[]>([]), grantColumns = ref<string[]>([])
const grantTables = ref<string[]>([]), newColumn = ref(''), bulkText = ref('')
const rule = computed(() => draft.tables.find(row => row.table === selected.value))
const columnChoices = computed(() => [...new Set([...physicalColumns.value, ...Object.keys(rule.value?.columns || {})])])
const pendingCount = ref(0), pendingNames = ref<string[]>([])
const hasPending = computed(() => pendingCount.value > 0)
const message = ref(''), busy = ref(false), loadFailed = ref(false), savedText = ref('')
const saved = computed(() => savedText.value === JSON.stringify(draft))
const self = '__current_reader__'
const reader = ref(self)
const sample = ref<Sample | null>(null)
const previewing = ref(false)
const members = ref<{email: string, name?: string}[]>([])
const memberOptions = computed(() => [
  {value: self, label: t('accessEditor.myself')},
  ...members.value.map(member => ({value: member.email, label: member.name ? member.name + ' · ' + member.email : member.email}))
])
let previewRevision = 0
function clearPreview() {
  previewRevision++
  sample.value = null
}
let generation = 0
async function loadCoverage(query: Record<string, unknown>) {
  const {data, error} = await useMyFetch(root + '/coverage', {query})
  if (error.value) throw Error()
  return data.value as Coverage
}
async function refresh() {
  const id = ++generation
  try {
    const result = await loadCoverage({search: search.value, status: filter.value, inventory_only: inventoryOnly.value, offset: offset.value, limit: pageSize})
    if (id === generation) {
      coverage.value = result
      pendingCount.value = result.pending_total
      pendingNames.value = result.pending_tables
    }
  } catch {
    if (id === generation) { coverage.value = null; message.value = t('fabricCompany.failed') }
  }
}
async function refreshPending() {
  const result = await loadCoverage({status: 'pending', limit: 12})
  pendingCount.value = result.pending_total
  pendingNames.value = result.pending_tables
}
async function loadColumns(table: string) {
  if (!table) return []
  const {data, error} = await useMyFetch(root + '/columns', {query: {table}})
  return error.value ? [] : data.value as string[]
}
async function searchGrantTables() {
  const term = draft.grants_table || ''
  const result = await loadCoverage({search: term, limit: 30})
  if (term === (draft.grants_table || '')) grantTables.value = result.tables.map(row => row.table)
}
watch([search, filter, inventoryOnly], () => { offset.value = 0; refresh() })
watch(offset, refresh)
watch(draft, clearPreview, {deep: true, flush: 'sync'})
watch(() => draft.grants_table, async name => {
  grantColumns.value = []
  const cols = await loadColumns(name || '')
  if (name === draft.grants_table) grantColumns.value = cols
})
watch(reader, clearPreview, {flush: 'sync'})
watch(() => hasFeature('rls'), async enabled => {
  if (!enabled) return
  try {
    const {data, error} = await useMyFetch(root + '/draft')
    if (error.value) throw Error()
    if (data.value) { Object.assign(draft, data.value); savedText.value = JSON.stringify(draft) }
    await refresh()
    if (canChooseMember.value && organization.value?.id) {
      const result = await useMyFetch('/organizations/' + organization.value.id + '/members')
      if (Array.isArray(result.data.value)) members.value = result.data.value.filter(member => member.email)
    }
  } catch { loadFailed.value = true; message.value = t('fabricCompany.failed') }
}, {immediate: true})
function propertyOptions(current: string) {
  return [...new Set([current, ...(coverage.value?.properties || [])])].filter(Boolean)
}
function propertyLabel(source: string) {
  const labels: Record<string, string> = {'user.email': 'auth.email', groups: 'quotaPolicies.groups', roles: 'quotaPolicies.roles'}
  return labels[source] ? t(labels[source]) : source
}
async function openTable(name: string) {
  selected.value = name
  screen.value = 'editor'
  tab.value = 'rows'
  newColumn.value = ''
  message.value = ''
  clearPreview()
  physicalColumns.value = []
  const cols = await loadColumns(name)
  if (selected.value === name) physicalColumns.value = cols
}
function addRule() {
  if (!rule.value) draft.tables.push({
    table: selected.value, company_column: '', columns: null, row_attribute: null,
    source: draft.source_column ? selected.value.split('.').pop()! : null
  })
}
function removeRule() {
  draft.tables = draft.tables.filter(row => row.table !== selected.value)
}
function setRowMode(mode: string) {
  if (!rule.value) return
  rule.value.row_attribute = mode === 'property' ? 'user.email' : null
  rule.value.source = mode === 'table' && draft.source_column ? selected.value.split('.').pop()! : null
}
function toggleResourceType(enabled: boolean) {
  draft.source_column = enabled ? '' : null
  for (const row of draft.tables) {
    if (!row.row_attribute) row.source = enabled ? row.table.split('.').pop()! : null
  }
}
function toggleColumns(on: boolean) {
  if (rule.value) rule.value.columns = on ? {} : null
}
function setVisible(name: string, visible: boolean) {
  const cols = rule.value?.columns
  if (!cols) return
  if (visible) cols[name] = null
  else delete cols[name]
}
function setAudience(name: string, mode: string) {
  if (rule.value?.columns) rule.value.columns[name] = mode === 'all' ? null : {source: 'roles', values: []}
}
function matchFor(name: string): Match {
  const match = rule.value?.columns?.[name]
  return Array.isArray(match) ? {source: 'user.email', values: match} : match || {source: 'roles', values: []}
}
function setMatch(name: string, field: 'values' | 'source', value: string) {
  if (!rule.value?.columns) return
  const match = matchFor(name)
  rule.value.columns[name] = field === 'values'
    ? {...match, values: value.split(',').map(v => v.trim()).filter(Boolean)}
    : {...match, source: value.trim()}
}
function addColumn() {
  if (!newColumn.value || !rule.value?.columns) return
  if (!Object.hasOwn(rule.value.columns, newColumn.value)) setVisible(newColumn.value, true)
  newColumn.value = ''
}
function openMapping() {
  mappingReturn.value = screen.value
  screen.value = 'mapping'
  searchGrantTables().catch(() => { message.value = t('fabricCompany.failed') })
}
function applyBulk() {
  const entries = bulkText.value.split('\n').map(line => line.trim()).filter(Boolean).map(line => line.split(',').map(v => v.trim()))
  if (!entries.length || entries.some(v => ![1, draft.source_column ? 3 : 2].includes(v.length) || v.some(field => !field))) {
    message.value = t(draft.source_column ? 'grantMapping.importTyped' : 'grantMapping.importSimple')
    return
  }
  for (const [table, key, source] of entries) {
    if (!draft.inventory.includes(table)) draft.inventory.push(table)
    if (key && !draft.tables.some(r => r.table === table)) draft.tables.push({table, company_column: key, source: source || null, row_attribute: null, columns: null})
  }
  bulkText.value = ''
  screen.value = 'catalog'
}
async function save() {
  busy.value = true
  try {
    const {data, error} = await useMyFetch(root + '/draft', {method: 'PUT', body: draft})
    if (error.value || !data.value) throw Error()
    Object.assign(draft, data.value)
    savedText.value = JSON.stringify(draft)
    message.value = t('fabricCompany.saved')
    await refresh()
  } catch { message.value = t('fabricCompany.failed') }
  finally { busy.value = false }
}
async function openReview() {
  try { await refreshPending(); screen.value = 'review' }
  catch { message.value = t('fabricCompany.failed') }
}
async function activate() {
  busy.value = true
  try {
    const {error} = await useMyFetch(root + '/activate', {method: 'POST', body: draft})
    if (error.value) throw Error()
    screen.value = 'catalog'
    message.value = t('liveAccess.active')
    await refresh()
  } catch { message.value = t('fabricCompany.failed') }
  finally { busy.value = false }
}
async function preview() {
  const table = selected.value, email = reader.value, version = savedText.value
  const revision = ++previewRevision
  busy.value = true
  previewing.value = true
  sample.value = null
  try {
    const {data, error} = await useMyFetch(root + '/draft/preview', {query: {table, member_email: email === self ? undefined : email}})
    if (error.value || !data.value) throw Error()
    if (revision === previewRevision && table === selected.value && email === reader.value && saved.value && version === savedText.value) sample.value = data.value as Sample
  } catch { if (revision === previewRevision) message.value = t('grantMapping.previewError') }
  finally { busy.value = false; previewing.value = false }
}
</script>
<style scoped>
.heading { @apply text-lg font-semibold tracking-tight text-ink; }
.hint { @apply mt-1 text-xs leading-5 text-ink-muted; }
.label { @apply flex min-w-0 flex-col gap-1.5 text-xs font-medium text-ink-2; }
.control { @apply min-w-0 rounded-md border border-line-strong bg-surface px-2.5 py-1.5 text-sm font-normal text-ink focus:border-primary-500 focus:outline-none focus:ring-1 focus:ring-primary-500; }
.catalog-row { @apply grid grid-cols-[minmax(0,1fr)_minmax(7rem,30%)_1rem] items-center gap-2 px-4 py-3; }
th, td { @apply border-b border-line p-1.5 text-start; }
</style>
