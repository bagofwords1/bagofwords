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