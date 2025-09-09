<template>
    <NuxtLayout name="default">
        <div class="flex pl-2 md:pl-4 text-sm">
            <div class="w-full md:w-3/4 px-4 pl-0 py-4">
                <div>
                    <h1 class="text-lg font-semibold">
                        Settings
                    </h1>
                    
                    <!-- Tabs navigation -->
                    <div class="border-b border-gray-200 mt-6">
                        <nav class="-mb-px flex space-x-8">
                            <NuxtLink
                                v-for="tab in visibleTabs"
                                :key="tab.name"
                                :to="`/settings/${tab.name}`"
                                :class="[
                                    route.path === `/settings/${tab.name}`
                                        ? 'border-blue-500 text-blue-600'
                                        : 'border-transparent text-gray-500 hover:border-gray-300 hover:text-gray-700',
                                    'whitespace-nowrap border-b-2 py-4 px-1 text-sm font-medium'
                                ]"
                            >
                                {{ tab.label }}
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

// All available tabs with their required permissions
const allTabs = [
    { name: 'members', label: 'Members', requiredPermission: "view_organization_members" },
    { name: 'models', label: 'LLM', requiredPermission: "view_llm_settings" },
    { name: 'ai_settings', label: 'AI Settings', requiredPermission: "view_settings" },
    { name: 'general', label: 'General', requiredPermission: "view_settings" },
    { name: "integrations", label: "Integrations", requiredPermission: "manage_organization_external_platforms" },
]

// Filter tabs based on user permissions
const visibleTabs = computed(() => {
    return allTabs.filter(tab => {
        return useCan(tab.requiredPermission)
    })
})
</script> 