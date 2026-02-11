<template>
    <UModal v-model="isOpen" :ui="{ width: 'sm:max-w-sm' }">
        <div class="p-5">
            <div class="flex items-center justify-between mb-4">
                <h3 class="text-sm font-semibold text-gray-900">Set Source Scope</h3>
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
                        <template v-if="isGlobalSelected">
                            <span class="flex items-center gap-1 text-xs">
                                <UIcon name="i-heroicons-globe-alt" class="w-3.5 h-3.5 text-gray-500" />
                                Global
                            </span>
                        </template>
                        <template v-else>
                            <span v-for="val in selectedValues.slice(0, 2)" :key="val" class="flex items-center gap-1 text-xs">
                                <DataSourceIcon :type="getOptionType(val)" class="h-3.5" />
                                {{ getOptionLabel(val) }}
                            </span>
                            <span v-if="selectedValues.length > 2" class="text-xs text-gray-400">
                                +{{ selectedValues.length - 2 }}
                            </span>
                        </template>
                    </div>
                    <span v-else class="text-gray-400">Select scope</span>
                </template>
                <template #option="{ option }">
                    <div class="flex items-center gap-2">
                        <UIcon v-if="option.value === 'global'" name="i-heroicons-globe-alt" class="w-4 h-4 text-gray-500" />
                        <DataSourceIcon v-else :type="option.type" class="h-4" />
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
import DataSourceIcon from '~/components/DataSourceIcon.vue'

interface DataSource {
    id: string
    name: string
    type?: string
}

interface Option {
    value: string
    label: string
    type?: string
}

const props = defineProps<{
    modelValue: boolean
    dataSources: DataSource[]
}>()

const emit = defineEmits<{
    'update:modelValue': [value: boolean]
    'set-scope': [dataSourceIds: string[]]
    'clear-scope': []
}>()

const isOpen = computed({
    get: () => props.modelValue,
    set: (value) => emit('update:modelValue', value)
})

const selectedValues = ref<string[]>(['global'])

const options = computed<Option[]>(() => [
    { value: 'global', label: 'Global (all sources)' },
    ...props.dataSources.map(ds => ({
        value: ds.id,
        label: ds.name,
        type: ds.type
    }))
])

const isGlobalSelected = computed(() => selectedValues.value.includes('global'))

function getOptionLabel(value: string): string {
    return options.value.find(o => o.value === value)?.label || value
}

function getOptionType(value: string): string | undefined {
    return options.value.find(o => o.value === value)?.type
}

// When selection changes, handle mutual exclusivity between Global and specific sources
watch(selectedValues, (newValues, oldValues) => {
    const hadGlobal = oldValues.includes('global')
    const hasGlobal = newValues.includes('global')
    
    if (hasGlobal && !hadGlobal) {
        // Just selected Global - clear other selections
        selectedValues.value = ['global']
    } else if (hasGlobal && newValues.length > 1) {
        // Had Global but added a specific source - remove Global
        selectedValues.value = newValues.filter(v => v !== 'global')
    }
}, { deep: true })

function handleConfirm() {
    if (isGlobalSelected.value) {
        emit('clear-scope')
    } else {
        emit('set-scope', selectedValues.value)
    }
    isOpen.value = false
}

// Reset state when modal opens
watch(isOpen, (open) => {
    if (open) {
        selectedValues.value = ['global']
    }
})
</script>
