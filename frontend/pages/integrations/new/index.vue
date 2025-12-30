<template>
  <div class="flex pl-2 md:pl-4 text-sm mx-auto md:w-1/2 md:pt-10">
    <div class="w-full px-4 pl-0 py-4">
      <div>
        <h1 class="text-lg font-semibold text-center">Add Connection</h1>
        <p class="mt-4 text-gray-500 text-center">Connect a new data source</p>
      </div>

      <!-- Type Selection Grid -->
      <div v-if="!selectedDataSource" class="mt-6">
        <!-- Grid of available data sources (matching onboarding style) -->
        <div class="mt-3 grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-5 gap-3">
          <button
            v-for="ds in filteredDataSources"
            :key="ds.type"
            type="button"
            @click="selectDataSource(ds)"
            class="group rounded-lg p-3 bg-white hover:bg-gray-50 transition-colors w-full"
          >
            <div class="flex flex-col items-center text-center">
              <div class="p-1">
                <DataSourceIcon class="h-5" :type="ds.type" />
              </div>
              <div class="text-xs text-gray-500">
                {{ ds.title }}
              </div>
            </div>
          </button>
        </div>

        <!-- Sample databases -->
        <div v-if="uninstalledDemos.length > 0" class="mt-6">
          <div class="text-xs text-gray-400 mb-2">Or try a sample database:</div>
          <div class="flex flex-wrap gap-2">
            <button 
              v-for="demo in uninstalledDemos" 
              :key="`demo-${demo.id}`"
              @click="installDemo(demo.id)"
              :disabled="installingDemo === demo.id"
              class="inline-flex items-center gap-2 px-3 py-1.5 text-xs text-gray-600 rounded-full border border-gray-200 bg-white hover:bg-gray-50 hover:border-gray-300 transition-colors disabled:opacity-50 disabled:cursor-not-allowed"
            >
              <Spinner v-if="installingDemo === demo.id" class="h-3" />
              <DataSourceIcon v-else class="h-4" :type="demo.type" />
              {{ demo.name }}
              <span class="text-[9px] font-medium uppercase tracking-wide text-purple-600 bg-purple-100 px-1.5 py-0.5 rounded">sample</span>
            </button>
          </div>
        </div>

        <!-- Back to integrations -->
        <div class="mt-6 text-center">
          <NuxtLink to="/integrations" class="text-sm text-gray-500 hover:text-gray-700">
            ← Back to Integrations
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
import Spinner from '~/components/Spinner.vue'

const route = useRoute()
const available_ds = ref<any[]>([])
const demo_ds = ref<any[]>([])
const selectedDataSource = ref<any | null>(null)
const loadingAvailable = ref(true)
const installingDemo = ref<string | null>(null)

const filteredDataSources = computed(() => available_ds.value)

const uninstalledDemos = computed(() => (demo_ds.value || []).filter((demo: any) => !demo.installed))

async function getAvailableDataSources() {
  loadingAvailable.value = true
  try {
    const response = await useMyFetch('/available_data_sources', { method: 'GET' })
    if (response.data.value) {
      available_ds.value = response.data.value as any[]
    }
  } finally {
    loadingAvailable.value = false
  }
}

async function getDemoDataSources() {
  try {
    const response = await useMyFetch('/data_sources/demos', { method: 'GET' })
    if (response.data.value) {
      demo_ds.value = response.data.value as any[]
    }
  } catch (e) {
    // Ignore errors
  }
}

async function installDemo(demoId: string) {
  installingDemo.value = demoId
  try {
    const response = await useMyFetch(`/data_sources/demos/${demoId}`, { method: 'POST' })
    const result = response.data.value as any
    if (result?.success && result.data_source_id) {
      navigateTo(`/integrations/new/${result.data_source_id}/schema`)
    }
  } finally {
    installingDemo.value = null
  }
}

function selectDataSource(ds: any) {
  selectedDataSource.value = ds
}

function backToGrid() {
  selectedDataSource.value = null
}

function handleSuccess(ds: any) {
  const id = ds?.id
  if (id) {
    navigateTo(`/integrations/new/${id}/schema`)
  } else {
    navigateTo('/integrations')
  }
}

onMounted(async () => {
  await Promise.all([
    getAvailableDataSources(),
    getDemoDataSources(),
  ])
  
  // Check if type was passed via query param (for backward compatibility)
  const typeParam = route.query.type as string
  if (typeParam && available_ds.value.length > 0) {
    const matchingDs = available_ds.value.find((ds: any) => ds.type === typeParam)
    if (matchingDs) {
      selectedDataSource.value = matchingDs
    }
  }
})
</script>
