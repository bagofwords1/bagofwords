<template>
  <div class="flex flex-col min-h-screen bg-white relative">
    <div v-if="models.filter(model => model.is_enabled).length === 0">
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
      <h1 class="text-5xl mt-10 font-bold">
        Get the data you need
      </h1>
      <div class="w-full mx-auto mt-5 space-x-3 space-y-3 bg-red-100">
      </div>
      <p class="text-lg mt-5 text-gray-500">
          Create reports, dashboards, and simply get the data you need
      </p>
      <div class="w-full md:w-2/3 mx-auto mt-10 rounded-lg border border-gray-200 relative z-10">
          <PromptBox 
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
        class="flex cursor-pointer flex-col text-sm w-full text-left mt-4 p-2 bg-white rounded-md border border-gray-200 hover:shadow-md hover:border-blue-300">
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
import PromptBox from '~/components/PromptBox.vue';

import { useCan } from '~/composables/usePermissions'
const router = useRouter()
const previous_reports = ref([])
const selectedDataSources = ref([])
const models = ref([])

const getModels = async () => {
  const response = await useMyFetch('/llm/models', {
      method: 'GET',
  });

  if (!response.code === 200) {
      throw new Error('Could not fetch models');
  }

    models.value = await response.data.value;
}

const { signIn, signOut, data: currentUser, status, lastRefreshedAt, getSession } = useAuth()
const { organization, clearOrganization, ensureOrganization } = useOrganization()

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
  [{ label: currentUser.value?.name, icon: 'i-heroicons-user'},
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

onMounted(async () => {
ensureOrganization()
nextTick(async () => {
  await getModels()
  await getDataSourceOptions()
  await getReports()
})
})

const checkExcelStatus = () => {
console.log('Manually checking Excel status:', isExcel.value)
// You can add more logic here if needed
}

const getReports = async () => {
  const response = await useMyFetch('/reports', {
      method: 'GET',
  });

  if (!response.code === 200) {
      throw new Error('Could not fetch reports');
  }

  previous_reports.value = await response.data.value;
}

const getDataSourceOptions = async () => {
  const response = await useMyFetch('/data_sources', {
      method: 'GET',
  });

  if (!response.code === 200) {
      throw new Error('Could not fetch data sources');
  }

  selectedDataSources.value = (await response.data.value).filter((data_source: any) => data_source.is_active !== false);
}

const subscription = computed(() => currentUser.value?.organizations?.find(org => org.id === organization.value.id)?.subscription)


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
