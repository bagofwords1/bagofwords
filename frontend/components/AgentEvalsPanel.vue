<template>
    <div class="text-sm">
        <div class="px-6 py-5">
            <!-- Inline stat row -->
            <div class="flex flex-wrap items-center gap-x-5 gap-y-1 text-xs text-gray-500 mb-4">
                <span class="inline-flex items-center gap-1.5">
                    Status:
                    <span :class="['inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-[10px] font-medium', reliabilityBadgeClass]">
                        <UIcon :name="reliabilityIcon" class="w-3 h-3" />{{ reliabilityLabel }}
                    </span>
                </span>
                <span><span class="font-semibold text-gray-900">{{ agentCases.length }}</span> test cases</span>
                <span><span class="font-semibold text-gray-900">{{ agentRuns.length }}</span> runs</span>
                <span class="inline-flex items-center gap-1.5">
                    Last result:
                    <span v-if="lastRunStatus" :class="['inline-flex items-center px-2 py-0.5 rounded-full text-[10px] font-medium', statusClass(lastRunStatus)]">
                        {{ localizedStatus(lastRunStatus) }}
                    </span>
                    <span v-else class="text-gray-400">—</span>
                </span>
            </div>

            <!-- Sub-tabs + toolbar -->
            <div class="flex items-center gap-2 mb-4">
                <div class="flex items-center gap-1">
                    <button type="button" :class="tabClass('runs')" @click="activeTab = 'runs'">{{ $t('evals.tabs.runs') }}</button>
                    <button type="button" :class="tabClass('tests')" @click="activeTab = 'tests'">{{ $t('evals.tabs.tests') }}</button>
                    <button v-if="canManage" type="button" :class="tabClass('automation')" @click="activeTab = 'automation'">Automation</button>
                </div>
                <div class="ms-auto flex items-center gap-2">
                    <input
                        v-if="activeTab === 'tests'"
                        v-model="searchTerm"
                        type="text"
                        :placeholder="$t('evals.tests.search')"
                        class="h-7 px-2 text-xs bg-gray-50 border border-gray-200 rounded-md outline-none focus:border-gray-400 focus:bg-white placeholder:text-gray-400 w-40"
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
                        <tr class="border-b border-gray-100 text-[10px] font-semibold uppercase tracking-wider text-gray-400">
                            <th class="py-2 pe-2 w-8 text-start">
                                <input type="checkbox" :checked="allVisibleSelected" @change="toggleAllVisible" />
                            </th>
                            <th class="py-2 pe-4 text-start">{{ $t('evals.tests.colPrompt') }}</th>
                            <th class="py-2 pe-4 text-start">{{ $t('evals.tests.colRules') }}</th>
                            <th class="py-2 pe-4 text-start">{{ $t('evals.tests.colSuite') }}</th>
                            <th class="py-2 text-start">{{ $t('evals.tests.colOptions') }}</th>
                        </tr>
                    </thead>
                    <tbody class="divide-y divide-gray-50">
                        <tr v-if="loadingCases">
                            <td colspan="5" class="text-center text-gray-400 text-xs py-8">{{ $t('common.loading') }}</td>
                        </tr>
                        <tr v-for="c in pagedCases" :key="c.id" class="hover:bg-gray-50">
                            <td class="py-2 pe-2 w-8 align-top">
                                <input type="checkbox" :checked="selectedIds.has(c.id)" @change="toggleOne(c.id)" />
                            </td>
                            <td class="py-2 pe-4">
                                <div class="flex items-center gap-1.5 max-w-[360px]">
                                    <span v-if="c.status === 'draft'" class="inline-flex items-center rounded-full bg-amber-100 text-amber-800 text-[10px] font-medium px-2 py-0.5 shrink-0">Draft</span>
                                    <span v-else-if="c.status === 'archived'" class="inline-flex items-center rounded-full bg-gray-200 text-gray-700 text-[10px] font-medium px-2 py-0.5 shrink-0">Archived</span>
                                    <span v-if="c.auto_generated" class="inline-flex items-center rounded-full bg-purple-100 text-purple-800 text-[10px] font-medium px-2 py-0.5 shrink-0">Auto</span>
                                    <span class="truncate flex-1" :title="c.prompt_json?.content || ''">{{ c.prompt_json?.content || '—' }}</span>
                                </div>
                            </td>
                            <td class="py-2 pe-4 text-gray-700">
                                <div class="flex flex-wrap gap-1 max-w-[200px]">
                                    <span
                                        v-for="cat in categoriesForCase(c)"
                                        :key="cat.key"
                                        :class="['inline-flex items-center rounded-full border text-[10px] px-2 py-0.5', badgeClassesFor(cat.key)]"
                                    >{{ cat.label }}</span>
                                </div>
                            </td>
                            <td class="py-2 pe-4 text-gray-600">{{ c.suite_name }}</td>
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
                            <td colspan="5" class="text-center text-gray-400 text-xs py-8">{{ $t('evals.tests.empty') }}</td>
                        </tr>
                    </tbody>
                </table>
                <div class="flex items-center justify-between pt-3">
                    <div class="text-xs text-gray-500">{{ $t('evals.pagination.showing', { page: casesPage, n: pagedCases.length }) }}</div>
                    <div class="flex items-center gap-1.5">
                        <UButton size="xs" variant="ghost" :disabled="casesPage <= 1" @click="casesPage--">{{ $t('evals.pagination.prev') }}</UButton>
                        <UButton size="xs" variant="ghost" :disabled="!casesHasNext" @click="casesPage++">{{ $t('evals.pagination.next') }}</UButton>
                    </div>
                </div>
            </div>

            <!-- Runs tab (manual checks + automation, one list) -->
            <div v-else-if="activeTab === 'runs'">
                <div class="flex items-center justify-between mb-3">
                    <div class="text-xs text-gray-500">Every eval run — manual checks and automation.</div>
                    <UButton v-if="canManage" color="blue" size="xs" variant="soft" icon="i-heroicons-bolt"
                        :loading="triggering" :disabled="agentCases.length === 0"
                        :title="agentCases.length === 0 ? 'Add a test case first — there is nothing to evaluate yet.' : ''"
                        @click="runAutomationNow">
                        Run reliability check
                    </UButton>
                </div>
                <div v-if="canManage && autoEnabled === false" class="mb-4 rounded-md border border-amber-200 bg-amber-50 px-3 py-2 text-xs text-amber-800">
                    Automation is off for this agent. Turn it on in the Automation tab to auto-run evals and self-heal instructions when tables or instructions change.
                </div>
                <table class="min-w-full text-xs">
                    <thead>
                        <tr class="border-b border-gray-100 text-[10px] font-semibold uppercase tracking-wider text-gray-400">
                            <th class="py-2 pe-4 text-start">{{ $t('evals.runs.colTitle') }}</th>
                            <th class="py-2 pe-4 text-start">{{ $t('evals.runs.colStarted') }}</th>
                            <th class="py-2 pe-4 text-start">{{ $t('evals.runs.colTrigger') }}</th>
                            <th class="py-2 pe-4 text-start">{{ $t('evals.runs.colStatus') }}</th>
                            <th class="py-2 pe-4 text-start">{{ $t('evals.runs.colResults') }}</th>
                            <th class="py-2 text-start">{{ $t('evals.runs.colDuration') }}</th>
                        </tr>
                    </thead>
                    <tbody class="divide-y divide-gray-50">
                        <tr v-if="loadingRuns">
                            <td colspan="6" class="text-center text-gray-400 text-xs py-8">{{ $t('common.loading') }}</td>
                        </tr>
                        <tr v-for="r in agentRuns" :key="r.id" class="hover:bg-gray-50">
                            <td class="py-2 pe-4">
                                <NuxtLink :to="`/evals/runs/${r.id}`" class="text-blue-600 hover:underline">
                                    {{ r.title || $t('evals.runs.fallbackTitle') }}
                                </NuxtLink>
                            </td>
                            <td class="py-2 pe-4 text-gray-600 whitespace-nowrap">{{ formatDate(r.started_at) }}</td>
                            <td class="py-2 pe-4">
                                <span class="capitalize text-gray-600">{{ r.trigger_reason || $t('evals.run.triggerManually') }}</span>
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
                            <td class="py-2 text-gray-600">{{ formatDuration(r.started_at, r.finished_at) }}</td>
                        </tr>
                        <tr v-if="!loadingRuns && agentRuns.length === 0">
                            <td colspan="6" class="text-center text-gray-400 text-xs py-8">{{ $t('evals.runs.empty') }}</td>
                        </tr>
                    </tbody>
                </table>
            </div>

            <!-- Automation tab (minimal: master switch + one autonomy preset) -->
            <div v-else-if="activeTab === 'automation'" class="max-w-xl space-y-5">
                <!-- Master switch -->
                <div class="flex items-center justify-between">
                    <div class="pe-4">
                        <div class="text-sm font-medium text-gray-900">Enable automation</div>
                        <div class="text-xs text-gray-500">Let this agent measure and improve itself.</div>
                    </div>
                    <UToggle v-model="form.enabled" :disabled="!canManage" @update:model-value="onEnableChange" />
                </div>

                <!-- Autonomy preset -->
                <div class="flex items-start justify-between" :class="{ 'opacity-50 pointer-events-none': !form.enabled }">
                    <div class="pe-4">
                        <div class="text-sm font-medium text-gray-900">Autonomy</div>
                        <div class="text-xs text-gray-500 mt-0.5 max-w-sm">{{ presetHelp }}</div>
                    </div>
                    <select v-model="preset" :disabled="!canManage || !form.enabled" @change="applyPreset"
                        class="h-7 px-2 text-xs bg-white border border-gray-200 rounded-md outline-none focus:border-gray-400 disabled:bg-gray-50 disabled:text-gray-400 shrink-0">
                        <option value="manual">Manual</option>
                        <option value="assisted">Assisted</option>
                        <option value="autonomous">Autonomous</option>
                        <option v-if="preset === 'custom'" value="custom">Custom</option>
                    </select>
                </div>

                <!-- Advanced: per-stage control -->
                <div :class="{ 'opacity-50 pointer-events-none': !form.enabled }">
                    <button type="button" class="flex items-center gap-1 text-xs font-medium text-gray-500 hover:text-gray-800" @click="showAdvanced = !showAdvanced">
                        <UIcon :name="showAdvanced ? 'i-heroicons-chevron-down' : 'i-heroicons-chevron-right'" class="w-3 h-3" />
                        Advanced
                    </button>
                    <div v-if="showAdvanced" class="mt-2">
                        <p class="text-[11px] text-gray-400 mb-2">Each stage: <span class="font-medium">Off</span> · <span class="font-medium">Suggest</span> (stop for review) · <span class="font-medium">Auto</span> (end to end).</p>
                        <div class="rounded-md border border-gray-200 divide-y divide-gray-100">
                            <!-- Merged eval triggers -->
                            <div class="flex items-center justify-between px-3 py-2.5">
                                <div class="pe-4">
                                    <div class="text-xs font-medium text-gray-800">Re-run evals when things change</div>
                                    <div class="text-[11px] text-gray-500">Tables activated/changed, this agent's instructions, or a global build promoted.</div>
                                </div>
                                <select v-model="evalTrigger" :disabled="!canManage"
                                    class="h-7 px-2 text-xs bg-white border border-gray-200 rounded-md outline-none focus:border-gray-400 shrink-0">
                                    <option v-for="o in AUTONOMY_OPTS" :key="o.value" :value="o.value">{{ o.label }}</option>
                                </select>
                            </div>
                            <!-- Per-stage dials -->
                            <div v-for="dial in advancedDials" :key="dial.key" class="flex items-center justify-between px-3 py-2.5">
                                <div class="pe-4">
                                    <div class="text-xs font-medium text-gray-800">{{ dial.label }}</div>
                                    <div class="text-[11px] text-gray-500">{{ dial.help }}</div>
                                </div>
                                <select v-model="(form as any)[dial.key]" :disabled="!canManage" @change="onAdvancedChange"
                                    class="h-7 px-2 text-xs bg-white border border-gray-200 rounded-md outline-none focus:border-gray-400 shrink-0">
                                    <option v-for="o in dial.options" :key="o.value" :value="o.value">{{ o.label }}</option>
                                </select>
                            </div>
                            <!-- On repeated failure -->
                            <div class="flex items-center justify-between px-3 py-2.5">
                                <div class="pe-4">
                                    <div class="text-xs font-medium text-gray-800">On repeated failure</div>
                                    <div class="text-[11px] text-gray-500">When the loop can't reach green: keep it in training (still visible to users) or move it to development (only agent admins can see it).</div>
                                </div>
                                <select v-model="form.on_repeated_failure" :disabled="!canManage" @change="markDirty"
                                    class="h-7 px-2 text-xs bg-white border border-gray-200 rounded-md outline-none focus:border-gray-400 shrink-0">
                                    <option value="none">Do nothing</option>
                                    <option value="training">Keep in training</option>
                                    <option value="development">Move to development</option>
                                </select>
                            </div>
                            <!-- Max iterations -->
                            <div class="flex items-center justify-between px-3 py-2.5">
                                <div class="pe-4">
                                    <div class="text-xs font-medium text-gray-800">Max training iterations</div>
                                    <div class="text-[11px] text-gray-500">Cap on the train → re-eval loop before giving up. Guards cost.</div>
                                </div>
                                <input v-model.number="form.max_iterations" type="number" min="1" max="10" :disabled="!canManage" @change="markDirty"
                                    class="h-7 w-16 px-2 text-xs bg-white border border-gray-200 rounded-md outline-none focus:border-gray-400 shrink-0" />
                            </div>
                        </div>
                    </div>
                </div>

                <div v-if="canManage" class="flex items-center gap-2">
                    <UButton color="blue" size="xs" :disabled="!dirty" :loading="savingSettings" @click="saveSettings">Save</UButton>
                    <UButton color="gray" variant="ghost" size="xs" :disabled="!dirty" @click="loadAutomation">Reset</UButton>
                    <span v-if="savedOk" class="text-xs text-green-600">Saved</span>
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

const { t } = useI18n()
const router = useRouter()
const toast = useToast()

const props = defineProps<{ agentId: string }>()
const agentId = computed(() => props.agentId || '')

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
        'h-7 px-3 rounded-md text-xs font-medium transition-colors',
        isActive ? 'bg-gray-100 text-gray-900' : 'text-gray-500 hover:bg-gray-50',
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
    if (s === 'success') return 'inline-flex px-2 py-0.5 rounded-full bg-green-100 text-green-800'
    if (s === 'fail') return 'inline-flex px-2 py-0.5 rounded-full bg-red-100 text-red-800'
    return 'inline-flex px-2 py-0.5 rounded-full bg-gray-100 text-gray-800'
}

function formatDate(iso?: string | null) {
    if (!iso) return '—'
    try { return new Date(iso).toLocaleString() } catch { return '—' }
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

// ===== Reliability automation =====

const canManage = computed(() =>
    agentId.value ? useCan('manage', { type: 'data_source', id: agentId.value }) : false,
)

const AUTONOMY_OPTS = [
    { value: 'off', label: 'Off' },
    { value: 'suggest', label: 'Suggest' },
    { value: 'auto', label: 'Auto' },
]
// Per-stage dials shown under "Advanced". The three eval triggers are collapsed
// into a single control (see `evalTrigger`); on_repeated_failure + max_iterations
// are rendered explicitly in the template.
const advancedDials = [
    { key: 'train_on_failure', label: 'Train on failure', help: 'When evals fail, draft instructions that fix them.', options: AUTONOMY_OPTS },
    { key: 'approve_instructions', label: 'Approve instructions', help: 'Push a passing build live. Auto = no human in the loop.', options: AUTONOMY_OPTS },
    { key: 'auto_promote_evals', label: 'Auto-promote thumbs-up evals', help: 'Promote auto-drafted evals (from a thumbs-up) straight to active.', options: AUTONOMY_OPTS },
]

const showAdvanced = ref(false)

// One autonomy preset bundles the per-stage dials. "Custom" appears when the
// Advanced dials don't match any preset. Presets don't touch on_repeated_failure
// or max_iterations — those stay independent knobs under Advanced.
const PRESETS: Record<string, { trigger: string; train_on_failure: string; approve_instructions: string; auto_promote_evals: string }> = {
    manual:     { trigger: 'off',  train_on_failure: 'off',  approve_instructions: 'suggest', auto_promote_evals: 'off' },
    assisted:   { trigger: 'auto', train_on_failure: 'auto', approve_instructions: 'suggest', auto_promote_evals: 'off' },
    autonomous: { trigger: 'auto', train_on_failure: 'auto', approve_instructions: 'auto',    auto_promote_evals: 'auto' },
}
const preset = ref<'manual' | 'assisted' | 'autonomous' | 'custom'>('assisted')
const presetHelp = computed(() => ({
    manual: 'Runs only when you click “Run reliability check”. Nothing changes on its own.',
    assisted: 'Runs evals on changes and drafts fixes — you approve before anything goes live.',
    autonomous: 'Measures, fixes, and promotes end to end. No human in the loop.',
    custom: 'Custom per-stage settings — see Advanced below.',
} as Record<string, string>)[preset.value])

function detectPreset(): 'manual' | 'assisted' | 'autonomous' | 'custom' {
    const f = form.value
    const triggersEqual = f.eval_on_table_change === f.eval_on_change && f.eval_on_change === f.eval_on_global_change
    if (triggersEqual) {
        for (const name of ['manual', 'assisted', 'autonomous'] as const) {
            const p = PRESETS[name]
            if (f.eval_on_change === p.trigger && f.train_on_failure === p.train_on_failure
                && f.approve_instructions === p.approve_instructions && f.auto_promote_evals === p.auto_promote_evals) {
                return name
            }
        }
    }
    return 'custom'
}
function applyPreset() {
    const p = PRESETS[preset.value]
    if (!p) return  // 'custom' isn't directly selectable
    form.value.eval_on_table_change = p.trigger
    form.value.eval_on_change = p.trigger
    form.value.eval_on_global_change = p.trigger
    form.value.train_on_failure = p.train_on_failure
    form.value.approve_instructions = p.approve_instructions
    form.value.auto_promote_evals = p.auto_promote_evals
    markDirty()
}
// Single control standing in for the three eval-trigger dials.
const evalTrigger = computed<string>({
    get: () => form.value.eval_on_change,
    set: (v: string) => {
        form.value.eval_on_table_change = v
        form.value.eval_on_change = v
        form.value.eval_on_global_change = v
        onAdvancedChange()
    },
})
function onAdvancedChange() { markDirty(); preset.value = detectPreset() }
function onEnableChange() { markDirty() }

const defaultForm = () => ({
    enabled: false,
    eval_on_table_change: 'suggest',
    eval_on_change: 'suggest',
    eval_on_global_change: 'suggest',
    train_on_failure: 'suggest',
    approve_instructions: 'suggest',
    auto_promote_evals: 'off',
    on_repeated_failure: 'training',
    max_iterations: 3,
})
const form = ref<Record<string, any>>(defaultForm())
const storedOverride = ref<Record<string, any>>({})
const dirty = ref(false)
const savingSettings = ref(false)
const savedOk = ref(false)
const autoEnabled = ref<boolean | null>(null)
const reliabilityStatus = ref('training')
const publishStatus = ref('published')

const autoRuns = ref<any[]>([])
const loadingAutoRuns = ref(false)
const triggering = ref(false)

function markDirty() { dirty.value = true; savedOk.value = false }

const reliabilityLabel = computed(() => {
    if (publishStatus.value === 'disabled') return 'Disabled'
    if (reliabilityStatus.value === 'development') return 'Development'
    if (reliabilityStatus.value === 'training') return 'Training'
    return 'Healthy'
})
const reliabilityBadgeClass = computed(() => {
    if (publishStatus.value === 'disabled') return 'bg-red-100 text-red-800'
    if (reliabilityStatus.value === 'development') return 'bg-amber-100 text-amber-800'
    if (reliabilityStatus.value === 'training') return 'bg-blue-100 text-blue-800'
    return 'bg-green-100 text-green-800'
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
    if (s === 'passed') return 'bg-green-100 text-green-800'
    if (s === 'passed_pending') return 'bg-blue-100 text-blue-800'
    if (s === 'gave_up' || s === 'error') return 'bg-red-100 text-red-800'
    if (s === 'running') return 'bg-gray-100 text-gray-700'
    return 'bg-gray-100 text-gray-600'
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
        autoEnabled.value = !!(data.effective?.enabled)
        storedOverride.value = data.override || {}
        // form = effective, so unset rows show the inherited value
        form.value = { ...defaultForm(), ...(data.effective || {}) }
        preset.value = detectPreset()
        dirty.value = false
        savedOk.value = false
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

async function saveSettings() {
    const id = agentId.value
    if (!id) return
    savingSettings.value = true
    savedOk.value = false
    try {
        // Send the full form as the override (explicit is clearer than diffing
        // against the inherited org default for v1).
        await useMyFetch(`/data_sources/${id}/automation`, { method: 'PATCH', body: { ...form.value } })
        savedOk.value = true
        dirty.value = false
        await loadAutomation()
    } catch (e) {
        toast.add({ title: 'Failed to save automation settings', color: 'red' })
    } finally { savingSettings.value = false }
}

async function runAutomationNow() {
    const id = agentId.value
    if (!id) return
    triggering.value = true
    try {
        await useMyFetch(`/data_sources/${id}/automation/run`, { method: 'POST' })
        toast.add({ title: 'Reliability check started', color: 'green' })
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
