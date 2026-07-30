<template>
    <NuxtLayout name="default">
        <div class="flex justify-center ps-2 md:ps-4 text-sm">
            <div class="w-full max-w-7xl px-4 ps-0 py-4">
                <div>
                    <h1 class="text-lg font-semibold">
                        {{ $t('settings.title') }}
                    </h1>
                    
                    <!-- Tabs navigation -->
                    <div class="border-b border-gray-200 dark:border-gray-700 mt-6">
                        <nav class="-mb-px flex space-x-8">
                            <NuxtLink
                                v-for="tab in visibleTabs"
                                :key="tab.name"
                                :to="`/settings/${tab.name}`"
                                :class="[
                                    route.path === `/settings/${tab.name}`
                                        ? 'border-blue-500 text-blue-600'
                                        : 'border-transparent text-gray-500 dark:text-gray-400 hover:border-gray-300 hover:text-gray-700',
                                    'whitespace-nowrap border-b-2 py-4 px-1 text-sm font-medium'
                                ]"
                            >
                                {{ $t(tab.label) }}
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
const { hasFeature } = useEnterprise()

// All available tabs with their required permissions. `requiredFeature` (when
// set) additionally gates the tab on an enterprise license feature.
const allTabs = [
    { name: 'members', label: 'settings.membersTab', requiredPermission: "view_members" },
    { name: 'models', label: 'settings.llm', requiredPermission: "manage_llm" },
    { name: 'ai_settings', label: 'settings.aiSettings', requiredPermission: "manage_settings" },
    { name: 'pii', label: 'settings.piiTab', requiredPermission: "manage_settings", requiredFeature: "pii_protection" },
    { name: 'general', label: 'settings.general', requiredPermission: "manage_settings" },
    { name: "integrations", label: "settings.integrations.title", requiredPermission: "manage_settings" },
    { name: 'audit', label: 'settings.auditLogs', requiredPermission: "view_audit_logs" },
    { name: 'identity-provider', label: 'settings.identityProviderTab', requiredPermission: "manage_identity_providers" },
    { name: 'smtp', label: 'settings.smtpTab', requiredPermission: "manage_settings" },
    { name: 'license', label: 'settings.license', requiredPermission: "manage_settings" },
]

// Filter tabs based on user permissions + enterprise feature availability
const visibleTabs = computed(() => {
    return allTabs.filter(tab => {
        if (!useCan(tab.requiredPermission)) return false
        if (tab.requiredFeature && !hasFeature(tab.requiredFeature)) return false
        return true
    })
})
</script> 