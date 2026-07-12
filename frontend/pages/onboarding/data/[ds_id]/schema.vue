<template>
  <div class="min-h-screen bg-gray-50 dark:bg-gray-900 flex items-center justify-center py-12 px-4">
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
                class="rounded-lg border border-blue-100 dark:border-blue-900 bg-blue-50/40 dark:bg-blue-950 p-3 space-y-3"
              >
                <div
                  v-for="conn in indexingConnections"
                  :key="conn.id"
                  class="space-y-1"
                >
                  <div class="flex items-center gap-2 text-xs text-gray-700 dark:text-gray-300">
                    <DataSourceIcon class="h-3.5" :type="conn.type" />
                    <span class="font-medium">{{ conn.name }}</span>
                  </div>
                  <ConnectionIndexingProgress :indexing="conn.indexing" :show-logs="false" />
                </div>
              </div>

              <CatalogSelector
                :key="tablesKey"
                :ds-id="dsId"
                :connections="scopeConnections"
                :registry-by-type="registryByType"
                :can-update="true"
                :show-continue="!anyIndexing"
                :continue-label="$t('onboarding.schema.save')"
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
import CatalogSelector from '@/components/datasources/CatalogSelector.vue'
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

// Connections with config (for CatalogSelector's file-scope) + registry shapes.
const scopeConnections = ref<any[]>([])
const registryByType = ref<Record<string, any>>({})

async function fetchDataSource() {
  if (!dsId.value) return
  const { data } = await useMyFetch(`/data_sources/${dsId.value}`, { method: 'GET' })
  dataSource.value = data.value || dataSource.value
}

async function fetchScopeMeta() {
  try {
    const [reg, conns] = await Promise.all([
      useMyFetch('/available_data_sources', { method: 'GET' }),
      useMyFetch(`/data_sources/${dsId.value}/connections`, { method: 'GET' }),
    ])
    for (const entry of (reg.data.value as any[]) || []) registryByType.value[entry.type] = entry
    scopeConnections.value = (conns.data.value as any[]) || []
  } catch (e) { /* non-fatal */ }
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
    await Promise.all([fetchDataSource(), fetchScopeMeta()])
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
