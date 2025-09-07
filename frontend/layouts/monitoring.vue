<template>
    <NuxtLayout name="default">
        <div class="flex justify-center pl-2 md:pl-4 text-sm">
            <div class="w-full max-w-7xl px-4 pl-0 py-2">
                <div>
                    <h1 class="text-lg font-semibold">
                        Monitoring
                    </h1>
                    
                    <!-- Tabs navigation -->
                    <div class="border-b border-gray-200 mt-6">
                        <nav class=" flex space-x-8">
                            <NuxtLink
                                v-for="tab in visibleTabs"
                                :key="tab.name"
                                :to="`/monitoring/${tab.name}`"
                                :class="[
                                    isTabActive(tab.name)
                                        ? 'border-blue-500 text-blue-600'
                                        : 'border-transparent text-gray-500 hover:border-gray-300 hover:text-gray-700',
                                    'whitespace-nowrap border-b-2 py-2 px-2 text-sm font-medium flex items-center space-x-2'
                                ]"
                            >
                                <Icon :name="tab.icon" class="w-4 mr-1" />
                                <span>{{ tab.label }}</span>
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

// All available tabs with their required permissions
const allTabs = [
    { name: '', label: 'Explore', icon: 'i-heroicons-chart-bar', requiredPermission: "view_console" },
    { name: 'diagnosis', label: 'Diagnosis', icon: 'i-heroicons-wrench', requiredPermission: "view_console" }
]

// Filter tabs based on user permissions
const visibleTabs = computed(() => {
    return allTabs.filter(tab => {
        return useCan(tab.requiredPermission)
    })
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