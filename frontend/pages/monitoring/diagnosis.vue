<template>
    <div class="mt-6">
        <!-- Date Range Picker with Agent/User/Search filters -->
        <DateRangePicker
            :selected-period="selectedPeriod"
            :date-range="dateRange"
            extended
            @period-change="handlePeriodChange"
            @range-change="handleRangeChange"
        >
            <AgentSelector :collapsed="false" :show-text="true" :show-label="false" console-scope />

            <!-- User filter -->
            <USelectMenu
                v-model="selectedUsers"
                :options="userMenuOptions"
                multiple
                searchable
                by="id"
                option-attribute="name"
                size="sm"
                class="min-w-[160px]"
            >
                <template #label>
                    <span v-if="selectedUsers.length === 0" class="text-gray-500 dark:text-gray-400">{{ $t('monitoring.diagnosis.filterUsersAll') }}</span>
                    <span v-else class="truncate max-w-[180px]">{{ selectedUsers.map(u => u.name).join(', ') }}</span>
                </template>
                <template #option="{ option }">
                    <span v-if="option.id === ALL_SENTINEL" class="block w-full -mx-1.5 px-1.5 -my-1.5 py-1.5 font-medium border-b border-gray-200 dark:border-gray-700">{{ option.name }}</span>
                    <span v-else class="block truncate">{{ option.name }}</span>
                </template>
            </USelectMenu>

            <!-- Tool filter -->
            <USelectMenu
                v-model="selectedTools"
                :options="toolMenuOptions"
                multiple
                searchable
                by="name"
                option-attribute="label"
                size="sm"
                class="min-w-[160px]"
            >
                <template #label>
                    <span v-if="selectedTools.length === 0" class="text-gray-500 dark:text-gray-400">{{ $t('monitoring.diagnosis.filterToolsAll') }}</span>
                    <span v-else class="truncate max-w-[180px]">{{ selectedTools.map(t => t.name).join(', ') }}</span>
                </template>
                <template #option="{ option }">
                    <span v-if="option.name === ALL_SENTINEL" class="block w-full -mx-1.5 px-1.5 -my-1.5 py-1.5 font-medium border-b border-gray-200 dark:border-gray-700">{{ option.label }}</span>
                    <span v-else class="block truncate">{{ option.label }}</span>
                </template>
            </USelectMenu>

            <!-- Table filter -->
            <USelectMenu
                v-model="selectedTables"
                :options="tableMenuOptions"
                multiple
                searchable
                by="id"
                option-attribute="label"
                size="sm"
                class="min-w-[160px]"
            >
                <template #label>
                    <span v-if="selectedTables.length === 0" class="text-gray-500 dark:text-gray-400">{{ $t('monitoring.diagnosis.filterTablesAll') }}</span>
                    <span v-else class="truncate max-w-[180px]">{{ selectedTables.map(t => t.name).join(', ') }}</span>
                </template>
                <template #option="{ option }">
                    <span v-if="option.id === ALL_SENTINEL" class="block w-full -mx-1.5 px-1.5 -my-1.5 py-1.5 font-medium border-b border-gray-200 dark:border-gray-700">{{ option.label }}</span>
                    <span v-else class="flex items-center gap-1.5 min-w-0">
                        <DataSourceIcon
                            v-if="option.agent"
                            :type="option.agent.connections?.[0]?.type"
                            :connector-key="option.agent.connections?.[0]?.connector_key"
                            :icon="option.agent.icon"
                            class="h-3.5 w-3.5 flex-shrink-0"
                        />
                        <span class="truncate">{{ option.name }}</span>
                        <span class="text-gray-400 dark:text-gray-500 truncate text-xs">{{ option.data_source_name }}</span>
                    </span>
                </template>
            </USelectMenu>

            <!-- Failed-only toggle. Always visible: with tools selected it means
                 "runs where a selected tool failed"; with none it means "runs
                 where any tool failed". -->
            <UCheckbox
                v-model="toolFailedOnly"
                :label="$t('monitoring.diagnosis.failedOnly')"
                :ui="{ label: 'text-xs text-gray-600 dark:text-gray-400 whitespace-nowrap' }"
            />

            <!-- Free-text prompt search -->
            <UInput
                v-model="searchQuery"
                icon="i-heroicons-magnifying-glass"
                size="sm"
                :placeholder="$t('monitoring.diagnosis.searchPlaceholder')"
                class="min-w-[220px]"
            />
        </DateRangePicker>

        <!-- Activity Chart (observability-style daily bars) -->
        <DiagnosisActivityChart
            :points="timeseriesPoints"
            :is-loading="isTimeseriesLoading"
            :selected-date="selectedDay"
            @select-day="handleDaySelect"
        />

        <!-- Summary Cards (matching MetricsCards.vue style) -->
        <div class="grid grid-cols-1 md:grid-cols-4 gap-6 mb-8">
            <!-- Failed Queries -->
            <div class="bg-white dark:bg-gray-900 p-6 border border-gray-200 dark:border-gray-700 rounded-xl shadow-sm">
                <div class="text-2xl font-bold text-gray-900 dark:text-white">
                    {{ dashboardMetrics?.failed_queries || 0 }}
                </div>
                <div class="text-sm font-medium text-gray-600 dark:text-gray-400 mt-1">{{ $t('monitoring.diagnosis.cardFailedQueries') }}</div>
            </div>

            <!-- Negative Feedback -->
            <div class="bg-white dark:bg-gray-900 p-6 border border-gray-200 dark:border-gray-700 rounded-xl shadow-sm">
                <div class="text-2xl font-bold text-gray-900 dark:text-white">
                    {{ dashboardMetrics?.negative_feedback || 0 }}
                </div>
                <div class="text-sm font-medium text-gray-600 dark:text-gray-400 mt-1">{{ $t('monitoring.diagnosis.cardNegativeFeedback') }}</div>
            </div>

            <!-- Instruction Coverage -->
            <div class="bg-white dark:bg-gray-900 p-6 border border-gray-200 dark:border-gray-700 rounded-xl shadow-sm">
                <div class="text-2xl font-bold text-gray-900 dark:text-white">
                    {{ isJudgeEnabled ? (getInstructionsEffectiveness() + '%') : $t('monitoring.diagnosis.naAbbr') }}
                </div>
                <div class="text-sm font-medium text-gray-600 dark:text-gray-400 mt-1 flex items-center">
                    {{ $t('monitoring.diagnosis.cardInstructionCoverage') }}
                    <UTooltip :text="isJudgeEnabled ? $t('monitoring.diagnosis.judgeEnabledTooltip') : $t('monitoring.diagnosis.judgeDisabledTooltip')">
                        <UIcon name="i-heroicons-information-circle" class="w-4 h-4 ms-1 text-gray-400 dark:text-gray-600 cursor-help" />
                    </UTooltip>
                </div>
            </div>

            <!-- Total Items -->
            <div class="bg-white dark:bg-gray-900 p-6 border border-gray-200 dark:border-gray-700 rounded-xl shadow-sm">
                <div class="text-2xl font-bold text-gray-900 dark:text-white">
                    {{ dashboardMetrics?.total_items || 0 }}
                </div>
                <div class="text-sm font-medium text-gray-600 dark:text-gray-400 mt-1">{{ $t('monitoring.diagnosis.cardTotalAgentRuns') }}</div>
            </div>
        </div>

        <!-- Filter Tabs -->
        <div class="mb-6">
            <div class="border-b border-gray-200 dark:border-gray-700">
                <nav class="-mb-px flex space-x-8">
                    <button
                        v-for="filter in tabOptions"
                        :key="filter.value"
                        @click="handleFilterChange(filter)"
                        :class="[
                            selectedFilter.value === filter.value
                                ? 'border-blue-500 text-blue-600'
                                : 'border-transparent text-gray-500 dark:text-gray-400 hover:text-gray-700 hover:border-gray-300',
                            'whitespace-nowrap py-2 px-1 border-b-2 font-medium text-sm'
                        ]"
                    >
                        {{ filter.label }}
                        <span
                            v-if="filter.count !== undefined && filter.count >= 0"
                            :class="[
                                selectedFilter.value === filter.value
                                    ? 'bg-blue-100 dark:bg-blue-900/50 text-blue-600'
                                    : 'bg-gray-100 dark:bg-gray-800 text-gray-600 dark:text-gray-400',
                                'ms-2 py-0.5 px-2 rounded-full text-xs font-medium'
                            ]"
                        >
                            {{ filter.count }}
                        </span>
                    </button>
                </nav>
            </div>
        </div>

        <!-- Active filter chips: one per selected value, each removable -->
        <div v-if="activeFilterChips.length > 0" class="mb-4 -mt-2 flex flex-wrap items-center gap-2">
            <button
                v-for="chip in activeFilterChips"
                :key="chip.key"
                @click="chip.clear()"
                class="inline-flex items-center gap-1.5 px-3 py-1 bg-blue-50 dark:bg-blue-950 text-blue-700 border border-blue-200 rounded-full text-xs font-medium hover:bg-blue-100 max-w-[240px]"
            >
                <span class="truncate">{{ chip.label }}</span>
                <UIcon name="i-heroicons-x-mark" class="w-3.5 h-3.5 flex-shrink-0" />
            </button>
            <button
                v-if="activeFilterChips.length > 1"
                @click="clearAllFilters"
                class="text-xs text-gray-500 dark:text-gray-400 hover:text-gray-700 dark:hover:text-gray-300 underline"
            >
                {{ $t('monitoring.diagnosis.clearAll') }}
            </button>
        </div>

        <!-- Loading state -->
        <div v-if="isLoading" class="flex items-center justify-center py-12">
            <div class="flex items-center space-x-2">
                <div class="animate-spin rounded-full h-6 w-6 border-b-2 border-blue-600"></div>
                <span class="text-gray-600 dark:text-gray-400">{{ $t('monitoring.diagnosis.loading') }}</span>
            </div>
        </div>

        <!-- Top Errors tab: failed tool calls grouped by tool + message; click narrows to that tool's failed runs -->
        <div v-else-if="selectedFilter.value === 'top_errors'" class="bg-white dark:bg-gray-900 shadow-sm border border-gray-200 dark:border-gray-700 rounded-lg overflow-hidden">
            <table class="w-full table-fixed divide-y divide-gray-200 dark:divide-gray-700">
                <thead class="bg-gray-50 dark:bg-gray-900">
                    <tr>
                        <th class="w-[10%] px-4 py-3 text-start text-xs font-medium text-gray-500 dark:text-gray-400 uppercase tracking-wider">{{ $t('monitoring.diagnosis.colRuns') }}</th>
                        <th class="w-[20%] px-4 py-3 text-start text-xs font-medium text-gray-500 dark:text-gray-400 uppercase tracking-wider">{{ $t('monitoring.diagnosis.colTools') }}</th>
                        <th class="w-[70%] px-4 py-3 text-start text-xs font-medium text-gray-500 dark:text-gray-400 uppercase tracking-wider">{{ $t('monitoring.diagnosis.colError') }}</th>
                    </tr>
                </thead>
                <tbody class="bg-white dark:bg-gray-900 divide-y divide-gray-200 dark:divide-gray-700 text-xs">
                    <tr v-for="(group, idx) in errorGroups" :key="idx" class="hover:bg-gray-50 dark:hover:bg-gray-800 cursor-pointer" @click="applyErrorGroupFilter(group)">
                        <td class="px-4 py-3">
                            <span class="inline-flex items-center justify-center min-w-[28px] px-1.5 py-0.5 bg-red-100 dark:bg-red-900/50 text-red-800 dark:text-red-300 rounded-full text-xs font-semibold">{{ group.count }}</span>
                        </td>
                        <td class="px-4 py-3">
                            <span class="px-2 py-0.5 bg-gray-100 dark:bg-gray-800 text-gray-700 dark:text-gray-300 rounded text-[11px]">{{ group.tool_name }}</span>
                        </td>
                        <td class="px-4 py-3">
                            <span class="block truncate text-gray-700 dark:text-gray-300">{{ group.error_message || $t('monitoring.diagnosis.noErrorMessage') }}</span>
                        </td>
                    </tr>
                </tbody>
            </table>
            <div v-if="errorGroups.length === 0" class="text-center py-12">
                <UIcon name="i-heroicons-check-circle" class="mx-auto h-12 w-12 text-gray-400 dark:text-gray-600" />
                <p class="mt-2 text-sm text-gray-500 dark:text-gray-400">{{ $t('monitoring.diagnosis.noErrorsFound') }}</p>
            </div>
        </div>

        <!-- Agent Executions Table -->
        <div v-else class="bg-white dark:bg-gray-900 shadow-sm border border-gray-200 dark:border-gray-700 rounded-lg overflow-hidden">
            <div class="overflow-x-auto">
                <!-- Fixed layout so column widths are stable and predictable;
                     min-w keeps them readable, the wrapper scrolls horizontally
                     on narrow screens. -->
                <table class="w-full min-w-[1080px] table-fixed divide-y divide-gray-200 dark:divide-gray-700">
                    <thead class="bg-gray-50 dark:bg-gray-900">
                        <tr>
                            <th class="w-[24%] px-4 py-3 text-start text-xs font-medium text-gray-500 dark:text-gray-400 uppercase tracking-wider">{{ $t('monitoring.diagnosis.colPrompt') }}</th>
                            <th class="w-[9%] px-4 py-3 text-start text-xs font-medium text-gray-500 dark:text-gray-400 uppercase tracking-wider">{{ $t('monitoring.diagnosis.colUser') }}</th>
                            <th class="w-[8%] px-4 py-3 text-start text-xs font-medium text-gray-500 dark:text-gray-400 uppercase tracking-wider">{{ $t('monitoring.diagnosis.colStatus') }}</th>
                            <th class="w-[17%] px-4 py-3 text-start text-xs font-medium text-gray-500 dark:text-gray-400 uppercase tracking-wider">{{ $t('monitoring.diagnosis.colData') }}</th>
                            <th class="w-[9%] px-4 py-3 text-start text-xs font-medium text-gray-500 dark:text-gray-400 uppercase tracking-wider">{{ $t('monitoring.diagnosis.colTools') }}</th>
                            <th class="w-[11%] px-4 py-3 text-start text-xs font-medium text-gray-500 dark:text-gray-400 uppercase tracking-wider">{{ $t('monitoring.diagnosis.colFeedback') }}</th>
                            <th class="w-[12%] px-4 py-3 text-start text-xs font-medium text-gray-500 dark:text-gray-400 uppercase tracking-wider">{{ $t('monitoring.diagnosis.colReport') }}</th>
                            <th class="w-[10%] px-4 py-3 text-start text-xs font-medium text-gray-500 dark:text-gray-400 uppercase tracking-wider">{{ $t('monitoring.diagnosis.colDate') }}</th>
                        </tr>
                    </thead>
                    <tbody class="bg-white dark:bg-gray-900 divide-y divide-gray-200 dark:divide-gray-700 text-xs">
                        <tr v-for="item in executionItems" :key="item.agent_execution_id" class="hover:bg-gray-50 dark:hover:bg-gray-800 cursor-pointer" @click="openTraceFromAE(item)">
                            <td class="px-4 py-3">
                                <div class="text-xs text-gray-900 dark:text-white">
                                    <div class="relative group max-w-full">
                                        <p class="truncate flex items-center gap-1.5">
                                            <UTooltip v-if="platformOf(item)" :text="platformOf(item).label">
                                                <img v-if="platformOf(item).img" :src="platformOf(item).img" class="h-3.5 w-3.5 inline flex-shrink-0" :alt="platformOf(item).label" />
                                                <UIcon v-else-if="platformOf(item).icon" :name="platformOf(item).icon" class="w-3.5 h-3.5 flex-shrink-0 text-gray-500" />
                                            </UTooltip>
                                            <span class="truncate">{{ truncate(item.prompt || '', 40) }}</span>
                                        </p>
                                        <div class="pointer-events-none absolute start-0 top-full mt-1 z-10 hidden group-hover:block bg-white dark:bg-gray-900 border border-gray-200 dark:border-gray-700 rounded-md shadow-sm p-2 text-xs whitespace-pre-wrap max-w-[520px] max-h-56 overflow-auto">
                                            {{ item.prompt || '—' }}
                                        </div>
                                    </div>
                                </div>
                            </td>
                            <td class="px-4 py-3">
                                <div class="text-xs text-gray-900 dark:text-white truncate">{{ item.user_name || '—' }}</div>
                            </td>
                            <td class="px-4 py-3 whitespace-nowrap">
                                <div class="relative inline-block group">
                                    <span class="inline-flex px-2 py-1 text-xs font-medium rounded-full"
                                          :class="item.agent_execution_status === 'error' ? 'bg-red-100 dark:bg-red-900/50 text-red-800' : (item.agent_execution_status === 'completed' || item.agent_execution_status === 'success') ? 'bg-green-100 dark:bg-green-900/50 text-green-800' : 'bg-gray-100 dark:bg-gray-800 text-gray-800'">
                                        {{ item.agent_execution_status === 'error' ? $t('monitoring.diagnosis.statusError') : $t('monitoring.diagnosis.statusSuccess') }}
                                    </span>
                                    <div v-if="item.agent_execution_status === 'error' && item.error_json?.message" class="pointer-events-none absolute start-0 top-full mt-1 z-10 hidden group-hover:block bg-white dark:bg-gray-900 border border-gray-200 dark:border-gray-700 rounded-md shadow-sm p-2 text-xs text-red-700 whitespace-pre-wrap max-w-[520px] max-h-56 overflow-auto">
                                        {{ item.error_json.message }}
                                    </div>
                                </div>
                            </td>
                            <td class="px-4 py-3">
                                <div class="flex flex-wrap gap-1">
                                    <span v-for="(title, idx) in item.step_titles || []" :key="idx" class="px-2 py-0.5 bg-gray-100 dark:bg-gray-800 text-gray-700 dark:text-gray-300 rounded text-[11px] max-w-full truncate">
                                        {{ title }}
                                    </span>
                                    <span v-if="(item.step_titles || []).length === 0" class="text-gray-400 dark:text-gray-600">{{ $t('monitoring.diagnosis.none') }}</span>
                                </div>
                            </td>
                            <td class="px-4 py-3">
                                <div class="text-xs text-gray-900 dark:text-white">{{ $t('monitoring.diagnosis.totalPrefix', { n: item.total_tools }) }}</div>
                                <div class="flex items-center space-x-4 mt-1">
                                    <div class="flex items-center space-x-1 text-green-600">
                                        <UIcon name="i-heroicons-check-circle" class="w-4 h-4" />
                                        <span>{{ item.total_successful_tools }}</span>
                                    </div>
                                    <div class="flex items-center space-x-1 text-red-600">
                                        <UIcon name="i-heroicons-x-circle" class="w-4 h-4" />
                                        <span>{{ item.total_failed_tools }}</span>
                                    </div>
                                </div>
                            </td>
                            <td class="px-4 py-3">
                                <div class="flex flex-col">
                                    <span class="inline-flex px-2 py-1 text-xs font-medium rounded-full self-start"
                                          :class="item.feedback_direction > 0 ? 'bg-green-100 dark:bg-green-900/50 text-green-800' : item.feedback_direction < 0 ? 'bg-red-100 dark:bg-red-900/50 text-red-800' : 'bg-gray-100 dark:bg-gray-800 text-gray-800'">
                                        {{ item.feedback_direction > 0 ? $t('monitoring.diagnosis.feedbackPositive') : (item.feedback_direction < 0 ? $t('monitoring.diagnosis.feedbackNegative') : $t('monitoring.diagnosis.feedbackNone')) }}
                                    </span>
                                    <div v-if="item.feedback_direction < 0 && item.feedback_message" class="mt-1 text-xs text-gray-600 dark:text-gray-400 max-w-full">
                                        <UTooltip :text="item.feedback_message">
                                            <span class="block truncate cursor-help">{{ truncate(item.feedback_message, 120) }}</span>
                                        </UTooltip>
                                    </div>
                                </div>
                            </td>
                            <td class="px-4 py-3">
                                <NuxtLink v-if="item.report_link" :to="item.report_link" class="block truncate text-blue-600 hover:underline" @click.stop>
                                    {{ item.report_name || item.report_id }}
                                </NuxtLink>
                                <span v-else class="block truncate">{{ item.report_name || item.report_id }}</span>
                            </td>
                            <td class="px-4 py-3">
                                <span class="text-xs text-gray-500 dark:text-gray-400">{{ formatDateTime(item.created_at as any) }}</span>
                            </td>
                        </tr>
                    </tbody>
                </table>
            </div>

            <!-- Empty state -->
            <div v-if="executionItems.length === 0 && !isLoading" class="text-center py-12">
                <UIcon name="i-heroicons-clipboard-document-check" class="mx-auto h-12 w-12 text-gray-400 dark:text-gray-600" />
                <h3 class="mt-2 text-sm font-medium text-gray-900 dark:text-white">{{ $t('monitoring.diagnosis.emptyTitle') }}</h3>
                <p class="mt-1 text-sm text-gray-500 dark:text-gray-400">
                    {{ $t('monitoring.diagnosis.emptySubtitle') }}
                </p>
                <div class="mt-2 text-xs text-gray-400 dark:text-gray-600">
                    {{ $t('monitoring.diagnosis.debugPrefix', { info: debugInfo }) }}
                </div>
            </div>
        </div>

        <!-- Pagination (runs list only) -->
        <div v-if="executionItems.length > 0 && selectedFilter.value !== 'top_errors'" class="mt-6 flex items-center justify-between">
            <div class="text-sm text-gray-700 dark:text-gray-300">
                {{ $t('monitoring.diagnosis.paginationRange', { start: (currentPage - 1) * pageSize + 1, end: Math.min(currentPage * pageSize, totalItems), total: totalItems }) }}
            </div>

            <div class="flex items-center space-x-2">
                <UButton
                    icon="i-heroicons-chevron-left"
                    color="gray"
                    variant="ghost"
                    size="sm"
                    @click="currentPage--"
                    :disabled="currentPage === 1"
                >
                    {{ $t('monitoring.diagnosis.previous') }}
                </UButton>
                
                <div class="flex items-center space-x-1">
                    <UButton
                        v-for="page in visiblePages"
                        :key="page"
                        :color="page === currentPage ? 'blue' : 'gray'"
                        :variant="page === currentPage ? 'solid' : 'ghost'"
                        size="sm"
                        @click="currentPage = page"
                        class="min-w-[32px]"
                    >
                        {{ page }}
                    </UButton>
                </div>
                
                <UButton
                    icon="i-heroicons-chevron-right"
                    color="gray"
                    variant="ghost"
                    size="sm"
                    @click="currentPage++"
                    :disabled="currentPage === totalPages"
                >
                    {{ $t('monitoring.diagnosis.next') }}
                </UButton>
            </div>
        </div>
        
        <!-- Trace Modal -->
        <TraceModal
            v-model="showTraceModal"
            :report-id="selectedTraceItem?.report_id || ''"
            :completion-id="selectedTraceItem?.completion_id || selectedTraceItem?.id || ''"
        />
    </div>
</template>

<script setup lang="ts">
import DateRangePicker from '~/components/console/DateRangePicker.vue'
import TraceModal from '~/components/console/TraceModal.vue'
import AgentSelector from '~/components/AgentSelector.vue'
import DiagnosisActivityChart from '~/components/console/DiagnosisActivityChart.vue'
const { isJudgeEnabled } = useOrgSettings()
// The console is scoped to the agents the user manages, so the filter it sends
// is the selection narrowed to that set — never the raw chat-context selection.
const { consoleSelectedAgents, consoleSelectionKey, initAgent, agents, selectedAgents, selectAgents } = useAgent()
const { t } = useI18n()

definePageMeta({
    auth: true,
    layout: 'monitoring',
    // Mirrors the /console/* gate: org admins see the org-wide console, agent
    // managers see it scoped to the agents they manage.
    // Keep in step with useCanAccessMonitoring() — the sidebar entry and the tab
    // strip use that predicate, and a page that guards on less would let a user
    // click an entry that bounces them straight back to '/'.
    anyOf: ['manage_settings', 'manage_connections', { permission: 'manage', resourceType: 'data_source' }]
})

// Types for compact issues
interface CompactIssueItem {
    completion_id: string
    created_at: string
    issue_type: string
    summary_text: string
    full_message?: string
    tool_name?: string
    tool_action?: string
    user_name?: string
    user_email?: string
    head_prompt_snippet?: string
    report_id: string
    trace_url?: string
}

interface CompactIssuesResponse {
    items: CompactIssueItem[]
    total_items: number
    date_range: {
        start: string
        end: string
    }
}

interface DateRange {
    start: string
    end: string
}

// State (same as ConsoleOverview)
const isLoading = ref(false)
const metrics = ref<CompactIssuesResponse | null>(null)
const overallMetrics = ref<CompactIssuesResponse | null>(null) // Static metrics for top cards
const diagnosisItems = ref<CompactIssueItem[]>([])
const currentPage = ref(1)
const pageSize = ref(10)
const totalItems = ref(0)
const debugInfo = ref('')
const instructionsEffectiveness = ref<number | null>(null)
// New data for agent execution summaries
const executionItems = ref<any[]>([])
const dashboardMetrics = ref<any>(null)

// Top errors tab (failed tool calls grouped by tool + message)
interface DiagnosisErrorGroup { tool_name: string; error_message: string; count: number }
const errorGroups = ref<DiagnosisErrorGroup[]>([])
// Exact-error drill-down set by clicking a Top Errors row ('' = the
// no-error-message group; null = no error filter)
const selectedToolError = ref<string | null>(null)

// Activity chart timeseries (daily agent runs by status)
interface DiagnosisStatusPoint { date: string; success: number; error: number }
const timeseriesPoints = ref<DiagnosisStatusPoint[] | null>(null)
const isTimeseriesLoading = ref(false)
// Day selected by clicking a bar in the activity chart (YYYY-MM-DD); narrows the table only
const selectedDay = ref<string | null>(null)

// Filter state
const filterLabelFor = (value: string): string => {
    switch (value) {
        case 'all': return t('monitoring.diagnosis.filterAll')
        case 'negative_feedback': return t('monitoring.diagnosis.filterNegative')
        case 'failed_queries': return t('monitoring.diagnosis.filterFailed')
        case 'low_confidence': return t('monitoring.diagnosis.filterLowConfidence')
        case 'low_instruction_coverage': return t('monitoring.diagnosis.filterLowCoverage')
        case 'top_errors': return t('monitoring.diagnosis.topErrors')
        default: return value
    }
}
const selectedFilter = ref({ label: filterLabelFor('all'), value: 'all' })
const filterOptions = ref([
    { label: filterLabelFor('all'), value: 'all', count: 0 },
    { label: filterLabelFor('negative_feedback'), value: 'negative_feedback', count: 0 },
    { label: filterLabelFor('failed_queries'), value: 'failed_queries', count: 0 },
    { label: filterLabelFor('low_confidence'), value: 'low_confidence', count: 0 },
    { label: filterLabelFor('low_instruction_coverage'), value: 'low_instruction_coverage', count: 0 }
])

// The tab strip = the metrics-driven tabs plus the grouped-errors tab, whose
// count is the number of distinct error patterns in the current slice
const tabOptions = computed(() => [
    ...filterOptions.value,
    { label: filterLabelFor('top_errors'), value: 'top_errors', count: errorGroups.value.length }
])

// Add these to the state section
const showTraceModal = ref(false)
const selectedTraceItem = ref<any | null>(null)

// User filter state
interface DiagnosisUserOption { id: string; name: string; email: string }
const userOptions = ref<DiagnosisUserOption[]>([])
const selectedUsers = ref<DiagnosisUserOption[]>([])

// Tool filter state
interface DiagnosisToolOption { name: string; total: number; failed: number }
const toolOptions = ref<DiagnosisToolOption[]>([])
const selectedTools = ref<DiagnosisToolOption[]>([])
const toolFailedOnly = ref(false)

// Data-table filter state (label carries the data source name — two sources
// can both have an `orders` table)
interface DiagnosisTableOption { id: string; name: string; data_source_id: string; data_source_name: string; label: string }
const tableOptions = ref<DiagnosisTableOption[]>([])
const selectedTables = ref<DiagnosisTableOption[]>([])

// "Select all" row at the top of each facet dropdown, mirroring the agent
// selector: picking it clears the selection (empty selection = no filter).
// The sentinel never stays selected — the watchers swap it for a clear.
const ALL_SENTINEL = '__all__'
const userMenuOptions = computed(() => [
    { id: ALL_SENTINEL, name: t('monitoring.diagnosis.filterUsersAll'), email: '' },
    ...userOptions.value
])
const toolMenuOptions = computed(() => [
    { name: ALL_SENTINEL, label: t('monitoring.diagnosis.filterToolsAll') },
    ...toolOptions.value.map(o => ({ ...o, label: o.name }))
])
const tableMenuOptions = computed(() => {
    // Attach the owning agent so each table row can show its agent's logo
    const agentById = new Map((agents.value || []).map((a: any) => [String(a.id), a]))
    return [
        { id: ALL_SENTINEL, label: t('monitoring.diagnosis.filterTablesAll') } as any,
        ...tableOptions.value.map(o => ({ ...o, agent: agentById.get(o.data_source_id) || null }))
    ]
})

// Free-text prompt search (debounced)
const searchQuery = ref('')
let searchDebounceTimer: ReturnType<typeof setTimeout> | null = null

// Date range state (same as ConsoleOverview)
const selectedPeriod = ref({ label: t('monitoring.diagnosis.periodAllTime'), value: 'all_time' })
const dateRange = ref<DateRange>({
    start: '',
    end: ''
})

// Computed
const totalPages = computed(() => Math.ceil(totalItems.value / pageSize.value))

const visiblePages = computed(() => {
    const pages = []
    const total = totalPages.value
    const current = currentPage.value
    
    // Show maximum 5 pages
    let start = Math.max(1, current - 2)
    let end = Math.min(total, start + 4)
    
    // Adjust start if we're near the end
    if (end - start < 4) {
        start = Math.max(1, end - 4)
    }
    
    for (let i = start; i <= end; i++) {
        pages.push(i)
    }
    
    return pages
})

// Methods (same pattern as ConsoleOverview)
const initializeDateRange = () => {
    // Default to all time
    selectedPeriod.value = { label: t('monitoring.diagnosis.periodAllTime'), value: 'all_time' }
    dateRange.value = {
        start: '',
        end: new Date().toISOString().split('T')[0]
    }
}

const handlePeriodChange = (period: { label: string, value: string }) => {
    selectedPeriod.value = period

    // Date-input modes: the picker emits rangeChange with the concrete dates.
    if (period.value === 'exact_day' || period.value === 'custom') {
        return
    }

    const end = new Date()
    let start: Date | null = null

    switch (period.value) {
        case '7_days':
            start = new Date()
            start.setDate(start.getDate() - 7)
            break
        case '30_days':
            start = new Date()
            start.setDate(start.getDate() - 30)
            break
        case '90_days':
            start = new Date()
            start.setDate(start.getDate() - 90)
            break
        case 'all_time':
        default:
            start = null
            break
    }

    dateRange.value = {
        start: start ? start.toISOString().split('T')[0] : '',
        end: end.toISOString().split('T')[0]
    }

    refreshAll()
}

// Exact-day / custom-range picked in the DateRangePicker inputs
const handleRangeChange = (range: DateRange) => {
    dateRange.value = { ...range }
    refreshAll()
}

const refreshAll = () => {
    currentPage.value = 1
    selectedDay.value = null
    // Refresh overall metrics, timeseries, error groups, and diagnosis data when filters change
    Promise.all([
        fetchOverallMetrics(),
        fetchTimeseries(),
        fetchErrorGroups(),
        fetchDiagnosisData()
    ])
}



const fetchDiagnosisData = async () => {
    isLoading.value = true
    try {
        const params = new URLSearchParams({
            page: currentPage.value.toString(),
            page_size: pageSize.value.toString()
        })

        appendDateParams(params)

        // Add filter parameter
        if (selectedFilter.value.value !== 'all') {
            params.append('filter', selectedFilter.value.value)
        }

        appendScopeParams(params)

        debugInfo.value = `Fetching with params: ${params.toString()}`

        // Fetch agent execution summaries instead of compact issues
        const diagnosisResponse = await useMyFetch<any>(`/api/console/agent_executions/summaries?${params}`)
        
        if (diagnosisResponse.error.value) {
            console.error('Error fetching diagnosis data:', diagnosisResponse.error.value)
            debugInfo.value = `Error: ${diagnosisResponse.error.value}`
            metrics.value = null
            diagnosisItems.value = []
            totalItems.value = 0
        } else if (diagnosisResponse.data.value) {
            const data = diagnosisResponse.data.value
            executionItems.value = data.items || []
            totalItems.value = data.total_items || 0
            debugInfo.value = `Loaded ${executionItems.value.length} agent executions, total: ${totalItems.value}`
        }
    } catch (error) {
        console.error('Failed to fetch diagnosis data:', error)
        debugInfo.value = `Exception: ${error}`
        metrics.value = null
        executionItems.value = []
        totalItems.value = 0
    } finally {
        isLoading.value = false
    }
}

const getIssueTypeClass = (issueType: string) => {
    switch (issueType) {
        case 'failed_step':
        case 'failed_query':
            return 'bg-red-100 dark:bg-red-900/50 text-red-800'
        case 'validation_error':
            return 'bg-yellow-100 text-yellow-800'
        case 'negative_feedback':
            return 'bg-orange-100 dark:bg-orange-950 text-orange-800'
        case 'no_issue':
            return 'bg-green-100 dark:bg-green-900/50 text-green-800'
        default:
            return 'bg-gray-100 dark:bg-gray-800 text-gray-800'
    }
}

const getIssueTypeLabel = (issueType: string) => {
    switch (issueType) {
        case 'failed_query':
            return 'Failed Query'
        case 'validation_error':
            return 'Validation Error'
        case 'negative_feedback':
            return 'Negative Feedback'
        case 'no_issue':
            return 'OK'
        default:
            return 'Unknown'
    }
}

const _df = useFormatDate()
const formatDate = (dateString: string) => {
    if (!dateString) return ''
    return _df.formatDate(dateString)
}
const formatDateTime = (dateString: string) => {
    if (!dateString) return ''
    return _df.formatDateTime(dateString)
}

// Origin platform icon for a run. null/unknown = web UI (no icon — the default).
// Icons reuse the /icons/<platform>.png assets from the Members table; platforms
// with no PNG (email) fall back to a heroicons glyph.
const PLATFORM_LABELS: Record<string, string> = {
    slack: 'Slack', teams: 'Teams', whatsapp: 'WhatsApp', mcp: 'MCP', email: 'Email',
}
const PLATFORM_FALLBACK_ICON: Record<string, string> = {
    email: 'i-heroicons-envelope',
}
const platformOf = (item: any) => {
    const p = (item?.external_platform || '').toLowerCase()
    if (!p || !(p in PLATFORM_LABELS)) return null
    return {
        label: PLATFORM_LABELS[p],
        img: p in PLATFORM_FALLBACK_ICON ? null : `/icons/${p}.png`,
        icon: PLATFORM_FALLBACK_ICON[p] || null,
    }
}

// Add these methods to the existing script section

// Append start/end date params. When a chart day is selected, narrow to that
// single day — using explicit UTC midnight so the backend (which normalizes a
// date to its full day) lands on the right calendar day regardless of timezone.
// Append the agent (data source) and user filters shared by all diagnosis endpoints.
const appendScopeParams = (params: URLSearchParams) => {
    if (consoleSelectedAgents.value.length > 0) {
        params.append('data_source_ids', consoleSelectedAgents.value.join(','))
    }
    if (selectedUsers.value.length > 0) {
        params.append('user_ids', selectedUsers.value.map(u => u.id).join(','))
    }
    if (selectedTools.value.length > 0) {
        params.append('tool_names', selectedTools.value.map(t => t.name).join(','))
    }
    // Standalone: without tools it filters to runs where any tool failed
    if (toolFailedOnly.value) {
        params.append('tool_failed_only', 'true')
    }
    // Exact error message from the Top Errors drill-down ('' is meaningful)
    if (selectedToolError.value !== null) {
        params.append('tool_error', selectedToolError.value)
    }
    if (selectedTables.value.length > 0) {
        params.append('table_ids', selectedTables.value.map(t => t.id).join(','))
    }
    // Free-text prompt search filters everything (table, KPI cards, chart),
    // like every other filter here — not just the table.
    if (searchQuery.value.trim()) {
        params.append('prompt_search', searchQuery.value.trim())
    }
}

const appendDateParams = (params: URLSearchParams) => {
    if (selectedDay.value) {
        params.append('start_date', `${selectedDay.value}T00:00:00.000Z`)
        params.append('end_date', `${selectedDay.value}T00:00:00.000Z`)
        return
    }
    if (dateRange.value.start) {
        params.append('start_date', new Date(dateRange.value.start).toISOString())
    }
    if (dateRange.value.end) {
        params.append('end_date', new Date(dateRange.value.end).toISOString())
    }
}

const fetchOverallMetrics = async () => {
    try {
        const params = new URLSearchParams()
        appendDateParams(params)
        appendScopeParams(params)

        // Fetch dashboard metrics and judge response
        const [dashboardResponse, judgeResponse] = await Promise.all([
            useMyFetch<any>(`/api/console/diagnosis/metrics?${params}`),
            useMyFetch<any>(`/api/console/metrics?${params}`)
        ])
        
        if (dashboardResponse.data.value) {
            dashboardMetrics.value = dashboardResponse.data.value

            // Update filter counts
            filterOptions.value = [
                { label: filterLabelFor('all'), value: 'all', count: dashboardResponse.data.value.total_items },
                { label: filterLabelFor('negative_feedback'), value: 'negative_feedback', count: dashboardResponse.data.value.negative_feedback },
                { label: filterLabelFor('failed_queries'), value: 'failed_queries', count: dashboardResponse.data.value.failed_queries },
                { label: filterLabelFor('low_confidence'), value: 'low_confidence', count: dashboardResponse.data.value.low_confidence || 0 },
                { label: filterLabelFor('low_instruction_coverage'), value: 'low_instruction_coverage', count: dashboardResponse.data.value.low_instruction_coverage || 0 }
            ]
        }
        
        if (judgeResponse.data.value) {
            instructionsEffectiveness.value = judgeResponse.data.value.instructions_effectiveness
        }
    } catch (error) {
        console.error('Failed to fetch overall metrics:', error)
    }
}

const fetchErrorGroups = async () => {
    try {
        const params = new URLSearchParams({ limit: '20' })
        appendDateParams(params)
        appendScopeParams(params)
        const response = await useMyFetch<any>(`/api/console/diagnosis/errors?${params}`)
        errorGroups.value = response.data.value?.groups || []
    } catch (error) {
        console.error('Failed to fetch diagnosis error groups:', error)
        errorGroups.value = []
    }
}

// Guard for bulk filter mutations: watchers skip their per-change refresh so a
// multi-field change triggers exactly one refetch round.
let bulkFilterChange = false

// Clicking a top-error row narrows the screen to exactly that error — the
// tool AND the specific message — and jumps back to the runs list
const applyErrorGroupFilter = (group: DiagnosisErrorGroup) => {
    const opt = toolOptions.value.find(t => t.name === group.tool_name)
        || { name: group.tool_name, total: 0, failed: 0 }
    bulkFilterChange = true
    toolFailedOnly.value = true
    selectedTools.value = [opt]
    selectedToolError.value = group.error_message
    selectedFilter.value = { label: filterLabelFor('all'), value: 'all' }
    nextTick(() => { bulkFilterChange = false; refreshAll() })
}

// Active-filter chips: one per selected value, each removable, plus clear-all
const activeFilterChips = computed(() => {
    const chips: { key: string; label: string; clear: () => void }[] = []
    const agentName = (id: string) => (agents.value || []).find((a: any) => String(a.id) === id)?.name || id
    for (const id of consoleSelectedAgents.value) {
        chips.push({ key: `agent-${id}`, label: agentName(id), clear: () => removeAgentFromSelection(id) })
    }
    for (const u of selectedUsers.value) {
        chips.push({ key: `user-${u.id}`, label: u.name, clear: () => { selectedUsers.value = selectedUsers.value.filter(x => x.id !== u.id) } })
    }
    for (const tool of selectedTools.value) {
        chips.push({ key: `tool-${tool.name}`, label: tool.name, clear: () => { selectedTools.value = selectedTools.value.filter(x => x.name !== tool.name) } })
    }
    if (toolFailedOnly.value) {
        chips.push({ key: 'failed-only', label: t('monitoring.diagnosis.failedOnly'), clear: () => { toolFailedOnly.value = false } })
    }
    if (selectedToolError.value !== null) {
        chips.push({
            key: 'tool-error',
            label: selectedToolError.value || t('monitoring.diagnosis.noErrorMessage'),
            clear: () => { selectedToolError.value = null }
        })
    }
    for (const tbl of selectedTables.value) {
        chips.push({ key: `table-${tbl.id}`, label: tbl.name, clear: () => { selectedTables.value = selectedTables.value.filter(x => x.id !== tbl.id) } })
    }
    if (searchQuery.value.trim()) {
        chips.push({ key: 'search', label: `"${searchQuery.value.trim()}"`, clear: () => { searchQuery.value = '' } })
    }
    if (selectedDay.value) {
        chips.push({ key: 'day', label: t('monitoring.diagnosis.dayFilterLabel', { date: formatDate(selectedDay.value) }), clear: clearDayFilter })
    }
    return chips
})

// Dropping one agent id mirrors AgentSelector.selectAll: only in-scope ids are
// touched, so a chat-context selection outside monitoring survives.
const removeAgentFromSelection = (id: string) => {
    selectAgents([...selectedAgents.value].filter(a => a !== id))
}

const clearAllFilters = () => {
    bulkFilterChange = true
    const scoped = new Set<string>(consoleSelectedAgents.value)
    selectAgents([...selectedAgents.value].filter(id => !scoped.has(id)))
    selectedUsers.value = []
    selectedTools.value = []
    toolFailedOnly.value = false
    selectedToolError.value = null
    selectedTables.value = []
    searchQuery.value = ''
    selectedDay.value = null
    nextTick(() => { bulkFilterChange = false; refreshAll() })
}


const fetchTimeseries = async () => {
    isTimeseriesLoading.value = true
    try {
        const params = new URLSearchParams()
        if (dateRange.value.start) {
            params.append('start_date', new Date(dateRange.value.start).toISOString())
        }
        if (dateRange.value.end) {
            params.append('end_date', new Date(dateRange.value.end).toISOString())
        }
        appendScopeParams(params)

        const response = await useMyFetch<any>(`/api/console/diagnosis/timeseries?${params}`)
        if (response.data.value) {
            timeseriesPoints.value = response.data.value.points || []
        } else {
            timeseriesPoints.value = []
        }
    } catch (error) {
        console.error('Failed to fetch diagnosis timeseries:', error)
        timeseriesPoints.value = []
    } finally {
        isTimeseriesLoading.value = false
    }
}

const getInstructionsEffectiveness = () => {
    if (instructionsEffectiveness.value === null || instructionsEffectiveness.value === undefined) {
        return t('monitoring.diagnosis.naAbbr')
    }
    return Math.round(instructionsEffectiveness.value)
}

const getDateRangeDays = () => {
    if (!dateRange.value.start || !dateRange.value.end) return '30'
    
    const start = new Date(dateRange.value.start)
    const end = new Date(dateRange.value.end)
    const diffTime = Math.abs(end.getTime() - start.getTime())
    const diffDays = Math.ceil(diffTime / (1000 * 60 * 60 * 24))
    
    return diffDays.toString()
}

// Add this method
const openTrace = (item: any) => {
    selectedTraceItem.value = item
    showTraceModal.value = true
}

const openReport = (item: any) => {
    if (item.report_link) {
        window.open(item.report_link, '_blank')
    }
}

const openTraceFromAE = (item: any) => {
    selectedTraceItem.value = {
        report_id: item.report_id,
        completion_id: item.completion_id || '',
        id: item.completion_id || ''
    }
    showTraceModal.value = true
}

const formatFeedback = (dir: number | null | undefined) => {
    if (dir == null) return '0/0'
    if (dir > 0) return '1/0'
    if (dir < 0) return '0/1'
    return '0/0'
}

const truncate = (text: string, length: number) => {
    if (!text) return ''
    if (text.length <= length) return text
    return text.slice(0, length) + '…'
}

// Filter methods
const handleFilterChange = (filter: { label: string, value: string }) => {
    selectedFilter.value = filter
    currentPage.value = 1
    // The top-errors tab renders the already-fetched groups, not the runs list
    if (filter.value !== 'top_errors') {
        fetchDiagnosisData()
    }
}

// Activity chart bar click -> filter the KPI cards + table to that day (toggle off if same day)
const handleDaySelect = (date: string) => {
    selectedDay.value = selectedDay.value === date ? null : date
    currentPage.value = 1
    Promise.all([
        fetchOverallMetrics(),
        fetchErrorGroups(),
        fetchDiagnosisData()
    ])
}

const clearDayFilter = () => {
    selectedDay.value = null
    currentPage.value = 1
    Promise.all([
        fetchOverallMetrics(),
        fetchErrorGroups(),
        fetchDiagnosisData()
    ])
}



// Users facet for the user filter dropdown
const fetchUserOptions = async () => {
    try {
        const response = await useMyFetch<any>('/api/console/diagnosis/users')
        userOptions.value = response.data.value?.users || []
    } catch (error) {
        console.error('Failed to fetch diagnosis users:', error)
        userOptions.value = []
    }
}

// Tools facet for the tool filter dropdown
const fetchToolOptions = async () => {
    try {
        const response = await useMyFetch<any>('/api/console/diagnosis/tools')
        toolOptions.value = response.data.value?.tools || []
    } catch (error) {
        console.error('Failed to fetch diagnosis tools:', error)
        toolOptions.value = []
    }
}

// Tables facet for the data-table filter dropdown
const fetchTableOptions = async () => {
    try {
        const response = await useMyFetch<any>('/api/console/diagnosis/tables')
        tableOptions.value = (response.data.value?.tables || []).map((t: any) => ({
            ...t,
            label: t.data_source_name ? `${t.name} · ${t.data_source_name}` : t.name
        }))
    } catch (error) {
        console.error('Failed to fetch diagnosis tables:', error)
        tableOptions.value = []
    }
}

// Watch for page changes
watch(currentPage, () => {
    fetchDiagnosisData()
})

// Watch for agent selection changes (also fires once the agent list and the
// permission map land, which is what turns a raw selection into a scoped one)
watch(consoleSelectionKey, () => {
    if (bulkFilterChange) return
    refreshAll()
}, { deep: true })

// Watch for user filter changes
watch(selectedUsers, () => {
    if (bulkFilterChange) return
    if (selectedUsers.value.some(u => u.id === ALL_SENTINEL)) {
        selectedUsers.value = []
        return
    }
    refreshAll()
}, { deep: true })

// Watch for tool / data-table filter changes
watch(selectedTools, () => {
    if (bulkFilterChange) return
    if (selectedTools.value.some(t => t.name === ALL_SENTINEL)) {
        selectedTools.value = []
        return
    }
    // A manual tool change invalidates the exact-error drill-down — the
    // message belongs to the tool it was clicked under. Suppressed so its own
    // watcher doesn't fire a second refresh on top of this one.
    if (selectedToolError.value !== null) {
        bulkFilterChange = true
        selectedToolError.value = null
        nextTick(() => { bulkFilterChange = false })
    }
    refreshAll()
}, { deep: true })

watch(toolFailedOnly, () => {
    if (bulkFilterChange) return
    refreshAll()
})

watch(selectedToolError, () => {
    if (bulkFilterChange) return
    refreshAll()
})

watch(selectedTables, () => {
    if (bulkFilterChange) return
    if (selectedTables.value.some(t => t.id === ALL_SENTINEL)) {
        selectedTables.value = []
        return
    }
    refreshAll()
}, { deep: true })

// Debounced free-text search — filters everything, like the other filters
watch(searchQuery, () => {
    if (bulkFilterChange) return
    if (searchDebounceTimer) clearTimeout(searchDebounceTimer)
    searchDebounceTimer = setTimeout(() => {
        refreshAll()
    }, 400)
})

// Initialize
onMounted(async () => {
    initializeDateRange()
    // Initialize agents for the selector
    await initAgent()
    // Fetch dashboard metrics, timeseries, diagnosis data, and user facet on initial load
    await Promise.all([
        fetchOverallMetrics(),
        fetchTimeseries(),
        fetchErrorGroups(),
        fetchDiagnosisData(),
        fetchUserOptions(),
        fetchToolOptions(),
        fetchTableOptions()
    ])
})
</script>
