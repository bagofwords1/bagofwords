<template>
    <div class="text-sm">
        <div class="px-6 py-5">
            <!-- Inline stat row -->
            <div class="flex flex-wrap items-center gap-x-5 gap-y-1 text-xs text-gray-500 dark:text-gray-400 mb-4">
                <span v-if="!isGlobal" class="inline-flex items-center gap-1.5">
                    Status:
                    <span :class="['inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-[10px] font-medium', reliabilityBadgeClass]">
                        <UIcon :name="reliabilityIcon" class="w-3 h-3" />{{ reliabilityLabel }}
                    </span>
                </span>
                <span><span class="font-semibold text-gray-900 dark:text-white">{{ agentCases.length }}</span> test cases</span>
                <span><span class="font-semibold text-gray-900 dark:text-white">{{ agentRuns.length }}</span> runs</span>
                <span class="inline-flex items-center gap-1.5">
                    Last result:
                    <span v-if="lastRunStatus" :class="['inline-flex items-center px-2 py-0.5 rounded-full text-[10px] font-medium', statusClass(lastRunStatus)]">
                        {{ localizedStatus(lastRunStatus) }}
                    </span>
                    <span v-else class="text-gray-400 dark:text-gray-500">—</span>
                </span>
            </div>

            <!-- Sub-tabs + toolbar -->
            <div class="flex items-center gap-2 mb-4">
                <div class="flex items-center gap-1">
                    <button type="button" :class="tabClass('runs')" @click="activeTab = 'runs'">{{ $t('evals.tabs.runs') }}</button>
                    <button type="button" :class="tabClass('tests')" @click="activeTab = 'tests'">{{ $t('evals.tabs.tests') }}</button>
                </div>
                <div class="ms-auto flex items-center gap-2">
                    <button v-if="canManage && !isGlobal" type="button" class="h-7 px-2.5 rounded-md border border-gray-200 dark:border-gray-800 text-gray-700 dark:text-gray-300 text-xs font-medium hover:bg-gray-50 dark:hover:bg-gray-800/50 inline-flex items-center gap-1" title="Configure Self Learning" @click="showSelfLearning = true"><UIcon name="i-heroicons-sparkles" class="w-3.5 h-3.5 text-blue-500" />Self Learning</button>
                    <input
                        v-if="activeTab === 'tests'"
                        v-model="searchTerm"
                        type="text"
                        :placeholder="$t('evals.tests.search')"
                        class="h-7 px-2 text-xs bg-gray-50 border border-gray-200 rounded-md outline-none focus:border-gray-400 focus:bg-white placeholder:text-gray-400 w-40 dark:bg-gray-800 dark:border-gray-700 dark:text-gray-100 dark:placeholder-gray-500"
                    />
                    <template v-if="activeTab === 'tests'">
                        <UButton :disabled="selectedIds.size === 0" color="blue" size="xs" icon="i-heroicons-play" @click="runSelected">
                            {{ $t('evals.tests.runSelected') }}
                        </UButton>
                        <UButton color="blue" size="xs" variant="soft" icon="i-heroicons-plus" @click="addNewTest">
                            {{ $t('evals.tests.addNew') }}
                        </UButton>
                    </template>
                </div>
            </div>

            <!-- Tests tab -->
            <div v-if="activeTab === 'tests'">
                <table class="min-w-full text-xs">
                    <thead>
                        <tr class="border-b border-gray-100 dark:border-gray-800 text-[10px] font-semibold uppercase tracking-wider text-gray-400 dark:text-gray-500">
                            <th class="py-2 pe-2 w-8 text-start">
                                <input type="checkbox" :checked="allVisibleSelected" @change="toggleAllVisible" />
                            </th>
                            <th class="py-2 pe-4 text-start">{{ $t('evals.tests.colPrompt') }}</th>
                            <th class="py-2 pe-4 text-start">{{ $t('evals.tests.colRules') }}</th>
                            <th class="py-2 pe-4 text-start">{{ $t('evals.tests.colSuite') }}</th>
                            <th class="py-2 text-start">{{ $t('evals.tests.colOptions') }}</th>
                        </tr>
                    </thead>
                    <tbody class="divide-y divide-gray-50 dark:divide-gray-800">
                        <tr v-if="loadingCases">
                            <td colspan="5" class="text-center text-gray-400 dark:text-gray-500 text-xs py-8">{{ $t('common.loading') }}</td>
                        </tr>
                        <tr v-for="c in pagedCases" :key="c.id" class="hover:bg-gray-50 dark:hover:bg-gray-800/50">
                            <td class="py-2 pe-2 w-8 align-top">
                                <input type="checkbox" :checked="selectedIds.has(c.id)" @change="toggleOne(c.id)" />
                            </td>
                            <td class="py-2 pe-4">
                                <div class="flex items-center gap-1.5 max-w-[360px]">
                                    <span v-if="c.status === 'draft'" class="inline-flex items-center rounded-full bg-amber-100 text-amber-800 dark:bg-amber-500/10 dark:text-amber-400 text-[10px] font-medium px-2 py-0.5 shrink-0">Draft</span>
                                    <span v-else-if="c.status === 'archived'" class="inline-flex items-center rounded-full bg-gray-200 text-gray-700 dark:bg-gray-800 dark:text-gray-300 text-[10px] font-medium px-2 py-0.5 shrink-0">Archived</span>
                                    <span v-if="c.auto_generated" class="inline-flex items-center rounded-full bg-purple-100 text-purple-800 dark:bg-purple-500/10 dark:text-purple-400 text-[10px] font-medium px-2 py-0.5 shrink-0">Auto</span>
                                    <span class="truncate flex-1" :title="c.prompt_json?.content || ''">{{ c.prompt_json?.content || '—' }}</span>
                                </div>
                            </td>
                            <td class="py-2 pe-4 text-gray-700 dark:text-gray-300">
                                <div class="flex flex-wrap gap-1 max-w-[200px]">
                                    <span
                                        v-for="cat in categoriesForCase(c)"
                                        :key="cat.key"
                                        :class="['inline-flex items-center rounded-full border text-[10px] px-2 py-0.5', badgeClassesFor(cat.key)]"
                                    >{{ cat.label }}</span>
                                </div>
                            </td>
                            <td class="py-2 pe-4 text-gray-600 dark:text-gray-400">{{ c.suite_name }}</td>
                            <td class="py-2">
                                <div class="flex items-center gap-1">
                                    <UButton v-if="c.status === 'draft'" color="green" size="2xs" variant="ghost" icon="i-heroicons-check-badge" @click="promoteCase(c)">Promote</UButton>
                                    <UButton color="gray" size="2xs" variant="ghost" icon="i-heroicons-pencil-square" @click="editCase(c)">{{ $t('evals.tests.actionEdit') }}</UButton>
                                    <UButton color="blue" size="2xs" variant="ghost" icon="i-heroicons-play" @click="runCase(c)">{{ $t('evals.tests.actionRunTest') }}</UButton>
                                    <UButton color="red" size="2xs" variant="ghost" icon="i-heroicons-trash" @click="deleteCase(c)">{{ $t('evals.tests.actionDelete') }}</UButton>
                                </div>
                            </td>
                        </tr>
                        <tr v-if="!loadingCases && pagedCases.length === 0">
                            <td colspan="5" class="text-center text-gray-400 dark:text-gray-500 text-xs py-8">{{ $t('evals.tests.empty') }}</td>
                        </tr>
                    </tbody>
                </table>
                <div class="flex items-center justify-between pt-3">
                    <div class="text-xs text-gray-500 dark:text-gray-400">{{ $t('evals.pagination.showing', { page: casesPage, n: pagedCases.length }) }}</div>
                    <div class="flex items-center gap-1.5">
                        <UButton size="xs" variant="ghost" :disabled="casesPage <= 1" @click="casesPage--">{{ $t('evals.pagination.prev') }}</UButton>
                        <UButton size="xs" variant="ghost" :disabled="!casesHasNext" @click="casesPage++">{{ $t('evals.pagination.next') }}</UButton>
                    </div>
                </div>
            </div>

            <!-- Runs tab (manual checks + automation, one list) -->
            <div v-else-if="activeTab === 'runs'">
                <div class="flex items-center justify-between mb-3">
                    <div class="text-xs text-gray-500 dark:text-gray-400">Every eval run — manual checks and automation.</div>
                    <UButton v-if="canManage && !isGlobal && agentCases.length > 0" color="blue" size="xs" variant="soft" icon="i-heroicons-bolt"
                        :loading="triggering" @click="runAutomationNow">
                        Run evals now
                    </UButton>
                </div>
                <div v-if="canManage && !isGlobal && autoEnabled === false" class="mb-4 flex items-center justify-between gap-3 rounded-md border border-gray-200 dark:border-gray-800 bg-gray-50 dark:bg-gray-800/40 px-3 py-2">
                    <div class="flex items-center gap-2 text-xs text-gray-600 dark:text-gray-400">
                        <UIcon name="i-heroicons-sparkles" class="w-4 h-4 text-blue-500 shrink-0" />
                        Self Learning is off — auto-run evals &amp; self-heal instructions when things change.
                    </div>
                    <button type="button" class="shrink-0 h-7 px-2.5 rounded-md bg-blue-600 text-white text-xs font-medium hover:bg-blue-700" @click="showSelfLearning = true">Set up</button>
                </div>
                <table class="min-w-full text-xs">
                    <thead>
                        <tr class="border-b border-gray-100 dark:border-gray-800 text-[10px] font-semibold uppercase tracking-wider text-gray-400 dark:text-gray-500">
                            <th class="py-2 pe-4 text-start">{{ $t('evals.runs.colTitle') }}</th>
                            <th class="py-2 pe-4 text-start">{{ $t('evals.runs.colStarted') }}</th>
                            <th class="py-2 pe-4 text-start">{{ $t('evals.runs.colTrigger') }}</th>
                            <th class="py-2 pe-4 text-start">{{ $t('evals.runs.colStatus') }}</th>
                            <th class="py-2 pe-4 text-start">{{ $t('evals.runs.colResults') }}</th>
                            <th class="py-2 text-start">{{ $t('evals.runs.colDuration') }}</th>
                        </tr>
                    </thead>
                    <tbody class="divide-y divide-gray-50 dark:divide-gray-800">
                        <tr v-if="loadingRuns">
                            <td colspan="6" class="text-center text-gray-400 dark:text-gray-500 text-xs py-8">{{ $t('common.loading') }}</td>
                        </tr>
                        <tr v-for="r in agentRuns" :key="r.id" class="hover:bg-gray-50 dark:hover:bg-gray-800/50">
                            <td class="py-2 pe-4">
                                <NuxtLink :to="`/evals/runs/${r.id}`" class="text-blue-600 dark:text-blue-400 hover:underline">
                                    {{ r.title || $t('evals.runs.fallbackTitle') }}
                                </NuxtLink>
                            </td>
                            <td class="py-2 pe-4 text-gray-600 dark:text-gray-400 whitespace-nowrap">{{ formatDate(r.started_at) }}</td>
                            <td class="py-2 pe-4">
                                <span class="capitalize text-gray-600 dark:text-gray-400">{{ r.trigger_reason || $t('evals.run.triggerManually') }}</span>
                                <span v-if="runOutcome(r.id)" class="ms-1.5 inline-flex px-1.5 py-0.5 rounded text-[10px] font-medium align-middle" :class="runOutcome(r.id).cls" :title="runOutcome(r.id).title">
                                    {{ runOutcome(r.id).label }}
                                </span>
                            </td>
                            <td class="py-2 pe-4">
                                <span class="inline-flex px-2 py-0.5 text-[10px] font-medium rounded-full" :class="runStatusClass(r)">
                                    {{ localizedStatus(derivedRunStatus(r)) || '—' }}
                                </span>
                            </td>
                            <td class="py-2 pe-4">
                                <span :class="resultBadgeClass(r)">{{ resultSummary(r) }}</span>
                            </td>
                            <td class="py-2 text-gray-600 dark:text-gray-400">{{ formatDuration(r.started_at, r.finished_at) }}</td>
                        </tr>
                        <tr v-if="!loadingRuns && agentRuns.length === 0">
                            <td colspan="6" class="text-center text-gray-400 dark:text-gray-500 text-xs py-8">{{ $t('evals.runs.empty') }}</td>
                        </tr>
                    </tbody>
                </table>
            </div>

        </div>
        <AddTestCaseModal
            v-if="showAddCase"
            v-model="showAddCase"
            :suite-id="selectedSuiteId"
            :case-id="selectedCaseId"
            :agent-id="agentId"
            @created="onCaseCreated"
            @updated="onCaseUpdated"
        />

        <!-- Self Learning (per-agent automation policy) -->
        <UModal v-model="showSelfLearning" :ui="{ width: 'sm:max-w-lg' }">
            <div class="p-5">
                <div class="flex items-center gap-2 mb-1">
                    <UIcon name="i-heroicons-sparkles" class="w-4 h-4 text-blue-500" />
                    <div class="text-sm font-semibold text-gray-900 dark:text-white">Self Learning</div>
                </div>
                <AgentAutomationSettings v-if="showSelfLearning && agentId" :agent-id="agentId" @saved="onSelfLearningSaved" />
                <div class="flex justify-end mt-4">
                    <button class="px-3 py-1.5 text-xs border border-gray-300 dark:border-gray-700 text-gray-700 dark:text-gray-300 rounded-lg hover:bg-gray-50 dark:hover:bg-gray-800/50" @click="showSelfLearning = false">Close</button>
                </div>
            </div>
        </UModal>
    </div>
</template>

<script setup lang="ts">
import AddTestCaseModal from '~/components/monitoring/AddTestCaseModal.vue'
import AgentAutomationSettings from '~/components/AgentAutomationSettings.vue'

const { t } = useI18n()
const router = useRouter()
const toast = useToast()

const props = defineProps<{ agentId?: string; global?: boolean }>()
const agentId = computed(() => props.agentId || '')
// Global mode shows org-wide evals (cases not scoped to any data source/agent),
// which apply to ALL agents. Admin-gated via the `manage_evals` org permission.
const isGlobal = computed(() => !!props.global)

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

const activeTab = ref<'tests' | 'runs' | 'automation'>('runs')
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
    if (!isGlobal.value && !id) return []
    const term = searchTerm.value.trim().toLowerCase()
    return allCases.value.filter(c => {
        // Auto / empty data sources => "all agents" (like a global instruction).
        const dsids = c.data_source_ids_json || []
        // Global view: only org-wide cases (no data-source scope). Agent view:
        // org-wide cases + cases scoped to this agent.
        const matches = isGlobal.value ? dsids.length === 0 : (dsids.length === 0 || dsids.includes(id))
        if (!matches) return false
        if (term) return (c.prompt_json?.content || '').toLowerCase().includes(term)
        return true
    })
})

// Filter runs that contain any of this agent's cases
const agentCaseIds = computed(() => new Set(agentCases.value.map(c => c.id)))
const agentRuns = computed(() => {
    if (!isGlobal.value && !agentId.value) return []
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
        'h-7 px-3 rounded-md text-xs font-medium transition-colors',
        isActive ? 'bg-gray-100 dark:bg-gray-800 text-gray-900 dark:text-white' : 'text-gray-500 dark:text-gray-400 hover:bg-gray-50 dark:hover:bg-gray-800/50',
    ]
}

function statusClass(status?: string) {
    if (status === 'success') return 'bg-green-100 text-green-800 dark:bg-green-500/10 dark:text-green-400'
    if (status === 'fail') return 'bg-red-100 text-red-800 dark:bg-red-500/10 dark:text-red-400'
    return 'bg-gray-100 text-gray-800 dark:bg-gray-800 dark:text-gray-300'
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
    if (s === 'success') return 'bg-green-100 text-green-800 dark:bg-green-500/10 dark:text-green-400'
    if (s === 'fail') return 'bg-red-100 text-red-800 dark:bg-red-500/10 dark:text-red-400'
    return 'bg-gray-100 text-gray-800 dark:bg-gray-800 dark:text-gray-300'
}

function resultSummary(r: RunItem) {
    const c = runResults.value[r.id] || { total: 0, passed: 0, failed: 0, error: 0 }
    return `${c.passed}/${c.total}`
}

function resultBadgeClass(r: RunItem) {
    const s = derivedRunStatus(r)
    if (s === 'success') return 'inline-flex px-2 py-0.5 rounded-full bg-green-100 text-green-800 dark:bg-green-500/10 dark:text-green-400'
    if (s === 'fail') return 'inline-flex px-2 py-0.5 rounded-full bg-red-100 text-red-800 dark:bg-red-500/10 dark:text-red-400'
    return 'inline-flex px-2 py-0.5 rounded-full bg-gray-100 text-gray-800 dark:bg-gray-800 dark:text-gray-300'
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
        'tool:create_data': 'bg-blue-50 text-blue-700 border-blue-100 dark:bg-blue-500/10 dark:text-blue-400 dark:border-blue-500/30',
        'tool:clarify': 'bg-amber-50 text-amber-700 border-amber-100 dark:bg-amber-500/10 dark:text-amber-400 dark:border-amber-500/30',
        'tool:describe_table': 'bg-teal-50 text-teal-700 border-teal-100 dark:bg-teal-500/10 dark:text-teal-400 dark:border-teal-500/30',
        'metadata': 'bg-slate-50 text-slate-700 border-slate-100 dark:bg-slate-500/10 dark:text-slate-400 dark:border-slate-500/30',
        'completion': 'bg-purple-50 text-purple-700 border-purple-100 dark:bg-purple-500/10 dark:text-purple-400 dark:border-purple-500/30',
        'judge': 'bg-gray-100 text-gray-700 border-gray-200 dark:bg-gray-800 dark:text-gray-300 dark:border-gray-700',
    }
    return map[catKey] || 'bg-zinc-50 text-zinc-700 border-zinc-100 dark:bg-zinc-500/10 dark:text-zinc-400 dark:border-zinc-500/30'
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

watch(agentId, () => {
    if (isGlobal.value || agentId.value) { loadSuites().then(loadCases); loadRuns() }
}, { immediate: true })

// ===== Reliability automation =====

const canManage = computed(() =>
    isGlobal.value
        ? useCan('manage_evals')
        : (agentId.value ? useCan('manage', { type: 'data_source', id: agentId.value }) : false),
)

const autoEnabled = ref<boolean | null>(null)
const reliabilityStatus = ref('training')
const publishStatus = ref('published')
const showSelfLearning = ref(false)
function onSelfLearningSaved() { toast.add({ title: 'Self Learning settings saved', color: 'green' }) }

const autoRuns = ref<any[]>([])
const loadingAutoRuns = ref(false)
const triggering = ref(false)

const reliabilityLabel = computed(() => {
    if (publishStatus.value === 'disabled') return 'Disabled'
    if (reliabilityStatus.value === 'development') return 'Development'
    if (reliabilityStatus.value === 'training') return 'Training'
    return 'Healthy'
})
const reliabilityBadgeClass = computed(() => {
    if (publishStatus.value === 'disabled') return 'bg-red-100 text-red-800 dark:bg-red-500/10 dark:text-red-400'
    if (reliabilityStatus.value === 'development') return 'bg-amber-100 text-amber-800 dark:bg-amber-500/10 dark:text-amber-400'
    if (reliabilityStatus.value === 'training') return 'bg-blue-100 text-blue-800 dark:bg-blue-500/10 dark:text-blue-400'
    return 'bg-green-100 text-green-800 dark:bg-green-500/10 dark:text-green-400'
})
const reliabilityIcon = computed(() => {
    if (publishStatus.value === 'disabled') return 'i-heroicons-no-symbol'
    if (reliabilityStatus.value === 'development') return 'i-heroicons-wrench-screwdriver'
    if (reliabilityStatus.value === 'training') return 'i-heroicons-academic-cap'
    return 'i-heroicons-check-circle'
})

function triggerLabel(t?: string) {
    return ({
        table_change: 'Table change',
        instruction_change: 'Instruction change',
        global_change: 'Global change',
        manual: 'Manual',
    } as Record<string, string>)[t || ''] || t || '—'
}
function autoStatusClass(s?: string) {
    if (s === 'passed') return 'bg-green-100 text-green-800 dark:bg-green-500/10 dark:text-green-400'
    if (s === 'passed_pending') return 'bg-blue-100 text-blue-800 dark:bg-blue-500/10 dark:text-blue-400'
    if (s === 'gave_up' || s === 'error') return 'bg-red-100 text-red-800 dark:bg-red-500/10 dark:text-red-400'
    if (s === 'running') return 'bg-gray-100 text-gray-700 dark:bg-gray-800 dark:text-gray-300'
    return 'bg-gray-100 text-gray-600 dark:bg-gray-800 dark:text-gray-400'
}
function autoStatusLabel(s?: string) {
    return ({
        passed: 'Passed', passed_pending: 'Awaiting approval', gave_up: 'Gave up',
        no_evals: 'No evals', skipped: 'Skipped', running: 'Running', error: 'Error',
    } as Record<string, string>)[s || ''] || s || '—'
}
function autoDetailText(r: any) {
    const d = r?.detail || {}
    return d.reason || d.outcome_action || d.hint || ''
}

// Map each eval (test) run id -> the automation loop it belonged to, so the
// merged Runs tab can badge a run with the loop's outcome (e.g. "Gave up → Dev").
const runOutcomeMap = computed<Record<string, any>>(() => {
    const m: Record<string, any> = {}
    for (const ar of autoRuns.value) {
        for (const tid of (ar.test_run_ids || [])) m[String(tid)] = ar
    }
    return m
})
function runOutcome(runId: string) {
    const ar = runOutcomeMap.value[String(runId)]
    if (!ar) return null
    const action = ar.detail?.outcome_action
    const extra = action === 'development' ? ' → Development' : action === 'training' ? ' → Training' : ''
    return {
        label: autoStatusLabel(ar.status) + extra,
        cls: autoStatusClass(ar.status),
        title: autoDetailText(ar) || 'Automation run',
    }
}

async function loadAutomation() {
    const id = agentId.value
    if (!id) return
    try {
        const res = await useMyFetch<any>(`/data_sources/${id}/automation`, { method: 'GET' })
        const data: any = res.data.value
        if (!data) return
        reliabilityStatus.value = data.reliability_status || 'training'
        publishStatus.value = data.publish_status || 'published'
        autoEnabled.value = !!(data.effective?.mode && data.effective.mode !== 'off')
    } catch (e) { /* noop */ }
}

async function loadAutoRuns() {
    const id = agentId.value
    if (!id) return
    loadingAutoRuns.value = true
    try {
        const res = await useMyFetch<any[]>(`/data_sources/${id}/automation/runs?limit=20`, { method: 'GET' })
        autoRuns.value = (res.data.value as any[]) || []
    } catch (e) { autoRuns.value = [] }
    finally { loadingAutoRuns.value = false }
}

async function runAutomationNow() {
    const id = agentId.value
    if (!id) return
    triggering.value = true
    try {
        await useMyFetch(`/data_sources/${id}/automation/run`, { method: 'POST' })
        toast.add({ title: 'Eval run started', color: 'green' })
        // Give the background loop a moment, then refresh history.
        setTimeout(() => { loadAutoRuns(); loadAutomation() }, 1500)
    } catch (e) {
        toast.add({ title: 'Failed to start reliability check', color: 'red' })
    } finally { triggering.value = false }
}

watch(agentId, (id) => {
    if (id) { loadAutomation(); loadAutoRuns() }
}, { immediate: true })
</script>
