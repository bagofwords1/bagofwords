<template>
  <UModal :model-value="true" :ui="{ width: 'sm:max-w-4xl' }" @update:model-value="$emit('close')">
    <section class="p-4 sm:p-5 space-y-4 rounded-lg bg-white dark:bg-gray-900 max-h-[85vh] overflow-y-auto" aria-labelledby="artifact-data-title">
      <header class="flex items-center justify-between gap-4">
        <div><h2 id="artifact-data-title" class="text-base font-semibold">{{ t(view === 'analytics' ? 'artifactResources.analytics' : 'artifactResources.inspect') }}</h2>
          <p class="text-xs text-gray-400 mt-0.5">{{ t(view === 'resources' ? 'artifactResources.readOnlyLabel' : 'artifactResources.readOnly') }}</p></div>
        <UButton icon="i-heroicons-x-mark" color="gray" variant="ghost" :aria-label="t('common.close')" @click="$emit('close')" />
      </header>
      <p v-if="error" role="alert" class="text-sm text-red-600">{{ error }}</p>
      <div v-if="view === 'resources'" class="space-y-3">
        <div class="flex flex-wrap items-center gap-2">
          <USelectMenu v-model="collection" :options="collections" option-attribute="name" value-attribute="name" searchable size="xs"
            icon="i-heroicons-circle-stack" class="w-44 max-w-full" :disabled="!collections.length" :placeholder="t('artifactResources.collection')"
            :aria-label="t('artifactResources.collection')" :popper="{ placement: 'bottom-start' }" />
          <USelectMenu v-model="order" :options="orderOptions" option-attribute="label" value-attribute="value" size="xs"
            class="w-36" :aria-label="t('artifactResources.sort')" :disabled="!collection" :popper="{ placement: 'bottom-start' }" />
          <UButton v-if="indexed.length" size="xs" color="gray" :variant="filterOpen || appliedField ? 'soft' : 'ghost'"
            icon="i-heroicons-funnel" :aria-expanded="filterOpen" @click="filterOpen = !filterOpen">{{ t('artifactResources.filter') }}</UButton>
          <div class="ms-auto flex items-center gap-1">
            <UButton v-if="schema" size="xs" color="gray" :variant="schemaOpen ? 'soft' : 'ghost'" icon="i-heroicons-code-bracket"
              :aria-label="t('artifactResources.schema')" :title="t('artifactResources.schema')" :aria-expanded="schemaOpen" @click="schemaOpen = !schemaOpen" />
            <UButton size="xs" color="gray" variant="ghost" icon="i-heroicons-arrow-path" :loading="loading" :disabled="!collection"
              :aria-label="t('artifactResources.refresh')" :title="t('artifactResources.refresh')" @click="loadRows()" />
          </div>
        </div>
        <form v-if="filterOpen && indexed.length" class="flex flex-wrap items-center gap-2 rounded-lg bg-gray-50 dark:bg-gray-800/50 p-2" @submit.prevent="applyFilter">
          <USelectMenu v-model="filterField" :options="indexed" size="xs" class="w-36" :placeholder="t('artifactResources.filterField')"
            :aria-label="t('artifactResources.filterField')" :popper="{ placement: 'bottom-start' }" />
          <span class="text-xs text-gray-400">{{ t('artifactResources.filterValue') }}</span>
          <USelectMenu v-if="filterOptions.length" v-model="filterValue" :options="filterOptions" option-attribute="label" value-attribute="value"
            size="xs" class="flex-1 min-w-32" :aria-label="t('artifactResources.filterValue')" :popper="{ placement: 'bottom-start' }" />
          <UInput v-else v-model="filterValue" size="xs" class="flex-1 min-w-32" :type="filterType === 'number' ? 'number' : 'text'" step="any"
            :disabled="!filterField" :aria-label="t('artifactResources.filterValue')" />
          <UButton type="submit" size="xs" color="gray" variant="solid" :disabled="!canApplyFilter">{{ t('artifactResources.applyFilter') }}</UButton>
        </form>
        <div v-if="appliedField" class="flex items-center gap-1 text-xs text-gray-600 dark:text-gray-300">
          <span class="truncate">{{ appliedField }} = {{ display(appliedValue) }}</span>
          <UButton size="2xs" color="gray" variant="ghost" icon="i-heroicons-x-mark" :aria-label="t('artifactResources.clearFilter')" @click="clearFilter" />
        </div>
        <div v-if="schemaOpen && schema" class="rounded-lg border border-gray-200 dark:border-gray-700 px-3 py-2">
          <h3 class="text-xs font-medium text-gray-500 mb-2">{{ t('artifactResources.schema') }}</h3>
          <dl class="grid grid-cols-2 sm:grid-cols-3 gap-x-4 gap-y-1.5 text-xs"><div v-for="(field, name) in schema.fields" :key="name" class="flex justify-between gap-2 min-w-0"><dt class="font-mono truncate">{{ name }}</dt><dd class="text-gray-400">{{ field.type }}</dd></div></dl>
        </div>
        <div class="overflow-auto max-h-80 border border-gray-200 dark:border-gray-700 rounded-lg" :aria-busy="loading">
          <table class="w-full text-xs text-start">
            <thead class="sticky top-0 bg-gray-50 dark:bg-gray-800"><tr><th v-for="field in columns" :key="field" class="px-3 py-2 text-start font-medium text-gray-500 whitespace-nowrap">{{ field }}</th></tr></thead>
            <tbody><tr v-for="row in rows" :key="row.id" class="border-t border-gray-100 dark:border-gray-800 hover:bg-gray-50 dark:hover:bg-gray-800">
              <td v-for="field in columns" :key="field" class="px-3 py-2 max-w-56 truncate"><button dir="auto" class="text-start w-full truncate" @click="selected = row">{{ display(row.data[field]) }}</button></td>
            </tr><tr v-if="!rows.length"><td :colspan="Math.max(1, columns.length)" class="py-8 px-3 text-center text-gray-400">{{ loading ? t('common.loading') : t('artifactResources.empty') }}</td></tr></tbody>
          </table>
        </div>
        <div class="flex items-center justify-between gap-2"><span class="text-[11px] text-gray-400">{{ t('artifactResources.pageRows', { count: rows.length }) }}</span><UButton v-if="nextCursor" size="xs" color="gray" variant="ghost" trailing-icon="i-heroicons-arrow-small-right" :loading="loading" @click="loadRows(nextCursor)">{{ t('artifactResources.next') }}</UButton></div>
        <div v-if="selected" class="rounded-lg bg-gray-50 dark:bg-gray-800 p-3"><div class="flex items-center justify-between"><h3 class="text-xs font-medium">{{ t('artifactResources.details') }}</h3><UButton size="2xs" color="gray" variant="ghost" icon="i-heroicons-x-mark" :aria-label="t('common.close')" @click="selected = null" /></div><pre class="text-xs whitespace-pre-wrap break-words mt-2 max-h-48 overflow-auto">{{ JSON.stringify(selected.data, null, 2) }}</pre></div>
      </div>
      <div v-else class="space-y-5">
        <USelectMenu v-model="days" :options="ranges" option-attribute="label" value-attribute="value" class="w-44" :aria-label="t('artifactResources.analytics')" :popper="{ placement: 'bottom-start' }" />
        <div class="grid grid-cols-2 gap-4"><div class="border rounded-lg p-4"><p class="text-sm text-gray-500">{{ t('artifactResources.views') }}</p><p class="text-3xl font-semibold mt-2">{{ analytics?.views ?? '—' }}</p></div><div class="border rounded-lg p-4"><p class="text-sm text-gray-500">{{ t('artifactResources.viewers') }}</p><p class="text-3xl font-semibold mt-2">{{ analytics?.authenticatedViewers ?? '—' }}</p></div></div>
        <p class="text-xs text-gray-500">{{ t('artifactResources.anonymousNote') }}</p>
        <div v-if="analytics?.daily?.length" class="space-y-2"><div v-for="day in analytics.daily" :key="day.date" class="flex items-center gap-3 text-xs"><span class="w-24">{{ day.date }}</span><div class="bg-blue-500 rounded h-4" :style="{width: `${Math.max(1, day.views / maxViews * 65)}%`}"/><span>{{ day.views }}</span></div></div>
        <p v-else class="text-sm text-gray-500">{{ t('artifactResources.empty') }}</p>
        <dl v-if="analytics" class="flex gap-6 text-sm"><div v-for="surface in ['embedded','standalone']" :key="surface"><dt class="text-gray-500">{{ t(`artifactResources.${surface}`) }}</dt><dd>{{ analytics.surfaces[surface] || 0 }}</dd></div></dl>
      </div>
    </section>
  </UModal>
</template>
<script setup lang="ts">
const props = withDefaults(defineProps<{artifactId: string; view?: 'resources' | 'analytics'}>(), {view: 'resources'})
defineEmits(['close'])
const {t} = useI18n()
const {token} = useAuth()
const {getErrorMessage} = useErrorMessage()
const collection = ref(''), days = ref(30), filterField = ref(''), filterValue = ref('')
const filterOpen = ref(false), schemaOpen = ref(false)
const appliedField = ref(''), appliedValue = ref<any>(null)
const collections = ref<any[]>([]), rows = ref<any[]>([]), nextCursor = ref<string | null>(null), selected = ref<any>(null)
const order = ref('-created_at')
const orderOptions = computed(()=>[{value:'-created_at',label:t('artifactResources.newest')},{value:'created_at',label:t('artifactResources.oldest')}])
const error = ref(''), loading = ref(false), analytics = ref<any>(null)
const ranges = computed(()=>[7,30,90].map(value=>({value,label:t('artifactResources.days',{count:value})})))
const schema = computed(()=>collections.value.find(c=>c.name===collection.value))
const columns = computed(()=>Object.keys(schema.value?.fields || {}))
const indexed = computed(()=>columns.value.filter(f=>schema.value.fields[f].indexed))
const filterType = computed(() => schema.value?.fields[filterField.value]?.type)
const filterOptions = computed(() => {
  const field = schema.value?.fields[filterField.value]
  if (field?.type === 'boolean') return ['true', 'false'].map(value => ({ value, label: t(`artifactResources.${value}`) }))
  return (field?.enum || []).map((value: string) => ({ value, label: value }))
})
const canApplyFilter = computed(() => !!filterField.value && filterValue.value !== '' && (filterType.value !== 'number' || Number.isFinite(Number(filterValue.value))))
function applyFilter() {
  if (!canApplyFilter.value) return
  appliedField.value = filterField.value
  appliedValue.value = filterType.value === 'number' ? Number(filterValue.value) : filterType.value === 'boolean' ? filterValue.value === 'true' : filterValue.value
  loadRows()
}
function clearFilter() {
  appliedField.value = ''; appliedValue.value = null; filterValue.value = ''
  loadRows()
}
const maxViews = computed(()=>Math.max(1,...(analytics.value?.daily || []).map((d:any)=>d.views)))
const display = (v:any) => v == null ? '—' : typeof v === 'object' ? JSON.stringify(v) : String(v)
let generation = 0, analyticsGeneration = 0
async function api(path:string, options:any = {}) { return await $fetch(`/api/artifacts/${encodeURIComponent(props.artifactId)}/runtime${path}`,{...options,headers:{Authorization:token.value || ''}}) }
async function loadRows(cursor:string | null = null) {
  const request = ++generation; error.value='';loading.value=true; selected.value=null
  try { if(!collection.value){rows.value=[];nextCursor.value=null;return}
    const filter = appliedField.value ? {[appliedField.value]: appliedValue.value} : {}
    const result:any=await api(`/collections/${encodeURIComponent(collection.value)}/records`,{method:'POST',body:{action:'list',limit:20,cursor,filter,order_by:order.value}})
    if(request===generation){rows.value=result.items;nextCursor.value=result.nextCursor}
  } catch(e:any){if(request===generation){rows.value=[];nextCursor.value=null;error.value=getErrorMessage(e, t('artifactResources.failed'))}}
  finally {if(request===generation)loading.value=false}
}
async function loadAnalytics(){const request=++analyticsGeneration;error.value='';analytics.value=null;try{const result=await api(`/analytics?days=${days.value}`);if(request===analyticsGeneration)analytics.value=result}catch(e:any){if(request===analyticsGeneration){analytics.value=null;error.value=getErrorMessage(e, t('artifactResources.failed'))}}}
watch(order,()=>loadRows())
watch(filterField,()=>{filterValue.value=''})
watch(collection,()=>{filterField.value='';filterValue.value='';appliedField.value='';appliedValue.value=null;filterOpen.value=false;schemaOpen.value=false;nextCursor.value=null;rows.value=[];loadRows()})
watch(days,()=>{if(props.view==='analytics')loadAnalytics()})
onMounted(async()=>{if(props.view==='analytics'){await loadAnalytics();return}try{const result:any=await api('/resources');collections.value=result.items.filter((r:any)=>r.kind==='collection');collection.value=collections.value[0]?.name || ''}catch(e:any){error.value=getErrorMessage(e, t('artifactResources.failed'))}})
onUnmounted(()=>{generation++;analyticsGeneration++})
</script>
