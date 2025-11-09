<template>
    <div class="mt-6">
        <div class="flex items-center justify-between mb-4">
            <div>
                <h2 class="text-xl font-semibold text-gray-900">Test Suite</h2>
                <div class="text-sm text-gray-500">ID: {{ suiteId }}</div>
            </div>
            <div class="flex items-center space-x-2">
                <UButton color="gray" variant="ghost" @click="goBack">Back</UButton>
                <UButton color="blue" icon="i-heroicons-plus" @click="showAddCase = true">Add Case</UButton>
            </div>
        </div>

        <div class="bg-white shadow-sm border border-gray-200 rounded-lg overflow-hidden">
            <div class="px-6 py-4 border-b border-gray-200 flex items-center justify-between">
                <div class="text-sm font-medium text-gray-700">Test Cases</div>
            </div>
            <div class="overflow-x-auto">
                <table class="min-w-full divide-y divide-gray-200">
                    <thead class="bg-gray-50">
                        <tr>
                            <th class="px-6 py-3 text-left text-xs font-medium text-gray-500 uppercase tracking-wider">Prompt</th>
                            <th class="px-6 py-3 text-left text-xs font-medium text-gray-500 uppercase tracking-wider">Tests summary</th>
                            <th class="px-6 py-3 text-left text-xs font-medium text-gray-500 uppercase tracking-wider">Run / Edit / Previous Runs</th>
                        </tr>
                    </thead>
                    <tbody class="bg-white divide-y divide-gray-200 text-xs">
                        <tr v-for="c in cases" :key="c.id">
                            <td class="px-6 py-3 max-w-[520px]"><span class="truncate block" :title="c.prompt_json?.content || ''">{{ c.prompt_json?.content || '—' }}</span></td>
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
                            <td class="px-6 py-3">
                                <div class="flex items-center gap-2">
                                    <UButton color="blue" size="xs" variant="soft" icon="i-heroicons-play">Run</UButton>
                                    <UButton color="gray" size="xs" variant="soft" icon="i-heroicons-pencil-square" @click="editCase(c)">Edit</UButton>
                                    <UButton color="gray" size="xs" variant="ghost" icon="i-heroicons-clock">Previous Runs</UButton>
                                </div>
                            </td>
                        </tr>
                        <tr v-if="cases.length === 0">
                            <td colspan="3" class="px-6 py-6 text-center text-gray-500">No test cases found</td>
                        </tr>
                    </tbody>
                </table>
            </div>
        </div>
    </div>
    <AddTestCaseModal v-model="showAddCase" :suite-id="suiteId" @created="onCaseCreated" />
 </template>

<script setup lang="ts">
definePageMeta({ layout: 'monitoring' })

const route = useRoute()
const router = useRouter()
const suiteId = computed(() => route.params.id as string)

interface TestCaseItem { id: string; name: string; prompt_json?: { content?: string }, expectations_json?: { rules?: any[] }, data_source_ids_json?: string[] }
const cases = ref<TestCaseItem[]>([])
const showAddCase = ref(false)

const goBack = () => router.push('/monitoring/tests')

onMounted(async () => {
    try {
        const res = await useMyFetch<TestCaseItem[]>(`/api/test/suites/${suiteId.value}/cases`)
        if (res.data.value) cases.value = res.data.value
    } catch (e) {
        console.error('Failed to load test cases', e)
    }
})

const onCaseCreated = (c: any) => {
    cases.value = [...cases.value, c]
}

function editCase(c: TestCaseItem) {
    // Open modal in create mode for now; prefill could be added later
    showAddCase.value = true
}

// ---- Helpers to build human-readable test summary ----
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

function summaryForCase(c: TestCaseItem): string {
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

function categoriesForCase(c: TestCaseItem): string[] {
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

import AddTestCaseModal from '~/components/monitoring/AddTestCaseModal.vue'
</script>


