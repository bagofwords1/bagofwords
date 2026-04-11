<template>
    <NuxtLayout name="default">
        <div class="flex justify-center pl-2 md:pl-4 text-sm">
            <div class="w-full max-w-7xl px-4 pl-0 py-4">
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
    { name: 'members', label: 'Members', requiredPermission: "view_members" },
    { name: 'models', label: 'LLM', requiredPermission: "manage_llm" },
    { name: 'ai_settings', label: 'AI Settings', requiredPermission: "manage_settings" },
    { name: 'general', label: 'General', requiredPermission: "manage_settings" },
    { name: "integrations", label: "Integrations", requiredPermission: "manage_settings" },
    { name: 'audit', label: 'Audit Logs', requiredPermission: "view_audit_logs" },
    { name: 'identity-provider', label: 'Identity Provider', requiredPermission: "manage_identity_providers" },
    { name: 'license', label: 'License', requiredPermission: "manage_settings" },
]

// Filter tabs based on user permissions
const visibleTabs = computed(() => {
    return allTabs.filter(tab => {
        return useCan(tab.requiredPermission)
    })
})
</script> 