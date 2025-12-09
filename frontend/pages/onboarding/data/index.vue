<template>
  <div class="min-h-screen bg-gray-50 flex items-center justify-center py-6 px-4">
    <div class="w-full max-w-6xl">
      <OnboardingView forcedStepKey="data_source_created" :hideNextButton="true">
        <template #data>
          <div>
            <div v-if="!selectedDataSource">
              <div class="mt-3 grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-5 gap-3">
                <button
                  v-for="ds in available_ds"
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
            </div>

            <div v-else class="bg-white rounded-lg border border-gray-200 p-4">
              <div class="flex items-center gap-2 mb-3">
                <button type="button" @click="backToList" class="text-gray-500 hover:text-gray-700">
                  <Icon name="heroicons:chevron-left" class="w-5 h-5" />
                </button>
                <DataSourceIcon :type="selectedDataSource.type" class="h-5" />
                <span class="text-sm text-gray-800">{{ selectedDataSource.title || selectedDataSource.type }}</span>
              </div>

              <ConnectForm
                mode="create"
                :initialType="selectedDataSource.type"
                :allowNameEdit="true"
                :showLLMToggle="true"
                :showRequireUserAuthToggle="false"
                :forceShowSystemCredentials="true"
                :showTestButton="true"
                :hideHeader="true"
                @success="onCreateSuccess"
              />
            </div>
          </div>
        </template>
      </OnboardingView>
      <div class="text-center mt-4">
        <button @click="skipForNow" class="text-gray-500 hover:text-gray-700 text-sm">Skip onboarding</button>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
definePageMeta({ auth: true, layout: 'onboarding' })
import OnboardingView from '@/components/onboarding/OnboardingView.vue'
import ConnectForm from '@/components/datasources/ConnectForm.vue'
import Spinner from '~/components/Spinner.vue'

const { updateOnboarding } = useOnboarding()
const router = useRouter()
async function skipForNow() { await updateOnboarding({ dismissed: true }); router.push('/') }

const available_ds = ref<any[]>([])
const demo_ds = ref<any[]>([])
const selectedDataSource = ref<any | null>(null)
const installingDemo = ref<string | null>(null)

const uninstalledDemos = computed(() => (demo_ds.value || []).filter((demo: any) => !demo.installed))

async function getAvailableDataSources() {
  const { data, error } = await useMyFetch('/available_data_sources', { method: 'GET' })
  if (error.value) {
    throw new Error('Could not fetch available data sources')
  }
  available_ds.value = (data.value as any[]) || []
}

async function getDemoDataSources() {
  const { data } = await useMyFetch('/data_sources/demos', { method: 'GET' })
  if (data.value) {
    demo_ds.value = data.value as any[]
  }
}

async function installDemo(demoId: string) {
  installingDemo.value = demoId
  try {
    const { data } = await useMyFetch(`/data_sources/demos/${demoId}`, { method: 'POST' })
    const result = data.value as any
    if (result?.success && result.data_source_id) {
      updateOnboarding({ current_step: 'schema_selected' as any })
      navigateTo(`/onboarding/data/${result.data_source_id}/schema`)
    }
  } finally {
    installingDemo.value = null
  }
}

onMounted(async () => {
  nextTick(async () => {
    getAvailableDataSources()
    getDemoDataSources()
  })
})

function selectDataSource(ds: any) {
  selectedDataSource.value = ds
}

function backToList() {
  selectedDataSource.value = null
}

function onCreateSuccess(ds: any) {
  const dsId = ds?.id
  updateOnboarding({ current_step: 'schema_selected' as any })
  navigateTo(dsId ? `/onboarding/data/${dsId}/schema` : '/onboarding/data/schema')
}

</script>


