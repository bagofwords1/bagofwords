<template>
    <div class="mb-6 flex items-center gap-4 p-3 bg-gray-50 dark:bg-gray-900 rounded-lg">
        <div class="flex items-center gap-2">
            <span class="text-sm font-medium text-gray-700 dark:text-gray-300">{{ $t('monitoring.overview.timePeriod') }}:</span>
        </div>
        <div class="flex items-center gap-3">
            <USelectMenu
                :model-value="localizedSelectedPeriod"
                :options="periodOptions"
                @update:model-value="$emit('periodChange', $event)"
                size="sm"
                class="min-w-[140px]"
            />

            <!-- Custom date range inputs (shown when the "custom" period is selected) -->
            <div v-if="allowCustom && selectedPeriod.value === 'custom'" class="flex items-center gap-2">
                <input
                    type="date"
                    :value="dateRange.start"
                    :max="dateRange.end || undefined"
                    :aria-label="$t('monitoring.overview.startDate')"
                    @change="onCustomDateChange('start', $event)"
                    class="text-xs rounded-md border border-gray-300 dark:border-gray-700 bg-white dark:bg-gray-800 px-2 py-1 text-gray-700 dark:text-gray-300"
                />
                <span class="text-gray-400">–</span>
                <input
                    type="date"
                    :value="dateRange.end"
                    :min="dateRange.start || undefined"
                    :aria-label="$t('monitoring.overview.endDate')"
                    @change="onCustomDateChange('end', $event)"
                    class="text-xs rounded-md border border-gray-300 dark:border-gray-700 bg-white dark:bg-gray-800 px-2 py-1 text-gray-700 dark:text-gray-300"
                />
            </div>

            <div v-else-if="selectedPeriod.value !== 'all_time'" class="text-xs text-gray-500 dark:text-gray-400">
                {{ formatDateRange() }}
            </div>
        </div>
        <!-- Slot for additional filters (e.g., AgentSelector) -->
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
    // When true, adds a "Custom range" option that reveals two date inputs.
    allowCustom?: boolean
}

const props = withDefaults(defineProps<Props>(), {
    allowCustom: false,
})

const emit = defineEmits<{
    periodChange: [period: Period]
    // Emitted as the user edits the custom start/end date inputs.
    customRangeChange: [range: DateRange]
}>()

const { t } = useI18n()

// Options & the currently-selected period label are computed so they
// relocalize when the user switches languages without reloading.
const periodOptions = computed(() => {
    const options = [
        { label: t('monitoring.overview.allTime'), value: 'all_time' },
        { label: t('monitoring.overview.last30d'), value: '30_days' },
        { label: t('monitoring.overview.last90d'), value: '90_days' },
    ]
    if (props.allowCustom) {
        options.push({ label: t('monitoring.overview.customRange'), value: 'custom' })
    }
    return options
})

// Merge the edited field into the current range and bubble it up. The parent
// owns dateRange, so we emit rather than mutate.
const onCustomDateChange = (field: 'start' | 'end', event: Event) => {
    const value = (event.target as HTMLInputElement).value
    emit('customRangeChange', { ...props.dateRange, [field]: value })
}

const localizedSelectedPeriod = computed(() =>
    periodOptions.value.find(o => o.value === props.selectedPeriod.value) || props.selectedPeriod
)



const _df = useFormatDate()
const formatDateRange = () => {
    if (!props.dateRange.start || props.selectedPeriod.value === 'all_time') {
        return ''
    }

    return `${_df.formatDate(props.dateRange.start)} - ${_df.formatDate(props.dateRange.end)}`
}


</script> 