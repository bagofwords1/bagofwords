<template>
    <div class="mt-6">
        <!-- Top metrics -->
        <div class="grid grid-cols-1 md:grid-cols-3 gap-6 mb-8">
            <div class="bg-white p-6 border border-gray-200 rounded-xl shadow-sm">
                <div class="text-sm font-medium text-gray-600">Total Test Cases</div>
                <div class="text-2xl font-bold text-gray-900 mt-1">{{ metrics?.total_test_cases ?? 0 }}</div>
            </div>
            <div class="bg-white p-6 border border-gray-200 rounded-xl shadow-sm">
                <div class="text-sm font-medium text-gray-600">Total Test Runs</div>
                <div class="text-2xl font-bold text-gray-900 mt-1">{{ metrics?.total_test_runs ?? 0 }}</div>
            </div>
            <div class="bg-white p-6 border border-gray-200 rounded-xl shadow-sm">
                <div class="text-sm font-medium text-gray-600">Last Test Result</div>
                <div class="mt-1">
                    <span v-if="metrics?.last_result_status" :class="['inline-flex items-center px-2 py-1 rounded-full text-xs font-medium', statusClass(derivedStatus(metrics?.last_result_status))]">
                        {{ derivedStatus(metrics?.last_result_status) }}
                    </span>
                    <span v-else class="text-gray-500 text-sm">—</span>
                </div>
                <div class="text-xs text-gray-500 mt-1" v-if="metrics?.last_result_at">
                    {{ formatDate(metrics?.last_result_at) }}
                </div>
            </div>
        </div>

        <!-- Tabs -->
        <div class="border-b border-gray-200 mb-6">
            <nav class="-mb-px flex space-x-8" aria-label="Tabs">
                <button
                    type="button"
                    @click="activeTab = 'tests'"
                    :class="tabClass('tests')"
                >
                    Tests
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

        <!-- Tests tab -->
        <div v-if="activeTab === 'tests'">
            <div class="bg-white shadow-sm border border-gray-200 rounded-lg overflow-hidden">
                <div class="px-6 py-4 border-b border-gray-200">
                    <div class="flex flex-col md:flex-row md:items-center gap-3">
                        <div class="text-sm font-medium text-gray-700 mr-auto">Tests</div>
                        <div class="flex items-center gap-2 w-full md:w-auto">
                            <!-- Suite filter -->
                            <USelectMenu
                                v-model="suiteFilter"
                                :options="suiteFilterOptions"
                                option-attribute="label"
                                value-attribute="value"
                                size="xs"
                                class="text-xs w-full md:w-56"
                            />
                            <!-- Search -->
                            <input
                                v-model="searchTerm"
                                type="text"
                                placeholder="Search tests..."
                                class="border border-gray-300 rounded px-2 py-1 text-xs w-full md:w-56"
                            />
                            <!-- Actions -->
                            <UButton :disabled="selectedIds.size === 0" color="blue" size="xs" icon="i-heroicons-play" @click="runSelected">Run Selected</UButton>
                            <UButton color="blue" size="xs" variant="soft" icon="i-heroicons-plus" @click="addNewTest">Add New Test</UButton>
                        </div>
                    </div>
                </div>
                <div class="overflow-x-auto">
                    <table class="min-w-full divide-y divide-gray-200">
                        <thead class="bg-gray-50">
                            <tr>
                                <th class="px-4 py-3 w-10 text-center">
                                    <input type="checkbox" :checked="allVisibleSelected" @change="toggleAllVisible" />
                                </th>
                                <th class="px-6 py-3 text-left text-xs font-medium text-gray-500 uppercase tracking-wider">Prompt</th>
                                <th class="px-6 py-3 text-left text-xs font-medium text-gray-500 uppercase tracking-wider">Rules</th>
                                <th class="px-6 py-3 text-left text-xs font-medium text-gray-500 uppercase tracking-wider">Suite</th>
                                <th class="px-6 py-3 text-left text-xs font-medium text-gray-500 uppercase tracking-wider">Options</th>
                            </tr>
                        </thead>
                        <tbody class="bg-white divide-y divide-gray-200 text-xs">
                            <tr v-for="c in filteredTests" :key="c.id" class="hover:bg-gray-50">
                                <td class="px-4 py-3 w-10 text-center">
                                    <div class="flex items-center justify-center">
                                        <input type="checkbox" :checked="selectedIds.has(c.id)" @change="toggleOne(c.id)" />
                                    </div>
                                </td>
                                <td class="px-6 py-3">
                                    <span class="block max-w-[520px] truncate" :title="c.prompt_json?.content || ''">{{ c.prompt_json?.content || '—' }}</span>
                                </td>
                                <td class="px-6 py-3 text-gray-700">
                                    <div class="flex flex-wrap gap-1 max-w-[620px]">
                                        <span
                                          v-for="cat in categoriesForCase(c)"
                                          :key="cat"
                                          :class="['inline-flex items-center rounded-full border text-[11px] px-2 py-0.5', badgeClassesFor(cat)]"
                                          :title="cat"
                                        >{{ cat }}</span>
                                    </div>
                                </td>
                                <td class="px-6 py-3">{{ c.suite_name }}</td>
                                <td class="px-6 py-3">
                                    <div class="flex items-center gap-2">
                                        <UButton color="gray" size="xs" variant="soft" icon="i-heroicons-pencil-square" @click="editCase(c)">Edit</UButton>
                                        <UButton color="blue" size="xs" variant="soft" icon="i-heroicons-play" @click="runCase(c)">Run Test</UButton>
                                        <UButton color="red" size="xs" variant="soft" icon="i-heroicons-trash" @click="deleteCase(c)">Delete</UButton>
                                    </div>
                                </td>
                            </tr>
                            <tr v-if="filteredTests.length === 0">
                                <td colspan="5" class="px-6 py-6 text-center text-gray-500">No tests found</td>
                            </tr>
                        </tbody>
                    </table>
                </div>
                <div class="px-6 py-3 border-t border-gray-200 flex flex-col md:flex-row gap-3 md:items-center justify-between">
                    <div class="text-xs text-gray-500">Page {{ testsPage }} • Showing {{ filteredTests.length }} items</div>
                    <div class="flex items-center gap-2">
                        <USelectMenu
                          v-model="testsLimit"
                          :options="pageSizeOptions"
                          option-attribute="label"
                          value-attribute="value"
                          size="xs"
                          class="text-xs w-24"
                        />
                        <UButton size="xs" variant="soft" :disabled="testsPage <= 1" @click="prevTestsPage">Prev</UButton>
                        <UButton size="xs" variant="soft" :disabled="!testsHasNext" @click="nextTestsPage">Next</UButton>
                    </div>
                </div>
            </div>
        </div>

        <!-- Runs tab -->
        <div v-else>
            <div class="bg-white shadow-sm border border-gray-200 rounded-lg overflow-hidden">
                <div class="px-6 py-4 border-b border-gray-200">
                    <div class="flex flex-col md:flex-row md:items-center gap-3">
                        <div class="text-sm font-medium text-gray-700 mr-auto">Recent Test Runs</div>
                        <div class="flex items-center gap-2 w-full md:w-auto">
                            <USelectMenu
                              v-model="runSuiteFilter"
                              :options="suiteFilterOptions"
                              option-attribute="label"
                              value-attribute="value"
                              size="xs"
                              class="text-xs w-full md:w-48"
                            />
                            <USelectMenu
                              v-model="runCaseFilter"
                              :options="runCaseOptions"
                              option-attribute="label"
                              value-attribute="value"
                              size="xs"
                              class="text-xs w-full md:w-56"
                            />
                            <input
                              v-model="runSearchTerm"
                              type="text"
                              placeholder="Search runs..."
                              class="border border-gray-300 rounded px-2 py-1 text-xs w-full md:w-56"
                            />
                        </div>
                    </div>
                </div>
                <div class="overflow-x-auto">
                    <table class="min-w-full divide-y divide-gray-200">
                        <thead class="bg-gray-50">
                            <tr>
                                <th class="px-6 py-3 text-left text-xs font-medium text-gray-500 uppercase tracking-wider">Title</th>
                                <th class="px-6 py-3 text-left text-xs font-medium text-gray-500 uppercase tracking-wider">Started</th>
                                <th class="px-6 py-3 text-left text-xs font-medium text-gray-500 uppercase tracking-wider">Trigger</th>
                                <th class="px-6 py-3 text-left text-xs font-medium text-gray-500 uppercase tracking-wider">Status</th>
                                <th class="px-6 py-3 text-left text-xs font-medium text-gray-500 uppercase tracking-wider">Results</th>
                                <th class="px-6 py-3 text-left text-xs font-medium text-gray-500 uppercase tracking-wider">Duration</th>
                            </tr>
                        </thead>
                        <tbody class="bg-white divide-y divide-gray-200 text-xs">
                            <tr v-for="r in filteredRuns" :key="r.id" class="hover:bg-gray-50">
                                <td class="px-6 py-3 text-gray-900">
                                    <a :href="`/evals/runs/${r.id}`" class="text-blue-600 hover:underline">
                                        {{ r.title || 'Test Run' }}
                                    </a>
                                </td>
                                <td class="px-6 py-3">{{ formatDate(r.started_at) }}</td>
                                <td class="px-6 py-3 capitalize">{{ r.trigger_reason || 'manual' }}</td>
                                <td class="px-6 py-3">
                                    <span class="inline-flex px-2 py-1 text-xs font-medium rounded-full" :class="runStatusClass(r)">
                                        {{ derivedRunStatus(r) || '—' }}
                                    </span>
                                </td>
                                <td class="px-6 py-3">
                                    <span :class="resultBadgeClassByStatus(derivedRunStatus(r))">{{ resultSummaryReal(r) }}</span>
                                </td>
                                <td class="px-6 py-3">{{ formatDuration(r.started_at, r.finished_at) }}</td>
                            </tr>
                            <tr v-if="filteredRuns.length === 0">
                                <td colspan="6" class="px-6 py-6 text-center text-gray-500">No test runs yet</td>
                            </tr>
                        </tbody>
                    </table>
                </div>
                <div class="px-6 py-3 border-t border-gray-200 flex flex-col md:flex-row gap-3 md:items-center justify-between">
                    <div class="text-xs text-gray-500">Page {{ runsPage }} • Showing {{ filteredRuns.length }} items</div>
                    <div class="flex items-center gap-2">
                        <USelectMenu
                          v-model="runsLimit"
                          :options="pageSizeOptions"
                          option-attribute="label"
                          value-attribute="value"
                          size="xs"
                          class="text-xs w-24"
                        />
                        <UButton size="xs" variant="soft" :disabled="runsPage <= 1" @click="prevRunsPage">Prev</UButton>
                        <UButton size="xs" variant="soft" :disabled="!runsHasNext" @click="nextRunsPage">Next</UButton>
                    </div>
                </div>
            </div>
        </div>
    </div>
    <Teleport to="body">
        <AddTestCaseModal v-model="showAddCase" :suite-id="selectedSuiteId" :case-id="selectedCaseId" @created="onCaseCreated" @updated="onCaseUpdated" />
    </Teleport>
</template>

<script setup lang="ts">
definePageMeta({
    layout: 'default'
})

interface TestMetrics {
    total_test_cases: number
    total_test_runs: number
    last_result_status?: string | null
    last_result_at?: string | null
}

const metrics = ref<TestMetrics | null>(null)
const tests = ref<TestCaseRow[]>([])
const searchTerm = ref('')
const suiteFilter = ref<string>('all')
const selectedIds = ref<Set<string>>(new Set())
const testsPage = ref<number>(1)
const testsLimit = ref<number>(20)
const testsHasNext = computed(() => filteredTests.value.length >= testsLimit.value)
const pageSizeOptions = [
    { label: '10 / page', value: 10 },
    { label: '20 / page', value: 20 },
    { label: '50 / page', value: 50 },
]
// Server-side filtering; no client text filter needed beyond displaying returned results
const filteredTests = computed(() => tests.value)
const allVisibleSelected = computed(() => filteredTests.value.length > 0 && filteredTests.value.every(t => selectedIds.value.has(t.id)))
const suitesOrdered = ref<TestSuiteItem[]>([])
const suiteFilterOptions = computed(() => {
    const opts = [{ label: 'All suites', value: 'all' }]
    const entries = (suitesOrdered.value || []).map(s => ({ label: s.name, value: s.id }))
    return [...opts, ...entries]
})
const showAddCase = ref(false)
const selectedSuiteId = ref<string>('')
const selectedCaseId = ref<string>('')
const router = useRouter()
const activeTab = ref<'tests' | 'runs'>('tests')
// Components
import AddTestCaseModal from '~/components/monitoring/AddTestCaseModal.vue'
const toast = useToast()

const derivedStatus = (s?: string | null) => {
    if (!s) return '—'
    // Normalize 'success' -> 'success', others pass through
    const map: Record<string, string> = {
        'pass': 'pass',
        'fail': 'fail',
        'error': 'error',
        'success': 'success',
        'stopped': 'stopped',
        'in_progress': 'in_progress',
    }
    return map[s] || s
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

const tabClass = (tab: 'tests' | 'runs') => {
    const isActive = activeTab.value === tab
    return [
        'whitespace-nowrap py-4 px-1 border-b-2 text-sm font-medium',
        isActive
            ? 'border-blue-500 text-blue-600'
            : 'border-transparent text-gray-500 hover:text-gray-700 hover:border-gray-300'
    ]
}

// ---- Tests table state/helpers ----
interface TestSuiteItem { id: string; name: string }
interface TestCaseRow {
    id: string
    suite_id: string
    suite_name: string
    prompt_json?: { content?: string }
    expectations_json?: { rules?: any[] }
    data_source_ids_json?: string[]
}

const suitesById = ref<Record<string, string>>({})
interface RunItem { id: string; title?: string; started_at?: string; trigger_reason?: string; status?: string; finished_at?: string }
const runs = ref<RunItem[]>([])
const runResults = ref<Record<string, { total: number; passed: number; failed: number; error: number }>>({})
const runResultsCaseIds = ref<Record<string, Set<string>>>({})
const runSuiteFilter = ref<string>('all')
const runCaseFilter = ref<string>('all')
const runSearchTerm = ref<string>('')
const runsPage = ref<number>(1)
const runsLimit = ref<number>(20)
const runsHasNext = computed(() => runs.value.length >= runsLimit.value)
const runCaseOptions = computed(() => {
    const base = [{ label: 'All cases', value: 'all' }]
    // If a suite is chosen, list cases for that suite; otherwise list all known tests
    const chosenSuite = runSuiteFilter.value !== 'all' ? runSuiteFilter.value : null
    const source = (tests.value || []).filter(t => (chosenSuite ? t.suite_id === chosenSuite : true))
    const seen = new Set<string>()
    const opts = source.map(t => {
        if (seen.has(t.id)) return null
        seen.add(t.id)
        const label = (t.prompt_json?.content || '').slice(0, 80) || t.id
        return { label, value: t.id }
    }).filter(Boolean) as { label: string; value: string }[]
    return [...base, ...opts]
})
const filteredRuns = computed(() => {
    const term = (runSearchTerm.value || '').toLowerCase().trim()
    const caseId = runCaseFilter.value !== 'all' ? runCaseFilter.value : null
    return (runs.value || []).filter(r => {
        if (runSuiteFilter.value !== 'all') {
            // server already filtered when loading; keep as soft guard
        }
        if (term) {
            const t = (r.title || '').toLowerCase()
            const trig = (r.trigger_reason || '').toLowerCase()
            if (!t.includes(term) && !trig.includes(term)) return false
        }
        if (caseId) {
            const set = runResultsCaseIds.value[r.id] || new Set<string>()
            if (!set.has(caseId)) return false
        }
        return true
    })
})

const CATEGORY_LABELS: Record<string, string> = {
    'tool:create_data': 'Create Data',
    'tool:clarify': 'Clarify',
    'tool:describe_table': 'Describe Table',
    'metadata': 'Metadata',
    'completion': 'Completion',
    'judge': 'Judge',
}

function categoryName(cat: string): string {
    if (!cat) return ''
    const known = CATEGORY_LABELS[cat]
    if (known) return known
    if (cat.startsWith('tool:')) {
        const raw = cat.split(':')[1] || ''
        const spaced = raw.replace(/_/g, ' ')
        return spaced.replace(/\b\w/g, (m) => m.toUpperCase())
    }
    return cat
}

function summaryForCase(c: TestCaseRow): string {
    const rules = (c as any)?.expectations_json?.rules || []
    if (!Array.isArray(rules) || rules.length === 0) return '—'
    const seen = new Set<string>()
    for (const r of rules) {
        if (r?.type === 'field' && r?.target?.category) {
            const cat = String(r.target.category)
            seen.add(categoryName(cat))
        } else if (r?.type === 'tool.calls' && r?.tool) {
            seen.add(categoryName(`tool:${r.tool}`))
        } else {
            // ignore ordering or unknown types for the summary
        }
    }
    return Array.from(seen).join(' · ')
}

function categoriesForCase(c: TestCaseRow): string[] {
    const text = summaryForCase(c)
    if (text === '—') return []
    return text.split(' · ').filter(Boolean)
}

function badgeClassesFor(catLabel: string): string {
    const map: Record<string, string> = {
        'Create Data': 'bg-blue-50 text-blue-700 border-blue-100',
        'Clarify': 'bg-amber-50 text-amber-700 border-amber-100',
        'Describe Table': 'bg-teal-50 text-teal-700 border-teal-100',
        'Metadata': 'bg-slate-50 text-slate-700 border-slate-100',
        'Completion': 'bg-purple-50 text-purple-700 border-purple-100',
        'Judge': 'bg-gray-100 text-gray-700 border-gray-200',
    }
    return map[catLabel] || 'bg-zinc-50 text-zinc-700 border-zinc-100'
}

async function loadSuites() {
    const suitesRes = await useMyFetch<TestSuiteItem[]>('/api/tests/suites?limit=100')
    const suitesList = (suitesRes.data.value || []) as TestSuiteItem[]
    suitesOrdered.value = suitesList
    suitesById.value = Object.fromEntries(suitesList.map(s => [s.id, s.name]))
}

async function loadCases() {
    const params = new URLSearchParams()
    if (suiteFilter.value !== 'all') params.set('suite_id', suiteFilter.value)
    if ((searchTerm.value || '').trim().length > 0) params.set('search', searchTerm.value.trim())
    params.set('page', String(testsPage.value))
    params.set('limit', String(testsLimit.value))
    const url = `/api/tests/cases?${params.toString()}`
    const casesRes = await useMyFetch<any[]>(url)
    const items = (casesRes.data.value || []) as any[]
    tests.value = items.map((c: any) => ({
        id: c.id,
        suite_id: c.suite_id,
        suite_name: suitesById.value[c.suite_id] || c.suite_id,
        prompt_json: c.prompt_json,
        expectations_json: c.expectations_json,
        data_source_ids_json: c.data_source_ids_json || [],
    }))
    // Clear selections when page changes
    selectedIds.value = new Set()
}

async function loadRuns() {
    try {
        const params = new URLSearchParams()
        if (runSuiteFilter.value !== 'all') params.set('suite_id', runSuiteFilter.value)
        params.set('page', String(runsPage.value))
        params.set('limit', String(runsLimit.value))
        const res = await useMyFetch<RunItem[]>(`/api/tests/runs?${params.toString()}`)
        runs.value = (res.data.value as any[]) || []
        // fetch results per run to compute summary
        const fetches = (runs.value || []).map(r => useMyFetch<any[]>(`/api/tests/runs/${r.id}/results`))
        const responses = await Promise.all(fetches)
        const map: Record<string, { total: number; passed: number; failed: number; error: number }> = {}
        const caseMap: Record<string, Set<string>> = {}
        for (let i = 0; i < responses.length; i++) {
            const r = runs.value[i]
            const rows = (responses[i].data.value as any[]) || []
            const summary = { total: rows.length, passed: 0, failed: 0, error: 0 }
            for (const it of rows) {
                if (it.status === 'pass') summary.passed++
                else if (it.status === 'fail') summary.failed++
                else if (it.status === 'error') summary.error++
                if (!caseMap[r.id]) caseMap[r.id] = new Set<string>()
                if (it.case_id) caseMap[r.id].add(String(it.case_id))
            }
            map[r.id] = summary
        }
        runResults.value = map
        runResultsCaseIds.value = caseMap
    } catch (e) {
        runs.value = []
        runResults.value = {}
        runResultsCaseIds.value = {}
    }
}

function toggleOne(id: string) {
    const s = new Set(selectedIds.value)
    if (s.has(id)) s.delete(id)
    else s.add(id)
    selectedIds.value = s
}

function toggleAllVisible() {
    const s = new Set(selectedIds.value)
    const allSelected = filteredTests.value.every(t => s.has(t.id))
    if (allSelected) {
        for (const t of filteredTests.value) s.delete(t.id)
    } else {
        for (const t of filteredTests.value) s.add(t.id)
    }
    selectedIds.value = s
}

function editCase(c: TestCaseRow) {
    selectedSuiteId.value = c.suite_id
    selectedCaseId.value = c.id
    showAddCase.value = true
}

function goRuns() {
    activeTab.value = 'runs'
}

function resultBadgeClassByStatus(status?: string) {
    if (status === 'success') return 'inline-flex px-2 py-1 rounded-full bg-green-100 text-green-800'
    if (status === 'in_progress') return 'inline-flex px-2 py-1 rounded-full bg-gray-100 text-gray-800'
    if (status === 'fail') return 'inline-flex px-2 py-1 rounded-full bg-red-100 text-red-800'
    return 'inline-flex px-2 py-1 rounded-full bg-gray-100 text-gray-800'
}

function resultSummaryReal(r: RunItem) {
    const c = runResults.value[r.id] || { total: 0, passed: 0, failed: 0, error: 0 }
    // Show compact ratio, independent of status text
    return `${c.passed}/${c.total}`
}

function derivedRunStatus(r: RunItem) {
    const c = runResults.value[r.id] || { total: 0, passed: 0, failed: 0, error: 0 }
    if (r.status === 'in_progress') return 'in_progress'
    if (c.total > 0 && c.passed === c.total) return 'success'
    if (c.total > 0 && c.passed < c.total) return 'fail'
    // fallback to backend status if no results
    return r.status || 'in_progress'
}

function runStatusClass(r: RunItem) {
    const s = derivedRunStatus(r)
    if (s === 'success') return 'bg-green-100 text-green-800'
    if (s === 'fail') return 'bg-red-100 text-red-800'
    return 'bg-gray-100 text-gray-800'
}

async function runSelected() {
    try {
        if (selectedIds.value.size === 0) return
        const selectedCaseIds = tests.value.filter(t => selectedIds.value.has(t.id)).map(t => t.id)
        const res: any = await useMyFetch('/api/tests/runs', {
            method: 'POST',
            body: { case_ids: selectedCaseIds, trigger_reason: 'manual' }
        })
        const first = res?.data?.value
        if (first?.id) router.push(`/evals/runs/${first.id}`)
        else activeTab.value = 'runs'
    } catch (e) {
        console.error('Failed to run selected tests', e)
    }
}

function addNewTest() {
    let suiteId = suiteFilter.value !== 'all'
        ? suiteFilter.value
        : ((suitesOrdered.value[0]?.id) || (Object.keys(suitesById.value || {})[0] || ''))
    // Fallback: derive suite from existing tests if suites list hasn't loaded yet
    if (!suiteId && tests.value.length > 0) {
        suiteId = tests.value[0].suite_id
    }
    selectedSuiteId.value = suiteId || ''
    // Ensure we are not in edit mode when adding a new test
    selectedCaseId.value = ''
    showAddCase.value = true
}

async function runCase(c: TestCaseRow) {
    try {
        const res: any = await useMyFetch('/api/tests/runs', {
            method: 'POST',
            body: { case_ids: [c.id], trigger_reason: 'manual' }
        })
        if (res?.error?.value) throw res.error.value
        const run = res?.data?.value
        if (run?.id) router.push(`/evals/runs/${run.id}`)
    } catch (e) {
        console.error('Failed to run test', e)
    }
}

async function deleteCase(c: TestCaseRow) {
    try {
        const ok = window.confirm('Delete this test case? This cannot be undone.')
        if (!ok) return
        const res: any = await useMyFetch(`/api/tests/cases/${c.id}`, { method: 'DELETE' })
        if (res?.error?.value) throw res.error.value
        // Remove from local state
        tests.value = tests.value.filter(t => t.id !== c.id)
        const s = new Set(selectedIds.value)
        s.delete(c.id)
        selectedIds.value = s
        toast.add({ title: 'Test case deleted', icon: 'i-heroicons-check-circle', color: 'green' })
    } catch (e) {
        console.error('Failed to delete test case', e)
        try {
            const err: any = (e as any) || {}
            const detail = err?.data?.detail || err?.data?.message || err?.message || 'Failed to delete test case'
            toast.add({ title: 'Failed to delete test case', description: String(detail), icon: 'i-heroicons-x-circle', color: 'red' })
        } catch {}
    }
}

function onCaseCreated(c: any) {
    // Insert the new case into the list
    const insertRow = (suiteName: string) => {
        const row: TestCaseRow = {
            id: c.id,
            suite_id: c.suite_id,
            suite_name: suiteName || suitesById.value[c.suite_id] || '—',
            prompt_json: c.prompt_json,
            expectations_json: c.expectations_json,
            data_source_ids_json: c.data_source_ids_json || [],
        }
        tests.value = [...tests.value, row]
    }
    // Ensure suite map has the title; if not, fetch it once
    if (!suitesById.value[c.suite_id]) {
        useMyFetch(`/api/tests/suites/${c.suite_id}`).then((res: any) => {
            const suite = res?.data?.value
            if (suite?.id && suite?.name) {
                suitesById.value = { ...suitesById.value, [suite.id]: suite.name }
                const exists = (suitesOrdered.value || []).some(s => s.id === suite.id)
                if (!exists) suitesOrdered.value = [...suitesOrdered.value, { id: suite.id, name: suite.name }]
                insertRow(suite.name)
            } else {
                insertRow('—')
            }
        }).catch(() => insertRow('—')).finally(() => {
            toast.add({ title: 'Test case created', icon: 'i-heroicons-check-circle', color: 'green' })
        })
    } else {
        insertRow(suitesById.value[c.suite_id])
        toast.add({ title: 'Test case created', icon: 'i-heroicons-check-circle', color: 'green' })
    }
    selectedCaseId.value = ''
}

function onCaseUpdated(c: any) {
    const updatedRow: TestCaseRow = {
        id: c.id,
        suite_id: c.suite_id,
        suite_name: suitesById.value[c.suite_id] || '—',
        prompt_json: c.prompt_json,
        expectations_json: c.expectations_json,
        data_source_ids_json: c.data_source_ids_json || [],
    }
    const idx = tests.value.findIndex(t => t.id === c.id)
    if (idx >= 0) {
        const copy = [...tests.value]
        copy[idx] = updatedRow
        tests.value = copy
    } else {
        tests.value = [...tests.value, updatedRow]
    }
    selectedCaseId.value = ''
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

function prevTestsPage() {
    if (testsPage.value <= 1) return
    testsPage.value -= 1
    loadCases()
}
function nextTestsPage() {
    if (!testsHasNext.value) return
    testsPage.value += 1
    loadCases()
}
function prevRunsPage() {
    if (runsPage.value <= 1) return
    runsPage.value -= 1
    loadRuns()
}
function nextRunsPage() {
    if (!runsHasNext.value) return
    runsPage.value += 1
    loadRuns()
}

onMounted(async () => {
    try {
        const [mRes] = await Promise.all([
            useMyFetch<TestMetrics>('/api/tests/metrics'),
        ])
        if (mRes.data.value) metrics.value = mRes.data.value
        await loadSuites()
        await loadCases()
        await loadRuns()
    } catch (e) {
        console.error('Failed to load test dashboard', e)
    }
})

// Re-fetch when filters change (debounced search)
let _searchTimer: any = null
watch([suiteFilter, searchTerm], () => {
    if (_searchTimer) clearTimeout(_searchTimer)
    _searchTimer = setTimeout(() => {
        testsPage.value = 1
        loadCases()
    }, 300)
})

// Watch pagination size for tests
watch(testsLimit, () => {
    testsPage.value = 1
    loadCases()
})

// Re-fetch runs when run filters change (debounced)
let _runTimer: any = null
watch([runSuiteFilter, runCaseFilter, runSearchTerm], () => {
    if (_runTimer) clearTimeout(_runTimer)
    _runTimer = setTimeout(() => {
        runsPage.value = 1
        loadRuns()
    }, 300)
})
watch(runsLimit, () => {
    runsPage.value = 1
    loadRuns()
})
</script>