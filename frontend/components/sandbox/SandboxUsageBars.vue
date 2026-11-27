<template>
    <!-- CPU, memory and disk of one sandbox, as far as its server reports
         them. A value the server did not give is left out; none at all is "—". -->
    <div v-if="lines.length" class="space-y-1 w-36" data-testid="sandbox-usage">
        <div v-for="line in lines" :key="line.key" class="text-xs" :data-usage="line.key">
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
    <span v-else class="bow-cell-empty" data-testid="sandbox-usage-none">—</span>
</template>

<script setup lang="ts">
import type { SandboxFleetRow } from '~/composables/useSandboxApi'

const props = defineProps<{ sandbox: SandboxFleetRow }>()

const { t } = useI18n()
const { bytes, percent } = useSandboxFormat()

const MB = 1024 * 1024
const known = (value: unknown): value is number => typeof value === 'number' && Number.isFinite(value)
const ratioOf = (used: number, limit: number | null) => (limit && limit > 0 ? Math.min(used / limit, 1) : null)

const lines = computed(() => {
    const usage = props.sandbox.usage
    if (!usage) return []
    const limits = props.sandbox.limits || {}
    const out: Array<{ key: string; label: string; text: string; ratio: number | null }> = []
    if (known(usage.cpu_percent)) {
        // 100% is one full CPU; the bar is the share of the CPUs it may use.
        const cpus = known(limits.cpus) && limits.cpus > 0 ? limits.cpus : null
        out.push({
            key: 'cpu', label: t('sandbox.sandboxes.usage.cpu'), text: percent(usage.cpu_percent),
            ratio: ratioOf(usage.cpu_percent, cpus ? cpus * 100 : null),
        })
    }
    const sized = (key: 'memory' | 'disk', used: unknown, limitBytes: unknown, limitMb: unknown) => {
        if (!known(used)) return
        const limit = known(limitBytes) && limitBytes > 0 ? limitBytes : known(limitMb) && limitMb > 0 ? limitMb * MB : null
        out.push({
            key, label: t(`sandbox.sandboxes.usage.${key}`),
            text: limit ? t('sandbox.sandboxes.usage.of', { used: bytes(used), limit: bytes(limit) }) : bytes(used),
            ratio: ratioOf(used, limit),
        })
    }
    sized('memory', usage.memory_bytes, usage.memory_limit_bytes, limits.memory_mb)
    sized('disk', usage.disk_bytes, usage.disk_limit_bytes, limits.disk_mb)
    return out
})
</script>
