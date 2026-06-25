<template>
    <NuxtLayout name="default">
        <div class="flex justify-center ps-2 md:ps-4 text-sm">
            <div class="w-full max-w-7xl px-4 ps-0 py-2">
                <div>
                    <h1 class="text-lg font-semibold">
                        {{ $t('monitoring.title') }}
                    </h1>

                    <!-- Tabs navigation -->
                    <div class="border-b border-gray-200 dark:border-gray-700 mt-6">
                        <nav class=" flex space-x-8">
                            <NuxtLink
                                v-for="tab in visibleTabs"
                                :key="tab.name"
                                :to="`/monitoring/${tab.name}`"
                                :class="[
                                    isTabActive(tab.name)
                                        ? 'border-blue-500 text-blue-600'
                                        : 'border-transparent text-gray-500 dark:text-gray-400 hover:border-gray-300 hover:text-gray-700 dark:hover:text-gray-300',
                                    'whitespace-nowrap border-b-2 py-2 px-2 text-sm font-medium flex items-center space-x-2'
                                ]"
                            >
                                <Icon :name="tab.icon" class="w-4 me-1" />
                                <span>{{ $t(tab.label) }}</span>
                            </NuxtLink>
                        </nav>
                    </div>

                    <!-- Page content -->
                    <slot />
                </div>
            </div>
        </div>
    </NuxtLayout>
</template>

<script setup lang="ts">
const route = useRoute()

// Make route path reactive
const currentPath = computed(() => route.path)

// All available tabs. Visibility mirrors the page-level gate: only admins
// (users with `manage_settings`) can see the monitoring tabs.
const allTabs = [
    { name: '', label: 'monitoring.tabExplore', icon: 'i-heroicons-chart-bar' },
    { name: 'diagnosis', label: 'monitoring.tabDiagnosis', icon: 'i-heroicons-wrench' },
    // Cost is an enterprise feature (license-gated, see `cost_dashboard`).
    { name: 'cost', label: 'monitoring.tabCost', icon: 'i-heroicons-banknotes', feature: 'cost_dashboard' },
]

const { hasFeature } = useEnterprise()

const visibleTabs = computed(() => {
    // Admin-only: the monitoring tabs all call `manage_settings`-gated
    // /console/* endpoints, so only show them to users who can actually use them.
    if (!useCan('manage_settings')) return []
    // Drop tabs whose enterprise feature isn't licensed.
    return allTabs.filter(tab => !tab.feature || hasFeature(tab.feature))
})

// Helper function to check if tab is active
const isTabActive = (tabName: string) => {
    const path = currentPath.value
    if (tabName === '') {
        // For the first tab (Explore), it's active when on /monitoring or /monitoring/
        return path === '/monitoring' || path === '/monitoring/'
    }
    return path === `/monitoring/${tabName}`
}
</script>
