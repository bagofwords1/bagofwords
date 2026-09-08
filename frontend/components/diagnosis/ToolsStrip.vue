<template>
    <div v-if="tools.length" class="flex items-center gap-2 mt-4 overflow-x-auto">
        <span class="text-xs text-gray-400 me-0.5 flex-shrink-0">{{ $t('monitoring.diagnosis.toolsInRuns') }}</span>
        <button
            v-for="t in tools"
            :key="t.tool"
            type="button"
            :title="$t('monitoring.diagnosis.toolPillTitle', { tool: t.tool })"
            class="inline-flex items-center gap-2 h-7 px-2.5 rounded-md text-xs whitespace-nowrap ring-1 ring-inset transition-colors"
            :class="isActive(t.tool)
                ? 'bg-blue-50 dark:bg-blue-950 ring-blue-200 dark:ring-blue-800'
                : 'bg-white dark:bg-gray-900 ring-gray-200 dark:ring-gray-700 hover:bg-gray-50 dark:hover:bg-gray-800'"
            @click="$emit('pick', t.tool)"
        >
            <span class="font-mono" :class="isActive(t.tool) ? 'text-blue-700 dark:text-blue-300' : 'text-gray-900 dark:text-white'">{{ t.tool }}</span>
            <span class="text-gray-500">{{ t.calls.toLocaleString() }}</span>
            <span :class="t.errors ? 'text-red-700 dark:text-red-400' : 'text-gray-400'">{{ $t('monitoring.diagnosis.toolErrors', { n: t.errors }) }}</span>
            <span class="text-gray-500">{{ $t('monitoring.diagnosis.toolAvg', { d: fmtDuration(t.avg_ms) }) }}</span>
        </button>
    </div>
</template>

<script setup lang="ts">
import { fmtDuration, type ToolStat } from '~/composables/useDiagnosisQuery'
import { containsTerms } from '~/utils/diagnosisQuery'

const props = defineProps<{ tools: ToolStat[]; query: string }>()
defineEmits<{ (e: 'pick', tool: string): void }>()

const isActive = (tool: string) => containsTerms(props.query, `tool:${tool}`)
</script>
