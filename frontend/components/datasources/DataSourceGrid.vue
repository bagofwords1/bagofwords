<template>
  <div>
    <!-- Loading state -->
    <div v-if="loading" class="flex items-center justify-center py-12">
      <Spinner class="h-4 w-4 text-gray-400" />
    </div>

    <div v-else>
      <!-- Header (optional) -->
      <div v-if="showHeader" class="text-center mb-6">
        <h2 class="text-lg font-semibold text-gray-900">{{ title }}</h2>
        <p v-if="subtitle" class="mt-2 text-gray-500 text-sm">{{ subtitle }}</p>
      </div>

      <!-- Grid of available data sources -->
      <div class="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-5 gap-3">
        <button
          v-for="ds in dataSources"
          :key="ds.type"
          type="button"
          :disabled="isLocked(ds)"
          @click="!isLocked(ds) && $emit('select', ds)"
          :class="[
            'group rounded-lg p-3 bg-white border transition-all w-full',
            isLocked(ds)
              ? 'opacity-60 cursor-not-allowed border-gray-200'
              : 'hover:bg-gray-50 border-gray-100 hover:border-gray-200'
          ]"
        >
          <div class="flex flex-col items-center text-center">
            <div class="p-1 relative">
              <DataSourceIcon class="h-6" :type="ds.type" />
              <!-- Lock icon overlay for enterprise -->
              <div v-if="isLocked(ds)" class="absolute -top-1 -right-1">
                <svg class="h-3 w-3 text-gray-400" fill="currentColor" viewBox="0 0 20 20">
                  <path fill-rule="evenodd" d="M5 9V7a5 5 0 0110 0v2a2 2 0 012 2v5a2 2 0 01-2 2H5a2 2 0 01-2-2v-5a2 2 0 012-2zm8-2v2H7V7a3 3 0 016 0z" clip-rule="evenodd" />
                </svg>
              </div>
            </div>
            <div class="text-xs text-gray-500 mt-1">
              {{ ds.title }}
            </div>
            <!-- Enterprise badge -->
            <div v-if="isLocked(ds)" class="mt-1">
              <span class="text-[9px] font-medium uppercase tracking-wide text-purple-600 bg-purple-100 px-1.5 py-0.5 rounded">
                Enterprise
              </span>
            </div>
          </div>
        </button>
      </div>

      <!-- Sample databases -->
      <div v-if="showDemos && uninstalledDemos.length > 0" class="mt-6">
        <div class="text-xs text-gray-400 mb-2">Or try a sample database:</div>
        <div class="flex flex-wrap gap-2">
          <button
            v-for="demo in uninstalledDemos"
            :key="`demo-${demo.id}`"
            @click="handleInstallDemo(demo.id)"
            :disabled="installingDemo === demo.id"
            class="inline-flex items-center gap-2 px-3 py-1.5 text-xs text-gray-600 rounded-full border border-gray-200 bg-white hover:bg-gray-50 hover:border-gray-300 transition-colors disabled:opacity-50 disabled:cursor-not-allowed"
          >
            <Spinner v-if="installingDemo === demo.id" class="h-3 w-3" />
            <DataSourceIcon v-else class="h-4" :type="demo.type" />
            {{ demo.name }}
            <span class="text-[9px] font-medium uppercase tracking-wide text-purple-600 bg-purple-100 px-1.5 py-0.5 rounded">sample</span>
          </button>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import Spinner from '~/components/Spinner.vue'
import { useEnterprise } from '~/ee/composables/useEnterprise'

const props = withDefaults(defineProps<{
  title?: string
  subtitle?: string
  showHeader?: boolean
  showDemos?: boolean
  navigateOnDemo?: boolean
}>(), {
  title: 'Add Connection',
  subtitle: 'Connect a new data source',
  showHeader: false,
  showDemos: true,
  navigateOnDemo: true
})

const emit = defineEmits<{
  (e: 'select', ds: any): void
  (e: 'demo-installed', result: any): void
}>()

const { isLicensed } = useEnterprise()

const dataSources = ref<any[]>([])
const demos = ref<any[]>([])
const loading = ref(true)
const installingDemo = ref<string | null>(null)

const uninstalledDemos = computed(() => (demos.value || []).filter((demo: any) => !demo.installed))

// Check if data source requires enterprise license and user is not licensed
const isLocked = (ds: any) => ds.requires_license === 'enterprise' && !isLicensed.value

async function fetchDataSources() {
  loading.value = true
  try {
    const [availableRes, demosRes] = await Promise.all([
      useMyFetch('/available_data_sources', { method: 'GET' }),
      useMyFetch('/data_sources/demos', { method: 'GET' })
    ])

    if (availableRes.data.value) {
      dataSources.value = availableRes.data.value as any[]
    }
    if (demosRes.data.value) {
      demos.value = demosRes.data.value as any[]
    }
  } finally {
    loading.value = false
  }
}

async function handleInstallDemo(demoId: string) {
  installingDemo.value = demoId
  try {
    const response = await useMyFetch(`/data_sources/demos/${demoId}`, { method: 'POST' })
    const result = response.data.value as any
    if (result?.success) {
      emit('demo-installed', result)
      if (props.navigateOnDemo && result.data_source_id) {
        navigateTo(`/data/new/${result.data_source_id}/schema`)
      }
    }
  } finally {
    installingDemo.value = null
  }
}

onMounted(() => {
  fetchDataSources()
})
</script>
