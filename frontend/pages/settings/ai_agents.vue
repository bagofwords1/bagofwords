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
                     <!-- Optionally handle non-boolean values here -->
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
import { useToast } from '#imports' // Ensure useToast is imported

// Updated interface to use 'value' and include 'state'
interface AIFeature {
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
const aiFeatures = ref<Record<string, AIFeature>>({})

const toast = useToast()

// Fetch organization settings
const fetchSettings = async () => {
    loading.value = true
    error.value = ''
    try {
        const response = await useMyFetch('/api/organization/settings')
        // Adjust status check as needed
        if (response.status.value !== 'success') {
            const errorData = response.error?.value?.data || { message: 'Failed to fetch settings' }
            throw new Error(errorData.message || errorData.detail || 'Failed to fetch settings')
        }
        const data = response.data.value

        // Extract AI features, ensuring it's an object
        aiFeatures.value = (data.config && data.config.ai_features) ? data.config.ai_features : {}

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
const updateFeature = async (featureKey: string, feature: AIFeature) => {
    // Store original value for revert
    const originalValue = !feature.value;
    try {
        const payload = { config: { ai_features: {} } }
        // Send 'value' in the payload
        payload.config.ai_features[featureKey] = {
            value: aiFeatures.value[featureKey].value // Send the new value
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

         // Update the local state fully from the response if possible
        const updatedConfig = response.data?.value?.config;
        if (updatedConfig && updatedConfig.ai_features && updatedConfig.ai_features[featureKey]) {
             aiFeatures.value[featureKey] = updatedConfig.ai_features[featureKey];
        } else {
             // Fallback: manually update state based on new value if full object not returned
             aiFeatures.value[featureKey].state = aiFeatures.value[featureKey].value ? 'enabled' : 'disabled';
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
        aiFeatures.value[featureKey].value = originalValue;
         // Also revert state if possible
        aiFeatures.value[featureKey].state = originalValue ? 'enabled' : 'disabled';

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