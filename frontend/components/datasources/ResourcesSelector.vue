<template>
  <div class="w-full" v-if="totalResources > 0">
    <div v-if="showHeader" class="mb-2">
      <h1 class="text-lg font-semibold">{{ headerTitle }}</h1>
      <p class="text-gray-500 text-sm">{{ headerSubtitle }}</p>
    </div>

    <div>
      <input v-model="resourceSearch" type="text" placeholder="Search resources..." class="border border-gray-300 rounded-lg px-3 py-2 w-full h-9 text-sm focus:outline-none focus:border-blue-500" />
      <div class="mt-1 text-xs text-gray-500 text-right">{{ filteredResources.length }} of {{ totalResources }} shown</div>
    </div>

    <div v-if="loading" class="text-sm text-gray-500 py-10 flex items-center justify-center">
      <Spinner class="w-4 h-4 mr-2" />
      Loading resources...
    </div>

    <div v-else class="flex-1 flex flex-col h-full">
      <div v-if="filteredResources.length === 0" class="text-sm text-gray-500">No resources found.</div>
      <div v-else class="flex-1 flex flex-col min-h-full">
        <div class="flex-1 overflow-y-auto min-h-0 border border-gray-100 rounded" :style="{ maxHeight }">
          <ul class="divide-y divide-gray-100">
            <li v-for="res in filteredResources" :key="res.id" class="py-2 px-3">
              <div class="flex items-center">
                <UCheckbox v-if="canUpdate" v-model="res.is_active" class="mr-2" />
                <div class="font-semibold text-gray-600 cursor-pointer flex items-center" @click="toggleResource(res)">
                  <UIcon :name="expandedResources[res.id] ? 'heroicons-chevron-down' : 'heroicons-chevron-right'" class="w-4 h-4 mr-1" />
                  <UIcon v-if="res.resource_type === 'model' || res.resource_type === 'model_config'" name="heroicons:cube" class="w-4 h-4 text-gray-500 mr-1" />
                  <UIcon v-else-if="res.resource_type === 'metric'" name="heroicons:hashtag" class="w-4 h-4 text-gray-500 mr-1" />
                  <span class="text-sm">{{ res.name }}</span>
                </div>
              </div>
              <div v-if="expandedResources[res.id]" class="ml-6 mt-2">
                <ResourceDisplay :resource="res" />
              </div>
            </li>
          </ul>
        </div>
      </div>
    </div>

    <div v-if="showSave && canUpdate" class="mt-3 flex justify-end">
      <button @click="onSave" :disabled="saving" class="bg-blue-500 hover:bg-blue-600 text-white text-xs font-medium py-1.5 px-3 rounded disabled:opacity-50">
        <span v-if="saving">Saving...</span>
        <span v-else>{{ saveLabel }}</span>
      </button>
    </div>
  </div>
</template>

<script setup lang="ts">
import Spinner from '@/components/Spinner.vue'
import ResourceDisplay from '~/components/ResourceDisplay.vue'

type MetadataResource = Record<string, any> & {
  id: string
  name: string
  resource_type: string
  is_active?: boolean
}

const props = withDefaults(defineProps<{ 
  dsId: string
  canUpdate?: boolean
  showRefresh?: boolean
  refreshIconOnly?: boolean
  showSave?: boolean
  saveLabel?: string
  maxHeight?: string
  showHeader?: boolean
  headerTitle?: string
  headerSubtitle?: string
}>(), { canUpdate: true, showRefresh: true, refreshIconOnly: false, showSave: true, saveLabel: 'Save resources', maxHeight: '50vh', showHeader: false, headerTitle: 'Resources', headerSubtitle: 'Toggle which resources to enable' })

const emit = defineEmits<{ (e: 'saved', resources: MetadataResource[]): void; (e: 'error', err: any): void }>()

const loading = ref(false)
const saving = ref(false)
const resources = ref<MetadataResource[]>([])
const resourceSearch = ref('')
const expandedResources = ref<Record<string, boolean>>({})

const totalResources = computed(() => (resources.value || []).length)
const filteredResources = computed(() => {
  const q = resourceSearch.value.trim().toLowerCase()
  const list = resources.value || []
  if (!q) return list
  return list.filter((r: any) => String(r.name || '').toLowerCase().includes(q))
})

function toggleResource(res: MetadataResource) {
  expandedResources.value[res.id] = !expandedResources.value[res.id]
}

async function fetchResources() {
  if (!props.dsId) return
  loading.value = true
  try {
    const response = await useMyFetch(`/data_sources/${props.dsId}/metadata_resources`, { method: 'GET' })
    const payload: any = (response.data as any)?.value || { resources: [] }
    const list = (payload.resources || []) as any[]
    resources.value = list.map((r) => ({ resource_type: '', ...r })) as MetadataResource[]
  } catch (e) {
    emit('error', e)
  } finally {
    loading.value = false
  }
}

async function onSave() {
  if (saving.value) return
  saving.value = true
  try {
    const res = await useMyFetch(`/data_sources/${props.dsId}/update_metadata_resources`, { method: 'PUT', body: resources.value })
    if ((res as any)?.status?.value === 'success') {
      const updated = (((res as any).data?.value) || resources.value) as MetadataResource[]
      resources.value = updated
      emit('saved', updated)
    }
  } catch (e) {
    emit('error', e)
  } finally {
    saving.value = false
  }
}

defineExpose({ refresh: fetchResources })

watch(() => props.dsId, () => { if (props.dsId) fetchResources() }, { immediate: true })
</script>

<style scoped>
</style>


