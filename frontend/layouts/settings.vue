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
                                v-for="tab in tabs"
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

let tabs = [
    { name: 'members', label: 'Members' },
    { name: 'models', label: 'Models' },
    { name: 'configuration', label: 'Configuration' },
    { name: "ai_agents", label: "AI Agents" }
]

</script> 