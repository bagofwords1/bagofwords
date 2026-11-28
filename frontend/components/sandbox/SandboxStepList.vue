<template>
    <!-- Bootstrap steps and connection checks share one shape:
         key / title / status / detail. -->
    <ol class="rounded-lg border border-line divide-y divide-line overflow-hidden">
        <li
            v-for="item in items"
            :key="item.key"
            class="flex items-start gap-3 px-4 py-2.5"
            :data-testid="`sandbox-step-${item.key}`"
            :data-status="item.status"
        >
            <span class="flex items-center justify-center w-5 h-5 shrink-0 mt-px">
                <Spinner v-if="item.status === 'running'" class="w-4 h-4 text-blue-600 dark:text-blue-400" />
                <UIcon v-else :name="look(item.status).icon" :class="['w-5 h-5', look(item.status).class]" />
            </span>
            <div class="min-w-0 flex-1">
                <div class="flex items-center justify-between gap-3">
                    <span :class="['text-sm', item.status === 'pending' || item.status === 'skipped' ? 'text-ink-muted' : 'text-ink font-medium']">{{ item.title }}</span>
                    <span class="text-2xs text-ink-muted shrink-0">{{ $t(`sandbox.stepStatus.${statusKey(item.status)}`) }}</span>
                </div>
                <p
                    v-if="item.detail"
                    dir="auto"
                    :class="['mt-0.5 text-xs leading-relaxed break-words',
                        item.status === 'failed' ? 'text-red-600 dark:text-red-400'
                        : item.status === 'warning' ? 'text-amber-700 dark:text-amber-300' : 'text-ink-muted']"
                    :data-testid="`sandbox-step-detail-${item.key}`"
                >{{ item.detail }}</p>
            </div>
        </li>
    </ol>
</template>

<script setup lang="ts">
import Spinner from '~/components/Spinner.vue'

defineProps<{
    items: Array<{ key: string; title: string; status: string; detail?: string | null }>
}>()

const LOOKS: Record<string, { icon: string; class: string }> = {
    pending: { icon: 'i-heroicons-ellipsis-horizontal-circle', class: 'text-ink-subtle' },
    ok: { icon: 'i-heroicons-check-circle', class: 'text-green-600 dark:text-green-400' },
    warning: { icon: 'i-heroicons-exclamation-triangle', class: 'text-amber-500 dark:text-amber-400' },
    failed: { icon: 'i-heroicons-x-circle', class: 'text-red-600 dark:text-red-400' },
    skipped: { icon: 'i-heroicons-minus-circle', class: 'text-ink-subtle' },
}
const look = (status: string) => LOOKS[status] || LOOKS.pending
const statusKey = (status: string) => (status === 'running' || status in LOOKS ? status : 'pending')
</script>
