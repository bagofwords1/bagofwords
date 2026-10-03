<template>
    <div
        :data-testid="`ai-setting-${settingKey}`"
        :class="['flex flex-col', child ? 'ps-5 border-s-2 border-gray-100 dark:border-gray-800' : '']"
    >
        <div class="flex items-center justify-between gap-4">
            <div class="font-medium flex items-center">
                {{ label }}
                <UTooltip v-if="feature.is_lab" :text="$t('settings.aiSettingsPage.beta')">
                    <Icon name="heroicons:beaker" class="ms-2 w-4 h-4" />
                </UTooltip>
                <UTooltip v-if="locked" :text="$t('settings.aiSettingsPage.locked')">
                    <Icon name="heroicons:lock-closed" class="ms-2 w-4 h-4 text-gray-400 dark:text-gray-400" />
                </UTooltip>
            </div>
            <UToggle
                v-if="typeof feature.value === 'boolean'"
                v-model="feature.value"
                :disabled="!feature.editable || locked"
                @change="emit('change')"
            />
            <UInput
                v-else-if="feature.editable && !locked && typeof feature.value === 'number'"
                v-model.number="feature.value"
                type="number"
                class="w-28 shrink-0"
                @blur="emit('change')"
                @keyup.enter="emit('change')"
            />
            <UInput
                v-else-if="feature.editable && !locked"
                v-model="feature.value"
                type="text"
                class="w-56 shrink-0"
                @blur="emit('change')"
                @keyup.enter="emit('change')"
            />
            <span v-else class="text-sm text-gray-600 dark:text-gray-400">
                {{ feature.value }} {{ $t('settings.aiSettingsPage.notEditable') }}
            </span>
        </div>
        <p class="text-sm text-gray-500 dark:text-gray-400 mt-2">{{ description }}</p>
    </div>
</template>

<script setup lang="ts">
import { computed } from 'vue'

// Mirrors the backend FeatureConfig. The row mutates feature.value in place
// (the parent owns the object) and emits `change` to persist it.
interface Feature {
    name: string
    description: string
    value: any
    state: 'enabled' | 'disabled' | 'locked'
    editable: boolean
    is_lab: boolean
}

const props = defineProps<{
    settingKey: string
    feature: Feature
    label: string
    description: string
    child?: boolean
}>()
const emit = defineEmits<{ (e: 'change'): void }>()

const locked = computed(() => props.feature.state === 'locked')
</script>
