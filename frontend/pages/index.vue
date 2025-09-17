<template>
  <div class="flex flex-col min-h-screen bg-white relative">
    <div v-if="hasLoadedModels && models.filter(model => model.is_enabled).length === 0">
      <div
        @click="router.push('/settings/models')"
      class="text-center cursor-pointer text-gray-500 text-sm bg-blue-500 text-white p-2 flex items-center justify-center">
        <UIcon name="i-heroicons-cube-transparent" class="h-5 mr-2 bg-yellow-500" />
        <span>Connect your LLM to get started</span>
      </div>
    </div>
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
            <img src="/assets/logo.png" alt="Bag of words" class="h-7" />
        </div>
        <div class="flex items-center gap-4 ml-auto">
            <div class="hamburger md:hidden">
                <UDropdown :items="menuItems" :popper="{ placement: 'bottom-start' }">
                    <UButton color="white" label="" trailing-icon="i-heroicons-bars-3" />
                </UDropdown>
            </div>
        </div>
    </div>

    <div class="flex flex-col p-4 flex-grow md:w-2/3 text-center md:mx-auto mt-20">
      <div v-if="showSetupComplete" class="mb-4">
        <div class="mx-auto max-w-lg bg-green-50 border border-green-200 text-green-800 text-sm rounded-lg px-3 py-2 flex items-center justify-center">
          <span class="mr-2">✅</span>
          <span>Setup complete — you can now start asking questions in natural language.</span>
        </div>
      </div>
      <img :src="orgIconUrl || '/assets/logo-128.png'" alt="Bag of words" class="w-10 mx-auto" />
      <h1 class="text-5xl mt-5 font-bold">
        {{ orgAIAnalystName || 'AI Analyst' }}
      </h1>
      <div class="w-full mx-auto mt-2 space-x-3 space-y-3 bg-red-100">
      </div>
      <p class="text-lg mt-5 text-gray-500">
          Create reports, dashboards, and simply get the data you need
      </p>
      <div class="w-full md:w-4/5 mx-auto mt-10 rounded-lg relative z-10">
          <PromptBoxV2 
              :textareaContent="textareaContent"
              @update:modelValue="handlePromptUpdate"
          />
      </div>
      <div class="w-full mx-auto mt-5 space-x-3 space-y-3" v-if="selectedDataSources">
        <DataSourceQuestionsHome 
            :data_sources="selectedDataSources" 
            @update-content="updateTextarea" 
        />
      </div>


    </div>

    <!-- Existing content -->
    <div class="flex flex-col p-4 flex-grow md:w-1/3 md:mx-auto relative z-10">
     
      <div class="flex cursor-pointer flex-col text-sm w-full text-left mt-4 p-2 bg-white rounded-md border border-gray-200 hover:shadow-md hover:border-blue-300"
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
        @click="router.push('/integrations')" 
        class="flex hidden cursor-pointer flex-col text-sm w-full text-left mt-4 p-2 bg-white rounded-md border border-gray-200 hover:shadow-md hover:border-blue-300">
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

import { useCan } from '~/composables/usePermissions'
const router = useRouter()
const previous_reports = ref<any[]>([])
const selectedDataSources = ref<any[]>([])
const models = ref<any[]>([])
const isLoading = ref(true)
const hasLoadedModels = ref(false)

const getModels = async () => {
  try {
    const response = await useMyFetch('/llm/models', {
        method: 'GET',
    });

    if (response.error.value) {
        throw new Error(`Could not fetch models: ${response.error.value}`);
    }

    models.value = (response.data.value as any[]) || [];
  } catch (error) {
    console.error('Failed to fetch models:', error);
    models.value = [];
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

function handleMentionUpdate(value: string) {
  textContent.value = value
}

function handleMentionsUpdated(mentions: any) {
  console.log('Mentions updated:', mentions)
}

const menuItems = ref([
  [{ label: 'Reports', icon: 'i-heroicons-document-chart-bar', to: '/reports' }],
  [{ label: 'Memory', icon: 'i-heroicons-cube', to: '/memory' }],
  [{ label: 'Integrations', icon: 'i-heroicons-circle-stack', to: '/integrations' }],
  [{ label: (currentUser.value as any)?.name, icon: 'i-heroicons-user'},
  { label: organization.value.name, icon: 'i-heroicons-building-office'  }
  ],
  [{ label: 'Logout', icon: 'i-heroicons-arrow-right-on-rectangle', click: 
  () => {
    signOff()
  } }],
])

const { isExcel } = useExcel()

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
    
    // Only proceed with API calls if organization is available
    if (organization.value?.id) {
      await Promise.allSettled([
        withTimeout(getModels(), 6000, 'getModels'),
        withTimeout(getDataSourceOptions(), 6000, 'getDataSourceOptions'),
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

const checkExcelStatus = () => {
console.log('Manually checking Excel status:', isExcel.value)
// You can add more logic here if needed
}

const getReports = async () => {
  try {
    const response = await useMyFetch('/reports', {
        method: 'GET',
    });

    if (response.error.value) {
        throw new Error(`Could not fetch reports: ${response.error.value}`);
    }

    previous_reports.value = (response.data.value as any[]) || [];
  } catch (error) {
    console.error('Failed to fetch reports:', error);
    previous_reports.value = [];
  }
}

const getDataSourceOptions = async () => {
  try {
    const response = await useMyFetch('/data_sources', {
        method: 'GET',
    });

    if (response.error.value) {
        throw new Error(`Could not fetch data sources: ${response.error.value}`);
    }

    const dataSources = (response.data.value as any[]) || [];
    selectedDataSources.value = dataSources.filter((data_source: any) => data_source.is_active !== false);
  } catch (error) {
    console.error('Failed to fetch data sources:', error);
    selectedDataSources.value = [];
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
    background-image: linear-gradient(45deg, #BE93C5, #7BC6CC, #DBE6F6);
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
