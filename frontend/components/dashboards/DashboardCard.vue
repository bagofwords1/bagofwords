<template>
    <div class="group relative" data-testid="dashboard-card">
        <!-- One card per dashboard: it opens the dashboard's /d page. -->
        <NuxtLink
            :to="`/d/${dashboard.slug || dashboard.id}`"
            class="block bg-white dark:bg-gray-900 rounded-xl border border-gray-200 dark:border-gray-700 overflow-hidden hover:shadow-lg hover:border-blue-300 transition"
        >
            <div class="aspect-[4/3] relative overflow-hidden" :class="showThumbnail ? '' : modeStyle.cardBg">
                <img
                    v-if="showThumbnail"
                    :src="thumbnailUrl!"
                    :alt="title"
                    class="w-full h-full object-cover group-hover:scale-105 transition-transform"
                    @error="imageError = true"
                />
                <div v-else class="w-full h-full flex items-center justify-center">
                    <UIcon :name="modeStyle.icon" class="w-12 h-12" :class="modeStyle.iconColor" aria-hidden="true" />
                </div>

                <!-- Mode and status, at the bottom-start; the star sits at the bottom-end. -->
                <div class="absolute bottom-2 start-2 flex items-center gap-1 overflow-hidden max-w-[calc(100%-3.25rem)]">
                    <span class="shrink-0 px-2 py-0.5 text-xs font-medium rounded-full" :class="[modeStyle.bg, modeStyle.text]">
                        {{ t(modeStyle.label) }}
                    </span>
                    <span
                        v-if="pill"
                        class="inline-flex items-center gap-1 min-w-0 px-2 py-0.5 text-xs font-medium rounded-full"
                        :class="PILL_TONES[pill.tone]?.cls"
                        data-testid="dashboard-card-status"
                    >
                        <UIcon v-if="PILL_TONES[pill.tone]?.icon" :name="PILL_TONES[pill.tone]!.icon!" class="w-3 h-3 shrink-0" aria-hidden="true" />
                        <span class="truncate">{{ $t(pill.key) }}</span>
                    </span>
                </div>

                <!-- How it reached the reader, at the top-start (Shared only). -->
                <DashboardAccessLabel
                    :dashboard="dashboard"
                    variant="pill"
                    class="absolute top-2 start-2"
                    :class="editable ? 'max-w-[calc(100%-3.25rem)]' : 'max-w-[calc(100%-1rem)]'"
                />
            </div>

            <div class="p-3 text-start">
                <h3 class="font-medium text-ink truncate text-sm flex items-center gap-1.5">
                    <span v-text-dir class="truncate">{{ title }}</span>
                    <ReportStatusDot :report-id="dashboard.report_id || dashboard.id" />
                    <span v-if="agents.length" class="ms-auto flex items-center gap-1 shrink-0">
                        <UTooltip v-for="agent in agents" :key="agent.id" :text="agent.name || ''">
                            <DataSourceIcon :type="agent.type" :icon-token="agent.icon_token" class="h-3.5" />
                        </UTooltip>
                    </span>
                </h3>

                <!-- My: when it was edited and created. Shared: who owns it,
                     and when it was edited -- or, reached only through the
                     organization, when it was published and how fresh its data is.
                     A part without its value is left out (cardMeta). -->
                <p v-if="meta.length" class="text-xs text-ink-muted mt-1 truncate">
                    <template v-for="(part, i) in meta" :key="part">
                        <template v-if="i"> · </template>
                        <span v-if="part === 'by'">{{ $t('dashboards.card.by', { name: isolate(ownerName) }) }}</span>
                        <span v-else-if="part === 'edited'" :title="$t('dashboards.card.lastEdit', { time: formatDateTime(editedAt) })">{{ $t('dashboards.card.edited', { time: relativeTime(editedAt) }) }}</span>
                        <span v-else-if="part === 'created'" :title="formatDateTime(dashboard.created_at)">{{ $t('dashboards.card.created', { date: formatDate(dashboard.created_at) }) }}</span>
                        <span v-else :title="formatDateTime(dashboard.published_at)">{{ $t('dashboards.card.published', { date: formatDate(dashboard.published_at) }) }}</span>
                    </template>
                </p>

                <!-- Refresh failing: only its editors get last_run_status (default 8). -->
                <p
                    v-if="refresh === 'failing'"
                    class="text-xs mt-0.5 truncate flex items-center gap-1.5 text-red-600 dark:text-red-400"
                    :title="formatDateTime(dashboard.last_run_at)"
                    data-testid="dashboard-card-refresh"
                >
                    <span class="w-1.5 h-1.5 rounded-full shrink-0 bg-red-500" aria-hidden="true"></span>
                    {{ $t('dashboards.card.refreshFailing') }}
                </p>
                <p
                    v-else-if="refresh === 'dataAsOf'"
                    class="text-xs mt-0.5 truncate flex items-center gap-1.5 text-ink-muted"
                    :title="formatDateTime(dashboard.last_run_at)"
                    data-testid="dashboard-card-refresh"
                >
                    <span class="w-1.5 h-1.5 rounded-full shrink-0 bg-emerald-500" aria-hidden="true"></span>
                    {{ $t('dashboards.card.dataAsOf', { time: relativeTime(dashboard.last_run_at) }) }}
                </p>
            </div>
        </NuxtLink>

        <!-- The thumbnail's corners, over the link (a button cannot sit inside it). -->
        <div class="pointer-events-none absolute inset-x-0 top-0 aspect-[4/3]">
            <!-- Edit: always shown to whoever may edit it (its owner, its editors).
                 While any dashboard is opening (busy), every pencil waits;
                 the spinner is on the one being opened.
                 Both corner chips stay white in either theme (they sit on the
                 thumbnail), so their icons keep a fixed grey: a text-ink-* token
                 flips light in dark mode and would vanish on the white chip. -->
            <UButton
                v-if="editable"
                color="white"
                variant="ghost"
                size="xs"
                square
                icon="i-heroicons-pencil-square"
                class="pointer-events-auto absolute top-2 end-2 rounded-full bg-white/90 hover:bg-white dark:hover:bg-white shadow-sm text-gray-600 dark:text-gray-600 disabled:cursor-wait"
                :loading="editing"
                :disabled="busy"
                :aria-busy="editing ? 'true' : undefined"
                :aria-label="$t('dashboardCollab.list.edit')"
                :title="$t('dashboardCollab.list.edit')"
                data-testid="dashboard-card-edit"
                @click.prevent.stop="emit('edit')"
            />
            <!-- Star: with a mouse, on hover (or focus), or always once starred.
                 On a touch screen there is no hover: always shown.
                 It waits while its request is in flight. -->
            <UButton
                color="white"
                variant="ghost"
                size="xs"
                square
                class="pointer-events-auto absolute bottom-2 end-2 rounded-full bg-white/90 hover:bg-white dark:hover:bg-white shadow-sm transition-opacity disabled:cursor-wait"
                :class="dashboard.is_starred ? '' : HOVER_REVEAL"
                :disabled="starPending"
                :aria-busy="starPending ? 'true' : undefined"
                :aria-label="starLabel"
                :aria-pressed="!!dashboard.is_starred"
                :title="starLabel"
                data-testid="dashboard-card-star"
                @click.prevent.stop="emit('toggle-star')"
            >
                <UIcon
                    :name="dashboard.is_starred ? 'i-heroicons-star-solid' : 'i-heroicons-star'"
                    class="w-4 h-4"
                    :class="dashboard.is_starred ? 'text-amber-400' : 'text-gray-500'"
                    aria-hidden="true"
                />
            </UButton>
        </div>
    </div>
</template>

<script setup lang="ts">
import DashboardAccessLabel from '~/components/dashboards/DashboardAccessLabel.vue'
import DataSourceIcon from '~/components/DataSourceIcon.vue'
import ReportStatusDot from '~/components/ReportStatusDot.vue'
import { cardMeta, isolate, refreshLine, statusPill, type DashboardCardData } from '~/utils/dashboardList'

const props = defineProps<{
    dashboard: DashboardCardData
    tab: 'my' | 'shared'
    // This card's dashboard is being opened for editing.
    editing: boolean
    // Some dashboard is being opened for editing: every pencil waits.
    busy?: boolean
    // This card's star request is in flight.
    starPending?: boolean
}>()
const emit = defineEmits<{ (e: 'edit'): void; (e: 'toggle-star'): void }>()

const { t } = useI18n()
const { relativeTime } = useRelativeTime()
const { formatDate, formatDateTime } = useFormatDate()

const title = computed(() => props.dashboard.title || t('dashboardCollab.page.untitled'))
const meta = computed(() => cardMeta(props.dashboard, props.tab))
const editable = computed(() => props.dashboard.role === 'owner' || props.dashboard.role === 'edit')
const refresh = computed(() => refreshLine(props.dashboard, props.tab))
// Hidden until hover only where the device can hover; focus shows it too.
const HOVER_REVEAL = '[@media(hover:hover)]:opacity-0 [@media(hover:hover)]:group-hover:opacity-100 [@media(hover:hover)]:focus:opacity-100'
const editedAt = computed(() => props.dashboard.edited_at || props.dashboard.created_at)
const ownerName = computed(() => props.dashboard.owner?.name || '')
const agents = computed(() => props.dashboard.agents || [])
const pill = computed(() => statusPill(props.dashboard, props.tab))
const starLabel = computed(() => t(props.dashboard.is_starred ? 'dashboards.unstar' : 'dashboards.star'))

const MODE_STYLES: Record<string, { label: string; icon: string; bg: string; text: string; cardBg: string; iconColor: string }> = {
    slides: { label: 'dashboards.mode.slides', icon: 'i-heroicons-presentation-chart-bar', bg: 'bg-purple-100', text: 'text-purple-700', cardBg: 'bg-purple-50 dark:bg-purple-950', iconColor: 'text-purple-300' },
    doc: { label: 'dashboards.mode.doc', icon: 'i-heroicons-document-text', bg: 'bg-emerald-100', text: 'text-emerald-700', cardBg: 'bg-emerald-50 dark:bg-emerald-950', iconColor: 'text-emerald-300' },
    page: { label: 'dashboards.mode.page', icon: 'i-heroicons-chart-bar-square', bg: 'bg-blue-100', text: 'text-blue-700', cardBg: 'bg-blue-50 dark:bg-blue-950', iconColor: 'text-blue-300' },
}
const modeStyle = computed(() => MODE_STYLES[props.dashboard.mode] ?? MODE_STYLES.page!)

const PILL_TONES: Record<string, { cls: string; icon?: string }> = {
    new: { cls: 'bg-violet-100 text-violet-700 dark:bg-violet-900/50 dark:text-violet-200' },
    changes: { cls: 'bg-amber-100 text-amber-700 dark:bg-amber-900/50 dark:text-amber-200' },
    published: { cls: 'bg-green-100 text-green-700 dark:bg-green-900/50 dark:text-green-200' },
    org: { cls: 'bg-blue-100 text-blue-700 dark:bg-blue-900/50 dark:text-blue-200', icon: 'i-heroicons-building-office' },
    public: { cls: 'bg-blue-100 text-blue-700 dark:bg-blue-900/50 dark:text-blue-200', icon: 'i-heroicons-globe-alt' },
}

const config = useRuntimeConfig()
const imageError = ref(false)
const thumbnailUrl = computed(() => props.dashboard.thumbnail_url ? `${config.public.baseURL}${props.dashboard.thumbnail_url}` : null)
const showThumbnail = computed(() => !!thumbnailUrl.value && !imageError.value)
watch(() => props.dashboard.thumbnail_url, () => { imageError.value = false })
</script>
