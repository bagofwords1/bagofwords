<template>
    <!-- How a Shared dashboard reached the reader: its primary reason only
         (whether they can edit is the pencil's to say). The hover text gives
         every reason and what they may do. -->
    <span
        v-if="label"
        class="inline-flex items-center gap-1 min-w-0 rounded-full"
        :class="variant === 'pill'
            ? 'px-2 py-0.5 text-xs font-medium bg-white/90 dark:bg-gray-900/90 text-ink-2 shadow-sm'
            : 'border border-gray-200 dark:border-gray-700 px-2 py-0.5 text-2xs text-ink-muted'"
        :title="title"
        data-testid="dashboard-card-role"
    >
        <UIcon :name="label.icon" class="w-3 h-3 shrink-0" aria-hidden="true" />
        <!-- No v-text-dir: the label follows the page; only the name is isolated. -->
        <span class="truncate">{{ $t(label.key, isolateParams(label.params)) }}</span>
    </span>
</template>

<script setup lang="ts">
import { accessLabel, accessTitleParts, isolateParams, type DashboardCardData } from '~/utils/dashboardList'

const props = defineProps<{
    dashboard: DashboardCardData
    variant: 'pill' | 'chip'
}>()

const { t } = useI18n()

const label = computed(() => accessLabel(props.dashboard))

// "Dana shared it with you · also: Shared with everyone in the organization (view only)"
const title = computed(() => {
    const parts = accessTitleParts(props.dashboard)
    if (!parts.length) return undefined
    const role = parts[parts.length - 1]!
    const reasons = parts.slice(0, -1).map(p => t(p.key, isolateParams(p.params)))
    const said = [reasons[0], ...reasons.slice(1).map(reason => t('dashboards.reasonTitle.also', { reason }))]
    return `${said.join(' · ')} ${t(role.key, role.params)}`
})
</script>
