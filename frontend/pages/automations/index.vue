<template>
  <div class="py-6">
    <div class="max-w-3xl mx-auto px-4">
      <div class="mb-4">
        <h1 class="text-lg font-semibold text-gray-900 dark:text-white">
          <GoBackChevron v-if="isExcel" />
          {{ $t('automations.title') }}
        </h1>
        <!-- Tabs -->
        <div class="mt-3 flex items-center gap-1 border-b border-gray-200 dark:border-gray-800">
          <button
            v-for="tab in tabs"
            :key="tab.key"
            @click="setTab(tab.key)"
            class="inline-flex items-center gap-1.5 px-3 py-2 text-xs font-medium border-b-2 -mb-px transition-colors"
            :class="activeTab === tab.key
              ? 'border-blue-500 text-blue-600 dark:text-blue-400'
              : 'border-transparent text-gray-500 dark:text-gray-400 hover:text-gray-700 dark:hover:text-gray-300'"
            :data-testid="`automations-tab-${tab.key}`"
          >
            <UIcon :name="tab.icon" class="w-3.5 h-3.5" />
            {{ $t(tab.label) }}
          </button>
        </div>
      </div>

      <AutomationsScheduledTab v-if="activeTab === 'scheduled'" />
      <AutomationsTriggersTab v-else-if="activeTab === 'triggers'" />
    </div>
  </div>
</template>

<script setup lang="ts">
import GoBackChevron from '@/components/excel/GoBackChevron.vue'
import AutomationsScheduledTab from '~/components/automations/ScheduledTab.vue'
import AutomationsTriggersTab from '~/components/automations/TriggersTab.vue'

definePageMeta({ auth: true })

const { isExcel } = useExcel()
const route = useRoute()
const router = useRouter()

const tabs = [
  { key: 'scheduled', label: 'automations.scheduledTab', icon: 'heroicons-clock' },
  { key: 'triggers', label: 'automations.triggersTab', icon: 'heroicons-bolt' },
]

const activeTab = ref<string>((route.query.tab as string) === 'triggers' ? 'triggers' : 'scheduled')

function setTab(key: string) {
  activeTab.value = key
  router.replace({ query: { ...route.query, tab: key } })
}

watch(() => route.query.tab, (tab) => {
  if (tab === 'triggers' || tab === 'scheduled') activeTab.value = tab as string
})
</script>
