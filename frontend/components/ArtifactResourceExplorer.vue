<template>
  <UModal :model-value="true" :ui="{ width: 'sm:max-w-5xl' }" @update:model-value="$emit('close')">
    <section class="p-6 space-y-5 rounded-lg bg-white dark:bg-gray-900" aria-labelledby="artifact-data-title">
      <header class="flex items-center justify-between gap-4">
        <div><h2 id="artifact-data-title" class="text-lg font-semibold">{{ t('artifactResources.inspect') }}</h2>
          <p class="text-sm text-gray-500">{{ t('artifactResources.readOnly') }}</p></div>
        <UButton icon="i-heroicons-x-mark" color="gray" variant="ghost" :aria-label="t('common.close')" @click="$emit('close')" />
      </header>
      <div class="flex gap-2 border-b pb-3">
        <UButton v-for="item in tabs" :key="item.id" :variant="tab === item.id ? 'solid' : 'ghost'" color="gray" @click="tab = item.id">{{ item.label }}</UButton>
      </div>
      <p v-if="error" role="alert" class="text-sm text-red-600">{{ error }}</p>
      <div v-if="tab === 'data'" class="space-y-4">
        <div class="flex flex-wrap items-center gap-3">
          <USelect v-model="collection" :options="collections" option-attribute="name" value-attribute="name" :placeholder="t('artifactResources.collection')" />
          <USelect v-model="order" :options="orderOptions" option-attribute="label" value-attribute="value" />
          <UButton variant="ghost" icon="i-heroicons-arrow-path" :loading="loading" @click="loadRows()">{{ t('artifactResources.refresh') }}</UButton>
        </div>
        <details v-if="schema"><summary class="text-sm cursor-pointer">{{ t('artifactResources.schema') }}</summary>
          <dl class="grid grid-cols-2 gap-2 mt-3 text-sm"><template v-for="(field, name) in schema.fields" :key="name"><dt class="font-mono">{{ name }}</dt><dd>{{ field.type }}</dd></template></dl>
        </details>
        <form v-if="indexed.length" class="flex flex-wrap gap-2" @submit.prevent="loadRows()">
          <USelect v-model="filterField" :options="indexed" :placeholder="t('artifactResources.filterField')" />
          <UInput v-model="filterValue" :placeholder="t('artifactResources.filterValue')" />
          <UButton type="submit" variant="soft">{{ t('artifactResources.filter') }}</UButton>
        </form>
        <div class="overflow-auto max-h-96 border rounded-lg">
          <table class="w-full text-sm text-start"><thead class="bg-gray-50 dark:bg-gray-800"><tr><th v-for="field in columns" :key="field" class="p-3 text-start font-medium">{{ field }}</th></tr></thead>
            <tbody><tr v-for="row in rows" :key="row.id" class="border-t hover:bg-gray-50 dark:hover:bg-gray-800"><td v-for="field in columns" :key="field" class="p-3 max-w-64 truncate"><button dir="auto" class="text-start w-full truncate" @click="selected = row">{{ display(row.data[field]) }}</button></td></tr></tbody>
          </table>
        </div>
        <p v-if="!loading && !rows.length" class="text-sm text-gray-500">{{ t('artifactResources.empty') }}</p>
        <div class="flex justify-between"><span class="text-xs text-gray-500">{{ t('artifactResources.pageRows', { count: rows.length }) }}</span><UButton v-if="nextCursor" variant="soft" :loading="loading" @click="loadRows(nextCursor)">{{ t('artifactResources.next') }}</UButton></div>
        <div v-if="selected" class="rounded-lg bg-gray-50 dark:bg-gray-800 p-4"><div class="flex justify-between"><h3 class="text-sm font-medium">{{ t('artifactResources.details') }}</h3><UButton size="xs" variant="ghost" @click="selected = null">{{ t('common.close') }}</UButton></div><pre class="text-xs whitespace-pre-wrap break-words mt-2 max-h-48 overflow-auto">{{ JSON.stringify(selected.data, null, 2) }}</pre></div>
      </div>
      <div v-else class="space-y-5">
        <USelect v-model="days" :options="ranges" option-attribute="label" value-attribute="value" />
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
const props = defineProps<{artifactId: string}>()
defineEmits(['close'])
const {t} = useI18n()
const {token} = useAuth()
const {getErrorMessage} = useErrorMessage()
const tab = ref('data'), collection = ref(''), days = ref(30), filterField = ref(''), filterValue = ref('')
const collections = ref<any[]>([]), rows = ref<any[]>([]), nextCursor = ref<string | null>(null), selected = ref<any>(null)
const order = ref('-created_at')
const orderOptions = computed(()=>[{value:'-created_at',label:t('artifactResources.newest')},{value:'created_at',label:t('artifactResources.oldest')}])
const error = ref(''), loading = ref(false), analytics = ref<any>(null)
const tabs = computed(()=>[{id:'data',label:t('artifactResources.data')},{id:'analytics',label:t('artifactResources.analytics')}])
const ranges = computed(()=>[7,30,90].map(value=>({value,label:t('artifactResources.days',{count:value})})))
const schema = computed(()=>collections.value.find(c=>c.name===collection.value))
const columns = computed(()=>Object.keys(schema.value?.fields || {}))
const indexed = computed(()=>columns.value.filter(f=>schema.value.fields[f].indexed))
const maxViews = computed(()=>Math.max(1,...(analytics.value?.daily || []).map((d:any)=>d.views)))
const display = (v:any) => v == null ? '—' : typeof v === 'object' ? JSON.stringify(v) : String(v)
let generation = 0, analyticsGeneration = 0
async function api(path:string, options:any = {}) { return await $fetch(`/api/artifacts/${encodeURIComponent(props.artifactId)}/runtime${path}`,{...options,headers:{Authorization:token.value || ''}}) }
async function loadRows(cursor:string | null = null) {
  const request = ++generation; error.value='';loading.value=true; selected.value=null
  try { if(!collection.value){rows.value=[];return}
    const filter:any={};if(filterField.value && filterValue.value){const type=schema.value.fields[filterField.value].type;filter[filterField.value]=type==='number'?Number(filterValue.value):type==='boolean'?filterValue.value==='true':filterValue.value}
    const result:any=await api(`/collections/${encodeURIComponent(collection.value)}/records`,{method:'POST',body:{action:'list',limit:20,cursor,filter,order_by:order.value}})
    if(request===generation){rows.value=result.items;nextCursor.value=result.nextCursor}
  } catch(e:any){if(request===generation){rows.value=[];nextCursor.value=null;error.value=getErrorMessage(e, t('artifactResources.failed'))}}
  finally {if(request===generation)loading.value=false}
}
async function loadAnalytics(){const request=++analyticsGeneration;error.value='';analytics.value=null;try{const result=await api(`/analytics?days=${days.value}`);if(request===analyticsGeneration)analytics.value=result}catch(e:any){if(request===analyticsGeneration){analytics.value=null;error.value=getErrorMessage(e, t('artifactResources.failed'))}}}
watch(order,()=>loadRows())
watch(collection,()=>{filterField.value='';filterValue.value='';loadRows()})
watch([tab,days],()=>{if(tab.value==='analytics')loadAnalytics()})
onMounted(async()=>{try{const result:any=await api('/resources');collections.value=result.items.filter((r:any)=>r.kind==='collection');collection.value=collections.value[0]?.name || ''}catch(e:any){error.value=getErrorMessage(e, t('artifactResources.failed'))}})
onUnmounted(()=>{generation++;analyticsGeneration++})
</script>
