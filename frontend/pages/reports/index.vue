<template>
    <div class="flex justify-center ps-2 md:ps-4 text-sm">
        <div class="w-full max-w-4xl px-4 ps-0 py-8">
            <!-- Header -->
            <div class="flex items-center justify-between gap-4 mb-6">
                <h1 class="text-xl font-semibold text-gray-900 dark:text-white">
                    <GoBackChevron v-if="isExcel" />
                    {{ $t('reports.title') }}
                </h1>
                <button
                    type="button"
                    name="new-report"
                    @click="createNewReport"
                    :disabled="creatingReport"
                    class="inline-flex items-center gap-1.5 rounded-lg bg-blue-600 hover:bg-blue-700 text-white text-sm font-medium px-3 py-1.5 transition-colors disabled:opacity-50 disabled:cursor-not-allowed"
                >
                    <Spinner v-if="creatingReport" class="animate-spin w-4 h-4" />
                    <UIcon v-else name="i-heroicons-plus" class="w-4 h-4" />
                    {{ creatingReport ? $t('common.loading') : $t('reports.newReport') }}
                </button>
            </div>

            <!-- Toolbar: scope tabs, search, filters -->
            <!-- Scope tabs, with search + filters riding on the same rule.
                 Underlined tabs keep /reports consistent with /dashboards and
                 /queries; the trailing controls follow the ms-auto pattern
                 /dashboards already uses on its tab row. -->
            <div class="border-b border-gray-200 dark:border-gray-800 mb-3">
                <nav class="-mb-px flex items-center gap-6">
                    <button
                        type="button"
                        class="whitespace-nowrap border-b-2 py-2 px-1 text-sm transition-colors"
                        :class="activeFilter === 'my'
                            ? 'border-blue-600 text-blue-600'
                            : 'border-transparent text-gray-500 dark:text-gray-400 hover:border-gray-300 dark:hover:border-gray-600 hover:text-gray-700 dark:hover:text-gray-300'"
                        @click="setActiveFilter('my')"
                    >
                        {{ $t('reports.myReports') }}
                    </button>
                    <button
                        type="button"
                        class="whitespace-nowrap border-b-2 py-2 px-1 text-sm transition-colors"
                        :class="activeFilter === 'shared'
                            ? 'border-blue-600 text-blue-600'
                            : 'border-transparent text-gray-500 dark:text-gray-400 hover:border-gray-300 dark:hover:border-gray-600 hover:text-gray-700 dark:hover:text-gray-300'"
                        @click="setActiveFilter('shared')"
                    >
                        {{ $t('reports.sharedWithMe') }}
                    </button>

                    <div class="ms-auto flex items-center gap-2 pb-1.5">
                        <!-- Search -->
                        <div class="relative w-40 sm:w-64">
                            <input
                                v-model="searchTerm"
                                type="text"
                                :placeholder="$t('reports.searchPlaceholder')"
                                class="w-full ps-8 pe-3 py-1.5 text-sm rounded-lg border border-gray-200 dark:border-gray-700 bg-white dark:bg-gray-900 placeholder:text-gray-400 focus:outline-none focus:ring-2 focus:ring-blue-600 focus:border-transparent"
                            />
                            <UIcon
                                name="i-heroicons-magnifying-glass"
                                class="absolute start-2.5 top-1/2 -translate-y-1/2 h-4 w-4 text-gray-400"
                            />
                        </div>

                        <!-- Filters (My reports only) -->
                        <div v-if="activeFilter === 'my'" class="relative shrink-0" ref="filtersRef">
                            <UTooltip :text="$t('reports.filtersButton')">
                                <button
                                    type="button"
                                    @click="showFilters = !showFilters"
                                    class="relative inline-flex items-center justify-center w-8 h-8 rounded-lg border transition-colors"
                                    :class="showFilters || activeFilterCount > 0
                                        ? 'border-blue-200 dark:border-blue-900 bg-blue-50 dark:bg-blue-950 text-blue-700 dark:text-blue-300'
                                        : 'border-gray-200 dark:border-gray-700 text-gray-500 dark:text-gray-400 hover:bg-gray-50 dark:hover:bg-gray-800'"
                                >
                                    <UIcon name="i-heroicons-funnel" class="h-4 w-4" />
                                    <span
                                        v-if="activeFilterCount > 0"
                                        class="absolute -top-1 -end-1 inline-flex items-center justify-center min-w-[15px] h-[15px] px-1 text-[10px] font-semibold rounded-full bg-blue-600 text-white"
                                    >
                                        {{ activeFilterCount }}
                                    </span>
                                </button>
                            </UTooltip>

                            <!-- Filters popover -->
                            <div
                                v-if="showFilters"
                                class="absolute end-0 z-20 mt-2 w-[360px] bg-white dark:bg-gray-900 border border-gray-200 dark:border-gray-800 rounded-xl shadow-lg p-4"
                            >
                                <div class="space-y-3">
                                    <div class="flex items-center justify-between gap-3">
                                        <span class="text-xs text-gray-500 dark:text-gray-400">{{ $t('reports.filters.typeLabel') }}</span>
                                        <USelectMenu
                                            :model-value="typeFilter"
                                            @update:model-value="setTypeFilter"
                                            :options="typeFilterOptions"
                                            value-attribute="value"
                                            option-attribute="label"
                                            class="w-48"
                                        >
                                            <template #label>
                                                <span class="text-xs whitespace-nowrap">{{ selectedTypeLabel }}</span>
                                            </template>
                                        </USelectMenu>
                                    </div>
                                    <div class="flex items-center justify-between gap-3">
                                        <span class="text-xs text-gray-500 dark:text-gray-400">{{ $t('reports.filters.statusLabel') }}</span>
                                        <USelectMenu
                                            :model-value="statusFilter"
                                            @update:model-value="setStatusFilter"
                                            :options="statusFilterOptions"
                                            value-attribute="value"
                                            option-attribute="label"
                                            class="w-48"
                                        >
                                            <template #label>
                                                <span class="text-xs whitespace-nowrap">{{ selectedStatusLabel }}</span>
                                            </template>
                                        </USelectMenu>
                                    </div>
                                    <div class="flex items-center justify-between gap-3">
                                        <span class="text-xs text-gray-500 dark:text-gray-400">{{ $t('reports.filters.scheduleLabel') }}</span>
                                        <USelectMenu
                                            :model-value="scheduledFilter"
                                            @update:model-value="setScheduledFilter"
                                            :options="scheduleFilterOptions"
                                            value-attribute="value"
                                            option-attribute="label"
                                            class="w-48"
                                        >
                                            <template #label>
                                                <span class="text-xs whitespace-nowrap">{{ selectedScheduleLabel }}</span>
                                            </template>
                                        </USelectMenu>
                                    </div>
                                    <div class="flex items-center justify-between gap-3">
                                        <span class="text-xs text-gray-500 dark:text-gray-400">{{ $t('reports.filters.dataSourceLabel') }}</span>
                                        <USelectMenu
                                            :model-value="dataSourceFilter"
                                            @update:model-value="setDataSourceFilter"
                                            :options="dataSourceFilterOptions"
                                            value-attribute="value"
                                            option-attribute="label"
                                            class="w-48"
                                        >
                                            <template #label>
                                                <span class="text-xs whitespace-nowrap truncate">{{ selectedDataSourceLabel }}</span>
                                            </template>
                                        </USelectMenu>
                                    </div>
                                    <div class="flex items-center justify-between gap-3">
                                        <span class="text-xs text-gray-500 dark:text-gray-400">{{ $t('reports.filters.artifactsLabel') }}</span>
                                        <USelectMenu
                                            :model-value="artifactFilter"
                                            @update:model-value="setArtifactFilter"
                                            :options="artifactFilterOptions"
                                            value-attribute="value"
                                            option-attribute="label"
                                            class="w-48"
                                        >
                                            <template #label>
                                                <span class="text-xs whitespace-nowrap">{{ selectedArtifactLabel }}</span>
                                            </template>
                                        </USelectMenu>
                                    </div>
                                </div>
                                <div v-if="activeFilterCount > 0" class="mt-4 pt-3 border-t border-gray-100 dark:border-gray-800 flex justify-end">
                                    <button
                                        type="button"
                                        @click="clearFilters"
                                        class="text-xs font-medium text-gray-500 dark:text-gray-400 hover:text-gray-700 dark:hover:text-gray-300"
                                    >
                                        {{ $t('reports.filters.clear') }}
                                    </button>
                                </div>
                            </div>
                        </div>
                    </div>
                </nav>
            </div>

            <!-- Directory list -->
            <div
                v-if="isLoading || visibleReports.length"
                class="divide-y divide-gray-100 dark:divide-gray-800 rounded-xl border border-gray-200 dark:border-gray-800 overflow-hidden"
            >
                <!-- Selection strip: doubles as the select-all control and the
                     bulk-action bar, so the Actions dropdown can stay off the toolbar. -->
                <div
                    v-if="activeFilter === 'my' && !isLoading && visibleReports.length"
                    class="flex items-center gap-2 px-4 py-2 bg-gray-50 dark:bg-gray-800/40 text-xs text-gray-500 dark:text-gray-400"
                >
                    <input
                        type="checkbox"
                        :checked="allVisibleSelected"
                        @change="toggleAllVisible"
                        class="rounded border-gray-300 dark:border-gray-600 text-blue-600 focus:ring-blue-600"
                    />
                    <span v-if="selectedIds.size > 0">{{ selectedIds.size }} selected</span>
                    <span v-else>Select all</span>
                    <button
                        v-if="selectedIds.size > 0"
                        type="button"
                        @click="archiveSelected"
                        class="ms-auto inline-flex items-center gap-1 rounded-md px-2 py-1 font-medium text-gray-600 dark:text-gray-300 hover:bg-white dark:hover:bg-gray-800 hover:text-red-600 dark:hover:text-red-400 transition-colors"
                    >
                        <UIcon name="i-heroicons-archive-box" class="w-3.5 h-3.5" />
                        {{ $t('reports.archiveSelected') }}
                    </button>
                </div>

                <!-- Loading skeleton -->
                <template v-if="isLoading">
                    <div v-for="i in 5" :key="i" class="flex items-center gap-3 px-4 py-3 bg-white dark:bg-gray-900">
                        <div class="w-9 h-9 rounded-lg bg-gray-100 dark:bg-gray-800 animate-pulse shrink-0"></div>
                        <div class="flex-1 space-y-2">
                            <div class="h-3 w-1/3 rounded bg-gray-100 dark:bg-gray-800 animate-pulse"></div>
                            <div class="h-2.5 w-1/5 rounded bg-gray-100 dark:bg-gray-800 animate-pulse"></div>
                        </div>
                    </div>
                </template>

                <!-- Report rows -->
                <ul v-else class="divide-y divide-gray-100 dark:divide-gray-800">
                    <!-- Draggable onto a project row in the sidebar
                         (see layouts/default.vue) — same move as the
                         sidebar row menu's "Move to project". Only the
                         owner can file a report, which is exactly the
                         "My reports" tab; the other tabs would drag
                         into a 403. -->
                    <li
                        v-for="report in visibleReports"
                        :key="report.id"
                        @click="goToReport(report)"
                        :draggable="activeFilter === 'my'"
                        @dragstart="startReportDrag($event, report)"
                        @dragend="endReportDrag"
                        :data-testid="`report-row-${report.id}`"
                        class="group flex items-center gap-3 px-4 py-3 bg-white dark:bg-gray-900 hover:bg-gray-50 dark:hover:bg-gray-800/60 transition-colors cursor-pointer"
                        :class="draggingReport?.id === report.id ? 'opacity-50' : ''"
                    >
                        <!-- Bulk select (My reports) -->
                        <input
                            v-if="activeFilter === 'my'"
                            type="checkbox"
                            :checked="selectedIds.has(report.id)"
                            @change="toggleOne(report.id)"
                            @click.stop
                            class="shrink-0 rounded border-gray-300 dark:border-gray-600 text-blue-600 focus:ring-blue-600 opacity-0 group-hover:opacity-100 checked:opacity-100 transition-opacity"
                        />

                        <!-- Star -->
                        <UTooltip :text="report.is_starred ? $t('reports.tooltips.unstar') : $t('reports.tooltips.star')">
                            <button
                                type="button"
                                @click.stop="toggleStar(report)"
                                class="inline-flex items-center justify-center focus:outline-none"
                            >
                                <UIcon
                                    :name="report.is_starred ? 'i-heroicons-star-solid' : 'i-heroicons-star'"
                                    class="h-4 w-4 transition-colors"
                                    :class="report.is_starred ? 'text-amber-400 hover:text-amber-500' : 'text-gray-300 dark:text-gray-600 hover:text-amber-400'"
                                />
                            </button>
                        </UTooltip>

                        <!-- Type avatar -->
                        <span class="flex items-center justify-center w-9 h-9 rounded-lg bg-gray-50 dark:bg-gray-800 shrink-0">
                            <UIcon :name="reportTypeIcon(report)" class="w-5 h-5 text-gray-400 dark:text-gray-500" />
                        </span>

                        <!-- Title block -->
                        <div class="min-w-0 flex-1">
                            <div class="flex items-center gap-1.5 flex-wrap">
                                <NuxtLink
                                    v-if="reportLink(report)"
                                    :to="reportLink(report)"
                                    @click.stop
                                    class="text-sm font-medium text-gray-900 dark:text-white hover:text-blue-600 truncate"
                                >
                                    {{ report.title }}
                                </NuxtLink>
                                <span
                                    v-else
                                    @click.stop
                                    class="text-sm font-medium text-gray-900 dark:text-white truncate"
                                >
                                    {{ report.title }}
                                </span>
                                <ReportStatusDot :report-id="report.id" />
                                <!-- Visibility badges -->
                                <UTooltip v-if="report.artifact_modes?.length > 0" :text="report.artifact_visibility !== 'none' ? $t('reports.dashboardWithVisibility', { visibility: visibilityLabel(report.artifact_visibility) }) : $t('reports.dashboardPrivate')">
                                    <span class="inline-flex items-center gap-1 rounded-full border border-gray-200 dark:border-gray-700 px-2 py-0.5 text-[11px] text-gray-400 dark:text-gray-500">
                                        {{ $t('reports.dashboardLabel') }}
                                        <UIcon v-if="report.artifact_visibility !== 'none'" :name="visibilityIcon(report.artifact_visibility)" class="w-3 h-3" />
                                    </span>
                                </UTooltip>
                                <UTooltip v-if="report.conversation_visibility !== 'none'" :text="visibilityLabel(report.conversation_visibility)">
                                    <span class="inline-flex items-center gap-1 rounded-full border border-gray-200 dark:border-gray-700 px-2 py-0.5 text-[11px] text-gray-400 dark:text-gray-500">
                                        {{ $t('reports.conversationLabel') }}
                                        <UIcon :name="visibilityIcon(report.conversation_visibility)" class="w-3 h-3" />
                                    </span>
                                </UTooltip>
                                <!-- Platform icons -->
                                <img v-if="report.external_platform?.platform_type === 'slack'" src="/icons/slack.png" class="h-3 inline" />
                                <img v-if="report.external_platform?.platform_type === 'teams'" src="/icons/teams.png" class="h-3 inline" />
                                <UTooltip v-if="report.external_platform?.platform_type === 'mcp'" :text="$t('reports.tooltips.createdViaMcp')">
                                    <img src="/icons/mcp.png" class="h-3 inline" />
                                </UTooltip>
                                <UTooltip v-if="report.external_platform?.platform_type === 'excel'" :text="$t('reports.tooltips.createdViaExcel')">
                                    <img src="/data_sources_icons/excel.png" class="h-3 inline" />
                                </UTooltip>
                                <UTooltip v-if="report.cron_schedule && !report.has_scheduled_prompts" :text="$t('reports.tooltips.runningOnSchedule')">
                                    <UIcon name="i-heroicons-clock" class="h-3.5 w-3.5 text-gray-400" />
                                </UTooltip>
                                <!-- Trigger provenance: session spawned by a trigger delivery -->
                                <UTooltip v-if="report.webhook_id" :text="$t('reports.tooltips.createdByTrigger')">
                                    <UIcon name="i-heroicons-bolt-solid" class="h-3.5 w-3.5 text-amber-500" data-testid="trigger-origin-icon" />
                                </UTooltip>
                                <!-- Scheduled-run provenance: report spawned by a scheduled task run -->
                                <UTooltip v-if="report.scheduled_prompt_id" :text="$t('reports.tooltips.createdBySchedule')">
                                    <UIcon name="i-heroicons-clock-solid" class="h-3.5 w-3.5 text-sky-500" data-testid="schedule-origin-icon" />
                                </UTooltip>
                            </div>
                            <!-- Type sub-label + data sources + metrics -->
                            <div class="mt-0.5 flex items-center gap-1.5 text-xs text-gray-400 dark:text-gray-500 flex-wrap">
                                <span class="inline-flex items-center gap-1">
                                    <UIcon :name="reportTypeIcon(report)" class="h-3.5 w-3.5" />
                                    {{ reportTypeLabel(report) }}
                                </span>
                                <template v-if="report.data_sources.length">
                                    <span class="text-gray-300 dark:text-gray-600">·</span>
                                    <span class="inline-flex items-center gap-1.5">
                                        <UTooltip
                                            v-for="data_source in report.data_sources.slice(0, 2)"
                                            :key="data_source.id || data_source.name"
                                            :text="data_source.name"
                                        >
                                            <DataSourceIcon :type="data_source.type" :icon="data_source.icon" class="h-3 inline" />
                                        </UTooltip>
                                        <UTooltip
                                            v-if="report.data_sources.length > 2"
                                            :text="report.data_sources.slice(2).map(d => d.name).join(', ')"
                                        >
                                            <span class="text-[11px]">+{{ report.data_sources.length - 2 }}</span>
                                        </UTooltip>
                                    </span>
                                </template>
                                <template v-if="report.query_count || report.artifact_count || report.scheduled_prompt_count || report.instruction_count || report.webhook_count">
                                    <span class="text-gray-300 dark:text-gray-600">·</span>
                                    <span class="inline-flex items-center gap-1.5 flex-wrap">
                                        <span v-if="report.query_count" class="inline-flex items-center gap-1">
                                            <UIcon name="i-heroicons-chat-bubble-left-right" class="w-3 h-3" />
                                            {{ report.query_count }}
                                        </span>
                                        <span v-if="report.artifact_count" class="inline-flex items-center gap-1">
                                            <UIcon name="i-heroicons-chart-bar-square" class="w-3 h-3" />
                                            {{ report.artifact_count }}
                                        </span>
                                        <span v-if="report.scheduled_prompt_count" class="inline-flex items-center gap-1">
                                            <UIcon name="i-heroicons-clock" class="w-3 h-3" />
                                            {{ report.scheduled_prompt_count }}
                                        </span>
                                        <span v-if="report.instruction_count" class="inline-flex items-center gap-1">
                                            <UIcon name="i-heroicons-academic-cap" class="w-3 h-3" />
                                            {{ report.instruction_count }}
                                        </span>
                                        <span v-if="report.webhook_count" class="inline-flex items-center gap-1">
                                            <UIcon name="i-heroicons-bolt" class="w-3 h-3" />
                                            {{ report.webhook_count }}
                                        </span>
                                    </span>
                                </template>
                            </div>
                        </div>

                        <!-- Right metadata: date -->
                        <div class="shrink-0 hidden sm:block text-xs text-gray-400 dark:text-gray-500">
                            {{ formatDate(report.created_at) }}
                        </div>

                        <!-- Archive action -->
                        <div class="shrink-0 w-6 flex justify-end">
                            <UTooltip v-if="canDeleteReport(report)" :text="$t('reports.archive')">
                                <button
                                    type="button"
                                    @click.stop="confirmDelete(report.id)"
                                    class="text-gray-300 dark:text-gray-600 hover:text-red-600 opacity-0 group-hover:opacity-100 transition-opacity"
                                >
                                    <UIcon name="i-heroicons-archive-box" class="w-4 h-4" />
                                </button>
                            </UTooltip>
                        </div>
                    </li>
                </ul>
            </div>

            <!-- Empty state -->
            <div v-else class="text-center py-16 border border-dashed border-gray-200 dark:border-gray-800 rounded-xl">
                <UIcon name="i-heroicons-document-text" class="w-8 h-8 text-gray-300 dark:text-gray-600 mx-auto mb-2" />
                <p class="text-sm font-medium text-gray-900 dark:text-white">
                    {{ activeFilter === 'shared' ? $t('reports.sharedEmpty') : $t('reports.empty') }}
                </p>
                <!-- These reports belong to other people, so the shared tab gets
                     no create button: a new report would not land in this list. -->
                <p
                    class="text-sm text-gray-400 dark:text-gray-500"
                    :class="activeFilter === 'shared' ? '' : 'mb-4'"
                >
                    {{ activeFilter === 'shared' ? $t('reports.sharedEmptyHint') : $t('reports.emptyHint') }}
                </p>
                <button
                    v-if="activeFilter !== 'shared'"
                    type="button"
                    @click="createNewReport"
                    :disabled="creatingReport"
                    class="inline-flex items-center gap-1.5 rounded-lg border border-gray-200 dark:border-gray-700 text-sm text-gray-700 dark:text-gray-200 px-3 py-1.5 hover:bg-gray-50 dark:hover:bg-gray-800 transition-colors disabled:opacity-50"
                >
                    <UIcon name="i-heroicons-plus" class="w-4 h-4" />
                    {{ $t('reports.newReport') }}
                </button>
            </div>

            <!-- Pagination. The row appears as soon as there are more reports
                 than the smallest page size, not just when there are two pages:
                 raising the size to 50 can collapse the list to a single page,
                 and hiding the row then would strip the only way back to 10. -->
            <div
                v-if="!isLoading && visibleReports.length && pagination.total > rowsPerPageOptions[0]"
                class="mt-3 flex items-center justify-between gap-3 text-xs text-gray-400 dark:text-gray-500"
            >
                <div class="flex items-center gap-1.5">
                    <span class="whitespace-nowrap">{{ $t('reports.pagination.rowsPerPage') }}</span>
                    <USelectMenu
                        :model-value="pagination.limit"
                        @update:model-value="setRowsPerPage"
                        :options="rowsPerPageOptions"
                        size="xs"
                        class="w-[72px]"
                    />
                </div>

                <div v-if="pagination.total_pages > 1" class="flex items-center gap-1">
                <button
                    type="button"
                    class="p-1 rounded hover:bg-gray-100 dark:hover:bg-gray-800 disabled:opacity-40 disabled:hover:bg-transparent"
                    :disabled="currentPage <= 1"
                    @click="changePage(currentPage - 1)"
                    :aria-label="$t('common.previous')"
                >
                    <UIcon name="i-heroicons-chevron-left" class="w-3.5 h-3.5 rtl-flip" />
                </button>
                <span>{{ currentPage }} / {{ pagination.total_pages }}</span>
                <button
                    type="button"
                    class="p-1 rounded hover:bg-gray-100 dark:hover:bg-gray-800 disabled:opacity-40 disabled:hover:bg-transparent"
                    :disabled="currentPage >= pagination.total_pages"
                    @click="changePage(currentPage + 1)"
                    :aria-label="$t('common.next')"
                >
                    <UIcon name="i-heroicons-chevron-right" class="w-3.5 h-3.5 rtl-flip" />
                </button>
                </div>
            </div>
        </div>
    </div>
</template>

<script setup lang="ts">
import GoBackChevron from '@/components/excel/GoBackChevron.vue'
import Spinner from '@/components/Spinner.vue'
const creatingReport = ref(false)

const { t } = useI18n()
const { data: currentUser } = useAuth()
const toast = useToast()
const { fetchActivity, sortByActivity } = useReportActivity()
// Rows are drag sources; the sidebar's project rows are the drop targets.
const { draggingReport, startReportDrag, endReportDrag } = useReportDrag()
const router = useRouter()
const { selectedAgentObjects } = useAgent()

definePageMeta({ auth: true })

const reports = ref<any[]>([])
const activeFilter = ref<'my' | 'shared' | 'published'>('my')
const currentPage = ref(1)
const isLoading = ref(true)
const pagination = ref({
    total: 0,
    page: 1,
    limit: 10,
    total_pages: 0,
    has_next: false,
    has_prev: false,
})
const searchTerm = ref('')
const selectedIds = ref<Set<string>>(new Set())
const statusFilter = ref<'all' | 'draft' | 'published'>('all')
const scheduledFilter = ref<boolean | null>(null)
const typeFilter = ref<string>('all')
const dataSourceFilter = ref<string>('all')
const artifactFilter = ref<string>('all')
const dataSources = ref<any[]>([])
const showFilters = ref(false)
const filtersRef = ref<HTMLElement | null>(null)
const rowsPerPageOptions = [10, 25, 50, 100]
const { isExcel } = useExcel()

const visibilityIcon = (v: string) => {
    switch (v) {
        case 'public': return 'i-heroicons-globe-alt'
        case 'internal': return 'i-heroicons-building-office'
        case 'shared': return 'i-heroicons-user-group'
        default: return 'i-heroicons-lock-closed'
    }
}

const visibilityLabel = (v: string) => {
    switch (v) {
        case 'public': return t('reports.visibility.public')
        case 'internal': return t('reports.visibility.internal')
        case 'shared': return t('reports.visibility.shared')
        default: return t('reports.visibility.private')
    }
}

const reportTypeIcon = (report: any) => {
    if (report.artifact_modes?.includes('page')) return 'i-heroicons-chart-bar-square'
    if (report.artifact_modes?.includes('slides')) return 'i-heroicons-presentation-chart-bar'
    return 'i-heroicons-chat-bubble-left-right'
}

const reportTypeLabel = (report: any) => {
    if (report.artifact_modes?.includes('page')) return t('reports.type.dashboard')
    if (report.artifact_modes?.includes('slides')) return t('reports.type.slides')
    return t('reports.type.chat')
}

// Resolve where a report row/title should link to.
// "Shared with me" reports belong to another user, so the owner's full
// /reports/:id editing page isn't accessible — open the read-only shared
// conversation view at /c/:token instead. If sharing produced no token
// there's nowhere to link, so return null and skip navigation.
const reportLink = (report: any): string | null => {
    if (activeFilter.value === 'shared') {
        return report.conversation_share_token
            ? `/c/${report.conversation_share_token}`
            : null
    }
    return `/reports/${report.id}`
}

const goToReport = (report: any) => {
    const link = reportLink(report)
    if (link) router.push(link)
}

const _df = useFormatDate()
const formatDate = (iso: string) => {
    if (!iso) return ''
    const datePart = _df.format(iso, { month: 'short', day: 'numeric', year: 'numeric' })
    if (!datePart) return ''
    const timePart = _df.format(iso, { hour: 'numeric', minute: '2-digit', hour12: true })
    return `${datePart} • ${timePart}`
}

const statusFilterOptions = computed(() => [
    { value: 'all', label: t('reports.filters.allStatus') },
    { value: 'draft', label: t('reports.filters.private') },
    { value: 'published', label: t('reports.filters.shared') },
])

const scheduleFilterOptions = computed(() => [
    { value: null, label: t('reports.filters.allSchedules') },
    { value: true, label: t('reports.filters.scheduled') },
    { value: false, label: t('reports.filters.notScheduled') },
])

const typeFilterOptions = computed(() => [
    { value: 'all', label: t('reports.filters.allModes') },
    { value: 'chat', label: t('reports.filters.chat') },
    { value: 'training', label: t('reports.filters.training') },
])

const artifactFilterOptions = computed(() => [
    { value: 'all', label: t('reports.filters.allDashboards') },
    { value: 'yes', label: t('reports.filters.withDashboard') },
    { value: 'no', label: t('reports.filters.noDashboard') },
])

const dataSourceFilterOptions = computed(() => {
    const options: { value: string; label: string }[] = [{ value: 'all', label: t('reports.filters.allSources') }]
    for (const ds of dataSources.value) {
        options.push({ value: ds.id, label: ds.name })
    }
    return options
})

const selectedStatusLabel = computed(() => {
    const option = statusFilterOptions.value.find(o => o.value === statusFilter.value)
    return option?.label || t('reports.filters.status')
})

const selectedScheduleLabel = computed(() => {
    const option = scheduleFilterOptions.value.find(o => o.value === scheduledFilter.value)
    return option?.label || t('reports.filters.schedule')
})

const selectedTypeLabel = computed(() => {
    const option = typeFilterOptions.value.find(o => o.value === typeFilter.value)
    return option?.label || t('reports.filters.type')
})

const selectedDataSourceLabel = computed(() => {
    const option = dataSourceFilterOptions.value.find(o => o.value === dataSourceFilter.value)
    return option?.label || t('reports.filters.dataSource')
})

const selectedArtifactLabel = computed(() => {
    const option = artifactFilterOptions.value.find(o => o.value === artifactFilter.value)
    return option?.label || t('reports.filters.artifacts')
})

const activeFilterCount = computed(() => {
    let count = 0
    if (statusFilter.value !== 'all') count++
    if (scheduledFilter.value !== null) count++
    if (typeFilter.value !== 'all') count++
    if (dataSourceFilter.value !== 'all') count++
    if (artifactFilter.value !== 'all') count++
    return count
})

// Live ordering within the current page: starred first, then latest
// activity — including events that arrived over the stream since the fetch.
const visibleReports = computed(() => sortByActivity(reports.value))

const allVisibleSelected = computed(() => {
    return visibleReports.value.length > 0 && visibleReports.value.every(r => selectedIds.value.has(r.id))
})

const canDeleteReport = (report: any) => {
    return currentUser.value && (report.user.id === currentUser.value.id || report.user.email === currentUser.value.email)
}

const changePage = async (page: number) => {
    if (page === currentPage.value || page < 1 || page > pagination.value.total_pages) {
        return
    }
    currentPage.value = page
    await fetchReports(page, activeFilter.value, searchTerm.value, scheduledFilter.value, statusFilter.value)
}

// Rows per page outlives the visit, so it is stored under the same `bow.`
// prefix the locale picker uses. Anything outside the offered set is ignored:
// the API caps `limit` at 100 and would 422 on a tampered value.
const ROWS_PER_PAGE_KEY = 'bow.reports.rowsPerPage'

const storedRowsPerPage = (): number | null => {
    if (typeof localStorage === 'undefined') return null
    const stored = Number(localStorage.getItem(ROWS_PER_PAGE_KEY))
    return rowsPerPageOptions.includes(stored) ? stored : null
}

const setRowsPerPage = async (limit: number) => {
    if (pagination.value.limit === limit) return
    pagination.value.limit = limit
    currentPage.value = 1
    if (typeof localStorage !== 'undefined') {
        localStorage.setItem(ROWS_PER_PAGE_KEY, String(limit))
    }
    await refreshReports()
}

const setActiveFilter = async (filter: 'my' | 'shared' | 'published') => {
    if (activeFilter.value === filter) return
    activeFilter.value = filter
    statusFilter.value = 'all'
    currentPage.value = 1
    scheduledFilter.value = null
    typeFilter.value = 'all'
    dataSourceFilter.value = 'all'
    artifactFilter.value = 'all'
    showFilters.value = false
    await fetchReports(1, filter, searchTerm.value, null, 'all')
}

const setStatusFilter = async (status: 'all' | 'draft' | 'published') => {
    if (statusFilter.value === status) return
    statusFilter.value = status
    currentPage.value = 1
    await fetchReports(1, activeFilter.value, searchTerm.value, scheduledFilter.value, status)
}

const setScheduledFilter = async (scheduled: boolean | null) => {
    if (scheduledFilter.value === scheduled) return
    scheduledFilter.value = scheduled
    currentPage.value = 1
    await refreshReports()
}

const setTypeFilter = async (type: string) => {
    if (typeFilter.value === type) return
    typeFilter.value = type
    currentPage.value = 1
    await refreshReports()
}

const setDataSourceFilter = async (dsId: string) => {
    if (dataSourceFilter.value === dsId) return
    dataSourceFilter.value = dsId
    currentPage.value = 1
    await refreshReports()
}

const setArtifactFilter = async (value: string) => {
    if (artifactFilter.value === value) return
    artifactFilter.value = value
    currentPage.value = 1
    await refreshReports()
}

const clearFilters = async () => {
    statusFilter.value = 'all'
    scheduledFilter.value = null
    typeFilter.value = 'all'
    dataSourceFilter.value = 'all'
    artifactFilter.value = 'all'
    currentPage.value = 1
    await refreshReports()
}

const refreshReports = () => {
    return fetchReports(1, activeFilter.value, searchTerm.value, scheduledFilter.value, statusFilter.value)
}

const fetchReports = async (page: number = 1, filter: 'my' | 'shared' | 'published' = 'my', search: string = '', scheduled: boolean | null = null, status: string | null = null) => {
    isLoading.value = true
    try {
        const response = await useMyFetch('/reports', {
            method: 'GET',
            query: {
                page,
                limit: pagination.value.limit,
                filter,
                search: search?.trim() || undefined,
                scheduled: scheduled !== null ? scheduled : undefined,
                status: status && status !== 'all' ? status : undefined,
                data_source_id: dataSourceFilter.value !== 'all' ? dataSourceFilter.value : undefined,
                mode: typeFilter.value !== 'all' ? typeFilter.value : undefined,
                has_artifacts: artifactFilter.value !== 'all' ? artifactFilter.value : undefined,
            },
        })

        if (response.status.value === 'success' && response.data.value) {
            reports.value = response.data.value.reports
            pagination.value = response.data.value.meta
            selectedIds.value = new Set()

            // Archiving the last row of the last page leaves `page` past the end
            // of the result set, so the server returns nothing while reports
            // still exist. The empty state would then replace the list and the
            // pager hides itself, stranding the user on a dead page with no way
            // back. Retry once on the last page that still holds rows.
            const meta: any = response.data.value.meta
            const lastPage = Math.max(1, meta?.total_pages || 1)
            if (!reports.value.length && (meta?.total || 0) > 0 && page > lastPage) {
                currentPage.value = lastPage
                return await fetchReports(lastPage, filter, search, scheduled, status)
            }

            fetchActivity(reports.value.map((r: any) => r.id))
        } else {
            throw new Error('Could not fetch reports')
        }
    } catch (error) {
        console.error('Error fetching reports:', error)
        toast.add({
            title: t('common.error'),
            description: t('reports.toasts.failedFetch'),
            color: 'red',
        })
    } finally {
        isLoading.value = false
    }
}

const toggleOne = (id: string) => {
    const s = new Set(selectedIds.value)
    if (s.has(id)) s.delete(id)
    else s.add(id)
    selectedIds.value = s
}

const toggleAllVisible = () => {
    const s = new Set(selectedIds.value)
    const allSelected = visibleReports.value.length > 0 && visibleReports.value.every(r => s.has(r.id))
    if (allSelected) {
        for (const r of visibleReports.value) s.delete(r.id)
    } else {
        for (const r of visibleReports.value) s.add(r.id)
    }
    selectedIds.value = s
}

async function confirmDelete(reportId: string) {
    if (confirm(t('reports.archiveConfirm'))) {
        await deleteReport(reportId)
        await fetchReports(currentPage.value, activeFilter.value, searchTerm.value, scheduledFilter.value, statusFilter.value)
    }
}

async function toggleStar(report: any) {
    const next = !report.is_starred
    // Optimistic update
    report.is_starred = next
    try {
        const response: any = await useMyFetch(`/reports/${report.id}/star`, {
            method: next ? 'POST' : 'DELETE',
        })
        if (response?.error?.value) {
            throw response.error.value
        }
        // Server controls ordering (starred first), so refetch the page
        await fetchReports(currentPage.value, activeFilter.value, searchTerm.value, scheduledFilter.value, statusFilter.value)
    } catch (error: any) {
        // Revert on failure
        report.is_starred = !next
        console.error('Error toggling star', error)
        toast.add({
            title: t('reports.toasts.starFailed'),
            description: String(error?.data?.detail || error?.message || ''),
            color: 'red',
        })
    }
}

async function archiveSelected() {
    if (selectedIds.value.size === 0) return
    const ok = window.confirm(t('reports.archiveConfirmBulk', { count: selectedIds.value.size }))
    if (!ok) return
    try {
        const response: any = await useMyFetch('/reports/bulk/archive', {
            method: 'POST',
            body: Array.from(selectedIds.value),
        })
        if (response?.error?.value) {
            throw response.error.value
        }
        const archived = (response?.data?.value as any)?.archived ?? selectedIds.value.size
        toast.add({
            title: t('reports.toasts.archivedBulk'),
            description: t('reports.toasts.archivedBulkDesc', { count: archived }),
            color: 'green',
        })
        await fetchReports(currentPage.value, activeFilter.value, searchTerm.value, scheduledFilter.value, statusFilter.value)
    } catch (error: any) {
        console.error('Error bulk archiving reports', error)
        const message =
            error?.data?.detail ||
            error?.data?.message ||
            error?.message ||
            t('reports.toasts.archiveBulkFailed')
        toast.add({
            title: t('reports.toasts.archiveBulkFailed'),
            description: String(message),
            color: 'red',
        })
    }
}

async function deleteReport(reportId: string) {
    try {
        const response = await useMyFetch(`/reports/${reportId}`, {
            method: 'DELETE',
        })

        if (response.status.value === 'success') {
            toast.add({
                title: t('reports.toasts.archived'),
                description: t('reports.toasts.archivedDesc'),
                color: 'green',
            })
        } else {
            throw new Error('Failed to archive report')
        }
    } catch (error: any) {
        console.error('Error archiving report', error)
        const message =
            error?.data?.detail ||
            error?.data?.message ||
            error?.message ||
            t('reports.toasts.archiveFailed')
        toast.add({
            title: t('reports.toasts.archiveFailed'),
            description: String(message),
            color: 'red',
        })
    }
}

const createNewReport = async () => {
    if (creatingReport.value) return
    creatingReport.value = true
    try {
        // Empty under Auto — the backend reads that as "resolve my scope per
        // run", so don't substitute the whole roster here.
        const dataSourceIds = selectedAgentObjects.value.map((a: any) => a.id)

        const response: any = await useMyFetch('/reports', {
            method: 'POST',
            body: JSON.stringify({
                title: 'untitled report',
                files: [],
                data_sources: dataSourceIds,
            }),
        })

        if (response?.error?.value) {
            throw new Error('Report creation failed')
        }

        const data: any = response?.data?.value
        if (data?.id) {
            router.push({ path: `/reports/${data.id}` })
        }
    } catch (e: any) {
        console.error('Failed to create report', e)
        const message =
            e?.data?.detail ||
            e?.data?.message ||
            e?.message ||
            t('reports.toasts.createFailed')
        toast.add({
            title: t('reports.toasts.createFailed'),
            description: String(message),
            color: 'red',
        })
    } finally {
        creatingReport.value = false
    }
}

const onClickOutside = (e: MouseEvent) => {
    if (showFilters.value && filtersRef.value && !filtersRef.value.contains(e.target as Node)) {
        showFilters.value = false
    }
}

let _searchTimer: any = null
watch(searchTerm, () => {
    if (_searchTimer) clearTimeout(_searchTimer)
    _searchTimer = setTimeout(() => {
        currentPage.value = 1
        fetchReports(1, activeFilter.value, searchTerm.value, scheduledFilter.value, statusFilter.value)
    }, 300)
})

onMounted(async () => {
    await nextTick()
    document.addEventListener('click', onClickOutside)
    const preferred = storedRowsPerPage()
    if (preferred) pagination.value.limit = preferred
    const [_, dsResponse] = await Promise.all([
        fetchReports(1, 'my', ''),
        useMyFetch('/data_sources', { method: 'GET' }),
    ])
    if (dsResponse?.data?.value) {
        dataSources.value = (dsResponse.data.value as any[]) || []
    }
})

onUnmounted(() => {
    document.removeEventListener('click', onClickOutside)
})
</script>
