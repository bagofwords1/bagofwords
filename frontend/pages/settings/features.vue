<template>
    <div class="mt-6">
        <h2 class="text-lg font-medium text-gray-900">Features
            <p class="text-sm text-gray-500 font-normal">
                Customizing features available to pro plan organizations.
            </p>
        </h2>

        <!-- Features content from original file -->
        <div class="space-y-4">
            <div class="flex flex-col md:w-2/3 space-y-1">
                <div class="flex items-center justify-between">
                    <div class="font-medium">Data Modeling</div>
                    <UToggle v-model="features.dataModeling" disabled />
                </div>
                <p class="text-sm text-gray-500">Design and optimize your data structures with advanced modeling
                    capabilities.</p>
            </div>

            <div class="flex flex-col md:w-2/3 space-y-1">
                <div class="flex items-center justify-between">
                    <div class="font-medium">Coding</div>
                    <UToggle v-model="features.coding" disabled />
                </div>
                <p class="text-sm text-gray-500">Write and execute code with intelligent assistance and real-time
                    validation.</p>
            </div>

            <div class="flex flex-col md:w-2/3 space-y-1">
                <div class="flex items-center justify-between">
                    <div class="font-medium">Dashboard Design</div>
                    <UToggle v-model="features.dashboardDesign" disabled />
                </div>
                <p class="text-sm text-gray-500">Create interactive dashboards with drag-and-drop components and custom
                    layouts.</p>
            </div>

            <div class="flex flex-col md:w-2/3 space-y-1">
                <div class="flex items-center justify-between">
                    <div class="font-medium flex items-center">
                        Semantic Dataframes
                        <UTooltip text="Beta feature">
                            <Icon name="heroicons:beaker" class="ml-2 w-4 h-4" />
                        </UTooltip>
                    </div>
                    <UToggle v-model="features.semanticDataframes" disabled />
                </div>
                <p class="text-sm text-gray-500">
                    Query, search, classify and filter unstructured data.
                </p>
            </div>

            <div class="flex flex-col md:w-2/3 space-y-1">
                <div class="flex items-center justify-between">
                    <div class="font-medium flex items-center">
                        Diff
                        <UTooltip text="Diff">
                            <Icon name="heroicons:beaker" class="ml-2 w-4 h-4" />
                        </UTooltip>
                    </div>
                    <UToggle v-model="features.diff" disabled />
                </div>
                <p class="text-sm text-gray-500">
                    When making changes to data models / code, use diff logic to update the data instead of re-running
                    the entire query.
                </p>
            </div>
        </div>
    </div>
</template>

<script setup lang="ts">
definePageMeta({ auth: true, permissions: ['modify_settings'], layout: 'settings' })

const features = ref({
    dataModeling: true,
    coding: true,
    dashboardDesign: true,
    semanticDataframes: false,
    diff: false
})
</script>