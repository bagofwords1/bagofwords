<template>
    <div class="flex items-center gap-2 flex-shrink-0">
        <USelectMenu
            :model-value="selected"
            :options="options"
            size="sm"
            class="w-40"
            @update:model-value="onPreset"
        >
            <template #label>
                <span class="flex items-center gap-1.5">
                    <UIcon name="i-heroicons-calendar" class="w-4 h-4 text-gray-500" />
                    <span>{{ selected.label }}</span>
                </span>
            </template>
        </USelectMenu>
        <template v-if="range.preset === 'custom'">
            <input
                type="date"
                :value="isoDay(range.start)"
                :max="isoDay(range.end)"
                class="text-xs border border-gray-300 dark:border-gray-700 rounded-md px-2 py-1.5 bg-white dark:bg-gray-800 text-gray-700 dark:text-gray-300"
                @change="onCustom('start', ($event.target as HTMLInputElement).value)"
            />
            <span class="text-xs text-gray-400">–</span>
            <input
                type="date"
                :value="isoDay(range.end)"
                :min="isoDay(range.start)"
                :max="isoDay(new Date())"
                class="text-xs border border-gray-300 dark:border-gray-700 rounded-md px-2 py-1.5 bg-white dark:bg-gray-800 text-gray-700 dark:text-gray-300"
                @change="onCustom('end', ($event.target as HTMLInputElement).value)"
            />
        </template>
    </div>
</template>

<script setup lang="ts">
import { rangeForPreset, type RangePreset, type TimeRange } from '~/composables/useDiagnosisQuery'

const props = defineProps<{ range: TimeRange }>()
const emit = defineEmits<{ (e: 'change', r: TimeRange): void }>()
const { t } = useI18n()

const options = computed(() => [
    { value: '24h', label: t('monitoring.diagnosis.range24h') },
    { value: '7d', label: t('monitoring.diagnosis.range7d') },
    { value: '30d', label: t('monitoring.diagnosis.range30d') },
    { value: '90d', label: t('monitoring.diagnosis.range90d') },
    { value: 'custom', label: t('monitoring.diagnosis.rangeCustom') },
])
const selected = computed(() => options.value.find(o => o.value === props.range.preset) || options.value[2])

const isoDay = (d: Date) => d.toISOString().slice(0, 10)

const onPreset = (opt: { value: RangePreset }) => {
    if (opt.value === 'custom') {
        emit('change', { preset: 'custom', start: props.range.start, end: props.range.end })
        return
    }
    emit('change', rangeForPreset(opt.value))
}

const onCustom = (which: 'start' | 'end', value: string) => {
    if (!value) return
    const start = which === 'start' ? new Date(value + 'T00:00:00') : props.range.start
    const end = which === 'end' ? new Date(value + 'T23:59:59.999') : props.range.end
    if (end <= start) return
    emit('change', { preset: 'custom', start, end })
}
</script>
