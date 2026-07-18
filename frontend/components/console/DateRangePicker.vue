<template>
    <div class="mb-6 flex flex-wrap items-center gap-4 p-3 bg-gray-50 dark:bg-gray-900 rounded-lg">
        <div class="flex items-center gap-2">
            <span class="text-sm font-medium text-gray-700 dark:text-gray-300">{{ $t('monitoring.overview.timePeriod') }}:</span>
        </div>
        <div class="flex items-center gap-3">
            <USelectMenu
                :model-value="localizedSelectedPeriod"
                :options="periodOptions"
                @update:model-value="onPeriodSelect"
                size="sm"
                class="min-w-[140px]"
            />

            <!-- Exact day: single date input -->
            <input
                v-if="selectedPeriod.value === 'exact_day'"
                type="date"
                v-model="exactDay"
                :max="today"
                class="text-xs border border-gray-300 dark:border-gray-700 rounded-md px-2 py-1.5 bg-white dark:bg-gray-800 text-gray-700 dark:text-gray-300"
                @change="emitExactDay"
            />

            <!-- Custom range: start/end date inputs -->
            <div v-else-if="selectedPeriod.value === 'custom'" class="flex items-center gap-2">
                <label class="text-xs text-gray-500 dark:text-gray-400">{{ $t('monitoring.overview.from') }}</label>
                <input
                    type="date"
                    v-model="customStart"
                    :max="customEnd || today"
                    class="text-xs border border-gray-300 dark:border-gray-700 rounded-md px-2 py-1.5 bg-white dark:bg-gray-800 text-gray-700 dark:text-gray-300"
                    @change="emitCustomRange"
                />
                <label class="text-xs text-gray-500 dark:text-gray-400">{{ $t('monitoring.overview.to') }}</label>
                <input
                    type="date"
                    v-model="customEnd"
                    :min="customStart"
                    :max="today"
                    class="text-xs border border-gray-300 dark:border-gray-700 rounded-md px-2 py-1.5 bg-white dark:bg-gray-800 text-gray-700 dark:text-gray-300"
                    @change="emitCustomRange"
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
    /** Adds 7d / exact-day / custom-range options. Consumers must handle the
     *  'rangeChange' event (and '7_days'/'exact_day'/'custom' period values). */
    extended?: boolean
}

const props = withDefaults(defineProps<Props>(), { extended: false })

const emit = defineEmits<{
    periodChange: [period: Period]
    rangeChange: [range: DateRange]
}>()

const { t } = useI18n()

const today = new Date().toISOString().split('T')[0]

// Local state for the custom/exact-day inputs, seeded from the current range.
const exactDay = ref(props.dateRange.start || today)
const customStart = ref(props.dateRange.start || '')
const customEnd = ref(props.dateRange.end || today)

// Options & the currently-selected period label are computed so they
// relocalize when the user switches languages without reloading.
const periodOptions = computed(() => [
    { label: t('monitoring.overview.allTime'), value: 'all_time' },
    ...(props.extended ? [{ label: t('monitoring.overview.last7d'), value: '7_days' }] : []),
    { label: t('monitoring.overview.last30d'), value: '30_days' },
    { label: t('monitoring.overview.last90d'), value: '90_days' },
    ...(props.extended ? [
        { label: t('monitoring.overview.exactDay'), value: 'exact_day' },
        { label: t('monitoring.overview.customRange'), value: 'custom' },
    ] : []),
])

const localizedSelectedPeriod = computed(() =>
    periodOptions.value.find(o => o.value === props.selectedPeriod.value) || props.selectedPeriod
)

const onPeriodSelect = (period: Period) => {
    emit('periodChange', period)
    // Entering a date-input mode immediately applies the currently shown value
    // so the page doesn't sit on a stale range while the user fiddles.
    if (period.value === 'exact_day') {
        emitExactDay()
    } else if (period.value === 'custom' && customStart.value) {
        emitCustomRange()
    }
}

const emitExactDay = () => {
    if (!exactDay.value) return
    emit('rangeChange', { start: exactDay.value, end: exactDay.value })
}

const emitCustomRange = () => {
    if (!customStart.value || !customEnd.value) return
    emit('rangeChange', { start: customStart.value, end: customEnd.value })
}

const _df = useFormatDate()
const formatDateRange = () => {
    if (!props.dateRange.start || props.selectedPeriod.value === 'all_time') {
        return ''
    }

    return `${_df.formatDate(props.dateRange.start)} - ${_df.formatDate(props.dateRange.end)}`
}


</script>
