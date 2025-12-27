<template>
    <UModal v-model="isOpen" :ui="{ width: 'sm:max-w-2xl' }">
        <UCard>
            <template #header>
                <div class="flex items-center justify-between">
                    <h3 class="text-lg font-semibold text-gray-900">Manage Test Suites</h3>
                    <UButton color="gray" variant="ghost" icon="i-heroicons-x-mark-20-solid" @click="close" />
                </div>
            </template>

            <div class="space-y-4">
                <!-- Header with create button -->
                <div class="flex items-center justify-between">
                    <div class="text-sm text-gray-600">{{ suites.length }} suite{{ suites.length !== 1 ? 's' : '' }}</div>
                    <UButton color="blue" size="xs" variant="soft" icon="i-heroicons-plus" @click="showCreateModal = true">
                        Create New Suite
                    </UButton>
                </div>

                <!-- Loading state -->
                <div v-if="isLoading" class="py-8 text-center text-gray-500 text-sm">
                    Loading suites...
                </div>

                <!-- Empty state -->
                <div v-else-if="suites.length === 0" class="py-8 text-center">
                    <div class="text-gray-500 text-sm mb-2">No test suites yet</div>
                    <UButton color="blue" size="xs" variant="soft" icon="i-heroicons-plus" @click="showCreateModal = true">
                        Create your first suite
                    </UButton>
                </div>

                <!-- Suites list -->
                <div v-else class="max-h-[400px] overflow-y-auto">
                    <table class="min-w-full divide-y divide-gray-200">
                        <thead class="bg-gray-50 sticky top-0">
                            <tr>
                                <th class="px-4 py-2 text-left text-xs font-medium text-gray-500 uppercase tracking-wider">Name</th>
                                <th class="px-4 py-2 text-left text-xs font-medium text-gray-500 uppercase tracking-wider">Tests</th>
                                <th class="px-4 py-2 text-left text-xs font-medium text-gray-500 uppercase tracking-wider">Suite ID</th>
                                <th class="px-4 py-2 text-right text-xs font-medium text-gray-500 uppercase tracking-wider">Actions</th>
                            </tr>
                        </thead>
                        <tbody class="bg-white divide-y divide-gray-200">
                            <tr v-for="suite in suites" :key="suite.id" class="hover:bg-gray-50">
                                <td class="px-4 py-3 text-sm text-gray-900">{{ suite.name }}</td>
                                <td class="px-4 py-3 text-sm text-gray-600">
                                    <span class="inline-flex items-center px-2 py-0.5 rounded-full text-xs font-medium bg-blue-100 text-blue-800">
                                        {{ suite.tests_count }} test{{ suite.tests_count !== 1 ? 's' : '' }}
                                    </span>
                                </td>
                                <td class="px-4 py-3">
                                    <div class="flex items-center gap-1">
                                        <code class="text-xs text-gray-500 bg-gray-100 px-1.5 py-0.5 rounded font-mono truncate max-w-[140px]" :title="suite.id">
                                            {{ suite.id }}
                                        </code>
                                        <UButton
                                            color="gray"
                                            variant="ghost"
                                            size="2xs"
                                            icon="i-heroicons-clipboard-document"
                                            @click="copyId(suite.id)"
                                            title="Copy Suite ID"
                                        />
                                    </div>
                                </td>
                                <td class="px-4 py-3 text-right">
                                    <UButton
                                        color="red"
                                        variant="ghost"
                                        size="xs"
                                        icon="i-heroicons-trash"
                                        :loading="deletingId === suite.id"
                                        @click="confirmDelete(suite)"
                                    >
                                        Delete
                                    </UButton>
                                </td>
                            </tr>
                        </tbody>
                    </table>
                </div>
            </div>

            <template #footer>
                <div class="flex items-center justify-end">
                    <UButton color="gray" variant="soft" @click="close">Close</UButton>
                </div>
            </template>
        </UCard>
    </UModal>

    <!-- Create Suite Modal -->
    <Teleport to="body">
        <CreateSuiteModal v-model="showCreateModal" @created="onSuiteCreated" />
    </Teleport>
</template>

<script setup lang="ts">
import CreateSuiteModal from '~/components/monitoring/CreateSuiteModal.vue'

interface SuiteSummary {
    id: string
    name: string
    tests_count: number
    last_run_at?: string | null
    last_status?: string | null
    pass_rate?: number | null
}

const props = defineProps<{ modelValue: boolean }>()
const emit = defineEmits<{
    (e: 'update:modelValue', v: boolean): void
    (e: 'suiteCreated', suite: { id: string; name: string }): void
    (e: 'suiteDeleted', suiteId: string): void
}>()

const isOpen = computed({
    get: () => props.modelValue,
    set: (v) => emit('update:modelValue', v)
})

const suites = ref<SuiteSummary[]>([])
const isLoading = ref(false)
const deletingId = ref<string | null>(null)
const showCreateModal = ref(false)
const toast = useToast()

const close = () => emit('update:modelValue', false)

const loadSuites = async () => {
    isLoading.value = true
    try {
        const res: any = await useMyFetch('/api/tests/suites/summary')
        if (res?.error?.value) throw res.error.value
        suites.value = (res?.data?.value || []) as SuiteSummary[]
    } catch (e) {
        console.error('Failed to load suites', e)
        suites.value = []
    } finally {
        isLoading.value = false
    }
}

const copyId = async (id: string) => {
    try {
        await navigator.clipboard.writeText(id)
        toast.add({ title: 'Suite ID copied', icon: 'i-heroicons-check-circle', color: 'green' })
    } catch (e) {
        console.error('Failed to copy', e)
        toast.add({ title: 'Failed to copy', icon: 'i-heroicons-x-circle', color: 'red' })
    }
}

const confirmDelete = async (suite: SuiteSummary) => {
    const msg = suite.tests_count > 0
        ? `Delete suite "${suite.name}" and its ${suite.tests_count} test case${suite.tests_count !== 1 ? 's' : ''}? This cannot be undone.`
        : `Delete suite "${suite.name}"? This cannot be undone.`
    
    if (!window.confirm(msg)) return
    
    deletingId.value = suite.id
    try {
        const res: any = await useMyFetch(`/api/tests/suites/${suite.id}`, { method: 'DELETE' })
        if (res?.error?.value) throw res.error.value
        suites.value = suites.value.filter(s => s.id !== suite.id)
        emit('suiteDeleted', suite.id)
        toast.add({ title: 'Suite deleted', icon: 'i-heroicons-check-circle', color: 'green' })
    } catch (e) {
        console.error('Failed to delete suite', e)
        toast.add({ title: 'Failed to delete suite', icon: 'i-heroicons-x-circle', color: 'red' })
    } finally {
        deletingId.value = null
    }
}

const onSuiteCreated = (suite: { id: string; name: string }) => {
    // Reload to get accurate counts
    loadSuites()
    emit('suiteCreated', suite)
}

// Load suites when modal opens
watch(isOpen, (open) => {
    if (open) loadSuites()
})
</script>


