<template>
    <!-- What the sandboxes on one compute hold, out of what they may be given:
         the limits they were created with, not live usage. A resource the
         compute did not report is "—". -->
    <div class="w-40" data-testid="sandbox-compute-resources">
        <SandboxBars :lines="lines" />
        <div v-if="notRunning > 0" class="mt-1 text-xs text-ink-muted" data-testid="sandbox-compute-not-running">
            {{ $t('sandbox.computes.resources.notRunning', { count: notRunning }, notRunning) }}
        </div>
    </div>
</template>

<script setup lang="ts">
import type { SandboxBarLine, SandboxCompute } from '~/composables/useSandboxApi'
import SandboxBars from '~/components/sandbox/SandboxBars.vue'

const props = defineProps<{ compute: SandboxCompute }>()

const { t } = useI18n()
const { megabytes, number } = useSandboxFormat()

const known = (value: unknown): value is number => typeof value === 'number' && Number.isFinite(value)

const lines = computed<SandboxBarLine[]>(() => {
    const total = props.compute.allocatable || {}
    const held = props.compute.allocated || {}
    const line = (key: 'cpus' | 'memory_mb' | 'disk_mb', label: string, format: (value: number) => string): SandboxBarLine => {
        const limit = total[key]
        if (!known(limit)) return { key, label, text: '—', ratio: null, title: t('sandbox.computes.resources.unknown') }
        const used = known(held[key]) ? held[key]! : 0
        return {
            key, label,
            text: t('sandbox.sandboxes.usage.of', { used: format(used), limit: format(limit) }),
            ratio: limit > 0 ? Math.min(used / limit, 1) : (used > 0 ? 1 : 0),
        }
    }
    const capacity = props.compute.capacity
    const running = props.compute.running_sandboxes || 0
    return [
        line('cpus', t('sandbox.sandboxes.usage.cpu'), value => number(value)),
        line('memory_mb', t('sandbox.sandboxes.usage.memory'), megabytes),
        line('disk_mb', t('sandbox.sandboxes.usage.disk'), megabytes),
        {
            key: 'running', label: t('sandbox.computes.resources.running'),
            text: t('sandbox.sandboxes.usage.of', { used: number(running), limit: number(capacity) }),
            ratio: capacity > 0 ? Math.min(running / capacity, 1) : null,
        },
    ]
})

// Stopped (and failed) sandboxes: no CPU or memory, but their disk is still held.
const notRunning = computed(() => {
    const existing = props.compute.allocated?.existing
    return known(existing) ? Math.max(0, existing - (props.compute.running_sandboxes || 0)) : 0
})
</script>
