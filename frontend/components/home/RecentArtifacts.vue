<template>
  <div v-if="!isLoading && hasAnyArtifacts" class="mt-12">
    <div class="flex items-center gap-1 mb-4">
      <USelectMenu
        v-model="viewMode"
        :options="availableOptions"
        value-attribute="value"
        option-attribute="label"
        size="sm"
        :ui="{
          trigger: 'ring-0 shadow-none bg-transparent hover:bg-gray-50 font-medium text-gray-900',
          width: 'w-48'
        }"
      >
        <template #default>
          <span class="text-sm font-medium text-gray-900">{{ selectedLabel }}</span>
          <UIcon name="i-heroicons-chevron-down-20-solid" class="w-4 h-4 text-gray-400 ml-1" />
        </template>
      </USelectMenu>
    </div>

    <div class="grid grid-cols-2 md:grid-cols-3 lg:grid-cols-4 gap-4">
      <RecentArtifactCard
        v-for="artifact in displayedArtifacts"
        :key="artifact.id"
        :artifact="artifact"
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
import RecentArtifactCard from './RecentArtifactCard.vue'

interface RecentArtifact {
  id: string
  report_id: string
  title?: string
  mode: string
  thumbnail_path?: string
  created_at: string
  report_title?: string
  user_id?: string
  is_published?: boolean
  user_name?: string
}

const { data: currentUser } = useAuth()
const { organization } = useOrganization()

const artifacts = ref<RecentArtifact[]>([])
const isLoading = ref(true)
const viewMode = ref('org')

const orgName = computed(() => organization.value?.name || 'Organization')

// Org = published reports only
const orgArtifacts = computed(() => {
  return artifacts.value.filter(a => a.is_published)
})

// My = current user's reports (published or not)
const myArtifacts = computed(() => {
  const userId = (currentUser.value as any)?.id
  if (!userId) return []
  return artifacts.value.filter(a => a.user_id === userId)
})

const hasAnyArtifacts = computed(() => {
  return orgArtifacts.value.length > 0 || myArtifacts.value.length > 0
})

// Build available options based on what's available
const availableOptions = computed(() => {
  const options = []
  if (orgArtifacts.value.length > 0) {
    options.push({ label: `${orgName.value} Analyses`, value: 'org' })
  }
  if (myArtifacts.value.length > 0) {
    options.push({ label: 'My Analyses', value: 'my' })
  }
  return options
})

const selectedLabel = computed(() => {
  if (viewMode.value === 'org') return `${orgName.value} Analyses`
  return 'My Analyses'
})

const displayedArtifacts = computed(() => {
  const list = viewMode.value === 'org' ? orgArtifacts.value : myArtifacts.value
  return list.slice(0, 8)
})

// Auto-select valid mode when data changes
watch([orgArtifacts, myArtifacts], () => {
  if (viewMode.value === 'org' && orgArtifacts.value.length === 0 && myArtifacts.value.length > 0) {
    viewMode.value = 'my'
  } else if (viewMode.value === 'my' && myArtifacts.value.length === 0 && orgArtifacts.value.length > 0) {
    viewMode.value = 'org'
  }
})

const fetchRecentArtifacts = async () => {
  try {
    const { data, error } = await useMyFetch('/artifacts/recent', {
      method: 'GET',
      query: { limit: 16 }
    })

    if (error.value) throw error.value
    artifacts.value = (data.value as RecentArtifact[]) || []
  } catch (e) {
    console.error('Failed to fetch recent artifacts:', e)
    artifacts.value = []
  } finally {
    isLoading.value = false
  }
}

onMounted(() => {
  fetchRecentArtifacts()
})
</script>
