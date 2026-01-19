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
                    <!-- Regular config features (excluding allow_llm_see_data) -->
                    <div v-for="(feature, key) in regularConfigFeatures" :key="`config_${key}`" class="flex flex-col md:w-2/3">
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
                            <UInput
                                v-else-if="feature.editable && feature.state !== 'locked' && typeof feature.value === 'number'"
                                v-model.number="feature.value"
                                type="number"
                                class="w-28"
                                @blur="updateConfigFeature(key, feature)"
                                @keyup.enter="updateConfigFeature(key, feature)"
                            />
                            <UInput
                                v-else-if="feature.editable && feature.state !== 'locked' && typeof feature.value !== 'number'"
                                v-model="feature.value"
                                type="text"
                                class="w-56"
                                @blur="updateConfigFeature(key, feature)"
                                @keyup.enter="updateConfigFeature(key, feature)"
                            />
                            <span v-else class="text-sm text-gray-600">
                                {{ feature.value }} (Not directly editable)
                            </span>
                        </div>
                        <p class="text-sm text-gray-500 mt-2.5">{{ feature.description }}</p>
                    </div>

                    <!-- Allow LLM See Data - Special highlighted setting at the end -->
                    <div v-if="configFeatures.allow_llm_see_data" class="flex flex-col md:w-2/3 mt-8 p-4 border-2 border-amber-300 bg-amber-50 rounded-lg">
                        <div class="flex items-center justify-between">
                            <div class="font-medium flex items-center">
                                <Icon name="heroicons:shield-exclamation" class="mr-2 w-5 h-5 text-amber-600" />
                                {{ configFeatures.allow_llm_see_data.name }}
                                <UTooltip v-if="configFeatures.allow_llm_see_data.state === 'locked'" text="This setting is locked and cannot be changed.">
                                    <Icon name="heroicons:lock-closed" class="ml-2 w-4 h-4 text-gray-400" />
                                </UTooltip>
                            </div>
                            <UToggle
                                v-model="configFeatures.allow_llm_see_data.value"
                                :disabled="!configFeatures.allow_llm_see_data.editable || configFeatures.allow_llm_see_data.state === 'locked'"
                                @change="handleAllowLlmSeeDataChange"
                            />
                        </div>
                        <p class="text-sm text-amber-700 mt-2.5">{{ configFeatures.allow_llm_see_data.description }}</p>
                        <p class="text-xs text-amber-600 mt-1 font-medium">
                            <Icon name="heroicons:exclamation-triangle" class="inline w-3 h-3 mr-1" />
                            Changing this setting affects data privacy. A confirmation will be required.
                        </p>
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
                            <UInput
                                v-else-if="feature.editable && feature.state !== 'locked' && typeof feature.value === 'number'"
                                v-model.number="feature.value"
                                type="number"
                                class="w-28"
                                @blur="updateAIFeature(key, feature)"
                                @keyup.enter="updateAIFeature(key, feature)"
                            />
                            <UInput
                                v-else-if="feature.editable && feature.state !== 'locked' && typeof feature.value !== 'number'"
                                v-model="feature.value"
                                type="text"
                                class="w-56"
                                @blur="updateAIFeature(key, feature)"
                                @keyup.enter="updateAIFeature(key, feature)"
                            />
                            <span v-else class="text-sm text-gray-600">
                                {{ feature.value }} (Not directly editable)
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

        <!-- Confirmation Modal for Allow LLM See Data -->
        <UModal v-model="showLlmConfirmModal" :ui="{ width: 'sm:max-w-lg' }">
            <UCard :ui="{ body: { padding: 'p-6' }, header: { padding: 'px-6 py-4' }, footer: { padding: 'px-6 py-4' } }">
                <template #header>
                    <h3 class="text-lg font-semibold text-gray-900">
                        {{ pendingLlmValue ? 'Enable' : 'Disable' }} LLM Data Access
                    </h3>
                </template>

                <div class="space-y-4">
                    <!-- Enable message -->
                    <p v-if="pendingLlmValue" class="text-sm text-gray-600">
                        The AI will be able to see sample rows from your data for better analysis.
                    </p>

                    <!-- Disable message with impact list -->
                    <template v-else>
                        <p class="text-sm text-gray-600">
                            Disabling this setting will have the following impact:
                        </p>
                        <ul class="text-sm text-gray-600 space-y-2 ml-1">
                            <li class="flex items-start gap-2">
                                <Icon name="heroicons:x-circle" class="w-4 h-4 text-red-500 mt-0.5 flex-shrink-0" />
                                <span><strong>Inspect Data</strong> tool will be disabled</span>
                            </li>
                            <li class="flex items-start gap-2">
                                <Icon name="heroicons:arrow-trending-down" class="w-4 h-4 text-amber-500 mt-0.5 flex-shrink-0" />
                                <span>Reduced analysis accuracy and performance</span>
                            </li>
                            <li class="flex items-start gap-2">
                                <Icon name="heroicons:eye-slash" class="w-4 h-4 text-gray-400 mt-0.5 flex-shrink-0" />
                                <span>AI will only see column names and statistics, not actual values</span>
                            </li>
                            <li class="mt-2 text-xs">
                            <UAlert description="File uploads are unaffected by this setting and will still be visible to the AI (you can disable file uploads separately)." class="text-xs" />
                        </li>
                        </ul>
                    </template>

                    <div class="pt-2">
                        <label class="block text-sm text-gray-600 mb-2">
                            Type <span class="font-mono bg-gray-100 px-1.5 py-0.5 rounded">I AM SURE</span> to confirm
                        </label>
                        <UInput
                            v-model="llmConfirmText"
                            placeholder="I AM SURE"
                            color="blue"
                            class="w-full"
                            @keyup.enter="confirmLlmChange"
                        />
                    </div>
                </div>

                <template #footer>
                    <div class="flex justify-end gap-3">
                        <UButton color="gray" variant="ghost" @click="cancelLlmChange">
                            Cancel
                        </UButton>
                        <UButton
                            :color="pendingLlmValue ? 'blue' : 'red'"
                            :disabled="llmConfirmText !== 'I AM SURE'"
                            @click="confirmLlmChange"
                        >
                            Confirm
                        </UButton>
                    </div>
                </template>
            </UCard>
        </UModal>
    </div>
</template>

<script setup lang="ts">
import { ref, computed, onMounted } from 'vue'
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

// Confirmation modal state
const showLlmConfirmModal = ref(false)
const llmConfirmText = ref('')
const pendingLlmValue = ref(false)

// Computed property to exclude allow_llm_see_data from regular features
const regularConfigFeatures = computed(() => {
    const features: Record<string, Feature> = {}
    for (const key in configFeatures.value) {
        if (key !== 'allow_llm_see_data') {
            features[key] = configFeatures.value[key]
        }
    }
    return features
})
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

// Handle allow_llm_see_data toggle - requires confirmation
const handleAllowLlmSeeDataChange = () => {
    // Store the new value and revert toggle until confirmed
    pendingLlmValue.value = configFeatures.value.allow_llm_see_data.value
    // Revert the toggle visually until confirmed
    configFeatures.value.allow_llm_see_data.value = !pendingLlmValue.value
    llmConfirmText.value = ''
    showLlmConfirmModal.value = true
}

// Confirm the allow_llm_see_data change
const confirmLlmChange = async () => {
    if (llmConfirmText.value !== 'I AM SURE') {
        toast.add({
            title: 'Confirmation Required',
            description: 'Please type "I AM SURE" to confirm this change.',
            color: 'amber',
            timeout: 3000,
            icon: 'i-heroicons-exclamation-triangle'
        })
        return
    }

    // Apply the pending value
    configFeatures.value.allow_llm_see_data.value = pendingLlmValue.value
    showLlmConfirmModal.value = false
    llmConfirmText.value = ''

    // Now update the setting
    await updateConfigFeature('allow_llm_see_data', configFeatures.value.allow_llm_see_data)
}

// Cancel the allow_llm_see_data change
const cancelLlmChange = () => {
    showLlmConfirmModal.value = false
    llmConfirmText.value = ''
    // Toggle stays at original value (already reverted in handleAllowLlmSeeDataChange)
}

// Fetch settings when the component is mounted
onMounted(async () => {
    await fetchSettings()
})
</script>
