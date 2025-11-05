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
                            <th class="px-6 py-3 text-left text-xs font-medium text-gray-500 uppercase tracking-wider">Name</th>
                            <th class="px-6 py-3 text-left text-xs font-medium text-gray-500 uppercase tracking-wider">Prompt</th>
                        </tr>
                    </thead>
                    <tbody class="bg-white divide-y divide-gray-200 text-xs">
                        <tr v-for="c in cases" :key="c.id">
                            <td class="px-6 py-3 text-gray-900">{{ c.name }}</td>
                            <td class="px-6 py-3 max-w-[520px]"><span class="truncate block" :title="c.prompt_json?.content || ''">{{ c.prompt_json?.content || '—' }}</span></td>
                        </tr>
                        <tr v-if="cases.length === 0">
                            <td colspan="2" class="px-6 py-6 text-center text-gray-500">No test cases found</td>
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

interface TestCaseItem { id: string; name: string; prompt_json?: { content?: string } }
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

import AddTestCaseModal from '~/components/monitoring/AddTestCaseModal.vue'
</script>


