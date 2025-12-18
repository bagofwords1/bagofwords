<template>
    <UModal v-model="isOpen" :ui="{ width: 'sm:max-w-lg' }">
        <div class="p-8">
            <!-- Header -->
            <div class="flex items-center justify-between mb-3 border-b border-gray-100 pb-3">
                <div class="flex items-center gap-2.5">
                    <UIcon 
                        :name="currentSavedMode === 'on' ? 'i-heroicons-bolt' : 'i-heroicons-bolt-slash'" 
                        class="w-5 h-5"
                        :class="currentSavedMode === 'on' ? 'text-amber-500' : 'text-gray-400'"
                    />
                    <h3 class="text-lg font-semibold text-gray-900">AI Suggestions</h3>
                </div>
                <button @click="close" class="text-gray-400 hover:text-gray-600">
                    <UIcon name="i-heroicons-x-mark" class="w-5 h-5" />
                </button>
            </div>

            <!-- Description -->
            <p class="text-sm text-gray-500 leading-relaxed mb-8">
                The AI intelligently learns from your interactions by extracting definitions and semantics, remembering corrections, and capturing patterns, then suggests instructions for your review.
            </p>

            <!-- Mode selection -->
            <div class="mb-8">
                <label class="block text-sm font-medium text-gray-700 mb-3">Mode</label>
                <USelectMenu
                    v-model="form.mode"
                    :options="modeOptions"
                    value-attribute="value"
                    option-attribute="label"
                    :disabled="isSaving || !canEdit"
                    size="lg"
                    class="w-full"
                >
                    <template #label>
                        <div class="flex flex-col items-start py-0.5">
                            <div class="flex items-center gap-2">
                                <span class="font-medium">{{ selectedOption?.label }}</span>
                                <span v-if="selectedOption?.recommended" class="text-[10px] font-medium px-1.5 py-0.5 rounded bg-emerald-100 text-emerald-700">
                                    Recommended
                                </span>
                            </div>
                            <span class="text-xs text-gray-500 mt-0.5">{{ selectedOption?.description }}</span>
                        </div>
                    </template>
                    <template #option="{ option }">
                        <div class="py-1.5">
                            <div class="flex items-center gap-2">
                                <p class="text-sm font-medium text-gray-900">{{ option.label }}</p>
                                <span v-if="option.recommended" class="text-[10px] font-medium px-1.5 py-0.5 rounded bg-emerald-100 text-emerald-700">
                                    Recommended
                                </span>
                            </div>
                            <p class="text-xs text-gray-500 mt-0.5">{{ option.description }}</p>
                        </div>
                    </template>
                </USelectMenu>
                <p v-if="!canEdit" class="text-xs text-gray-400 mt-3">
                    Only admins can change this setting.
                </p>
            </div>

            <!-- Footer -->
            <div class="flex justify-end gap-3 pt-5 border-t border-gray-100">
                <UButton color="gray" variant="ghost" @click="close" :disabled="isSaving">
                    {{ canEdit ? 'Cancel' : 'Close' }}
                </UButton>
                <UButton v-if="canEdit" color="blue" @click="save" :loading="isSaving">
                    Save
                </UButton>
            </div>
        </div>
    </UModal>
</template>

<script setup lang="ts">
import { useCan, usePermissionsLoaded } from '~/composables/usePermissions'

type LearningMode = 'off' | 'on'

interface LearningSettings {
    enabled: boolean
    sensitivity: number
    conditions: Record<string, boolean>
    mode?: LearningMode
}

interface FormState {
    mode: LearningMode
}

const props = defineProps<{
    modelValue: boolean
    settings?: LearningSettings | null
}>()

const emit = defineEmits<{
    'update:modelValue': [value: boolean]
    'saved': [settings: LearningSettings]
}>()

const toast = useToast()

// Permission check - only users with modify_settings can edit
const permissionsLoaded = usePermissionsLoaded()
const canEdit = computed(() => permissionsLoaded.value && useCan('modify_settings'))

const isOpen = computed({
    get: () => props.modelValue,
    set: (value) => emit('update:modelValue', value)
})

const isSaving = ref(false)

const modeOptions = [
    { value: 'on' as LearningMode, label: 'Enabled', description: 'Automatically suggest instructions from conversations', recommended: true },
    { value: 'off' as LearningMode, label: 'Disabled', description: 'No automatic instruction suggestions', recommended: false },
]

const selectedOption = computed(() => modeOptions.find(o => o.value === form.value.mode))

// Track the current saved mode (from props) for the header icon
const currentSavedMode = computed(() => props.settings?.mode ?? (props.settings?.enabled ? 'on' : 'off'))

// Map mode to sensitivity values
const modeToSensitivity: Record<LearningMode, number> = {
    off: 1.0,
    on: 0.6
}

// Map sensitivity back to mode
const sensitivityToMode = (enabled: boolean): LearningMode => {
    return enabled ? 'on' : 'off'
}

const form = ref<FormState>({ mode: 'on' })

// Watch for settings changes and update form
watch(() => props.settings, (newSettings) => {
    if (newSettings) {
        form.value.mode = newSettings.mode || sensitivityToMode(newSettings.enabled ?? true)
    } else {
        form.value.mode = 'on'
    }
}, { immediate: true, deep: true })

// Reset form when modal opens
watch(isOpen, (open) => {
    if (open && props.settings) {
        form.value.mode = props.settings.mode || sensitivityToMode(props.settings.enabled ?? true)
    }
})

const close = () => {
    isOpen.value = false
}

const save = async () => {
    isSaving.value = true
    try {
        const isEnabled = form.value.mode !== 'off'
        const sensitivity = modeToSensitivity[form.value.mode]

        // Send settings to backend
        const { error } = await useMyFetch('/organization/settings', {
            method: 'PUT',
            body: {
                config: {
                    suggest_instructions: {
                        name: 'Autogenerate instructions',
                        description: 'Automatically generate instructions following clarifications provided by the user',
                        is_lab: false,
                        editable: true,
                        state: isEnabled ? 'enabled' : 'disabled',
                        value: isEnabled,
                        sensitivity: sensitivity,
                        mode: form.value.mode,
                        conditions: {}
                    }
                }
            }
        })

        if (error.value) {
            throw error.value
        }

        toast.add({
            title: 'Settings saved',
            color: 'green'
        })

        emit('saved', { 
            enabled: isEnabled, 
            sensitivity, 
            conditions: {},
            mode: form.value.mode
        })
        close()
    } catch (err: any) {
        console.error('Failed to save learning settings:', err)
        toast.add({
            title: 'Failed to save settings',
            description: err?.data?.detail || 'An error occurred',
            color: 'red'
        })
    } finally {
        isSaving.value = false
    }
}
</script>

