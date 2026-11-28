<template>
    <!-- Labelled values with a thin bar under each: used for a sandbox's live
         usage and for what a compute's sandboxes hold. A line without a ratio
         has no bar. -->
    <div class="space-y-1">
        <div v-for="line in lines" :key="line.key" class="text-xs" :data-bar="line.key" :title="line.title">
            <div class="flex items-baseline justify-between gap-2 whitespace-nowrap">
                <span class="text-ink-muted">{{ line.label }}</span>
                <span class="nums text-ink-2" dir="ltr">{{ line.text }}</span>
            </div>
            <div
                v-if="line.ratio !== null"
                class="mt-0.5 h-1 rounded-full bg-surface-muted overflow-hidden"
                role="progressbar"
                :aria-label="line.label"
                aria-valuemin="0"
                aria-valuemax="100"
                :aria-valuenow="Math.round(line.ratio * 100)"
            >
                <div
                    class="h-full rounded-full"
                    :class="line.ratio >= 0.9 ? 'bg-red-500' : line.ratio >= 0.75 ? 'bg-amber-500' : 'bg-blue-500'"
                    :style="{ width: `${Math.max(line.ratio * 100, line.ratio > 0 ? 2 : 0)}%` }"
                />
            </div>
        </div>
    </div>
</template>

<script setup lang="ts">
import type { SandboxBarLine } from '~/composables/useSandboxApi'

defineProps<{ lines: SandboxBarLine[] }>()
</script>
