<template>
    <div class="flex pl-2 md:pl-4 text-sm">
        <div class="w-full md:w-3/4 px-4 pl-0 py-4">
            <div>
                <h1 class="text-lg font-semibold">
                    Console
                </h1>
                
                <!-- Tabs navigation -->
                <div class="border-b border-gray-200 mt-6">
                    <nav class="-mb-px flex space-x-8">
                        <NuxtLink
                            v-for="tab in availableTabs"
                            :key="tab.name"
                            :to="`/console?tab=${tab.name}`"
                            :class="[
                                currentTab === tab.name
                                    ? 'border-blue-500 text-blue-600'
                                    : 'border-transparent text-gray-500 hover:border-gray-300 hover:text-gray-700',
                                'whitespace-nowrap border-b-2 py-4 px-1 text-sm font-medium'
                            ]"
                        >
                            {{ tab.label }}
                        </NuxtLink>
                    </nav>
                </div>

                <!-- Tab Content -->
                <div class="mt-6">
                    <ConsoleOverview v-if="currentTab === 'overview'" />
                    <ConsoleInstructions v-else-if="currentTab === 'instructions'" />
                </div>
            </div>
        </div>
    </div>
</template>

<script setup lang="ts">
import ConsoleOverview from '~/components/ConsoleOverview.vue'
import ConsoleInstructions from '~/components/ConsoleInstructions.vue'

const route = useRoute()
const router = useRouter()

// Available tabs for the console
const availableTabs = [
    { name: "overview", label: "Overview" },
    { name: "instructions", label: "Instructions" }
]

// Get current tab from query parameter or default to overview
const currentTab = computed(() => {
    const tabFromQuery = route.query.tab as string
    return availableTabs.find(tab => tab.name === tabFromQuery)?.name || 'overview'
})

// Watch for route changes and update tab if needed
watch(() => route.query.tab, (newTab) => {
    if (!newTab) {
        // If no tab is specified, redirect to overview
        router.replace({ query: { tab: 'overview' } })
    }
}, { immediate: true })

const { organization } = useOrganization()

definePageMeta({ auth: true, permissions: ['view_console'], layout: 'default' })
</script>

