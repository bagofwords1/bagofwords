<template>
  <div class="max-w-2xl mx-auto space-y-4">
    <!-- Title -->
    <div>
      <label class="text-xs font-medium text-gray-700 mb-1 block">Title</label>
      <input 
        v-model="localForm.title" 
        type="text" 
        placeholder="Revenue by month" 
        class="border border-gray-300 rounded-lg px-3 py-1.5 w-full text-sm focus:outline-none focus:border-blue-500" 
      />
    </div>

    <!-- Description -->
    <div>
      <label class="text-xs font-medium text-gray-700 mb-1 block">Description</label>
      <textarea 
        v-model="localForm.description" 
        rows="4" 
        placeholder="Description" 
        class="border border-gray-300 rounded-lg px-3 py-1.5 w-full text-sm focus:outline-none focus:border-blue-500 min-h-[100px]" 
      />
    </div>

    <!-- Data Sources -->
    <div>
      <label class="text-xs font-medium text-gray-700 mb-1 block">Data Sources</label>
      <USelectMenu 
        v-model="selectedDataSourceIds" 
        :options="dataSourceOptions" 
        option-attribute="name"
        value-attribute="id"
        size="xs"
        multiple
        class="w-full text-xs shadow-none"
      >
        <template #label>
          <div class="flex items-center flex-wrap gap-1">
            <span v-if="selectedDataSourceIds.length === 0" class="text-gray-500">Select data sources</span>
            <div v-else class="flex items-center flex-wrap gap-1">
              <span v-for="ds in selectedDataSourceObjects" :key="ds.id" class="flex items-center bg-blue-100 text-blue-800 text-[10px] px-1.5 py-0.5 rounded">
                <DataSourceIcon :type="ds.type" class="h-3 mr-1" />
                {{ ds.name }}
              </span>
            </div>
          </div>
        </template>
        <template #option="{ option }">
          <div class="flex items-center justify-between w-full py-1 pr-2">
            <div class="flex items-center">
              <DataSourceIcon :type="option.type" class="h-3 mr-2" />
              <span class="text-xs">{{ option.name }}</span>
            </div>
            <UCheckbox 
              :model-value="selectedDataSourceIds.includes(String(option.id))" 
              @update:model-value="toggleDataSource(String(option.id))" 
              @click.stop 
              class="flex-shrink-0 ml-2" 
            />
          </div>
        </template>
      </USelectMenu>
    </div>

    <!-- Status -->
    <div v-if="showStatus">
      <label class="text-xs font-medium text-gray-600 mb-1.5 block">Status</label>
      <USelectMenu 
        size="xs" 
        v-model="localForm.status" 
        :options="statusOptions" 
        option-attribute="label" 
        value-attribute="value" 
        class="w-full text-xs"
      >
        <template #label>
          <div class="inline-flex items-center text-xs">
            <span :class="getStatusClass(localForm.status)" class="inline-flex px-2 py-0.5 text-[11px] font-medium rounded-full">
              {{ getCurrentStatusDisplayText() }}
            </span>
          </div>
        </template>
        <template #option="{ option }">
          <div class="flex items-center gap-2 text-xs">
            <span :class="getStatusClass(option.value)" class="inline-flex px-2 py-1 !text-xs font-medium rounded-full">
              {{ option.label }}
            </span>
          </div>
        </template>
      </USelectMenu>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, ref, watch, onMounted } from 'vue'
import DataSourceIcon from '~/components/DataSourceIcon.vue'
import { useMyFetch } from '~/composables/useMyFetch'

interface DataSource {
  id: string
  name: string
  type: string
}

interface EntityFormData {
  type: string
  title: string
  description: string | null
  status: string
  data_source_ids?: string[]
  global_status?: string | null
  entity_id?: string  // For checking if editing suggested entity
}

const props = withDefaults(defineProps<{
  modelValue: EntityFormData
  showStatus?: boolean
}>(), {
  showStatus: true
})

const emit = defineEmits<{
  (e: 'update:modelValue', value: EntityFormData): void
}>()

const availableDataSources = ref<DataSource[]>([])
const selectedDataSourceIds = ref<string[]>([])

const localForm = computed({
  get: () => props.modelValue,
  set: (value) => emit('update:modelValue', value)
})

const dataSourceOptions = computed(() => availableDataSources.value)

const selectedDataSourceObjects = computed(() => {
  return availableDataSources.value.filter(ds => selectedDataSourceIds.value.includes(ds.id))
})

// Make status options dynamic based on entity state
const statusOptions = computed(() => {
  const isEditingSuggested = props.modelValue.global_status === 'suggested'
  
  if (isEditingSuggested) {
    // For suggested entities being reviewed by admin
    return [
      { label: 'Draft - Pending Approval', value: 'draft' },
      { label: 'Published - Approve', value: 'published' },
      { label: 'Archived - Reject', value: 'archived' }
    ]
  } else {
    // For regular entities
    return [
      { label: 'Draft', value: 'draft' },
      { label: 'Published', value: 'published' },
      { label: 'Archived', value: 'archived' }
    ]
  }
})

const getCurrentStatusDisplayText = () => {
  const currentStatus = localForm.value.status
  const isEditingSuggested = props.modelValue.global_status === 'suggested'
  
  if (isEditingSuggested) {
    const suggestedStatusMap = {
      draft: 'Draft - Pending Approval',
      published: 'Published - Approve',
      archived: 'Archived - Reject'
    }
    return suggestedStatusMap[currentStatus as keyof typeof suggestedStatusMap] || currentStatus
  } else {
    return formatStatus(currentStatus)
  }
}

const formatStatus = (status: string) => {
  const statusMap = {
    draft: 'Draft',
    published: 'Published',
    archived: 'Archived'
  }
  return statusMap[status as keyof typeof statusMap] || status
}

const getStatusClass = (status: string) => {
  const statusClasses = {
    draft: 'bg-yellow-100 text-yellow-800',
    published: 'bg-green-100 text-green-800',
    archived: 'bg-gray-100 text-gray-800'
  }
  return statusClasses[status as keyof typeof statusClasses] || 'bg-gray-100 text-gray-800'
}

const toggleDataSource = (dataSourceId: string) => {
  if (selectedDataSourceIds.value.includes(dataSourceId)) {
    selectedDataSourceIds.value = selectedDataSourceIds.value.filter(id => id !== dataSourceId)
  } else {
    selectedDataSourceIds.value.push(dataSourceId)
  }
}

const fetchDataSources = async () => {
  try {
    const { data, error } = await useMyFetch<DataSource[]>('/data_sources/active', {
      method: 'GET'
    })
    
    if (error.value) {
      console.error('Failed to fetch data sources:', error.value)
    } else if (data.value) {
      availableDataSources.value = data.value
    }
  } catch (err) {
    console.error('Error fetching data sources:', err)
  }
}

// Watch for changes in selected data sources and update the parent form
watch(selectedDataSourceIds, (newIds) => {
  emit('update:modelValue', {
    ...props.modelValue,
    data_source_ids: newIds
  })
}, { deep: true })

// Initialize selected data sources from modelValue
watch(() => props.modelValue.data_source_ids, (newIds) => {
  if (newIds && JSON.stringify(newIds) !== JSON.stringify(selectedDataSourceIds.value)) {
    selectedDataSourceIds.value = [...newIds]
  }
}, { immediate: true })

onMounted(() => {
  fetchDataSources()
  // Initialize from existing data
  if (props.modelValue.data_source_ids) {
    selectedDataSourceIds.value = [...props.modelValue.data_source_ids]
  }
})
</script>

