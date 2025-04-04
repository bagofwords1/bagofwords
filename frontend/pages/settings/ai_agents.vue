<template>
    <div class="mt-6">
        <h2 class="text-lg font-medium text-gray-900">AI Agents
            <p class="text-sm text-gray-500 font-normal mb-8">
                Configure AI agents and capabilities available to your organization's members.
            </p>
        </h2>

        <!-- Loading state -->
        <div v-if="loading" class="py-4">
            <ULoader />
        </div>

        <!-- Error message -->
        <UAlert v-if="error" class="mt-4" type="danger">
            {{ error }}
        </UAlert>

        <!-- AI Agents content -->
        <div v-if="!loading && !error" class="space-y-5">
            <!-- AI Features -->
            <div v-for="(feature, key) in aiFeatures" :key="key" class="flex flex-col md:w-2/3">
                <div class="flex items-center justify-between">
                    <div class="font-medium flex items-center">
                        {{ feature.name }}
                        <UTooltip v-if="feature.is_lab" text="Beta feature">
                            <Icon name="heroicons:beaker" class="ml-2 w-4 h-4" />
                        </UTooltip>
                    </div>
                    <UToggle 
                        v-model="feature.enabled" 
                        :disabled="!feature.editable"
                        @change="updateFeature(key, feature)" 
                    />
                </div>
                <p class="text-sm text-gray-500 mt-2.5">{{ feature.description }}</p>
            </div>
        </div>
    </div>
</template>

<script setup lang="ts">
definePageMeta({ auth: true, permissions: ['modify_settings'], layout: 'settings' })

interface AIFeature {
    name: string
    description: string
    enabled: boolean
    editable: boolean
    is_lab: boolean
}

const loading = ref(true)
const error = ref('')
const aiFeatures = ref<Record<string, AIFeature>>({})

const toast = useToast()

// Fetch organization settings
const fetchSettings = async () => {
    loading.value = true
    error.value = ''
    try {
        const response = await useMyFetch('/api/organization/settings')
        if (!response.status.value == 'success') throw new Error('Failed to fetch settings')
        const data = response.data.value
        
        // Extract AI features
        aiFeatures.value = data.config.ai_features
    } catch (err) {
        error.value = err.message || 'An error occurred while fetching settings'
        toast.add({
            title: 'Error',
            description: error.value,
            color: 'red',
            timeout: 5000,
            icon: 'i-heroicons-exclamation-circle'
        })
    } finally {
        loading.value = false
    }
}

// Update feature setting
const updateFeature = async (featureKey: string, feature: AIFeature) => {
    try {
        const payload = { config: {} }
        payload.config.ai_features = {
            [featureKey]: {
                enabled: aiFeatures.value[featureKey].enabled
            }
        }

        const response = await useMyFetch('/api/organization/settings', {
            method: 'PUT',
            headers: {
                'Content-Type': 'application/json'
            },
            body: JSON.stringify(payload)
        })

        if (response.status.value !== 'success') throw new Error('Failed to update settings')

        // Show success toast
        toast.add({
            title: 'Success',
            description: `${feature.name} has been ${feature.enabled ? 'enabled' : 'disabled'}`,
            color: 'green',
            timeout: 3000
        })
    } catch (err) {
        // Revert the toggle if there was an error
        aiFeatures.value[featureKey].enabled = !aiFeatures.value[featureKey].enabled
        
        // Show error toast
        error.value = err.message || 'An error occurred while updating settings'
        toast.add({
            title: 'Error',
            description: error.value,
            color: 'red',
            timeout: 5000,
            icon: 'i-heroicons-exclamation-circle'
        })
    }
}

// Fetch settings when the component is mounted
onMounted(async () => {
    await fetchSettings()
})
</script> 