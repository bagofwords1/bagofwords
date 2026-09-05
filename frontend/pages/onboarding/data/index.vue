<template>
  <div class="min-h-screen bg-gray-50 dark:bg-gray-900 flex items-center justify-center py-6 px-4">
    <div class="w-full max-w-6xl">
      <OnboardingView forcedStepKey="data_source_created" :hideNextButton="true">
        <template #data>
          <div>
            <div v-if="!selectedDataSource">
              <!-- Search across the whole catalogue; the grid below shows only
                   the popular connectors until you search or expand. -->
              <div class="mt-3 relative">
                <input
                  v-model="query"
                  type="text"
                  data-testid="onboarding-ds-search"
                  :placeholder="$t('onboarding.data.searchPlaceholder')"
                  class="w-full ps-10 pe-4 py-2 text-sm rounded-lg border border-gray-200 dark:border-gray-700 bg-white dark:bg-gray-900 text-gray-700 dark:text-gray-200 placeholder-gray-400 dark:placeholder-gray-600 focus:outline-none focus:border-gray-300 dark:focus:border-gray-600"
                />
                <UIcon name="i-heroicons-magnifying-glass" class="absolute start-3 top-2.5 h-4 w-4 text-gray-400 dark:text-gray-600" />
              </div>

              <div v-if="noResults" class="mt-6 text-center text-sm text-gray-500 dark:text-gray-400">
                {{ $t('onboarding.data.noResults', { query: query.trim() }) }}
              </div>

              <div v-else class="mt-3 grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-5 gap-3">
                <button
                  v-for="ds in visibleDataSources"
                  :key="ds.type"
                  type="button"
                  :disabled="isLocked(ds)"
                  @click="!isLocked(ds) && selectDataSource(ds)"
                  :class="[
                    'group rounded-lg p-3 bg-white dark:bg-gray-900 transition-colors w-full',
                    isLocked(ds) ? 'opacity-60 cursor-not-allowed' : 'hover:bg-gray-50 dark:hover:bg-gray-800'
                  ]"
                >
                  <div class="flex flex-col items-center text-center">
                    <div class="p-1 relative">
                      <DataSourceIcon class="h-5" :type="ds.type" />
                      <!-- Lock icon overlay for enterprise -->
                      <div v-if="isLocked(ds)" class="absolute -top-1 -end-1">
                        <svg class="h-3 w-3 text-gray-400 dark:text-gray-600" fill="currentColor" viewBox="0 0 20 20">
                          <path fill-rule="evenodd" d="M5 9V7a5 5 0 0110 0v2a2 2 0 012 2v5a2 2 0 01-2 2H5a2 2 0 01-2-2v-5a2 2 0 012-2zm8-2v2H7V7a3 3 0 016 0z" clip-rule="evenodd" />
                        </svg>
                      </div>
                    </div>
                    <div class="text-xs text-gray-500 dark:text-gray-400">
                      {{ ds.title }}
                    </div>
                    <!-- Enterprise badge -->
                    <div v-if="isLocked(ds)" class="mt-1">
                      <span class="text-[9px] font-medium uppercase tracking-wide text-purple-600 bg-purple-100 dark:bg-purple-950 px-1.5 py-0.5 rounded">
                        {{ $t('onboarding.data.enterprise') }}
                      </span>
                    </div>
                  </div>
                </button>
              </div>

              <div v-if="!isSearching && hiddenCount > 0" class="mt-4 text-center">
                <button
                  type="button"
                  data-testid="onboarding-ds-show-all"
                  @click="showAll = !showAll"
                  class="text-xs text-gray-500 dark:text-gray-400 hover:text-gray-700 dark:hover:text-gray-200"
                >
                  {{ showAll ? $t('onboarding.data.showLess') : $t('onboarding.data.showAll', { count: hiddenCount }) }}
                </button>
              </div>

              <!-- Sample databases -->
              <div v-if="!isSearching && uninstalledDemos.length > 0" class="mt-6">
                <div class="text-xs text-gray-400 dark:text-gray-600 mb-2">{{ $t('onboarding.data.orTry') }}</div>
                <div class="flex flex-wrap gap-2">
                  <button
                    v-for="demo in uninstalledDemos"
                    :key="`demo-${demo.id}`"
                    @click="installDemo(demo.id)"
                    :disabled="installingDemo === demo.id"
                    :data-testid="`onboarding-demo-${demo.id}`"
                    class="inline-flex items-center gap-2 px-3 py-1.5 text-xs text-gray-600 dark:text-gray-400 rounded-full border border-gray-200 dark:border-gray-700 bg-white dark:bg-gray-900 hover:bg-gray-50 dark:hover:bg-gray-800 hover:border-gray-300 dark:hover:border-gray-600 transition-colors disabled:opacity-50 disabled:cursor-not-allowed"
                  >
                    <Spinner v-if="installingDemo === demo.id" class="h-3" />
                    <DataSourceIcon v-else class="h-4" :type="demo.type" />
                    {{ demo.name }}
                    <span class="text-[9px] font-medium uppercase tracking-wide text-purple-600 bg-purple-100 dark:bg-purple-950 px-1.5 py-0.5 rounded">{{ $t('onboarding.data.sample') }}</span>
                  </button>
                </div>
              </div>
            </div>

            <div v-else class="bg-white dark:bg-gray-900 rounded-lg border border-gray-200 dark:border-gray-700 p-4">
              <div class="flex items-center gap-2 mb-3">
                <button type="button" @click="backToList" class="text-gray-500 dark:text-gray-400 hover:text-gray-700">
                  <Icon name="heroicons:chevron-left" class="w-5 h-5" />
                </button>
                <DataSourceIcon :type="selectedDataSource.type" class="h-5" />
                <span class="text-sm text-gray-800 dark:text-gray-200">{{ selectedDataSource.title || selectedDataSource.type }}</span>
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
        <button @click="skipForNow" class="text-gray-500 dark:text-gray-400 hover:text-gray-700 text-sm">{{ $t('onboarding.skip') }}</button>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
definePageMeta({ auth: true, layout: 'onboarding' })
import OnboardingView from '@/components/onboarding/OnboardingView.vue'
import ConnectForm from '@/components/datasources/ConnectForm.vue'
import Spinner from '~/components/Spinner.vue'
import { useEnterprise } from '~/ee/composables/useEnterprise'

const { updateOnboarding } = useOnboarding()
const router = useRouter()
const { t } = useI18n()
async function skipForNow() { await updateOnboarding({ dismissed: true }); router.push('/') }

const { isLicensed } = useEnterprise()

const available_ds = ref<any[]>([])
const {
  query,
  showAll,
  isSearching,
  visible: visibleDataSources,
  hiddenCount,
  noResults,
} = useDataSourcePicker(available_ds)
const demo_ds = ref<any[]>([])
const selectedDataSource = ref<any | null>(null)
const installingDemo = ref<string | null>(null)

const uninstalledDemos = computed(() => (demo_ds.value || []).filter((demo: any) => !demo.installed))

// Check if data source requires enterprise license and user is not licensed
const isLocked = (ds: any) => ds.requires_license === 'enterprise' && !isLicensed.value

async function getAvailableDataSources() {
  const { data, error } = await useMyFetch('/available_data_sources', { method: 'GET' })
  if (error.value) {
    throw new Error(t('onboarding.data.errorAvailable'))
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

// Deliberately keeps the search query: coming back from a connect form should
// land on the results you picked from, not on the collapsed popular grid.
function backToList() {
  selectedDataSource.value = null
}

function onCreateSuccess(ds: any) {
  const dsId = ds?.id
  updateOnboarding({ current_step: 'schema_selected' as any })
  navigateTo(dsId ? `/onboarding/data/${dsId}/schema` : '/onboarding/data/schema')
}

</script>
