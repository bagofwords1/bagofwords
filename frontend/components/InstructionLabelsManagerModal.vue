<template>
    <!-- Labels Manager Modal -->
    <UModal v-model="isOpen" :ui="{ width: 'sm:max-w-2xl' }">
        <UCard>
            <template #header>
                <div class="flex items-center justify-between">
                    <div>
                        <h3 class="text-base font-semibold text-gray-900">Labels</h3>
                        <p class="text-sm text-gray-500 mt-1">Organize the labels used across instructions.</p>
                    </div>
                    <div class="flex items-center gap-2">
                        <UButton
                            v-if="canCreate"
                            icon="i-heroicons-plus"
                            color="blue"
                            variant="solid"
                            size="sm"
                            @click="openCreateLabelModal"
                        >
                            New label
                        </UButton>
                        <UButton
                            color="gray"
                            variant="ghost"
                            icon="i-heroicons-x-mark-20-solid"
                            @click="close"
                        />
                    </div>
                </div>
            </template>

            <div>
                <!-- Search -->
                <div class="mb-4">
                    <div class="relative">
                        <input
                            v-model="searchQuery"
                            type="text"
                            placeholder="Search all labels"
                            class="w-full pl-10 pr-4 py-2 border border-gray-300 rounded-md focus:outline-none focus:ring-2 focus:ring-blue-500 focus:border-blue-500 text-sm"
                        />
                        <UIcon name="i-heroicons-magnifying-glass" class="absolute left-3 top-2.5 h-4 w-4 text-gray-400" />
                    </div>
                </div>

                <!-- Labels List -->
                <div class="flex items-center justify-between mb-3">
                    <span class="text-sm text-gray-600">{{ filteredLabels.length }} labels</span>
                    <USelectMenu
                        v-model="sortBy"
                        :options="sortOptions"
                        option-attribute="label"
                        value-attribute="value"
                        size="xs"
                    >
                        <template #label>
                            <span class="text-xs text-gray-600">Sort</span>
                        </template>
                    </USelectMenu>
                </div>

                <div v-if="isLoading" class="py-8 text-center text-sm text-gray-500">
                    <div class="flex items-center justify-center gap-2">
                        <span class="animate-spin rounded-full h-5 w-5 border-b-2 border-blue-500"></span>
                        Loading labels...
                    </div>
                </div>
                <div v-else-if="filteredLabels.length === 0" class="py-10 text-center text-sm text-gray-500">
                    <p v-if="searchQuery">No labels match your search.</p>
                    <p v-else>No labels yet. Create your first label to start organizing instructions.</p>
                </div>
                <div v-else class="divide-y divide-gray-100">
                    <div
                        v-for="label in sortedLabels"
                        :key="label.id"
                        class="flex flex-col sm:flex-row sm:items-center justify-between gap-4 py-3 hover:bg-gray-50 transition-colors"
                    >
                        <div class="flex items-start gap-3 flex-1">
                            <span
                                class="mt-1 w-3 h-3 rounded-full border border-gray-200 flex-shrink-0"
                                :style="{ backgroundColor: label.color || '#CBD5F5', borderColor: label.color || '#CBD5F5' }"
                            ></span>
                            <div class="flex-1 min-w-0">
                                <p class="text-sm font-medium text-gray-900">
                                    {{ label.name }}
                                </p>
                                <p v-if="label.description" class="text-xs text-gray-500 mt-0.5">
                                    {{ label.description }}
                                </p>
                            </div>
                        </div>
                        <div class="flex items-center justify-end gap-2">
                            <UBadge v-if="labelUsageCount(label.id) > 0" size="sm" variant="soft" color="gray">
                                {{ labelUsageCount(label.id) }}
                            </UBadge>
                            <UDropdown :items="getLabelActionItems(label)">
                                <UButton
                                    color="white"
                                    variant="ghost"
                                    class="text-gray-500 hover:text-gray-900"
                                    icon="i-heroicons-ellipsis-vertical"
                                />
                            </UDropdown>
                        </div>
                    </div>
                </div>
            </div>
        </UCard>

        <!-- Label Form Modal (rendered inside same UModal so focus stays in this dialog) -->
        <InstructionLabelFormModal
            v-model="showLabelFormModal"
            :label="editingLabel"
            @saved="handleLabelSaved"
            @deleted="handleLabelDeleted"
        />
    </UModal>
</template>

<script setup lang="ts">
import { useCan, usePermissionsLoaded } from '~/composables/usePermissions'
import InstructionLabelFormModal from '~/components/InstructionLabelFormModal.vue'

interface InstructionLabel {
    id: string
    name: string
    color?: string | null
    description?: string | null
}

interface Instruction {
    id: string
    labels?: InstructionLabel[]
}

type DropdownItem = {
    label: string
    icon?: string
    click?: () => void
    disabled?: boolean
}

const props = defineProps<{
    modelValue: boolean
    instructions?: Instruction[]
}>()

const emit = defineEmits<{
    'update:modelValue': [value: boolean]
    'labelsUpdated': []
}>()

const toast = useToast()
const permissionsLoaded = usePermissionsLoaded()

const isOpen = computed({
    get: () => props.modelValue,
    set: (value) => emit('update:modelValue', value)
})

const canCreate = computed(() => permissionsLoaded.value && useCan('create_instructions'))
const canEdit = computed(() => permissionsLoaded.value && useCan('update_instructions'))
const canDelete = computed(() => permissionsLoaded.value && useCan('delete_instructions'))

const labels = ref<InstructionLabel[]>([])
const isLoading = ref(false)
const searchQuery = ref('')
const sortBy = ref<'name' | 'usage'>('name')

const showLabelFormModal = ref(false)
const editingLabel = ref<InstructionLabel | null>(null)

const sortOptions = [
    { label: 'Name', value: 'name' },
    { label: 'Usage', value: 'usage' }
]

const filteredLabels = computed(() => {
    if (!searchQuery.value.trim()) return labels.value
    const query = searchQuery.value.toLowerCase()
    return labels.value.filter(label =>
        label.name.toLowerCase().includes(query) ||
        (label.description || '').toLowerCase().includes(query)
    )
})

const sortedLabels = computed(() => {
    const list = [...filteredLabels.value]
    if (sortBy.value === 'name') {
        return list.sort((a, b) => a.name.localeCompare(b.name))
    } else {
        return list.sort((a, b) => {
            const countA = labelUsageCount(a.id)
            const countB = labelUsageCount(b.id)
            return countB - countA
        })
    }
})

const close = () => {
    isOpen.value = false
}

const fetchLabels = async () => {
    isLoading.value = true
    try {
        const { data, error } = await useMyFetch<InstructionLabel[]>('/instructions/labels', {
            method: 'GET'
        })
        if (error.value) {
            console.error('Failed to fetch labels:', error.value)
            labels.value = []
        } else if (data.value) {
            labels.value = Array.isArray(data.value) ? data.value : []
        }
    } catch (err) {
        console.error('Error fetching labels:', err)
        labels.value = []
    } finally {
        isLoading.value = false
    }
}

const labelUsageCount = (labelId: string) => {
    if (!labelId || !props.instructions) return 0
    return props.instructions.reduce((total, instruction) => {
        const hasLabel = (instruction.labels || []).some(label => label.id === labelId)
        return total + (hasLabel ? 1 : 0)
    }, 0)
}

const openCreateLabelModal = () => {
    editingLabel.value = null
    showLabelFormModal.value = true
}

const openEditLabelModal = (label: InstructionLabel) => {
    if (!label?.id) return
    editingLabel.value = label
    showLabelFormModal.value = true
}

const handleLabelSaved = async () => {
    showLabelFormModal.value = false
    await fetchLabels()
    emit('labelsUpdated')
}

const handleLabelDeleted = async (labelId: string) => {
    showLabelFormModal.value = false
    await fetchLabels()
    emit('labelsUpdated')
}

const deleteLabel = async (label: InstructionLabel) => {
    if (!label?.id || !canDelete.value) return
    
    const confirmed = confirm(label.name ? `Delete label "${label.name}"?` : 'Delete this label?')
    if (!confirmed) return

    try {
        const { error } = await useMyFetch(`/instructions/labels/${label.id}`, {
            method: 'DELETE'
        })
        if (error.value) {
            throw error.value
        }

        toast.add({
            title: 'Label deleted',
            color: 'green'
        })

        await fetchLabels()
        emit('labelsUpdated')
    } catch (error) {
        console.error('Failed to delete instruction label:', error)
        toast.add({
            title: 'Failed to delete label',
            color: 'red'
        })
    }
}

const getLabelActionItems = (label: InstructionLabel): DropdownItem[][] => {
    return [[
        {
            label: 'Edit',
            icon: 'i-heroicons-pencil-square-20-solid',
            disabled: !canEdit.value,
            click: () => openEditLabelModal(label)
        },
        {
            label: 'Delete',
            icon: 'i-heroicons-trash-20-solid',
            disabled: !canDelete.value,
            click: () => deleteLabel(label)
        }
    ]]
}

watch(isOpen, (isOpen) => {
    if (isOpen) {
        fetchLabels()
    } else {
        searchQuery.value = ''
        sortBy.value = 'name'
    }
})
</script>

