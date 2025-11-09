<template>
    <div class="mt-6">
        <!-- Top metrics -->
        <div class="grid grid-cols-1 md:grid-cols-2 gap-6 mb-8">
            <div class="bg-white p-6 border border-gray-200 rounded-xl shadow-sm">
                <div class="text-sm font-medium text-gray-600">Total Tests</div>
                <div class="text-2xl font-bold text-gray-900 mt-1">{{ metrics?.total_tests ?? 0 }}</div>
            </div>
            <div class="bg-white p-6 border border-gray-200 rounded-xl shadow-sm">
                <div class="text-sm font-medium text-gray-600">Success Rate</div>
                <div class="text-2xl font-bold text-gray-900 mt-1">{{ formatPercent(metrics?.success_rate) }}</div>
            </div>
        </div>

        <!-- Tabs -->
        <div class="border-b border-gray-200 mb-6">
            <nav class="-mb-px flex space-x-8" aria-label="Tabs">
                <button
                    type="button"
                    @click="activeTab = 'suites'"
                    :class="tabClass('suites')"
                >
                    Test Suites
                </button>
                <button
                    type="button"
                    @click="activeTab = 'runs'"
                    :class="tabClass('runs')"
                >
                    Test Runs
                </button>
            </nav>
        </div>

        <!-- Suites tab -->
        <div v-if="activeTab === 'suites'">
            <!-- Toolbar -->
            <div class="flex items-center justify-between mb-4">
                <div></div>
                <UButton color="blue" icon="i-heroicons-plus" @click="showCreate = true">New Suite</UButton>
            </div>
            <!-- Suites list -->
            <div class="bg-white shadow-sm border border-gray-200 rounded-lg overflow-hidden">
                <div class="px-6 py-4 border-b border-gray-200 flex items-center justify-between">
                    <div class="text-sm font-medium text-gray-700">Test Suites</div>
                </div>
                <div class="overflow-x-auto">
                    <table class="min-w-full divide-y divide-gray-200">
                        <thead class="bg-gray-50">
                            <tr>
                                <th class="px-6 py-3 text-left text-xs font-medium text-gray-500 uppercase tracking-wider">Suite</th>
                                <th class="px-6 py-3 text-left text-xs font-medium text-gray-500 uppercase tracking-wider">Tests</th>
                                <th class="px-6 py-3 text-left text-xs font-medium text-gray-500 uppercase tracking-wider">Last Run</th>
                                <th class="px-6 py-3 text-left text-xs font-medium text-gray-500 uppercase tracking-wider">Last Status</th>
                                <th class="px-6 py-3 text-left text-xs font-medium text-gray-500 uppercase tracking-wider">Pass Rate</th>
                            </tr>
                        </thead>
                        <tbody class="bg-white divide-y divide-gray-200 text-xs">
                            <tr v-for="s in suites" :key="s.id" class="hover:bg-gray-50">
                                <td class="px-6 py-3 text-gray-900">
                                    <a :href="`/monitoring/tests/suites/${s.id}`" class="text-blue-600 hover:underline">{{ s.name }}</a>
                                </td>
                                <td class="px-6 py-3">{{ s.tests_count }}</td>
                                <td class="px-6 py-3">{{ formatDate(s.last_run_at) }}</td>
                                <td class="px-6 py-3">
                                    <span class="inline-flex px-2 py-1 text-xs font-medium rounded-full"
                                          :class="statusClass(s.last_status)">
                                        {{ s.last_status || '—' }}
                                    </span>
                                </td>
                                <td class="px-6 py-3">{{ formatPercent(s.pass_rate) }}</td>
                            </tr>
                            <tr v-if="suites.length === 0">
                                <td colspan="5" class="px-6 py-6 text-center text-gray-500">No test suites found</td>
                            </tr>
                        </tbody>
                    </table>
                </div>
            </div>
        </div>

        <!-- Runs tab -->
        <div v-else>
            <div class="bg-white shadow-sm border border-gray-200 rounded-lg overflow-hidden">
                <div class="px-6 py-4 border-b border-gray-200 flex items-center justify-between">
                    <div class="text-sm font-medium text-gray-700">Recent Test Runs</div>
                </div>
                <div class="overflow-x-auto">
                    <table class="min-w-full divide-y divide-gray-200">
                        <thead class="bg-gray-50">
                            <tr>
                                <th class="px-6 py-3 text-left text-xs font-medium text-gray-500 uppercase tracking-wider">Started</th>
                                <th class="px-6 py-3 text-left text-xs font-medium text-gray-500 uppercase tracking-wider">Suite</th>
                                <th class="px-6 py-3 text-left text-xs font-medium text-gray-500 uppercase tracking-wider">Trigger</th>
                                <th class="px-6 py-3 text-left text-xs font-medium text-gray-500 uppercase tracking-wider">Status</th>
                                <th class="px-6 py-3 text-left text-xs font-medium text-gray-500 uppercase tracking-wider">Results</th>
                                <th class="px-6 py-3 text-left text-xs font-medium text-gray-500 uppercase tracking-wider">Duration</th>
                            </tr>
                        </thead>
                        <tbody class="bg-white divide-y divide-gray-200 text-xs">
                            <tr v-for="r in mockRuns" :key="r.id" class="hover:bg-gray-50">
                                <td class="px-6 py-3 text-gray-900">{{ formatDate(r.started_at) }}</td>
                                <td class="px-6 py-3">{{ r.suite_name }}</td>
                                <td class="px-6 py-3 capitalize">{{ r.trigger_reason || 'manual' }}</td>
                                <td class="px-6 py-3">
                                    <span class="inline-flex px-2 py-1 text-xs font-medium rounded-full"
                                          :class="statusClass(r.status)">
                                        {{ r.status || '—' }}
                                    </span>
                                </td>
                                <td class="px-6 py-3">
                                    <span :class="resultBadgeClass(r)">{{ resultSummary(r) }}</span>
                                </td>
                                <td class="px-6 py-3">{{ formatDuration(r.started_at, r.finished_at) }}</td>
                            </tr>
                            <tr v-if="mockRuns.length === 0">
                                <td colspan="6" class="px-6 py-6 text-center text-gray-500">No test runs yet</td>
                            </tr>
                        </tbody>
                    </table>
                </div>
            </div>
        </div>
    </div>
    <Teleport to="body">
        <CreateTestSuiteModal v-model="showCreate" @created="onSuiteCreated" />
    </Teleport>
</template>

<script setup lang="ts">
definePageMeta({
    layout: 'monitoring'
})

interface TestMetrics {
    total_tests: number
    success_rate: number
}

interface TestSuiteSummary {
    id: string
    name: string
    tests_count: number
    last_run_at?: string
    last_status?: string
    pass_rate?: number
}

const metrics = ref<TestMetrics | null>(null)
const suites = ref<TestSuiteSummary[]>([])
const showCreate = ref(false)
const router = useRouter()
const activeTab = ref<'suites' | 'runs'>('suites')
// Ensure explicit import in case auto-import is off for nested components
import CreateTestSuiteModal from '~/components/monitoring/CreateTestSuiteModal.vue'

const formatPercent = (v?: number | null) => {
    if (v == null) return '0%'
    return `${Math.round((v || 0) * 100)}%`
}

const formatDate = (iso?: string | null) => {
    if (!iso) return '—'
    try {
        return new Date(iso).toLocaleString()
    } catch {
        return '—'
    }
}

const statusClass = (status?: string) => {
    if (status === 'success') return 'bg-green-100 text-green-800'
    if (status === 'error') return 'bg-red-100 text-red-800'
    if (status === 'in_progress') return 'bg-gray-100 text-gray-800'
    return 'bg-gray-100 text-gray-800'
}

const tabClass = (tab: 'suites' | 'runs') => {
    const isActive = activeTab.value === tab
    return [
        'whitespace-nowrap py-4 px-1 border-b-2 text-sm font-medium',
        isActive
            ? 'border-blue-500 text-blue-600'
            : 'border-transparent text-gray-500 hover:text-gray-700 hover:border-gray-300'
    ]
}

interface TestRunRow {
    id: string
    suite_name: string
    trigger_reason: string
    status: 'in_progress' | 'success' | 'error'
    started_at?: string
    finished_at?: string
    results: { total: number; passed: number; failed: number; error: number }
}

// Mock: joined TestRun with aggregated TestResult counts
const mockRuns = ref<TestRunRow[]>([
    {
        id: 'run_1',
        suite_name: 'Revenue Checks',
        trigger_reason: 'manual',
        status: 'success',
        started_at: new Date(Date.now() - 1000 * 60 * 15).toISOString(),
        finished_at: new Date(Date.now() - 1000 * 60 * 14).toISOString(),
        results: { total: 8, passed: 8, failed: 0, error: 0 }
    },
    {
        id: 'run_2',
        suite_name: 'Churn Risk',
        trigger_reason: 'schedule',
        status: 'error',
        started_at: new Date(Date.now() - 1000 * 60 * 60 * 5).toISOString(),
        finished_at: new Date(Date.now() - 1000 * 60 * 60 * 5 + 1000 * 90).toISOString(),
        results: { total: 10, passed: 7, failed: 2, error: 1 }
    },
    {
        id: 'run_3',
        suite_name: 'Cost Guardrails',
        trigger_reason: 'context_change',
        status: 'in_progress',
        started_at: new Date(Date.now() - 1000 * 60 * 2).toISOString(),
        finished_at: undefined,
        results: { total: 12, passed: 5, failed: 1, error: 0 }
    }
])

const resultSummary = (r: TestRunRow) => {
    const { total, passed, failed, error } = r.results
    if (r.status === 'success') return `${passed}/${total} success`
    if (r.status === 'in_progress') return `${passed}/${total} passing…`
    const nonPass = failed + error
    return `${passed}/${total} passing (${nonPass} issues)`
}

const resultBadgeClass = (r: TestRunRow) => {
    if (r.status === 'success') return 'inline-flex px-2 py-1 rounded-full bg-green-100 text-green-800'
    if (r.status === 'in_progress') return 'inline-flex px-2 py-1 rounded-full bg-gray-100 text-gray-800'
    return 'inline-flex px-2 py-1 rounded-full bg-red-100 text-red-800'
}

const formatDuration = (start?: string | null, end?: string | null) => {
    if (!start) return '—'
    const s = new Date(start).getTime()
    const e = end ? new Date(end).getTime() : Date.now()
    const ms = Math.max(0, e - s)
    const secs = Math.round(ms / 1000)
    if (secs < 60) return `${secs}s`
    const mins = Math.floor(secs / 60)
    const rem = secs % 60
    return `${mins}m ${rem}s`
}

onMounted(async () => {
    try {
        const [mRes, sRes] = await Promise.all([
            useMyFetch<TestMetrics>('/api/test/metrics'),
            useMyFetch<TestSuiteSummary[]>('/api/test/suites/summary'),
        ])
        if (mRes.data.value) metrics.value = mRes.data.value
        if (sRes.data.value) suites.value = sRes.data.value
    } catch (e) {
        console.error('Failed to load test dashboard', e)
    }
})

const onSuiteCreated = (suite: any) => {
    router.push(`/monitoring/tests/suites/${suite.id}`)
}
</script>