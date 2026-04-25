<template>
  <div class="min-h-screen bg-gray-50 flex items-center justify-center py-12 px-4">
    <div class="w-full max-w-6xl">
      <OnboardingView forcedStepKey="schema_selected" :hideNextButton="true">
        <template #schema>
          <div class="relative space-y-3">
            <div v-if="initialLoading" class="flex items-center justify-center py-12">
              <Spinner class="h-4 w-4 text-gray-400" />
            </div>

            <template v-else>
              <div
                v-if="indexingConnections.length > 0"
                class="rounded-lg border border-blue-100 bg-blue-50/40 p-3 space-y-3"
              >
                <div
                  v-for="conn in indexingConnections"
                  :key="conn.id"
                  class="space-y-1"
                >
                  <div class="flex items-center gap-2 text-xs text-gray-700">
                    <DataSourceIcon class="h-3.5" :type="conn.type" />
                    <span class="font-medium">{{ conn.name }}</span>
                  </div>
                  <ConnectionIndexingProgress :indexing="conn.indexing" :show-logs="false" />
                </div>
              </div>

              <TablesSelector
              :key="tablesKey"
              :dsId="dsId"
              schema="full"
              :canUpdate="true"
              :showRefresh="false"
              :showSave="!anyIndexing"
              :saveLabel="$t('onboarding.schema.save')"
              maxHeight="50vh"
              :skipRefreshOnSave="true"
              @saved="onSaved"
            />
            </template>
          </div>
        </template>
      </OnboardingView>
    </div>
  </div>
</template>

<script setup lang="ts">
definePageMeta({ auth: true, layout: 'onboarding' })
import OnboardingView from '@/components/onboarding/OnboardingView.vue'
import TablesSelector from '@/components/datasources/TablesSelector.vue'
import ConnectionIndexingProgress from '~/components/ConnectionIndexingProgress.vue'
import DataSourceIcon from '~/components/DataSourceIcon.vue'
import Spinner from '~/components/Spinner.vue'
import { isIndexingActive } from '~/composables/useConnectionStatus'

const route = useRoute()
const { updateOnboarding } = useOnboarding()
const router = useRouter()

const dsId = computed(() => String(route.params.ds_id || ''))

const dataSource = ref<any>(null)
const tablesKey = ref(0)
const initialLoading = ref(true)

const connections = computed<any[]>(() => (dataSource.value?.connections || []) as any[])
const indexingConnections = computed(() =>
  connections.value.filter((c: any) => isIndexingActive(c?.indexing))
)
const anyIndexing = computed(() => indexingConnections.value.length > 0)

async function fetchDataSource() {
  if (!dsId.value) return
  const { data } = await useMyFetch(`/data_sources/${dsId.value}`, { method: 'GET' })
  dataSource.value = data.value || dataSource.value
}

const POLL_INTERVAL_MS = 2000
let pollTimer: ReturnType<typeof setInterval> | null = null

function stopPolling() {
  if (pollTimer) {
    clearInterval(pollTimer)
    pollTimer = null
  }
}

function maybeStartPolling() {
  if (anyIndexing.value && !pollTimer) {
    pollTimer = setInterval(async () => {
      const wasIndexing = anyIndexing.value
      await fetchDataSource()
      if (!anyIndexing.value) {
        stopPolling()
        if (wasIndexing) tablesKey.value++
      }
    }, POLL_INTERVAL_MS)
  }
}

onMounted(async () => {
  try {
    await fetchDataSource()
  } finally {
    initialLoading.value = false
  }
  maybeStartPolling()
})

onBeforeUnmount(() => stopPolling())

async function onSaved() {
  const target = `/onboarding/data/${String(dsId.value)}/context`
  await updateOnboarding({ current_step: 'instructions_added' as any, dismissed: false as any })
  router.replace(target)
}

async function skipForNow() { await updateOnboarding({ dismissed: true }); router.push('/') }
</script>
