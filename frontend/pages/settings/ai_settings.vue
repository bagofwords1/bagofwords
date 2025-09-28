<template>
    <div class="mt-6">
        <h2 class="text-lg font-medium text-gray-900">AI Settings
            <p class="text-sm text-gray-500 font-normal mb-8">
                Configure AI capabilities and general configuration settings for your organization.
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

        <!-- AI Settings content -->
        <div v-if="!loading && !error" class="space-y-8">
      <!-- General Configuration Section -->
            <div v-if="Object.keys(configFeatures).length > 0">
                
                <div class="space-y-5">
                    <div v-for="(feature, key) in configFeatures" :key="`config_${key}`" class="flex flex-col md:w-2/3">
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
                                @change="updateConfigFeature(key, feature)"
                            />
                            <span v-else class="text-sm text-gray-600">
                                {{ feature.value }} ({{ feature.editable && feature.state !== 'locked' ? 'Editable via API' : 'Not directly editable' }})
                            </span>
                        </div>
                        <p class="text-sm text-gray-500 mt-2.5">{{ feature.description }}</p>
                    </div>
                </div>
            </div>
            <hr />
            <!-- AI Agents Section -->
            <div v-if="Object.keys(aiFeatures).length > 0" class="hidden">
                <h3 class="text-base font-semibold text-gray-900 mb-4">AI Agents</h3>
                <p class="text-sm text-gray-500 mb-6">Configure AI agents and capabilities available to your organization's members.</p>
                
                <div class="space-y-5">
                    <div v-for="(feature, key) in aiFeatures" :key="`ai_${key}`" class="flex flex-col md:w-2/3">
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
                                @change="updateAIFeature(key, feature)"
                            />
                            <span v-else class="text-sm text-gray-600">
                                {{ feature.value }} ({{ feature.editable && feature.state !== 'locked' ? 'Editable via API' : 'Not directly editable' }})
                            </span>
                        </div>
                        <p class="text-sm text-gray-500 mt-2.5">{{ feature.description }}</p>
                    </div>
                </div>
            </div>

      

            <!-- No settings message -->
            <div v-if="Object.keys(aiFeatures).length === 0 && Object.keys(configFeatures).length === 0" class="text-center py-8">
                <p class="text-gray-500">No AI settings available.</p>
            </div>
        </div>
    </div>
</template>

<script setup lang="ts">
import { ref, onMounted } from 'vue'
import { useToast } from '#imports'

// Define feature interface matching backend FeatureConfig
interface Feature {
    name: string
    description: string
    value: any
    state: 'enabled' | 'disabled' | 'locked'
    editable: boolean
    is_lab: boolean
}

// Define response interface for better type safety
interface SettingsResponse {
    config?: {
        ai_features?: Record<string, Feature>
        [key: string]: any
    }
}

definePageMeta({ auth: true, permissions: ['modify_settings'], layout: 'settings' })

const loading = ref(true)
const error = ref('')
const aiFeatures = ref<Record<string, Feature>>({})
const configFeatures = ref<Record<string, Feature>>({})

const toast = useToast()

// Fetch organization settings
const fetchSettings = async () => {
    loading.value = true
    error.value = ''
    try {
        const response = await useMyFetch('/api/organization/settings')
        
        if (response.status.value !== 'success') {
            const errorData = response.error?.value?.data || { message: 'Failed to fetch settings' }
            throw new Error(errorData.message || errorData.detail || 'Failed to fetch settings')
        }
        
        const data = response.data.value as SettingsResponse

        // Extract AI features
        aiFeatures.value = (data.config?.ai_features) ? data.config.ai_features : {}

        // Extract general configuration features (excluding ai_features)
        const allConfig = data.config || {}
        const generalConfig: Record<string, Feature> = {}
        
        for (const key in allConfig) {
            if (key !== 'ai_features' && typeof allConfig[key] === 'object' && allConfig[key]?.name) {
                generalConfig[key] = allConfig[key] as Feature
            }
        }
        configFeatures.value = generalConfig

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

// Update AI feature setting
const updateAIFeature = async (featureKey: string, feature: Feature) => {
    const originalValue = !feature.value
    try {
        const payload = { 
            config: { 
                ai_features: {
                    [featureKey]: {
                        value: aiFeatures.value[featureKey].value
                    }
                }
            } 
        }

        const response = await useMyFetch('/api/organization/settings', {
            method: 'PUT',
            headers: {
                'Content-Type': 'application/json'
            },
            body: JSON.stringify(payload)
        })

        if (response.status.value !== 'success') {
            const errorData = response.error?.value?.data || { message: 'Failed to update setting' }
            throw new Error(errorData.message || errorData.detail || 'Failed to update setting')
        }

        // Update the local state from response
        const updatedConfig = (response.data?.value as SettingsResponse)?.config
        if (updatedConfig?.ai_features?.[featureKey]) {
            aiFeatures.value[featureKey] = updatedConfig.ai_features[featureKey]
        } else {
            // Fallback: manually update state based on new value
            aiFeatures.value[featureKey].state = aiFeatures.value[featureKey].value ? 'enabled' : 'disabled'
        }

        toast.add({
            title: 'Success',
            description: `${feature.name} has been set to ${feature.value ? 'enabled' : 'disabled'}`,
            color: 'green',
            timeout: 3000
        })
    } catch (err: any) {
        // Revert the toggle
        aiFeatures.value[featureKey].value = originalValue
        aiFeatures.value[featureKey].state = originalValue ? 'enabled' : 'disabled'

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

// Update general config feature setting
const updateConfigFeature = async (featureKey: string, feature: Feature) => {
    const originalValue = !feature.value
    try {
        const payload = { 
            config: {
                [featureKey]: {
                    value: configFeatures.value[featureKey].value
                }
            }
        }

        const response = await useMyFetch('/api/organization/settings', {
            method: 'PUT',
            headers: {
                'Content-Type': 'application/json'
            },
            body: JSON.stringify(payload)
        })

        if (response.status.value !== 'success') {
            const errorData = response.error?.value?.data || { message: 'Failed to update setting' }
            throw new Error(errorData.message || errorData.detail || 'Failed to update setting')
        }

        // Update the local state from response
        const updatedConfig = (response.data?.value as SettingsResponse)?.config
        if (updatedConfig?.[featureKey]) {
            configFeatures.value[featureKey] = updatedConfig[featureKey] as Feature
        } else {
            // Fallback: manually update state based on new value
            configFeatures.value[featureKey].state = configFeatures.value[featureKey].value ? 'enabled' : 'disabled'
        }

        toast.add({
            title: 'Success',
            description: `${feature.name} has been set to ${feature.value ? 'enabled' : 'disabled'}`,
            color: 'green',
            timeout: 3000
        })
    } catch (err: any) {
        // Revert the toggle
        configFeatures.value[featureKey].value = originalValue
        configFeatures.value[featureKey].state = originalValue ? 'enabled' : 'disabled'

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
