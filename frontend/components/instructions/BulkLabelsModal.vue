<template>
    <UModal v-model="isOpen" :ui="{ width: 'sm:max-w-sm' }">
        <div class="p-5">
            <div class="flex items-center justify-between mb-4">
                <h3 class="text-sm font-semibold text-gray-900">Set Labels</h3>
                <button @click="isOpen = false" class="text-gray-400 hover:text-gray-600">
                    <UIcon name="i-heroicons-x-mark" class="w-5 h-5" />
                </button>
            </div>
            
            <USelectMenu
                v-model="selectedValues"
                :options="options"
                option-attribute="label"
                value-attribute="value"
                size="sm"
                multiple
                class="w-full"
            >
                <template #label>
                    <div class="flex items-center gap-1 flex-wrap" v-if="selectedValues.length">
                        <template v-if="isNoneSelected">
                            <span class="flex items-center gap-1 text-xs text-gray-500">
                                <UIcon name="i-heroicons-x-circle" class="w-3.5 h-3.5" />
                                None
                            </span>
                        </template>
                        <template v-else>
                            <span 
                                v-for="val in selectedValues.slice(0, 2)" 
                                :key="val" 
                                class="flex items-center gap-1 text-xs px-1.5 py-0.5 rounded-full border"
                                :style="getLabelStyle(val)"
                            >
                                <span class="w-1.5 h-1.5 rounded-full" :style="{ backgroundColor: getLabelColor(val) }"></span>
                                {{ getOptionLabel(val) }}
                            </span>
                            <span v-if="selectedValues.length > 2" class="text-xs text-gray-400">
                                +{{ selectedValues.length - 2 }}
                            </span>
                        </template>
                    </div>
                    <span v-else class="text-gray-400">Select labels</span>
                </template>
                <template #option="{ option }">
                    <div class="flex items-center gap-2">
                        <UIcon v-if="option.value === 'none'" name="i-heroicons-x-circle" class="w-4 h-4 text-gray-400" />
                        <template v-else>
                            <span class="w-2 h-2 rounded-full" :style="{ backgroundColor: option.color || '#94A3B8' }"></span>
                        </template>
                        <span>{{ option.label }}</span>
                    </div>
                </template>
            </USelectMenu>
            
            <div class="flex justify-end gap-2 mt-4 pt-3 border-t border-gray-100">
                <UButton color="gray" variant="ghost" size="xs" @click="isOpen = false">
                    Cancel
                </UButton>
                <UButton 
                    color="blue" 
                    size="xs" 
                    @click="handleConfirm"
                    :disabled="selectedValues.length === 0"
                >
                    Apply
                </UButton>
            </div>
        </div>
    </UModal>
</template>

<script setup lang="ts">
interface Label {
    id: string
    name: string
    color?: string | null
}

interface Option {
    value: string
    label: string
    color?: string | null
}

const props = defineProps<{
    modelValue: boolean
    labels: Label[]
}>()

const emit = defineEmits<{
    'update:modelValue': [value: boolean]
    'set-labels': [labelIds: string[]]
    'clear-labels': []
}>()

const isOpen = computed({
    get: () => props.modelValue,
    set: (value) => emit('update:modelValue', value)
})

const selectedValues = ref<string[]>(['none'])

const options = computed<Option[]>(() => [
    { value: 'none', label: 'None (clear labels)' },
    ...props.labels.map(lbl => ({
        value: lbl.id,
        label: lbl.name,
        color: lbl.color
    }))
])

const isNoneSelected = computed(() => selectedValues.value.includes('none'))

function getOptionLabel(value: string): string {
    return options.value.find(o => o.value === value)?.label || value
}

function getLabelColor(value: string): string {
    return options.value.find(o => o.value === value)?.color || '#94A3B8'
}

function getLabelStyle(value: string) {
    const color = getLabelColor(value)
    return {
        borderColor: color,
        backgroundColor: `${color}15`
    }
}

// When selection changes, handle mutual exclusivity between None and specific labels
watch(selectedValues, (newValues, oldValues) => {
    const hadNone = oldValues.includes('none')
    const hasNone = newValues.includes('none')
    
    if (hasNone && !hadNone) {
        // Just selected None - clear other selections
        selectedValues.value = ['none']
    } else if (hasNone && newValues.length > 1) {
        // Had None but added a specific label - remove None
        selectedValues.value = newValues.filter(v => v !== 'none')
    }
}, { deep: true })

function handleConfirm() {
    if (isNoneSelected.value) {
        emit('clear-labels')
    } else {
        emit('set-labels', selectedValues.value)
    }
    isOpen.value = false
}

// Reset state when modal opens
watch(isOpen, (open) => {
    if (open) {
        selectedValues.value = ['none']
    }
})
</script>

