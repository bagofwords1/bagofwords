<template>
    <div class="py-6">
        <div v-if="fetchError" />
        <div v-else>
            <!-- Metric cards -->
            <div class="grid grid-cols-1 md:grid-cols-3 gap-4 mb-6">
                <div class="bg-white dark:bg-gray-900 p-5 border border-gray-200 dark:border-gray-700 rounded-xl">
                    <div class="text-2xl font-bold text-gray-900 dark:text-white">{{ agentCases.length }}</div>
                    <div class="text-sm text-gray-500 dark:text-gray-400 mt-1">{{ $t('evals.totalTestCases') }}</div>
                </div>
                <div class="bg-white dark:bg-gray-900 p-5 border border-gray-200 dark:border-gray-700 rounded-xl">
                    <div class="text-2xl font-bold text-gray-900 dark:text-white">{{ agentRuns.length }}</div>
                    <div class="text-sm text-gray-500 dark:text-gray-400 mt-1">{{ $t('evals.totalTestRuns') }}</div>
                </div>
                <div class="bg-white dark:bg-gray-900 p-5 border border-gray-200 dark:border-gray-700 rounded-xl">
                    <div class="mt-0.5">
                        <span v-if="lastRunStatus" :class="['inline-flex items-center px-2 py-1 rounded-full text-xs font-medium', statusClass(lastRunStatus)]">
                            {{ localizedStatus(lastRunStatus) }}
                        </span>
                        <span v-else class="text-gray-400 text-sm">—</span>
                    </div>
                    <div class="text-sm text-gray-500 dark:text-gray-400 mt-1">{{ $t('evals.lastTestResult') }}</div>
                </div>
            </div>

            <!-- Sub-tabs -->
            <div class="border-b border-gray-200 dark:border-gray-700 mb-4">
                <nav class="-mb-px flex space-x-8">
                    <button type="button" @click="activeTab = 'tests'" :class="tabClass('tests')">{{ $t('evals.tabs.tests') }}</button>
                    <button type="button" @click="activeTab = 'runs'" :class="tabClass('runs')">{{ $t('evals.tabs.runs') }}</button>
                </nav>
            </div>

            <!-- Tests tab -->
            <div v-if="activeTab === 'tests'">
                <div class="bg-white dark:bg-gray-900 border border-gray-200 dark:border-gray-700 rounded-lg overflow-hidden">
                    <div class="px-5 py-3 border-b border-gray-200 dark:border-gray-700 flex items-center gap-3">
                        <div class="text-sm font-medium text-gray-700 dark:text-gray-300 me-auto">{{ $t('evals.tests.title') }}</div>
                        <input
                            v-model="searchTerm"
                            type="text"
                            :placeholder="$t('evals.tests.search')"
                            class="border border-gray-300 dark:border-gray-600 rounded px-2 py-1 text-xs w-44"
                        />
                        <UButton :disabled="selectedIds.size === 0" color="blue" size="xs" icon="i-heroicons-play" @click="runSelected">
                            {{ $t('evals.tests.runSelected') }}
                        </UButton>
                        <UButton color="blue" size="xs" variant="soft" icon="i-heroicons-plus" @click="addNewTest">
                            {{ $t('evals.tests.addNew') }}
                        </UButton>
                    </div>
                    <div class="overflow-x-auto">
                        <table class="min-w-full divide-y divide-gray-200 dark:divide-gray-700">
                            <thead class="bg-gray-50 dark:bg-gray-900">
                                <tr>
                                    <th class="px-4 py-3 w-10">
                                        <input type="checkbox" :checked="allVisibleSelected" @change="toggleAllVisible" />
                                    </th>
                                    <th class="px-6 py-3 text-start text-xs font-medium text-gray-500 dark:text-gray-400 uppercase tracking-wider">{{ $t('evals.tests.colPrompt') }}</th>
                                    <th class="px-6 py-3 text-start text-xs font-medium text-gray-500 dark:text-gray-400 uppercase tracking-wider">{{ $t('evals.tests.colRules') }}</th>
                                    <th class="px-6 py-3 text-start text-xs font-medium text-gray-500 dark:text-gray-400 uppercase tracking-wider">{{ $t('evals.tests.colSuite') }}</th>
                                    <th class="px-6 py-3 text-start text-xs font-medium text-gray-500 dark:text-gray-400 uppercase tracking-wider">{{ $t('evals.tests.colOptions') }}</th>
                                </tr>
                            </thead>
                            <tbody class="bg-white dark:bg-gray-900 divide-y divide-gray-200 dark:divide-gray-700 text-xs">
                                <tr v-if="loadingCases">
                                    <td colspan="5" class="px-6 py-6 text-center text-gray-400 text-xs">{{ $t('common.loading') }}</td>
                                </tr>
                                <tr v-for="c in pagedCases" :key="c.id" class="hover:bg-gray-50 dark:hover:bg-gray-800">
                                    <td class="px-4 py-3 w-10 text-center">
                                        <input type="checkbox" :checked="selectedIds.has(c.id)" @change="toggleOne(c.id)" />
                                    </td>
                                    <td class="px-6 py-3">
                                        <div class="flex items-center gap-1.5 max-w-[520px]">
                                            <span v-if="c.status === 'draft'" class="inline-flex items-center rounded-full bg-amber-100 text-amber-800 text-[10px] font-medium px-2 py-0.5 shrink-0">Draft</span>
                                            <span v-else-if="c.status === 'archived'" class="inline-flex items-center rounded-full bg-gray-200 dark:bg-gray-700 text-gray-700 dark:text-gray-300 text-[10px] font-medium px-2 py-0.5 shrink-0">Archived</span>
                                            <span v-if="c.auto_generated" class="inline-flex items-center rounded-full bg-purple-100 text-purple-800 text-[10px] font-medium px-2 py-0.5 shrink-0">Auto</span>
                                            <span class="truncate flex-1" :title="c.prompt_json?.content || ''">{{ c.prompt_json?.content || '—' }}</span>
                                        </div>
                                    </td>
                                    <td class="px-6 py-3 text-gray-700 dark:text-gray-300">
                                        <div class="flex flex-wrap gap-1 max-w-xs">
                                            <span
                                                v-for="cat in categoriesForCase(c)"
                                                :key="cat.key"
                                                :class="['inline-flex items-center rounded-full border text-[11px] px-2 py-0.5', badgeClassesFor(cat.key)]"
                                            >{{ cat.label }}</span>
                                        </div>
                                    </td>
                                    <td class="px-6 py-3">{{ c.suite_name }}</td>
                                    <td class="px-6 py-3">
                                        <div class="flex items-center gap-1.5">
                                            <UButton v-if="c.status === 'draft'" color="green" size="xs" variant="soft" icon="i-heroicons-check-badge" @click="promoteCase(c)">Promote</UButton>
                                            <UButton color="gray" size="xs" variant="soft" icon="i-heroicons-pencil-square" @click="editCase(c)">{{ $t('evals.tests.actionEdit') }}</UButton>
                                            <UButton color="blue" size="xs" variant="soft" icon="i-heroicons-play" @click="runCase(c)">{{ $t('evals.tests.actionRunTest') }}</UButton>
                                            <UButton color="red" size="xs" variant="soft" icon="i-heroicons-trash" @click="deleteCase(c)">{{ $t('evals.tests.actionDelete') }}</UButton>
                                        </div>
                                    </td>
                                </tr>
                                <tr v-if="!loadingCases && pagedCases.length === 0">
                                    <td colspan="5" class="px-6 py-6 text-center text-gray-500 dark:text-gray-400">{{ $t('evals.tests.empty') }}</td>
                                </tr>
                            </tbody>
                        </table>
                    </div>
                    <div class="px-5 py-3 border-t border-gray-200 dark:border-gray-700 flex items-center justify-between">
                        <div class="text-xs text-gray-500 dark:text-gray-400">{{ $t('evals.pagination.showing', { page: casesPage, n: pagedCases.length }) }}</div>
                        <div class="flex items-center gap-1.5">
                            <UButton size="xs" variant="soft" :disabled="casesPage <= 1" @click="casesPage--">{{ $t('evals.pagination.prev') }}</UButton>
                            <UButton size="xs" variant="soft" :disabled="!casesHasNext" @click="casesPage++">{{ $t('evals.pagination.next') }}</UButton>
                        </div>
                    </div>
                </div>
            </div>

            <!-- Runs tab -->
            <div v-else>
                <div class="bg-white dark:bg-gray-900 border border-gray-200 dark:border-gray-700 rounded-lg overflow-hidden">
                    <div class="px-5 py-3 border-b border-gray-200 dark:border-gray-700">
                        <div class="text-sm font-medium text-gray-700 dark:text-gray-300">{{ $t('evals.runs.title') }}</div>
                    </div>
                    <div class="overflow-x-auto">
                        <table class="min-w-full divide-y divide-gray-200 dark:divide-gray-700">
                            <thead class="bg-gray-50 dark:bg-gray-900">
                                <tr>
                                    <th class="px-6 py-3 text-start text-xs font-medium text-gray-500 dark:text-gray-400 uppercase tracking-wider">{{ $t('evals.runs.colTitle') }}</th>
                                    <th class="px-6 py-3 text-start text-xs font-medium text-gray-500 dark:text-gray-400 uppercase tracking-wider">{{ $t('evals.runs.colStarted') }}</th>
                                    <th class="px-6 py-3 text-start text-xs font-medium text-gray-500 dark:text-gray-400 uppercase tracking-wider">{{ $t('evals.runs.colTrigger') }}</th>
                                    <th class="px-6 py-3 text-start text-xs font-medium text-gray-500 dark:text-gray-400 uppercase tracking-wider">{{ $t('evals.runs.colStatus') }}</th>
                                    <th class="px-6 py-3 text-start text-xs font-medium text-gray-500 dark:text-gray-400 uppercase tracking-wider">{{ $t('evals.runs.colResults') }}</th>
                                    <th class="px-6 py-3 text-start text-xs font-medium text-gray-500 dark:text-gray-400 uppercase tracking-wider">{{ $t('evals.runs.colDuration') }}</th>
                                </tr>
                            </thead>
                            <tbody class="bg-white dark:bg-gray-900 divide-y divide-gray-200 dark:divide-gray-700 text-xs">
                                <tr v-if="loadingRuns">
                                    <td colspan="6" class="px-6 py-6 text-center text-gray-400">{{ $t('common.loading') }}</td>
                                </tr>
                                <tr v-for="r in agentRuns" :key="r.id" class="hover:bg-gray-50 dark:hover:bg-gray-800">
                                    <td class="px-6 py-3">
                                        <NuxtLink :to="`/evals/runs/${r.id}`" class="text-blue-600 hover:underline">
                                            {{ r.title || $t('evals.runs.fallbackTitle') }}
                                        </NuxtLink>
                                    </td>
                                    <td class="px-6 py-3">{{ formatDate(r.started_at) }}</td>
                                    <td class="px-6 py-3 capitalize">{{ r.trigger_reason || $t('evals.run.triggerManually') }}</td>
                                    <td class="px-6 py-3">
                                        <span class="inline-flex px-2 py-1 text-xs font-medium rounded-full" :class="runStatusClass(r)">
                                            {{ localizedStatus(derivedRunStatus(r)) || '—' }}
                                        </span>
                                    </td>
                                    <td class="px-6 py-3">
                                        <span :class="resultBadgeClass(r)">{{ resultSummary(r) }}</span>
                                    </td>
                                    <td class="px-6 py-3">{{ formatDuration(r.started_at, r.finished_at) }}</td>
                                </tr>
                                <tr v-if="!loadingRuns && agentRuns.length === 0">
                                    <td colspan="6" class="px-6 py-6 text-center text-gray-500 dark:text-gray-400">{{ $t('evals.runs.empty') }}</td>
                                </tr>
                            </tbody>
                        </table>
                    </div>
                </div>
            </div>
        </div>
        <AddTestCaseModal
            v-if="showAddCase"
            v-model="showAddCase"
            :suite-id="selectedSuiteId"
            :case-id="selectedCaseId"
            @created="onCaseCreated"
            @updated="onCaseUpdated"
        />
    </div>
</template>

<script setup lang="ts">
import AddTestCaseModal from '~/components/monitoring/AddTestCaseModal.vue'
import type { Ref } from 'vue'

definePageMeta({ auth: true, layout: 'data' })

const { t } = useI18n()
const router = useRouter()
const toast = useToast()

const integration = inject<Ref<any>>('integration', ref(null))
const fetchError = inject<Ref<number | null>>('fetchError', ref(null))
const agentId = computed(() => integration.value?.id || '')

interface TestCaseRow {
    id: string
    suite_id: string
    suite_name: string
    prompt_json?: { content?: string }
    expectations_json?: { rules?: any[] }
    data_source_ids_json?: string[]
    status?: string
    auto_generated?: boolean
}
interface RunItem {
    id: string
    title?: string
    started_at?: string
    finished_at?: string
    trigger_reason?: string
    status?: string
}

const activeTab = ref<'tests' | 'runs'>('tests')
const loadingCases = ref(false)
const loadingRuns = ref(false)
const allCases = ref<TestCaseRow[]>([])
const allRuns = ref<RunItem[]>([])
const runResults = ref<Record<string, { total: number; passed: number; failed: number; error: number }>>({})
const runResultsCaseIds = ref<Record<string, Set<string>>>({})
const suitesById = ref<Record<string, string>>({})
const searchTerm = ref('')
const selectedIds = ref<Set<string>>(new Set())
const casesPage = ref(1)
const casesLimit = 20
const showAddCase = ref(false)
const selectedSuiteId = ref('')
const selectedCaseId = ref('')

// Filter cases to this agent
const agentCases = computed(() => {
    const id = agentId.value
    if (!id) return []
    const term = searchTerm.value.trim().toLowerCase()
    return allCases.value.filter(c => {
        const hasAgent = (c.data_source_ids_json || []).includes(id)
        if (!hasAgent) return false
        if (term) return (c.prompt_json?.content || '').toLowerCase().includes(term)
        return true
    })
})

// Filter runs that contain any of this agent's cases
const agentCaseIds = computed(() => new Set(agentCases.value.map(c => c.id)))
const agentRuns = computed(() => {
    if (!agentId.value) return []
    return allRuns.value.filter(r => {
        const caseIds = runResultsCaseIds.value[r.id]
        if (!caseIds) return false
        return [...caseIds].some(id => agentCaseIds.value.has(id))
    })
})

const pagedCases = computed(() => {
    const start = (casesPage.value - 1) * casesLimit
    return agentCases.value.slice(start, start + casesLimit)
})
const casesHasNext = computed(() => agentCases.value.length > casesPage.value * casesLimit)
const allVisibleSelected = computed(() => pagedCases.value.length > 0 && pagedCases.value.every(c => selectedIds.value.has(c.id)))

const lastRunStatus = computed(() => {
    const runs = agentRuns.value
    if (!runs.length) return null
    const latest = runs[0]
    return derivedRunStatus(latest)
})

watch(searchTerm, () => { casesPage.value = 1 })

function tabClass(tab: string) {
    const isActive = activeTab.value === tab
    return [
        'whitespace-nowrap py-4 px-1 border-b-2 text-sm font-medium',
        isActive ? 'border-blue-500 text-blue-600' : 'border-transparent text-gray-500 hover:text-gray-700 hover:border-gray-300',
    ]
}

function statusClass(status?: string) {
    if (status === 'success') return 'bg-green-100 text-green-800'
    if (status === 'fail') return 'bg-red-100 text-red-800'
    return 'bg-gray-100 text-gray-800'
}

function localizedStatus(status?: string) {
    if (!status) return ''
    const map: Record<string, string> = {
        success: 'evals.run.statusSuccess',
        fail: 'evals.run.statusFailed',
        error: 'evals.run.statusError',
        in_progress: 'evals.run.statusInProgress',
        pass: 'evals.run.rulePass',
        stopped: 'evals.run.completionFinished',
    }
    const k = map[status]
    return k ? t(k) : status
}

function derivedRunStatus(r: RunItem) {
    const c = runResults.value[r.id] || { total: 0, passed: 0, failed: 0, error: 0 }
    if (r.status === 'in_progress') return 'in_progress'
    if (c.total > 0 && c.passed === c.total) return 'success'
    if (c.total > 0 && c.passed < c.total) return 'fail'
    return r.status || 'in_progress'
}

function runStatusClass(r: RunItem) {
    const s = derivedRunStatus(r)
    if (s === 'success') return 'bg-green-100 text-green-800'
    if (s === 'fail') return 'bg-red-100 text-red-800'
    return 'bg-gray-100 text-gray-800'
}

function resultSummary(r: RunItem) {
    const c = runResults.value[r.id] || { total: 0, passed: 0, failed: 0, error: 0 }
    return `${c.passed}/${c.total}`
}

function resultBadgeClass(r: RunItem) {
    const s = derivedRunStatus(r)
    if (s === 'success') return 'inline-flex px-2 py-1 rounded-full bg-green-100 text-green-800'
    if (s === 'fail') return 'inline-flex px-2 py-1 rounded-full bg-red-100 text-red-800'
    return 'inline-flex px-2 py-1 rounded-full bg-gray-100 text-gray-800'
}

const _df = useFormatDate()
function formatDate(iso?: string | null) {
    if (!iso) return '—'
    try { return _df.formatDateTime(iso) } catch { return '—' }
}

function formatDuration(start?: string | null, end?: string | null) {
    if (!start) return '—'
    const s = new Date(start).getTime()
    const e = end ? new Date(end).getTime() : Date.now()
    const secs = Math.round(Math.max(0, e - s) / 1000)
    if (secs < 60) return `${secs}s`
    return `${Math.floor(secs / 60)}m ${secs % 60}s`
}

const CATEGORY_LABELS = computed<Record<string, string>>(() => ({
    'tool:create_data': t('evals.category.createData'),
    'tool:clarify': t('evals.category.clarify'),
    'tool:describe_table': t('evals.category.describeTable'),
    'metadata': t('evals.category.metadata'),
    'completion': t('evals.category.completion'),
    'judge': t('evals.category.judge'),
}))

function categoryKeysForCase(c: TestCaseRow): string[] {
    const rules = (c as any)?.expectations_json?.rules || []
    if (!Array.isArray(rules) || !rules.length) return []
    const seen = new Set<string>()
    for (const r of rules) {
        if (r?.type === 'field' && r?.target?.category) seen.add(String(r.target.category))
        else if (r?.type === 'tool.calls' && r?.tool) seen.add(`tool:${r.tool}`)
    }
    return Array.from(seen)
}

function categoriesForCase(c: TestCaseRow) {
    return categoryKeysForCase(c).map(key => ({
        key,
        label: CATEGORY_LABELS.value[key] || key,
    }))
}

function badgeClassesFor(catKey: string) {
    const map: Record<string, string> = {
        'tool:create_data': 'bg-blue-50 text-blue-700 border-blue-100',
        'tool:clarify': 'bg-amber-50 text-amber-700 border-amber-100',
        'tool:describe_table': 'bg-teal-50 text-teal-700 border-teal-100',
        'metadata': 'bg-slate-50 text-slate-700 border-slate-100',
        'completion': 'bg-purple-50 text-purple-700 border-purple-100',
        'judge': 'bg-gray-100 text-gray-700 border-gray-200',
    }
    return map[catKey] || 'bg-zinc-50 text-zinc-700 border-zinc-100'
}

function toggleOne(id: string) {
    const s = new Set(selectedIds.value)
    s.has(id) ? s.delete(id) : s.add(id)
    selectedIds.value = s
}

function toggleAllVisible() {
    const s = new Set(selectedIds.value)
    const allSel = pagedCases.value.every(c => s.has(c.id))
    for (const c of pagedCases.value) allSel ? s.delete(c.id) : s.add(c.id)
    selectedIds.value = s
}

function editCase(c: TestCaseRow) {
    selectedSuiteId.value = c.suite_id
    selectedCaseId.value = c.id
    showAddCase.value = true
}

function addNewTest() {
    const suiteId = Object.keys(suitesById.value)[0] || ''
    selectedSuiteId.value = suiteId
    selectedCaseId.value = ''
    showAddCase.value = true
}

async function runCase(c: TestCaseRow) {
    try {
        const res: any = await useMyFetch('/api/tests/runs', { method: 'POST', body: { case_ids: [c.id], trigger_reason: 'manual' } })
        if (res?.error?.value) throw res.error.value
        const run = res?.data?.value
        if (run?.id) router.push(`/evals/runs/${run.id}`)
    } catch (e) {
        toast.add({ title: 'Failed to run test', color: 'red' })
    }
}

async function runSelected() {
    if (!selectedIds.value.size) return
    try {
        const case_ids = [...selectedIds.value]
        const res: any = await useMyFetch('/api/tests/runs', { method: 'POST', body: { case_ids, trigger_reason: 'manual' } })
        const run = res?.data?.value
        if (run?.id) router.push(`/evals/runs/${run.id}`)
        else activeTab.value = 'runs'
    } catch {
        toast.add({ title: 'Failed to run tests', color: 'red' })
    }
}

async function promoteCase(c: TestCaseRow) {
    try {
        const res: any = await useMyFetch(`/api/tests/cases/${c.id}/status`, { method: 'PATCH', body: { status: 'active' } })
        if (res?.error?.value) throw res.error.value
        const updated = res?.data?.value
        if (updated) {
            const idx = allCases.value.findIndex(x => x.id === c.id)
            if (idx >= 0) { const copy = [...allCases.value]; copy[idx] = { ...copy[idx], status: updated.status }; allCases.value = copy }
        }
        toast.add({ title: 'Promoted to active', color: 'green' })
    } catch {
        toast.add({ title: 'Failed to promote', color: 'red' })
    }
}

async function deleteCase(c: TestCaseRow) {
    if (!confirm(t('evals.tests.deleteConfirm'))) return
    try {
        const res: any = await useMyFetch(`/api/tests/cases/${c.id}`, { method: 'DELETE' })
        if (res?.error?.value) throw res.error.value
        allCases.value = allCases.value.filter(x => x.id !== c.id)
        const s = new Set(selectedIds.value); s.delete(c.id); selectedIds.value = s
        toast.add({ title: t('evals.tests.toastDeleted'), color: 'green' })
    } catch {
        toast.add({ title: t('evals.tests.toastDeleteFailed'), color: 'red' })
    }
}

function onCaseCreated(c: any) {
    const row: TestCaseRow = {
        id: c.id,
        suite_id: c.suite_id,
        suite_name: suitesById.value[c.suite_id] || '—',
        prompt_json: c.prompt_json,
        expectations_json: c.expectations_json,
        data_source_ids_json: c.data_source_ids_json || [],
        status: c.status,
        auto_generated: !!c.auto_generated,
    }
    allCases.value = [...allCases.value, row]
    selectedCaseId.value = ''
    toast.add({ title: t('evals.tests.toastCreated'), color: 'green' })
}

function onCaseUpdated(c: any) {
    const row: TestCaseRow = {
        id: c.id,
        suite_id: c.suite_id,
        suite_name: suitesById.value[c.suite_id] || '—',
        prompt_json: c.prompt_json,
        expectations_json: c.expectations_json,
        data_source_ids_json: c.data_source_ids_json || [],
        status: c.status,
        auto_generated: !!c.auto_generated,
    }
    const idx = allCases.value.findIndex(x => x.id === c.id)
    if (idx >= 0) { const copy = [...allCases.value]; copy[idx] = row; allCases.value = copy }
    else allCases.value = [...allCases.value, row]
    selectedCaseId.value = ''
}

async function loadSuites() {
    try {
        const res = await useMyFetch<any[]>('/api/tests/suites?limit=100')
        const list = (res.data.value || []) as any[]
        suitesById.value = Object.fromEntries(list.map((s: any) => [s.id, s.name]))
    } catch {}
}

async function loadCases() {
    loadingCases.value = true
    try {
        const res = await useMyFetch<any[]>('/api/tests/cases?limit=500')
        const items = (res.data.value || []) as any[]
        allCases.value = items.map((c: any) => ({
            id: c.id,
            suite_id: c.suite_id,
            suite_name: suitesById.value[c.suite_id] || c.suite_id,
            prompt_json: c.prompt_json,
            expectations_json: c.expectations_json,
            data_source_ids_json: c.data_source_ids_json || [],
            status: c.status,
            auto_generated: !!c.auto_generated,
        }))
    } catch {
        allCases.value = []
    } finally {
        loadingCases.value = false
    }
}

async function loadRuns() {
    loadingRuns.value = true
    try {
        const res = await useMyFetch<any[]>('/api/tests/runs?limit=100')
        const runs = (res.data.value as any[]) || []
        allRuns.value = runs
        const fetches = runs.map((r: any) => useMyFetch<any[]>(`/api/tests/runs/${r.id}/results`))
        const responses = await Promise.all(fetches)
        const map: Record<string, any> = {}
        const caseMap: Record<string, Set<string>> = {}
        for (let i = 0; i < responses.length; i++) {
            const r = runs[i]
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
    } catch {
        allRuns.value = []
    } finally {
        loadingRuns.value = false
    }
}

watch(agentId, (id) => {
    if (id) { loadSuites().then(loadCases); loadRuns() }
}, { immediate: true })
</script>
