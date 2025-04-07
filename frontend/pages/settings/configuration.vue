<template>
    <div class="mt-6">
        <h2 class="text-lg font-medium text-gray-900">Configuration
            <p class="text-sm text-gray-500 font-normal mb-8">
                Manage general configuration settings
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

        <!-- Configuration content -->
        <div v-if="!loading && !error" class="space-y-5">
            <!-- General Features -->
            <div v-for="(feature, key) in configFeatures" :key="key" class="flex flex-col md:w-2/3">
                <div class="flex items-center justify-between">
                    <div class="font-medium flex items-center">
                        {{ feature.name }}
                        <UTooltip v-if="feature.is_lab" text="Beta feature">
                            <Icon name="heroicons:beaker" class="ml-2 w-4 h-4" />
                        </UTooltip>
                        <UTooltip v-if="feature.state === 'locked'" text="This setting is locked and cannot be changed.">
                            <Icon name="heroicons:lock-closed" class="ml-2 w-4 h-4 text-gray-400" />
                        </UTooltip>
                    </div>
                    <UToggle
                        v-if="typeof feature.value === 'boolean'"
                        v-model="feature.value"
                        :disabled="!feature.editable || feature.state === 'locked'"
                        @change="updateFeature(key, feature)"
                    />
                    <!-- Optionally handle non-boolean values here, e.g., UInput -->
                    <span v-else class="text-sm text-gray-600">
                        {{ feature.value }} ({{ feature.editable && feature.state !== 'locked' ? 'Editable via API' : 'Not directly editable' }})
                    </span>
                </div>
                <p class="text-sm text-gray-500 mt-2.5">{{ feature.description }}</p>
            </div>
        </div>
    </div>
</template>

<script setup lang="ts">
import { ref, onMounted } from 'vue'
import { useToast } from '#imports' // Ensure useToast is imported if not globally available

// Define feature interface matching backend FeatureConfig (including value, state)
interface Feature {
    name: string
    description: string
    value: any // Changed from enabled: boolean
    state: 'enabled' | 'disabled' | 'locked'
    editable: boolean
    is_lab: boolean
}

definePageMeta({ auth: true, permissions: ['modify_settings'], layout: 'settings' })

const loading = ref(true)
const error = ref('')
// Use the Feature interface
const configFeatures = ref<Record<string, Feature>>({})

const toast = useToast()

// Fetch organization settings
const fetchSettings = async () => {
    loading.value = true
    error.value = ''
    try {
        const response = await useMyFetch('/api/organization/settings')
        // Ensure status check is correct based on useMyFetch implementation
        if (response.status.value !== 'success') { // Example check, adjust if needed
             const errorData = response.error?.value?.data || { message: 'Failed to fetch settings' }
             throw new Error(errorData.message || errorData.detail || 'Failed to fetch settings')
        }
        const data = response.data.value

        // Extract configuration features directly from config object
        // Filter out ai_features as they are handled elsewhere
        const allConfig = data.config || {};
        const generalConfig = {};
        for (const key in allConfig) {
            if (key !== 'ai_features' && typeof allConfig[key] === 'object' && allConfig[key]?.name) {
                 // Assuming top-level keys are FeatureConfig objects
                 generalConfig[key] = allConfig[key];
            }
            // Add handling for non-feature config items if needed
        }
        configFeatures.value = generalConfig;


    } catch (err: any) {
        error.value = err.message || 'An error occurred while fetching settings'
        toast.add({
            title: 'Error Fetching Settings',
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
const updateFeature = async (featureKey: string, feature: Feature) => {
    // Store the original value in case of revert
    const originalValue = !feature.value;
    try {
        const payload = { config: {} }
        // Send the 'value' field in the update payload
        payload.config[featureKey] = {
            value: configFeatures.value[featureKey].value // Send the new value
        }

        const response = await useMyFetch('/api/organization/settings', {
            method: 'PUT',
            headers: {
                'Content-Type': 'application/json'
            },
            body: JSON.stringify(payload)
        })

        // Adjust status check as needed
        if (response.status.value !== 'success') {
            const errorData = response.error?.value?.data || { message: 'Failed to update setting' }
            throw new Error(errorData.message || errorData.detail || 'Failed to update setting')
        }

        // Update the local state fully from the response if possible, or just the value/state
        const updatedConfig = response.data?.value?.config;
        if (updatedConfig && updatedConfig[featureKey]) {
             configFeatures.value[featureKey] = updatedConfig[featureKey];
        } else {
             // Fallback: manually update state based on new value if full object not returned
             configFeatures.value[featureKey].state = configFeatures.value[featureKey].value ? 'enabled' : 'disabled';
        }


        // Show success toast using the new value
        toast.add({
            title: 'Success',
            description: `${feature.name} has been set to ${feature.value ? 'enabled' : 'disabled'}`,
            color: 'green',
            timeout: 3000
        })
    } catch (err: any) {
        // Revert the toggle using the stored original value
        configFeatures.value[featureKey].value = originalValue;
        // Also revert state if possible
        configFeatures.value[featureKey].state = originalValue ? 'enabled' : 'disabled';


        // Show error toast
        error.value = err.message || 'An error occurred while updating settings'
        toast.add({
            title: 'Error Updating Setting',
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