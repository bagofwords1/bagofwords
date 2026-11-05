<template>
    <!-- The list view's row (the layout of pages/reports/index.vue): star,
         mode, title with its pills, a meta line, the dates, and the pencil. -->
    <li
        class="group flex items-center gap-3 px-4 py-3 bg-white dark:bg-gray-900 hover:bg-gray-50 dark:hover:bg-gray-800/60 transition-colors cursor-pointer"
        data-testid="dashboard-row"
        @click="navigateTo(link)"
    >
        <UButton
            color="gray"
            variant="link"
            square
            :padded="false"
            class="shrink-0 justify-center disabled:cursor-wait"
            :disabled="starPending"
            :aria-busy="starPending ? 'true' : undefined"
            :aria-label="starLabel"
            :aria-pressed="!!dashboard.is_starred"
            :title="starLabel"
            data-testid="dashboard-card-star"
            @click.stop="emit('toggle-star')"
        >
            <UIcon
                :name="dashboard.is_starred ? 'i-heroicons-star-solid' : 'i-heroicons-star'"
                class="h-4 w-4 transition-colors"
                :class="dashboard.is_starred ? 'text-amber-400 hover:text-amber-500' : 'text-ink-subtle hover:text-amber-400'"
                aria-hidden="true"
            />
        </UButton>

        <span class="flex items-center justify-center w-9 h-9 rounded-lg shrink-0" :class="modeStyle.cardBg">
            <UIcon :name="modeStyle.icon" class="w-5 h-5" :class="modeStyle.iconColor" aria-hidden="true" />
        </span>

        <div class="min-w-0 flex-1">
            <div class="flex items-center gap-1.5 flex-wrap">
                <NuxtLink
                    v-text-dir
                    :to="link"
                    class="text-sm font-medium text-ink hover:text-blue-600 truncate"
                    @click.stop
                >{{ title }}</NuxtLink>
                <ReportStatusDot :report-id="dashboard.report_id || dashboard.id" />
                <span
                    v-if="pill"
                    class="inline-flex items-center gap-1 px-2 py-0.5 text-2xs font-medium rounded-full"
                    :class="PILL_TONES[pill.tone]?.cls"
                    data-testid="dashboard-card-status"
                >
                    <UIcon v-if="PILL_TONES[pill.tone]?.icon" :name="PILL_TONES[pill.tone]!.icon!" class="w-3 h-3" aria-hidden="true" />
                    {{ t(pill.key) }}
                </span>
                <DashboardAccessLabel :dashboard="dashboard" variant="chip" />
                <UTooltip v-if="dashboard.cron_schedule" :text="dashboard.cron_schedule">
                    <span role="img" :aria-label="t('dashboards.card.scheduledRefresh')" class="inline-flex">
                        <UIcon
                            name="i-heroicons-clock"
                            class="h-3.5 w-3.5"
                            :class="failing ? 'text-red-600 dark:text-red-400' : 'text-ink-muted'"
                            aria-hidden="true"
                        />
                    </span>
                </UTooltip>
            </div>
            <div class="mt-0.5 flex items-center gap-1.5 text-xs text-ink-muted flex-wrap">
                <span>{{ t(modeStyle.label) }}</span>
                <template v-if="agents.length">
                    <span class="text-ink-subtle" aria-hidden="true">·</span>
                    <span class="inline-flex items-center gap-1.5">
                        <DataSourceIcon
                            v-for="agent in agents"
                            :key="agent.id"
                            :type="agent.type"
                            :icon-token="agent.icon_token"
                            class="h-3 inline"
                        />
                        <span v-text-dir>{{ agents.map(a => a.name).filter(Boolean).join(', ') }}</span>
                    </span>
                </template>
                <template v-if="shared && ownerName">
                    <span class="text-ink-subtle" aria-hidden="true">·</span>
                    <span v-text-dir>{{ t('dashboards.card.by', { name: ownerName }) }}</span>
                </template>
                <template v-if="failing">
                    <span class="text-ink-subtle" aria-hidden="true">·</span>
                    <span class="text-red-600 dark:text-red-400" :title="formatDateTime(dashboard.last_run_at)">{{ t('dashboards.card.refreshFailing') }}</span>
                </template>
                <template v-else-if="shared && dashboard.last_run_at">
                    <span class="text-ink-subtle" aria-hidden="true">·</span>
                    <span :title="formatDateTime(dashboard.last_run_at)">{{ t('dashboards.card.dataAsOf', { time: relativeTime(dashboard.last_run_at) }) }}</span>
                </template>
            </div>
        </div>

        <div class="shrink-0 hidden sm:block text-xs text-ink-muted text-end">
            <div v-if="editedAt" :title="t('dashboards.card.lastEdit', { time: formatDateTime(editedAt) })">{{ relativeTime(editedAt) }}</div>
            <div v-if="dashboard.created_at" :title="formatDateTime(dashboard.created_at)">{{ t('dashboards.list.createdShort', { date: formatDate(dashboard.created_at) }) }}</div>
        </div>

        <!-- While any dashboard is opening (busy), every pencil waits; the spinner is on the one being opened. -->
        <UButton
            v-if="editable"
            color="gray"
            variant="ghost"
            size="xs"
            square
            icon="i-heroicons-pencil-square"
            class="shrink-0 rounded-full text-ink-muted hover:bg-white dark:hover:bg-gray-700 disabled:cursor-wait"
            :loading="editing"
            :disabled="busy"
            :aria-busy="editing ? 'true' : undefined"
            :aria-label="t('dashboardCollab.list.edit')"
            :title="t('dashboardCollab.list.edit')"
            data-testid="dashboard-card-edit"
            @click.stop="emit('edit')"
        />
        <span v-else class="shrink-0 w-7" aria-hidden="true"></span>
    </li>
</template>

<script setup lang="ts">
import DashboardAccessLabel from '~/components/dashboards/DashboardAccessLabel.vue'
import DataSourceIcon from '~/components/DataSourceIcon.vue'
import ReportStatusDot from '~/components/ReportStatusDot.vue'
import { statusPill, type DashboardCardData } from '~/utils/dashboardList'

const props = defineProps<{
    dashboard: DashboardCardData
    tab: 'my' | 'shared'
    // This row's dashboard is being opened for editing.
    editing: boolean
    // Some dashboard is being opened for editing: every pencil waits.
    busy?: boolean
    // This row's star request is in flight.
    starPending?: boolean
}>()
const emit = defineEmits<{ (e: 'edit'): void; (e: 'toggle-star'): void }>()

const { t } = useI18n()
const { relativeTime } = useRelativeTime()
const { formatDate, formatDateTime } = useFormatDate()

const link = computed(() => `/d/${props.dashboard.slug || props.dashboard.id}`)
const title = computed(() => props.dashboard.title || t('dashboardCollab.page.untitled'))
const shared = computed(() => props.tab === 'shared')
const editable = computed(() => props.dashboard.role === 'owner' || props.dashboard.role === 'edit')
const failing = computed(() => props.dashboard.last_run_status === 'failed')
const editedAt = computed(() => props.dashboard.edited_at || props.dashboard.created_at)
const ownerName = computed(() => props.dashboard.owner?.name || '')
const agents = computed(() => props.dashboard.agents || [])
const pill = computed(() => statusPill(props.dashboard, props.tab))
const starLabel = computed(() => t(props.dashboard.is_starred ? 'dashboards.unstar' : 'dashboards.star'))

const MODE_STYLES: Record<string, { label: string; icon: string; cardBg: string; iconColor: string }> = {
    slides: { label: 'dashboards.mode.slides', icon: 'i-heroicons-presentation-chart-bar', cardBg: 'bg-purple-50 dark:bg-purple-950', iconColor: 'text-purple-300' },
    doc: { label: 'dashboards.mode.doc', icon: 'i-heroicons-document-text', cardBg: 'bg-emerald-50 dark:bg-emerald-950', iconColor: 'text-emerald-300' },
    page: { label: 'dashboards.mode.page', icon: 'i-heroicons-chart-bar-square', cardBg: 'bg-blue-50 dark:bg-blue-950', iconColor: 'text-blue-300' },
}
const modeStyle = computed(() => MODE_STYLES[props.dashboard.mode] ?? MODE_STYLES.page!)

const PILL_TONES: Record<string, { cls: string; icon?: string }> = {
    new: { cls: 'bg-violet-100 text-violet-700 dark:bg-violet-900/50 dark:text-violet-200' },
    changes: { cls: 'bg-amber-100 text-amber-700 dark:bg-amber-900/50 dark:text-amber-200' },
    published: { cls: 'bg-green-100 text-green-700 dark:bg-green-900/50 dark:text-green-200' },
    org: { cls: 'bg-blue-100 text-blue-700 dark:bg-blue-900/50 dark:text-blue-200', icon: 'i-heroicons-building-office' },
    public: { cls: 'bg-blue-100 text-blue-700 dark:bg-blue-900/50 dark:text-blue-200', icon: 'i-heroicons-globe-alt' },
}
</script>
