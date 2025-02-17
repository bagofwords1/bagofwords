<template>
    <div class="space-y-6">
        <div class="flex justify-between items-center">
            <h2 class="text-lg font-medium text-gray-900">Available Models</h2>
            <button
                @click="showAddModelModal = true"
                class="inline-flex items-center px-4 py-2 border border-transparent text-sm font-medium rounded-md shadow-sm text-white bg-blue-600 hover:bg-blue-700"
            >
                Add Custom Model
            </button>
        </div>

        <!-- Pre-configured Models -->
        <div class="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-3">
            <div
                v-for="model in preconfiguredModels"
                :key="model.id"
                class="relative rounded-lg border border-gray-200 bg-white p-6 shadow-sm hover:shadow-md transition-shadow"
            >
                <div class="flex items-center space-x-3">
                    <img :src="model.icon" class="h-8 w-8" :alt="model.name">
                    <div>
                        <h3 class="text-sm font-medium text-gray-900">{{ model.name }}</h3>
                        <p class="text-sm text-gray-500">{{ model.description }}</p>
                    </div>
                </div>
                <div class="mt-4">
                    <span
                        :class="[
                            model.enabled ? 'bg-green-100 text-green-800' : 'bg-gray-100 text-gray-800',
                            'inline-flex items-center px-2.5 py-0.5 rounded-full text-xs font-medium'
                        ]"
                    >
                        {{ model.enabled ? 'Enabled' : 'Disabled' }}
                    </span>
                </div>
            </div>
        </div>

        <!-- Custom Models -->
        <div class="mt-8">
            <h3 class="text-lg font-medium text-gray-900 mb-4">Custom Models</h3>
            <div class="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-3">
                <div
                    v-for="model in customModels"
                    :key="model.id"
                    class="relative rounded-lg border border-gray-200 bg-white p-6 shadow-sm hover:shadow-md transition-shadow"
                >
                    <div class="flex items-center justify-between">
                        <h4 class="text-sm font-medium text-gray-900">{{ model.name }}</h4>
                        <button class="text-gray-400 hover:text-gray-500">
                            <span class="sr-only">Edit</span>
                            <!-- Add your edit icon here -->
                        </button>
                    </div>
                    <p class="mt-1 text-sm text-gray-500">{{ model.baseUrl }}</p>
                    <div class="mt-4">
                        <span class="bg-blue-100 text-blue-800 inline-flex items-center px-2.5 py-0.5 rounded-full text-xs font-medium">
                            Custom
                        </span>
                    </div>
                </div>
            </div>
        </div>
    </div>
</template>

<script setup lang="ts">
import { ref } from 'vue'

const showAddModelModal = ref(false)

// Example data - replace with actual API calls
const preconfiguredModels = ref([
    {
        id: 1,
        name: 'OpenAI GPT-4',
        description: 'Latest GPT-4 model from OpenAI',
        icon: '/icons/openai.svg',
        enabled: true,
    },
    {
        id: 2,
        name: 'Google Gemini Pro',
        description: 'Advanced language model from Google',
        icon: '/icons/google.svg',
        enabled: false,
    },
    // Add more preconfigured models
])

const customModels = ref([
    {
        id: 'custom-1',
        name: 'Custom OpenAI Deployment',
        baseUrl: 'https://api.custom-deployment.com',
    },
    // Add more custom models
])
</script>