<template>
    <!-- Things an admin should know about an environment as saved: a failing
         setup or start script, and whatever else the server warns about.
         Compact on the list row, with the script's last error in the form. -->
    <div
        v-if="lines.length"
        :class="detailed
            ? 'rounded-lg border border-amber-200 dark:border-amber-900/60 bg-amber-50 dark:bg-amber-950/40 px-4 py-3 text-sm text-amber-800 dark:text-amber-200'
            : 'text-xs text-amber-700 dark:text-amber-300'"
        role="status"
    >
        <ul class="space-y-1">
            <li v-for="line in lines" :key="line.key" class="flex items-start gap-1.5" :data-warning="line.key">
                <UIcon name="i-heroicons-exclamation-triangle" :class="['shrink-0 mt-0.5', detailed ? 'w-4 h-4' : 'w-3.5 h-3.5']" />
                <span class="min-w-0 break-words" :class="detailed ? '' : 'line-clamp-3'" :dir="line.server ? 'auto' : undefined" :title="detailed ? undefined : line.text">{{ line.text }}</span>
            </li>
        </ul>
        <template v-if="detailed && failures && environment.hook_last_error">
            <div class="mt-2 text-xs font-medium">{{ $t('sandbox.environments.hookFailing.lastError') }}</div>
            <pre dir="ltr" class="mt-1 max-h-32 overflow-auto rounded-md bg-surface px-3 py-2 text-xs font-mono whitespace-pre-wrap break-words text-start text-ink-2" data-testid="sandbox-environment-hook-error">{{ environment.hook_last_error }}</pre>
        </template>
    </div>
</template>

<script setup lang="ts">
import { SANDBOX_HOOK_FAILURE_CAP, type SandboxEnvironment } from '~/composables/useSandboxApi'

const props = defineProps<{ environment: SandboxEnvironment; detailed?: boolean }>()

const { t } = useI18n()

const failures = computed(() => Number(props.environment.hook_failures || 0))

const lines = computed(() => {
    const out: Array<{ key: string; text: string; server?: boolean }> = []
    if (failures.value >= SANDBOX_HOOK_FAILURE_CAP) {
        out.push({ key: 'hook_refused', text: t('sandbox.environments.hookFailing.refused', { count: failures.value }) })
    } else if (failures.value > 0) {
        out.push({
            key: 'hook_failing',
            text: t('sandbox.environments.hookFailing.failing', { count: failures.value, cap: SANDBOX_HOOK_FAILURE_CAP }, failures.value),
        })
    }
    for (const warning of props.environment.warnings || []) {
        // The server's sentence about the failing script is the line above, in
        // the reader's language. If its wording changes it is shown as well.
        if (failures.value && /^Its setup or start script failed/.test(warning)) continue
        out.push({ key: warning, text: warning, server: true })
    }
    return out
})
</script>
