<template>
  <div class="flex pl-2 md:pl-4 text-sm mx-auto md:w-1/2 md:pt-10">
    <div class="w-full px-4 pl-0 py-4">
      <div>
        <h1 class="text-lg font-semibold text-center">Add Connection</h1>
        <p class="mt-4 text-gray-500 text-center">Connect a new data source</p>
      </div>

      <!-- Type Selection Grid -->
      <div v-if="!selectedDataSource" class="mt-6">
        <DataSourceGrid 
          @select="selectDataSource"
          :navigate-on-demo="true"
        />

        <!-- Back to integrations -->
        <div class="mt-6 text-center">
          <NuxtLink to="/data" class="text-sm text-gray-500 hover:text-gray-700">
            ← Back to Data
          </NuxtLink>
        </div>
      </div>

      <!-- Connect Form (shown after type selection) -->
      <div v-else>
        <WizardSteps class="mt-4" current="connect" />

        <div class="mt-6 bg-white rounded-lg border border-gray-200 p-4">
          <div class="flex items-center gap-2 mb-4">
            <button type="button" @click="backToGrid" class="text-gray-500 hover:text-gray-700">
              <UIcon name="heroicons-chevron-left" class="w-5 h-5" />
            </button>
            <DataSourceIcon :type="selectedDataSource.type" class="h-5" />
            <span class="text-sm font-medium text-gray-800">{{ selectedDataSource.title }}</span>
          </div>

          <ConnectForm 
            @success="handleSuccess" 
            :initialType="selectedDataSource.type"
            :forceShowSystemCredentials="true" 
            :showRequireUserAuthToggle="true" 
            :initialRequireUserAuth="false" 
            :showTestButton="true" 
            :showLLMToggle="true" 
            :allowNameEdit="true" 
            :hideHeader="true"
            mode="create" 
          />
        </div>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
definePageMeta({ auth: true })
import ConnectForm from '@/components/datasources/ConnectForm.vue'
import WizardSteps from '@/components/datasources/WizardSteps.vue'
import DataSourceGrid from '@/components/datasources/DataSourceGrid.vue'

const route = useRoute()
const selectedDataSource = ref<any | null>(null)

function selectDataSource(ds: any) {
  selectedDataSource.value = ds
}

function backToGrid() {
  selectedDataSource.value = null
}

function handleSuccess(ds: any) {
  const id = ds?.id
  if (id) {
    navigateTo(`/data/new/${id}/schema`)
  } else {
    navigateTo('/data')
  }
}

onMounted(async () => {
  // Check if type was passed via query param (for backward compatibility)
  const typeParam = route.query.type as string
  if (typeParam) {
    // Fetch available data sources to find the matching type
    const response = await useMyFetch('/available_data_sources', { method: 'GET' })
    if (response.data.value) {
      const availableDs = response.data.value as any[]
      const matchingDs = availableDs.find((ds: any) => ds.type === typeParam)
      if (matchingDs) {
        selectedDataSource.value = matchingDs
      }
    }
  }
})
</script>
