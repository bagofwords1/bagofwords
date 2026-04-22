<template>
    <div class="mb-6 flex items-center gap-4 p-3 bg-gray-50 rounded-lg">
        <div class="flex items-center gap-2">
            <span class="text-sm font-medium text-gray-700">{{ $t('monitoring.overview.timePeriod') }}:</span>
        </div>
        <div class="flex items-center gap-3">
            <USelectMenu
                :model-value="localizedSelectedPeriod"
                :options="periodOptions"
                @update:model-value="$emit('periodChange', $event)"
                size="sm"
                class="min-w-[140px]"
            />

            <div v-if="selectedPeriod.value !== 'all_time'" class="text-xs text-gray-500">
                {{ formatDateRange() }}
            </div>
        </div>
        <!-- Slot for additional filters (e.g., DomainSelector) -->
        <slot></slot>
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

const { t } = useI18n()

// Options & the currently-selected period label are computed so they
// relocalize when the user switches languages without reloading.
const periodOptions = computed(() => [
    { label: t('monitoring.overview.allTime'), value: 'all_time' },
    { label: t('monitoring.overview.last30d'), value: '30_days' },
    { label: t('monitoring.overview.last90d'), value: '90_days' },
])

const localizedSelectedPeriod = computed(() =>
    periodOptions.value.find(o => o.value === props.selectedPeriod.value) || props.selectedPeriod
)



const formatDateRange = () => {
    if (!props.dateRange.start || props.selectedPeriod.value === 'all_time') {
        return ''
    }
    
    const start = new Date(props.dateRange.start)
    const end = new Date(props.dateRange.end)
    
    return `${start.toLocaleDateString()} - ${end.toLocaleDateString()}`
}


</script> 