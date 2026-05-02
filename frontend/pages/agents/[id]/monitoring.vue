<template>
    <div class="py-6">
        <div v-if="fetchError" />
        <div v-else>
            <!-- Date range picker -->
            <DateRangePicker
                :selected-period="selectedPeriod"
                :date-range="dateRange"
                @period-change="handlePeriodChange"
            />

            <!-- Metric cards -->
            <div class="grid grid-cols-1 md:grid-cols-4 gap-4 mb-6 mt-4">
                <div class="bg-white p-5 border border-gray-200 rounded-xl">
                    <div class="text-2xl font-bold text-gray-900">{{ dashboardMetrics?.failed_queries || 0 }}</div>
                    <div class="text-sm text-gray-500 mt-1">{{ $t('monitoring.diagnosis.cardFailedQueries') }}</div>
                </div>
                <div class="bg-white p-5 border border-gray-200 rounded-xl">
                    <div class="text-2xl font-bold text-gray-900">{{ dashboardMetrics?.negative_feedback || 0 }}</div>
                    <div class="text-sm text-gray-500 mt-1">{{ $t('monitoring.diagnosis.cardNegativeFeedback') }}</div>
                </div>
                <div class="bg-white p-5 border border-gray-200 rounded-xl">
                    <div class="text-2xl font-bold text-gray-900">
                        {{ isJudgeEnabled ? (getInstructionsEffectiveness() + '%') : $t('monitoring.diagnosis.naAbbr') }}
                    </div>
                    <div class="text-sm text-gray-500 mt-1 flex items-center gap-1">
                        {{ $t('monitoring.diagnosis.cardInstructionCoverage') }}
                        <UTooltip :text="isJudgeEnabled ? $t('monitoring.diagnosis.judgeEnabledTooltip') : $t('monitoring.diagnosis.judgeDisabledTooltip')">
                            <UIcon name="i-heroicons-information-circle" class="w-3.5 h-3.5 text-gray-400" />
                        </UTooltip>
                    </div>
                </div>
                <div class="bg-white p-5 border border-gray-200 rounded-xl">
                    <div class="text-2xl font-bold text-gray-900">{{ dashboardMetrics?.total_items || 0 }}</div>
                    <div class="text-sm text-gray-500 mt-1">{{ $t('monitoring.diagnosis.cardTotalAgentRuns') }}</div>
                </div>
            </div>

            <!-- Filter tabs -->
            <div class="border-b border-gray-200 mb-4">
                <nav class="-mb-px flex space-x-6">
                    <button
                        v-for="filter in filterOptions"
                        :key="filter.value"
                        @click="handleFilterChange(filter)"
                        :class="[
                            selectedFilter.value === filter.value
                                ? 'border-blue-500 text-blue-600'
                                : 'border-transparent text-gray-500 hover:text-gray-700 hover:border-gray-300',
                            'whitespace-nowrap py-2 px-1 border-b-2 text-sm font-medium'
                        ]"
                    >
                        {{ filter.label }}
                        <span
                            v-if="filter.count !== undefined && filter.count >= 0"
                            :class="[
                                selectedFilter.value === filter.value ? 'bg-blue-100 text-blue-600' : 'bg-gray-100 text-gray-600',
                                'ms-1.5 py-0.5 px-2 rounded-full text-xs font-medium'
                            ]"
                        >{{ filter.count }}</span>
                    </button>
                </nav>
            </div>

            <!-- Loading -->
            <div v-if="isLoading" class="flex items-center justify-center py-12">
                <div class="animate-spin rounded-full h-5 w-5 border-b-2 border-blue-600 me-2" />
                <span class="text-sm text-gray-500">{{ $t('monitoring.diagnosis.loading') }}</span>
            </div>

            <!-- Execution table -->
            <div v-else class="bg-white border border-gray-200 rounded-lg overflow-hidden">
                <div class="overflow-x-auto">
                    <table class="min-w-full divide-y divide-gray-200">
                        <thead class="bg-gray-50">
                            <tr>
                                <th class="px-6 py-3 text-start text-xs font-medium text-gray-500 uppercase tracking-wider min-w-[280px]">{{ $t('monitoring.diagnosis.colPrompt') }}</th>
                                <th class="px-6 py-3 text-start text-xs font-medium text-gray-500 uppercase tracking-wider">{{ $t('monitoring.diagnosis.colStatus') }}</th>
                                <th class="px-6 py-3 text-start text-xs font-medium text-gray-500 uppercase tracking-wider">{{ $t('monitoring.diagnosis.colData') }}</th>
                                <th class="px-6 py-3 text-start text-xs font-medium text-gray-500 uppercase tracking-wider">{{ $t('monitoring.diagnosis.colTools') }}</th>
                                <th class="px-6 py-3 text-start text-xs font-medium text-gray-500 uppercase tracking-wider">{{ $t('monitoring.diagnosis.colFeedback') }}</th>
                                <th class="px-6 py-3 text-start text-xs font-medium text-gray-500 uppercase tracking-wider">{{ $t('monitoring.diagnosis.colReport') }}</th>
                                <th class="px-6 py-3 text-start text-xs font-medium text-gray-500 uppercase tracking-wider">{{ $t('monitoring.diagnosis.colUser') }}</th>
                                <th class="px-6 py-3 text-start text-xs font-medium text-gray-500 uppercase tracking-wider">{{ $t('monitoring.diagnosis.colDate') }}</th>
                            </tr>
                        </thead>
                        <tbody class="bg-white divide-y divide-gray-200 text-xs">
                            <tr
                                v-for="item in executionItems"
                                :key="item.agent_execution_id"
                                class="hover:bg-gray-50 cursor-pointer"
                                @click="openTraceFromAE(item)"
                            >
                                <td class="px-6 py-4">
                                    <div class="relative group max-w-[280px]">
                                        <p class="truncate text-gray-900">{{ truncate(item.prompt || '', 40) }}</p>
                                        <div class="pointer-events-none absolute start-0 top-full mt-1 z-10 hidden group-hover:block bg-white border border-gray-200 rounded-md shadow-sm p-2 text-xs whitespace-pre-wrap max-w-[480px] max-h-48 overflow-auto">
                                            {{ item.prompt || '—' }}
                                        </div>
                                    </div>
                                </td>
                                <td class="px-6 py-4 whitespace-nowrap">
                                    <span class="inline-flex px-2 py-1 text-xs font-medium rounded-full"
                                        :class="item.agent_execution_status === 'error' ? 'bg-red-100 text-red-800' : 'bg-green-100 text-green-800'">
                                        {{ item.agent_execution_status === 'error' ? $t('monitoring.diagnosis.statusError') : $t('monitoring.diagnosis.statusSuccess') }}
                                    </span>
                                </td>
                                <td class="px-6 py-4">
                                    <div class="flex flex-wrap gap-1 max-w-xs">
                                        <span v-for="(title, idx) in item.step_titles || []" :key="idx"
                                            class="px-2 py-0.5 bg-gray-100 text-gray-700 rounded text-[11px]">{{ title }}</span>
                                        <span v-if="!(item.step_titles || []).length" class="text-gray-400">{{ $t('monitoring.diagnosis.none') }}</span>
                                    </div>
                                </td>
                                <td class="px-6 py-4">
                                    <div class="text-xs text-gray-900">{{ $t('monitoring.diagnosis.totalPrefix', { n: item.total_tools }) }}</div>
                                    <div class="flex items-center gap-3 mt-0.5">
                                        <span class="flex items-center gap-1 text-green-600">
                                            <UIcon name="i-heroicons-check-circle" class="w-3.5 h-3.5" />{{ item.total_successful_tools }}
                                        </span>
                                        <span class="flex items-center gap-1 text-red-600">
                                            <UIcon name="i-heroicons-x-circle" class="w-3.5 h-3.5" />{{ item.total_failed_tools }}
                                        </span>
                                    </div>
                                </td>
                                <td class="px-6 py-4">
                                    <span class="inline-flex px-2 py-1 text-xs font-medium rounded-full"
                                        :class="item.feedback_direction > 0 ? 'bg-green-100 text-green-800' : item.feedback_direction < 0 ? 'bg-red-100 text-red-800' : 'bg-gray-100 text-gray-800'">
                                        {{ item.feedback_direction > 0 ? $t('monitoring.diagnosis.feedbackPositive') : (item.feedback_direction < 0 ? $t('monitoring.diagnosis.feedbackNegative') : $t('monitoring.diagnosis.feedbackNone')) }}
                                    </span>
                                    <div v-if="item.feedback_direction < 0 && item.feedback_message" class="mt-1 text-xs text-gray-500 max-w-xs truncate">
                                        {{ truncate(item.feedback_message, 80) }}
                                    </div>
                                </td>
                                <td class="px-6 py-4 whitespace-nowrap">
                                    <a v-if="item.report_link" :href="item.report_link" class="text-blue-600 hover:underline" @click.stop>
                                        {{ item.report_name || item.report_id }}
                                    </a>
                                    <span v-else class="text-gray-400">—</span>
                                </td>
                                <td class="px-6 py-4 text-gray-700">{{ item.user_name || '—' }}</td>
                                <td class="px-6 py-4 text-gray-500">{{ formatDate(item.created_at) }}</td>
                            </tr>
                        </tbody>
                    </table>
                </div>

                <div v-if="executionItems.length === 0" class="text-center py-12">
                    <UIcon name="i-heroicons-clipboard-document-check" class="mx-auto h-10 w-10 text-gray-300" />
                    <p class="mt-2 text-sm text-gray-500">{{ $t('monitoring.diagnosis.emptyTitle') }}</p>
                </div>
            </div>

            <!-- Pagination -->
            <div v-if="executionItems.length > 0" class="mt-4 flex items-center justify-between">
                <div class="text-xs text-gray-500">
                    {{ $t('monitoring.diagnosis.paginationRange', { start: (currentPage - 1) * pageSize + 1, end: Math.min(currentPage * pageSize, totalItems), total: totalItems }) }}
                </div>
                <div class="flex items-center gap-1">
                    <UButton icon="i-heroicons-chevron-left" color="gray" variant="ghost" size="sm" :disabled="currentPage === 1" @click="currentPage--">
                        {{ $t('monitoring.diagnosis.previous') }}
                    </UButton>
                    <UButton
                        v-for="page in visiblePages" :key="page"
                        :color="page === currentPage ? 'blue' : 'gray'"
                        :variant="page === currentPage ? 'solid' : 'ghost'"
                        size="sm" class="min-w-[32px]"
                        @click="currentPage = page"
                    >{{ page }}</UButton>
                    <UButton icon="i-heroicons-chevron-right" color="gray" variant="ghost" size="sm" :disabled="currentPage === totalPages" @click="currentPage++">
                        {{ $t('monitoring.diagnosis.next') }}
                    </UButton>
                </div>
            </div>
        </div>

        <TraceModal
            v-model="showTraceModal"
            :report-id="selectedTraceItem?.report_id || ''"
            :completion-id="selectedTraceItem?.completion_id || ''"
        />
    </div>
</template>

<script setup lang="ts">
import DateRangePicker from '~/components/console/DateRangePicker.vue'
import TraceModal from '~/components/console/TraceModal.vue'
import type { Ref } from 'vue'

definePageMeta({ auth: true, layout: 'data' })

const { t } = useI18n()
const { isJudgeEnabled } = useOrgSettings()

const integration = inject<Ref<any>>('integration', ref(null))
const fetchError = inject<Ref<number | null>>('fetchError', ref(null))
const agentId = computed(() => integration.value?.id || '')

interface DateRange { start: string; end: string }

const selectedPeriod = ref({ label: t('monitoring.diagnosis.periodAllTime'), value: 'all_time' })
const dateRange = ref<DateRange>({ start: '', end: '' })
const isLoading = ref(false)
const currentPage = ref(1)
const pageSize = ref(20)
const totalItems = ref(0)
const executionItems = ref<any[]>([])
const dashboardMetrics = ref<any>(null)
const instructionsEffectiveness = ref<number | null>(null)
const showTraceModal = ref(false)
const selectedTraceItem = ref<any>(null)

const filterOptions = ref([
    { label: t('monitoring.diagnosis.filterAll'), value: 'all', count: 0 },
    { label: t('monitoring.diagnosis.filterNegative'), value: 'negative_feedback', count: 0 },
    { label: t('monitoring.diagnosis.filterFailed'), value: 'failed_queries', count: 0 },
    { label: t('monitoring.diagnosis.filterLowConfidence'), value: 'low_confidence', count: 0 },
])
const selectedFilter = ref(filterOptions.value[0])

const totalPages = computed(() => Math.ceil(totalItems.value / pageSize.value))
const visiblePages = computed(() => {
    const total = totalPages.value
    const current = currentPage.value
    let start = Math.max(1, current - 2)
    let end = Math.min(total, start + 4)
    if (end - start < 4) start = Math.max(1, end - 4)
    const pages = []
    for (let i = start; i <= end; i++) pages.push(i)
    return pages
})

function getInstructionsEffectiveness() {
    if (instructionsEffectiveness.value == null) return t('monitoring.diagnosis.naAbbr')
    return Math.round(instructionsEffectiveness.value)
}

function formatDate(dateString: string) {
    if (!dateString) return '—'
    try { return new Date(dateString).toLocaleString() } catch { return '—' }
}

function truncate(text: string, length: number) {
    if (!text || text.length <= length) return text
    return text.slice(0, length) + '…'
}

function buildParams() {
    const params = new URLSearchParams()
    if (dateRange.value.start) params.append('start_date', new Date(dateRange.value.start).toISOString())
    if (dateRange.value.end) params.append('end_date', new Date(dateRange.value.end).toISOString())
    if (agentId.value) params.append('data_source_ids', agentId.value)
    return params
}

async function fetchOverallMetrics() {
    if (!agentId.value) return
    try {
        const params = buildParams()
        const [dashRes, judgeRes] = await Promise.all([
            useMyFetch<any>(`/api/console/diagnosis/metrics?${params}`),
            useMyFetch<any>(`/api/console/metrics?${params}`),
        ])
        if (dashRes.data.value) {
            dashboardMetrics.value = dashRes.data.value
            filterOptions.value = [
                { label: t('monitoring.diagnosis.filterAll'), value: 'all', count: dashRes.data.value.total_items },
                { label: t('monitoring.diagnosis.filterNegative'), value: 'negative_feedback', count: dashRes.data.value.negative_feedback },
                { label: t('monitoring.diagnosis.filterFailed'), value: 'failed_queries', count: dashRes.data.value.failed_queries },
                { label: t('monitoring.diagnosis.filterLowConfidence'), value: 'low_confidence', count: dashRes.data.value.low_confidence || 0 },
            ]
        }
        if ((judgeRes.data.value as any)?.instructions_effectiveness != null) {
            instructionsEffectiveness.value = (judgeRes.data.value as any).instructions_effectiveness
        }
    } catch {}
}

async function fetchDiagnosisData() {
    if (!agentId.value) return
    isLoading.value = true
    try {
        const params = buildParams()
        params.set('page', currentPage.value.toString())
        params.set('page_size', pageSize.value.toString())
        if (selectedFilter.value.value !== 'all') params.append('filter', selectedFilter.value.value)

        const res = await useMyFetch<any>(`/api/console/agent_executions/summaries?${params}`)
        if (res.data.value) {
            executionItems.value = res.data.value.items || []
            totalItems.value = res.data.value.total_items || 0
        }
    } catch {
        executionItems.value = []
        totalItems.value = 0
    } finally {
        isLoading.value = false
    }
}

function handleFilterChange(filter: any) {
    selectedFilter.value = filter
    currentPage.value = 1
    fetchDiagnosisData()
}

function handlePeriodChange(period: { label: string; value: string }) {
    selectedPeriod.value = period
    const end = new Date()
    let start: Date | null = null
    if (period.value === '30_days') { start = new Date(); start.setDate(start.getDate() - 30) }
    else if (period.value === '90_days') { start = new Date(); start.setDate(start.getDate() - 90) }
    dateRange.value = {
        start: start ? start.toISOString().split('T')[0] : '',
        end: end.toISOString().split('T')[0],
    }
    currentPage.value = 1
    Promise.all([fetchOverallMetrics(), fetchDiagnosisData()])
}

function openTraceFromAE(item: any) {
    selectedTraceItem.value = { report_id: item.report_id, completion_id: item.completion_id || '' }
    showTraceModal.value = true
}

watch(currentPage, () => fetchDiagnosisData())

watch(agentId, (id) => {
    if (id) Promise.all([fetchOverallMetrics(), fetchDiagnosisData()])
})

onMounted(() => {
    dateRange.value = { start: '', end: new Date().toISOString().split('T')[0] }
    if (agentId.value) Promise.all([fetchOverallMetrics(), fetchDiagnosisData()])
})
</script>
