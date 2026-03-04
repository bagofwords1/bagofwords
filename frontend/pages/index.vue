<template>
  <div class="flex flex-col min-h-screen relative">

    <!-- Add background div with grid -->
    <div class="absolute inset-0 pointer-events-none" 
         style="background-image: linear-gradient(to right, rgb(15 23 42 / 0.04) 1px, transparent 1px),
                linear-gradient(to bottom, rgb(15 23 42 / 0.04) 1px, transparent 1px);
                background-size: 24px 24px;
                mask-image: linear-gradient(to bottom, transparent, black);
                -webkit-mask-image: linear-gradient(to bottom, transparent, black);">
    </div>
    <!-- Top bar -->
    <div class="flex justify-between items-center p-3">
        <div class="logo md:hidden">
            <img src="/assets/logo.svg" alt="MetricChat" class="h-7" />
        </div>
        <div class="flex items-center gap-4 ml-auto">
            <div class="hamburger md:hidden">
                <UDropdown :items="menuItems" :popper="{ placement: 'bottom-start' }">
                    <UButton color="white" label="" trailing-icon="i-heroicons-bars-3" />
                </UDropdown>
            </div>
        </div>
    </div>

    <div v-if="isLoading" class="flex flex-col items-center justify-center flex-grow py-20">
      <Spinner class="h-4 w-4 text-stone-400" />
      <p class="text-sm text-stone-500 mt-2">Loading...</p>
    </div>

    <div v-else class="flex flex-col p-4 flex-grow md:w-2/3 text-center md:mx-auto mt-14">
      <div v-if="showSetupComplete" class="mb-10">
        <div class="mx-auto max-w-xl bg-green-50 border border-green-200 text-green-800 text-sm rounded-lg px-3 py-2 flex items-center justify-center">
          <span class="mr-2 flex items-center">
            <Icon name="heroicons-check" />
          </span>
          <span class="flex items-center">Setup complete — you can now start asking questions in natural language.</span>
        </div>
      </div>
      <img :src="orgIconUrl || '/assets/logo-icon.svg'" alt="MetricChat" class="max-h-12 max-w-[180px] object-contain mx-auto" />
      <h1 class="text-5xl mt-5 font-bold">
        {{ orgAIAnalystName || 'AI Data Analyst' }}
      </h1>
      <div class="w-full mx-auto mt-2 space-x-3 space-y-3">
      </div>
      <p class="text-lg mt-5 font-light text-stone-500">
          Create reports, dashboards, and simply get the data you need
      </p>
      <div class="w-full md:w-4/5 mx-auto mt-10 rounded-lg relative z-10">
          <PromptBoxV2 
              :textareaContent="textareaContent"
              :initialSelectedDataSources="selectedDataSources"
              @update:modelValue="handlePromptUpdate"
          />
      </div>
      <!-- Quick CSV upload -->
      <div class="mt-3 flex justify-center">
        <input type="file" ref="csvFileInput" @change="handleCsvUpload" class="hidden" accept=".csv,.xlsx,.xls" />
        <button
          @click="($refs.csvFileInput as HTMLInputElement).click()"
          :disabled="uploadingCsv"
          class="inline-flex items-center gap-1.5 text-xs text-gray-400 hover:text-gray-600 transition-colors disabled:opacity-50"
        >
          <Spinner v-if="uploadingCsv" class="w-3 h-3" />
          <Icon v-else name="heroicons-arrow-up-tray" class="w-3.5 h-3.5" />
          Upload a CSV or Excel file to query
        </button>
      </div>

      <div class="w-full mx-auto mt-0 space-x-3 space-y-3" v-if="selectedDataSources">
        <DataSourceQuestionsHome
            :data_sources="selectedDataSources"
            @update-content="updateTextarea"
        />
      </div>

      <div class="w-full mx-auto mt-4">
        <RecentReports />
      </div>

    </div>

    <!-- Existing content -->
    <div v-if="!isLoading" class="flex flex-col p-4 flex-grow md:w-1/3 md:mx-auto relative z-10">
     
      <div class="flex cursor-pointer flex-col text-sm w-full text-left mt-4 p-2 bg-white rounded-md border border-stone-200 hover:shadow-md hover:border-primary-300"
        v-if="models.length === 0"
        @click="router.push('/settings/models')"
      >
        <div class="flex">
          <div class="w-4/5 pr-4">
            <p class="text-sm text-black flex ">
              <LLMProviderIcon provider="openai" class="h-3 inline-block " />
              <LLMProviderIcon provider="anthropic" class="h-2 inline-block ml-2" />
              <span class="inline-block ml-2">Connect your LLM</span>
            </p>
          </div>
          <div class="w-1/5 text-right">
            <button class="">
              <UIcon name="i-heroicons-arrow-right" />
            </button>
          </div>
        </div>
      </div>

        <div 
        @click="router.push('/data')" 
        class="flex hidden cursor-pointer flex-col text-sm w-full text-left mt-4 p-2 bg-white rounded-md border border-stone-200 hover:shadow-md hover:border-primary-300">
            <div class="flex">

                <div class="w-4/5 pr-4">
                    <p class="text-sm text-black">
                        <DataSourceIcon type="snowflake" class="h-5 inline mr-2" />
                        <DataSourceIcon type="salesforce" class="h-5 inline mr-2" />
                        <span v-if="useCan('create_data_source')">
                          Manage integrations
                      </span>
                      <span v-else>
                          View integrations
                      </span>
                    </p>
                    <!-- Existing reports list can go here -->
                </div>
                <div class="w-1/5 text-right">
                    <button class="">
                        <UIcon name="i-heroicons-arrow-right" />
                    </button>
                </div>
            </div>
        </div>


    </div>



    <div class="gradient-glow"></div>
  </div>
</template>

<script setup lang="ts">
import { useRouter } from 'vue-router';
import { useExcel } from '~/composables/useExcel';
import { onMounted, nextTick } from 'vue';
import Spinner from '@/components/Spinner.vue'
import PromptBoxV2 from '~/components/prompt/PromptBoxV2.vue';
import RecentReports from '~/components/home/RecentReports.vue';

import { useCan } from '~/composables/usePermissions'
import { KeyCode } from 'monaco-editor';
const router = useRouter()
const { onboarding, fetchOnboarding } = useOnboarding()
const { selectedDomainObjects } = useDomain()
const previous_reports = ref<any[]>([])
const models = ref<any[]>([])
const isLoading = ref(true)
const hasLoadedModels = ref(false)

// Use selected domains from DomainSelector as the data sources
const selectedDataSources = computed(() => selectedDomainObjects.value)

const getModels = async () => {
  try {
    const response = await useMyFetch('/llm/models', {
        method: 'GET',
    });

    if (response.error.value) {
        throw new Error(`Could not fetch models: ${response.error.value}`);
    }

    const modelsData = (response.data.value as any[]) || [];
    models.value = modelsData;
    return modelsData;
  } catch (error) {
    console.error('Failed to fetch models:', error);
    models.value = [];
    throw error;
  }
}

const { signIn, signOut, data: currentUser, status, lastRefreshedAt, getSession } = useAuth()
const { organization, ensureOrganization } = useOrganization()
const orgIconUrl = computed(() => {
  const orgId = organization.value?.id
  const orgs = (currentUser.value as any)?.organizations || []
  const org = orgs.find((o: any) => o.id === orgId) || orgs[0]
  return org?.icon_url || null
})

const orgAIAnalystName = computed(() => {
  const orgId = organization.value?.id
  const orgs = (currentUser.value as any)?.organizations || []
  const org = orgs.find((o: any) => o.id === orgId) || orgs[0]
  return org?.ai_analyst_name || "AI Analyst"
})

definePageMeta({ 
  layout: 'default',
  auth: true,
  permissions: ['view_reports']
})

const textContent = ref('')
const showOnboardingBanner = computed(() => {
  // If onboarding info not yet fetched, fallback to model/data heuristics below
  const steps = (onboarding.value as any)?.steps || {}
  const llmStatus = steps.llm_configured?.status
  const dataStatus = steps.data_source_created?.status
  // Show when not both done: (no llm and no data) OR (llm yes but data not done)
  if (llmStatus || dataStatus) {
    const llmDone = llmStatus === 'done'
    const dataDone = dataStatus === 'done'
    return !(llmDone && dataDone)
  }
  // Heuristic fallback: if no enabled models → prompt onboarding
  if (hasLoadedModels.value && models.value.filter(m => m.is_enabled).length === 0) return true
  return false
})


const menuItems = ref([
  [{ label: 'Reports', icon: 'i-heroicons-document-chart-bar', to: '/reports' }],
  [{ label: 'Data', icon: 'i-heroicons-circle-stack', to: '/data' }],
  [{ label: (currentUser.value as any)?.name, icon: 'i-heroicons-user'},
  { label: organization.value.name, icon: 'i-heroicons-building-office'  }
  ],
  [{ label: 'Logout', icon: 'i-heroicons-arrow-right-on-rectangle', click: 
  () => {
    signOff()
  } }],
])

const { isExcel } = useExcel()
const { initDomain, selectDomains } = useDomain()
const uploadingCsv = ref(false)
const toast = useToast()

async function findFileUploadConnection(): Promise<string | null> {
  try {
    const { data } = await useMyFetch('/connections', { method: 'GET' })
    const connections = (data.value as any[]) || []
    const fileUploadConn = connections.find(
      (c: any) => c.type === 'duckdb' && c.config?.is_file_upload === true
    )
    return fileUploadConn?.id || null
  } catch {
    return null
  }
}

async function handleCsvUpload(e: Event) {
  const input = e.target as HTMLInputElement
  const file = input.files?.[0]
  if (!file) return

  uploadingCsv.value = true
  try {
    // Check if there's an existing file-upload connection
    const existingConnId = await findFileUploadConnection()

    // 1. Upload
    const formData = new FormData()
    formData.append('file', file)
    const { data: uploadData, error: uploadError } = await useMyFetch('/files', {
      method: 'POST',
      body: formData,
    })
    if (uploadError.value || !uploadData.value) {
      toast.add({ title: 'Upload failed', description: 'Could not upload file', color: 'red' })
      return
    }

    // 2. Create data source (use existing connection if available)
    const fileId = (uploadData.value as any).id
    const url = existingConnId
      ? `/files/${fileId}/create_data_source?connection_id=${existingConnId}`
      : `/files/${fileId}/create_data_source`
    const { data: dsData, error: dsError } = await useMyFetch(url, {
      method: 'POST',
    })
    if (dsError.value || !dsData.value) {
      toast.add({ title: 'Error', description: 'Could not create data source from file', color: 'red' })
      return
    }

    const result = dsData.value as any
    const displayName = result.data_source_name || result.table_name || file.name
    toast.add({ title: 'Ready to query', description: `"${displayName}" has been added`, color: 'green' })

    // Refresh domains without full page reload
    await initDomain()
  } finally {
    uploadingCsv.value = false
    input.value = ''
  }
}

const textareaContent = ref('')

const updateTextarea = (content: string) => {
    textareaContent.value = content
}

const handlePromptUpdate = (value: string) => {
    textareaContent.value = value
}

const route = useRoute()
const showSetupComplete = computed(() => route.query.setup === 'done')

function withTimeout<T>(promise: Promise<T>, ms = 6000, label = 'request'): Promise<T | 'timeout'> {
  return new Promise((resolve) => {
    const timer = setTimeout(() => {
      console.warn(`${label} timed out after ${ms}ms`)
      resolve('timeout')
    }, ms)
    promise
      .then((v) => { clearTimeout(timer); resolve(v) })
      .catch((e) => { console.warn(`${label} failed:`, e); clearTimeout(timer); resolve('timeout') })
  })
}

onMounted(async () => {
  try {
    // Ensure organization is loaded first before making any API calls
    await withTimeout(ensureOrganization(), 6000, 'ensureOrganization')
    // Fetch onboarding state early for banner visibility
    try { await withTimeout(fetchOnboarding(), 6000, 'fetchOnboarding') } catch {}
    // If onboarding already started and not completed, redirect to correct step
    const ob = onboarding.value as any
    if (ob && !ob.completed && !ob.dismissed) {
      const step = ob.current_step
      if (step === 'llm_configured') router.replace('/onboarding/llm')
      else if (step === 'data_source_created') router.replace('/onboarding/data')
      else if (step === 'schema_selected') router.replace('/onboarding/data/schema')
      else if (step === 'instructions_added') router.replace('/onboarding/context')
      else router.replace('/onboarding')
      return
    }
    
    // Only proceed with API calls if organization is available
    // Note: domains are already loaded by the layout via initDomain()
    if (organization.value?.id) {
      await Promise.allSettled([
        withTimeout(getModels(), 6000, 'getModels'),
        withTimeout(getReports(), 6000, 'getReports')
      ])
    } else {
      console.warn('Organization not available, skipping API calls')
    }
  } catch (error) {
    console.error('Error during page initialization:', error)
  } finally {
    isLoading.value = false
    hasLoadedModels.value = true
  }
})

const getReports = async () => {
  try {
    const response = await useMyFetch('/reports', {
        method: 'GET',
    });

    if (response.error.value) {
        throw new Error(`Could not fetch reports: ${response.error.value}`);
    }

    const reportsData = (response.data.value as any[]) || [];
    previous_reports.value = reportsData;
    return reportsData;
  } catch (error) {
    console.error('Failed to fetch reports:', error);
    previous_reports.value = [];
    throw error;
  }
}


const subscription = computed(() => (currentUser.value as any)?.organizations?.find((org: any) => org.id === organization.value.id)?.subscription)


async function signOff() {
await signOut({ 
  callbackUrl: '/' 
})
}
</script>

<style scoped>
.gradient-glow {
    background-image: linear-gradient(45deg, #0C7C7C, #33A7A7, #C4841D);
    border-radius: 9999px;
    filter: blur(60px);
    height: 160px;
    left: 50%;
    pointer-events: none;
    position: absolute;
    bottom:-180px;
    transform: translate(-50%, -50%);
    transition: all 1s ease;
    width: 160px;
    z-index: 1;
    pointer-events: none;
    position: fixed;
}

@keyframes pulse {
  0% {
    transform: translate(-50%, -50%) scale(0.8);
    opacity: 0.1;
  }
  50% {
    transform: translate(-50%, -50%) scale(1);
    opacity: 0.15;
  }
  100% {
    transform: translate(-50%, -50%) scale(0.8);
    opacity: 0.1;
  }
}
</style>
