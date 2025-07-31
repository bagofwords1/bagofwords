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

            <div v-if="selectedPeriod.value !== 'all_time'" class="text-xs text-gray-500">
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
    dateRange: DateRange
}

const props = defineProps<Props>()

const emit = defineEmits<{
    periodChange: [period: Period]
}>()

const periodOptions = [
    { label: 'All Time', value: 'all_time' },
    { label: 'Last 30 Days', value: '30_days' },
    { label: 'Last 90 Days', value: '90_days' }
]



const formatDateRange = () => {
    if (!props.dateRange.start || props.selectedPeriod.value === 'all_time') {
        return ''
    }
    
    const start = new Date(props.dateRange.start)
    const end = new Date(props.dateRange.end)
    
    return `${start.toLocaleDateString()} - ${end.toLocaleDateString()}`
}


</script> 