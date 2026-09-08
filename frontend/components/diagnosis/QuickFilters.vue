<template>
    <div class="flex items-center gap-2 mt-2.5 flex-wrap">
        <span class="text-xs text-gray-400 me-0.5">{{ $t('monitoring.diagnosis.quickFilters') }}</span>
        <button
            v-for="chip in chips"
            :key="chip.id"
            type="button"
            :title="chip.q"
            class="inline-flex items-center gap-1.5 h-6 px-2.5 rounded-full text-xs font-medium transition-colors"
            :class="chip.active
                ? 'bg-blue-50 dark:bg-blue-950 text-blue-700 dark:text-blue-300 ring-1 ring-blue-200 dark:ring-blue-800'
                : 'bg-gray-100 dark:bg-gray-800 text-gray-600 dark:text-gray-400 hover:bg-gray-200 dark:hover:bg-gray-700'"
            @click="$emit('toggle', chip)"
        >
            {{ $t(`monitoring.diagnosis.chip_${chip.id}`) }}
            <UIcon v-if="chip.active" name="i-heroicons-x-mark" class="w-3 h-3 text-blue-400" />
        </button>
    </div>
</template>

<script setup lang="ts">
import { containsTerms } from '~/utils/diagnosisQuery'

export interface Chip { id: string; q: string; active: boolean }

const props = defineProps<{ query: string; quickFilters: { id: string; q: string }[] }>()
defineEmits<{ (e: 'toggle', chip: Chip): void }>()

const chips = computed<Chip[]>(() =>
    props.quickFilters.map(f => ({ ...f, active: containsTerms(props.query, f.q) })),
)
</script>
