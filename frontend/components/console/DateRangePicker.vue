<template>
    <div class="mb-6 flex items-center gap-4 p-3 bg-gray-50 rounded-lg">
        <div class="flex items-center gap-2">
            <span class="text-sm font-medium text-gray-700">Time Period:</span>
        </div>
        <div class="flex items-center gap-3">
            <USelectMenu
                :model-value="selectedPeriod"
                :options="periodOptions"
                @update:model-value="$emit('periodChange', $event)"
                size="sm"
                class="min-w-[140px]"
            />
            <div v-if="selectedPeriod.value === 'custom'" class="flex items-center gap-2">
                <UPopover :popper="{ placement: 'bottom-start' }">
                    <UButton variant="outline" size="sm" class="justify-start">
                        {{ formatCustomDateRange() || 'Select dates' }}
                    </UButton>
                    <template #panel="{ close }">
                        <div class="p-4">
                            <UDatePicker 
                                :model-value="customDateRange" 
                                range 
                                @update:model-value="handleCustomDateChange" 
                            />
                            <div class="flex justify-end gap-2 mt-4">
                                <UButton variant="ghost" size="sm" @click="close">Cancel</UButton>
                                <UButton size="sm" @click="applyCustomRange(close)">Apply</UButton>
                            </div>
                        </div>
                    </template>
                </UPopover>
            </div>
            <div v-else-if="selectedPeriod.value !== 'all_time'" class="text-xs text-gray-500">
                {{ formatDateRange() }}
            </div>
        </div>
    </div>
</template>

<script setup lang="ts">
interface Period {
    label: string
    value: string
}

interface DateRange {
    start: string
    end: string
}

interface Props {
    selectedPeriod: Period
    customDateRange?: Date[]
    dateRange: DateRange
}

const props = defineProps<Props>()

const emit = defineEmits<{
    periodChange: [period: Period]
    customRangeChange: [dates: Date[]]
    applyCustomRange: [dateRange: DateRange]
}>()

const periodOptions = [
    { label: 'All Time', value: 'all_time' },
    { label: 'Last 30 Days', value: '30_days' },
    { label: 'Last 90 Days', value: '90_days' },
    { label: 'Custom Range', value: 'custom' }
]

const formatCustomDateRange = () => {
    if (!props.customDateRange || props.customDateRange.length !== 2) {
        return ''
    }
    
    const [start, end] = props.customDateRange
    return `${start.toLocaleDateString()} - ${end.toLocaleDateString()}`
}

const formatDateRange = () => {
    if (!props.dateRange.start || props.selectedPeriod.value === 'all_time') {
        return ''
    }
    
    const start = new Date(props.dateRange.start)
    const end = new Date(props.dateRange.end)
    
    return `${start.toLocaleDateString()} - ${end.toLocaleDateString()}`
}

const handleCustomDateChange = (dates: Date[]) => {
    emit('customRangeChange', dates)
}

const applyCustomRange = (closePopover: () => void) => {
    if (props.customDateRange && props.customDateRange.length === 2) {
        const [start, end] = props.customDateRange
        const newDateRange = {
            start: start.toISOString().split('T')[0],
            end: end.toISOString().split('T')[0]
        }
        emit('applyCustomRange', newDateRange)
        closePopover()
    }
}
</script> 