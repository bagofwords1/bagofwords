<template>
  <div v-if="!isLoading && hasAnyReports" class="mt-12">
    <div class="flex items-center justify-between mb-4">
      <USelectMenu
        v-model="viewMode"
        :options="availableOptions"
        value-attribute="value"
        option-attribute="label"
        size="md"
        :ui="{
          trigger: 'ring-0 shadow-none bg-transparent hover:bg-gray-50 font-medium text-gray-900',
          width: 'w-72'
        }"
      >
        <template #default>
          <span class="text-base font-medium text-gray-900">{{ selectedLabel }}</span>
          <UIcon name="i-heroicons-chevron-down-20-solid" class="w-5 h-5 text-gray-400 ml-1" />
        </template>
      </USelectMenu>
      <NuxtLink to="/reports" class="text-sm text-blue-600 hover:text-blue-800 hover:underline">
        View All Reports
      </NuxtLink>
    </div>

    <div class="grid grid-cols-2 md:grid-cols-3 lg:grid-cols-4 gap-4">
      <RecentReportCard
        v-for="report in displayedReports"
        :key="report.id"
        :report="report"
        :view-mode="viewMode"
      />
    </div>
  </div>

  <!-- Loading state -->
  <div v-else-if="isLoading" class="mt-12">
    <div class="flex items-center gap-2 mb-4">
      <div class="h-5 w-32 bg-gray-200 rounded animate-pulse"></div>
    </div>
    <div class="grid grid-cols-2 md:grid-cols-3 lg:grid-cols-4 gap-4">
      <div
        v-for="i in 4"
        :key="i"
        class="bg-gray-100 rounded-xl overflow-hidden"
      >
        <div class="aspect-[4/3] bg-gray-200 animate-pulse"></div>
        <div class="p-3 space-y-2">
          <div class="h-4 bg-gray-200 rounded animate-pulse w-3/4"></div>
          <div class="h-3 bg-gray-200 rounded animate-pulse w-1/2"></div>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import RecentReportCard from './RecentReportCard.vue'

interface RecentReport {
  id: string
  title?: string
  slug: string
  status: string
  user: { id: string; name?: string; email?: string }
  artifact_modes: string[]
  conversation_share_enabled: boolean
  conversation_share_token?: string
  created_at: string
}

const { data: currentUser } = useAuth()
const { organization } = useOrganization()

const orgReports = ref<RecentReport[]>([])
const myReports = ref<RecentReport[]>([])
const isLoading = ref(true)
const viewMode = ref('org')

const orgName = computed(() => organization.value?.name || 'Organization')

const hasAnyReports = computed(() => {
  return orgReports.value.length > 0 || myReports.value.length > 0
})

// Build available options based on what's available
const availableOptions = computed(() => {
  const options = []
  if (orgReports.value.length > 0) {
    options.push({ label: `${orgName.value} Reports`, value: 'org' })
  }
  if (myReports.value.length > 0) {
    options.push({ label: 'My Reports', value: 'my' })
  }
  return options
})

const selectedLabel = computed(() => {
  if (viewMode.value === 'org') return `${orgName.value} Reports`
  return 'My Reports'
})

const displayedReports = computed(() => {
  const list = viewMode.value === 'org' ? orgReports.value : myReports.value
  return list.slice(0, 8)
})

// Auto-select valid mode when data changes
watch([orgReports, myReports], () => {
  if (viewMode.value === 'org' && orgReports.value.length === 0 && myReports.value.length > 0) {
    viewMode.value = 'my'
  } else if (viewMode.value === 'my' && myReports.value.length === 0 && orgReports.value.length > 0) {
    viewMode.value = 'org'
  }
})

const fetchReports = async () => {
  try {
    // Fetch org (published) reports and my reports in parallel
    const [orgResponse, myResponse] = await Promise.all([
      useMyFetch('/reports', {
        method: 'GET',
        query: { filter: 'published', limit: 8 }
      }),
      useMyFetch('/reports', {
        method: 'GET',
        query: { filter: 'my', limit: 8 }
      })
    ])

    if (!orgResponse.error.value && orgResponse.data.value) {
      orgReports.value = (orgResponse.data.value as any).reports || []
    }
    if (!myResponse.error.value && myResponse.data.value) {
      myReports.value = (myResponse.data.value as any).reports || []
    }
  } catch (e) {
    console.error('Failed to fetch recent reports:', e)
    orgReports.value = []
    myReports.value = []
  } finally {
    isLoading.value = false
  }
}

onMounted(() => {
  fetchReports()
})
</script>
