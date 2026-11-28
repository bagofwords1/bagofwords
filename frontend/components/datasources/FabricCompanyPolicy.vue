<template>
  <p v-if="!hasFeature('rls')" class="px-6 py-5 text-sm">{{ t('errors.license.enterprise_required') }}</p>
  <div v-else class="space-y-5 overflow-y-auto px-6 py-5 text-sm">
    <section class="space-y-2">
      <h4 class="font-semibold">{{ t('liveAccess.coverage') }}</h4>
      <p class="text-xs text-ink-muted">{{ t('liveAccess.freshness') }}</p>
      <div class="flex flex-wrap gap-2">
        <button v-for="s in statuses" :key="s" type="button" class="rounded border px-2 py-1"
          :class="{ 'bg-surface-muted font-semibold': filter === s }" @click="filter = s">
          {{ t('liveAccess.' + s) }} {{ coverage?.counts[s] ?? '…' }}
        </button>
      </div>
      <input v-model="search" class="field" :placeholder="t('liveAccess.search')" :aria-label="t('liveAccess.search')">
      <label v-if="draft.inventory.length"><input v-model="inventoryOnly" type="checkbox"> {{ t('liveAccess.inventory') }}</label>
      <p v-if="coverage?.unresolved_total">{{ t('liveAccess.inventory') }} · {{ t('liveAccess.missing') }} ({{ coverage.unresolved_total }})<br>{{ coverage.unresolved.join(', ') }}</p>
      <table class="w-full text-start">
        <thead><tr><th>{{ t('fabricCompany.table') }}</th><th>{{ t('liveAccess.active') }}</th><th>{{ t('liveAccess.pending') }}</th></tr></thead>
        <tbody><tr v-for="row in coverage?.tables" :key="row.table">
          <td><button type="button" class="text-blue-600 underline" @click="selectTable(row.table)">{{ row.table }}</button></td>
          <td>{{ row.protected ? t('liveAccess.protected') : t('liveAccess.missing') }}<span v-if="row.projected_columns != null"> · CLS</span></td>
          <td>{{ row.pending ? t('liveAccess.pending') : '—' }}</td>
        </tr></tbody>
      </table>
      <div class="flex gap-2">
        <UButton v-if="offset" color="gray" @click="offset -= pageSize">{{ t('liveAccess.previous') }}</UButton>
        <UButton v-if="coverage && offset + pageSize < coverage.counts[filter]" color="gray" @click="offset += pageSize">{{ t('liveAccess.next') }}</UButton>
      </div>
    </section>
    <fieldset :disabled="busy || loadFailed" class="space-y-3 border-t pt-4">
      <legend class="font-semibold">{{ t('fabricCompany.mapping') }}</legend>
      <label>{{ t('liveAccess.identity') }}
        <select v-model="draft.reader_source" class="field"><option value="entra">Microsoft Entra</option><option value="verified_email">{{ t('liveAccess.email') }}</option></select>
      </label>
      <div class="grid gap-2 sm:grid-cols-2">
        <label v-for="field in mappingFields" :key="field" v-show="field !== 'tenant_id' || draft.reader_source === 'entra'">
          {{ t('fabricCompany.' + field) }}<input v-model.trim="draft[field]" class="field">
        </label>
      </div>
      <details><summary>{{ t('liveAccess.bulk') }}</summary>
        <label>{{ t('liveAccess.bulkHint') }}<textarea v-model="bulkText" class="field" rows="4" /></label>
        <UButton color="gray" @click="applyBulk">{{ t('liveAccess.apply') }}</UButton>
      </details>
      <label>{{ t('fabricCompany.table') }}<input v-model.trim="selected" class="field" list="access-tables"></label>
      <datalist id="access-properties"><option v-for="name in coverage?.properties" :key="name" :value="name" /></datalist>
      <datalist id="access-tables"><option v-for="r in suggestions" :key="r.table" :value="r.table" /></datalist>
      <UButton v-if="selected && !rule" color="gray" @click="addRule">{{ t('fabricCompany.add') }}</UButton>
      <div v-if="rule" class="space-y-3 rounded border p-3">
        <label>{{ t('fabricCompany.company_column') }}<input v-model.trim="rule.company_column" class="field"></label>
        <label>{{ t('fabricCompany.source') }}<input v-model.trim="rule.source" class="field" @input="rule.row_attribute = null"></label>
        <label>{{ t('liveAccess.rowAttribute') }}<input v-model.trim="rule.row_attribute" class="field" placeholder="custom.regions" list="access-properties" @input="rule.source = null"></label>
        <label><input type="checkbox" :checked="rule.columns != null" @change="rule.columns = ($event.target as HTMLInputElement).checked ? {} : null"> {{ t('liveAccess.restrict') }}</label>
        <label v-if="rule.columns != null">{{ t('liveAccess.columns') }}<textarea :value="columnText" class="field" rows="3" :placeholder="columnExample" @change="setColumns(($event.target as HTMLTextAreaElement).value)" /></label>
        <UButton color="gray" @click="draft.tables = draft.tables.filter(r => r.table !== selected)">{{ t('common.remove') }}</UButton>
      </div>
    </fieldset>
    <div class="flex items-center gap-2">
      <UButton :disabled="busy || loadFailed || columnError" @click="save">{{ t('fabricCompany.save') }}</UButton>
      <UButton v-if="coverage?.activation_enabled" color="gray" :disabled="busy || !saved" @click="review = true">{{ t('liveAccess.review') }}</UButton>
    </div>
    <section v-if="review && saved" class="rounded border p-3">
      <p>{{ t('liveAccess.impact') }}</p>
      <p>{{ t('liveAccess.pending') }} · {{ coverage?.counts.pending }}</p>
      <UButton :disabled="busy" @click="activate">{{ t('liveAccess.activate') }}</UButton>
    </section>
    <section v-if="rule && saved" class="space-y-2 border-t pt-3">
      <h4 class="font-semibold">{{ t('liveAccess.compare') }}</h4>
      <div v-for="(_, i) in readers" :key="i">
        <label>{{ t('liveAccess.member') }} {{ i+1 }}<input v-model.trim="readers[i]" class="field" type="email"></label>
        <UButton color="gray" :disabled="busy" @click="preview(i)">{{ readers[i] ? t('liveAccess.previewMember') : t('fabricCompany.preview') }}</UButton>
        <div v-if="samples[i]" class="overflow-x-auto">
          <p>{{ t('liveAccess.' + samples[i]!.access_status) }}</p>
          <table><thead><tr><th v-for="col in samples[i]!.columns" :key="col">{{ col }}</th></tr></thead>
          <tbody><tr v-for="(row, j) in samples[i]!.data" :key="j"><td v-for="(cell, k) in row" :key="k">{{ cell }}</td></tr></tbody></table>
        </div>
      </div>
    </section>
    <p role="status">{{ message }}</p>
  </div>
</template>
<script setup lang="ts">
import { useEnterprise } from '~/ee/composables/useEnterprise'
const { hasFeature } = useEnterprise()
const { t } = useI18n()
const props = defineProps<{ connectionId: string }>()
const root = '/connections/' + encodeURIComponent(props.connectionId) + '/company-policy'
const statuses = ['all','protected','missing','pending'] as const
type Status = typeof statuses[number]
type ColumnMatch = null | string[] | {source:string, values:string[]}
type Rule = {table:string, company_column:string, source:string|null, row_attribute:string|null, columns:Record<string,ColumnMatch>|null}
const mappingFields = ['tenant_id','grants_table','employee_column','company_column','source_column'] as const
const draft = reactive({reader_source:'entra',tenant_id:null,grants_table:null,employee_column:null,company_column:null,source_column:null,inventory:[] as string[],tables:[] as Rule[]})
const selected = ref(''), search = ref(''), filter = ref<Status>('all'), inventoryOnly=ref(false)
const rule = computed(() => draft.tables.find(r=>r.table === selected.value))
const suggestions = computed(() => draft.tables.filter(r => r.table.toLowerCase().includes(selected.value.toLowerCase())).slice(0, 50))
const columnError = ref(false)
watch(selected, () => { columnError.value = false })
const columnExample = 'company_id\namount\nmargin | roles | Finance'
const columnText = computed(() => Object.entries(rule.value?.columns || {}).map(([name,v]) => !v ? name :
  Array.isArray(v) ? [name,'user.email',v.join(',')].join(' | ') : [name,v.source,v.values.join(',')].join(' | ')).join('\n'))
const busy=ref(false), loadFailed=ref(false), review=ref(false), message=ref(''), bulkText=ref('')
const savedText=ref(''), saved=computed(()=>!columnError.value && savedText.value === JSON.stringify(draft))
const offset=ref(0), pageSize=12, readers=ref(['',''])
type Sample={columns:string[],data:unknown[][],access_status:string}
const samples=ref<(Sample|null)[]>([null,null])
const coverage=ref<{counts:Record<Status,number>, properties:string[], activation_enabled:boolean,unresolved_total:number,unresolved:string[],tables:{table:string,protected:boolean,pending:boolean,projected_columns:number|null}[]}|null>(null)
let request=0
async function refresh() {
  const generation=++request
  const {data,error}=await useMyFetch(root+'/coverage',{query:{search:search.value,status:filter.value,inventory_only:inventoryOnly.value,offset:offset.value,limit:pageSize}})
  if(generation !== request) return
  if(error.value) { coverage.value=null; message.value=t('fabricCompany.failed'); return }
  coverage.value=data.value as typeof coverage.value
}
watch([search,filter,inventoryOnly],()=>{ offset.value=0; refresh() })
watch(offset,refresh)
watch([draft,selected],()=>{samples.value=[null,null];review.value=false},{deep:true})
for(const index of [0,1]) watch(()=>readers.value[index],()=>{samples.value[index]=null})
onMounted(async()=>{
  if(!hasFeature('rls')) return
  const {data,error}=await useMyFetch(root+'/draft')
  if(error.value) {loadFailed.value=true;message.value=t('fabricCompany.failed');return}
  if(data.value) {Object.assign(draft,data.value);selected.value=draft.tables[0]?.table || '';savedText.value=JSON.stringify(draft)}
  await refresh()
})
function selectTable(name:string){selected.value=name}
function addRule(){ if(selected.value && !rule.value) draft.tables.push({table:selected.value,company_column:'company_id',source:null,row_attribute:null,columns:null}) }
function setColumns(value:string) {
  if(!rule.value) return
  columnError.value=false
  const result:Record<string,ColumnMatch>={}
  for(const line of value.split('\n').map(v=>v.trim()).filter(Boolean)){
    const [column,source,values,...extra]=line.split('|').map(v=>v.trim())
    if(extra.length || (!!source !== !!values) || Object.hasOwn(result,column)){columnError.value=true;message.value=t('fabricCompany.failed');return}
    result[column]=source ? {source,values:values.split(',').map(v=>v.trim())} : null
  }
  rule.value.columns=result
}
function applyBulk(){
  const lines=bulkText.value.split('\n').map(v=>v.trim()).filter(Boolean)
  const entries=lines.map(line=>line.split(',').map(v=>v.trim()))
  if(entries.some(v=>v.length!==1 && v.length!==3)){message.value=t('fabricCompany.failed');return}
  for(const [table,key,source] of entries){
    if(!draft.inventory.includes(table)) draft.inventory.push(table)
    if(key && source && !draft.tables.some(r=>r.table===table)) draft.tables.push({table,company_column:key,source,row_attribute:null,columns:null})
  }
  bulkText.value=''
}
async function save(){
  busy.value=true
  try {
    const {data,error}=await useMyFetch(root+'/draft',{method:'PUT',body:draft})
    if(error.value || !data.value) throw Error()
    Object.assign(draft,data.value);savedText.value=JSON.stringify(draft);message.value=t('fabricCompany.saved');await refresh()
  } catch {message.value=t('fabricCompany.failed')} finally {busy.value=false}
}
async function activate(){
  busy.value=true
  try {
    const {error}=await useMyFetch(root+'/activate',{method:'POST',body:draft})
    if(error.value) throw Error()
    review.value=false;message.value=t('liveAccess.active');await refresh()
  } catch {message.value=t('fabricCompany.failed')} finally {busy.value=false}
}
async function preview(i:number){
  const table=selected.value, email=readers.value[i], version=savedText.value
  busy.value=true;samples.value[i]=null
  try {
    const {data,error}=await useMyFetch(root+'/draft/preview',{query:{table,member_email:email || undefined}})
    if(error.value || !data.value) throw Error()
    if(table===selected.value && email===readers.value[i] && saved.value && version===savedText.value) samples.value[i]=data.value as Sample
  } catch {message.value=t('fabricCompany.previewFailed')} finally {busy.value=false}
}
</script>
<style scoped>
.field { @apply mt-1 block w-full rounded border border-gray-300 bg-transparent px-2 py-1.5 dark:border-gray-700; }
th, td { @apply p-1.5 text-start; }
</style>
