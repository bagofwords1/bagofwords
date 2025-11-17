<template>
    <!-- Shared label create/edit modal used across instructions UI -->
    <!-- Implemented as a simple overlay + card to avoid nested UModal key handling issues -->
    <div v-if="isOpen" class="fixed inset-0 z-[70] flex items-center justify-center">
        <!-- Backdrop -->
        <div class="absolute inset-0 bg-black/20" @click="close"></div>

        <!-- Dialog -->
        <div class="relative z-10 w-full max-w-md px-4">
            <UCard>
                <template #header>
                    <div class="flex items-center justify-between">
                        <h3 class="text-base font-semibold text-gray-900">{{ title }}</h3>
                        <UButton
                            color="gray"
                            variant="ghost"
                            icon="i-heroicons-x-mark-20-solid"
                            @click="close"
                        />
                    </div>
                </template>

                <div class="space-y-3">
                    <div class="flex flex-col">
                        <label class="text-xs font-medium text-gray-600 mb-1">Name</label>
                        <input
                            ref="nameInput"
                            v-model="form.name"
                            type="text"
                            class="w-full text-sm p-2 border border-gray-200 rounded-md focus:ring-0 focus:outline-none focus:border-gray-300"
                            placeholder="e.g., Finance"
                            required
                        />
                    </div>

                    <div class="flex flex-col">
                        <label class="text-xs font-medium text-gray-600 mb-1">Description</label>
                        <textarea
                            v-model="form.description"
                            rows="3"
                            class="w-full text-sm p-2 border border-gray-200 rounded-md focus:ring-0 focus:outline-none focus:border-gray-300"
                            placeholder="Short description (optional)"
                        />
                    </div>

                    <div class="flex flex-col">
                        <label class="text-xs font-medium text-gray-600 mb-1.5">Color</label>
                        <UPopover>
                            <template #default="{ open }">
                                <button
                                    type="button"
                                    class="flex items-center gap-2 border border-gray-200 rounded-md px-3 py-1.5 text-xs w-20 justify-between"
                                >
                                    <div class="flex items-center gap-2">
                                        <span class="w-4 h-4 rounded-full border border-gray-300" :style="{ backgroundColor: form.color }"></span>
                                    </div>
                                    <Icon :name="open ? 'heroicons:chevron-up' : 'heroicons:chevron-down'" class="w-4 h-4 text-gray-400" />
                                </button>
                            </template>
                            <template #panel>
                                <div class="p-3 w-48">
                                    <div class="grid grid-cols-4 gap-2">
                                        <button
                                            v-for="color in defaultColors"
                                            :key="color"
                                            type="button"
                                            class="w-7 h-7 rounded-full border focus:outline-none focus:ring-2 focus:ring-offset-1 focus:ring-blue-200"
                                            :class="form.color === color ? 'border-gray-900' : 'border-gray-200'"
                                            :style="{ backgroundColor: color }"
                                            @click="form.color = color"
                                        />
                                    </div>
                                </div>
                            </template>
                        </UPopover>
                    </div>
                </div>

                <template #footer>
                    <div class="flex items-center justify-between gap-2">
                        <UButton
                            v-if="editingLabelId"
                            color="red"
                            variant="soft"
                            @click="handleDelete"
                            :loading="isDeleting"
                            :disabled="!canDelete"
                        >
                            Delete
                        </UButton>
                        <div class="ml-auto flex items-center gap-2">
                            <UButton 
                                color="gray" 
                                variant="soft" 
                                @click="close" 
                                :disabled="isSaving || isDeleting"
                            >
                                Cancel
                            </UButton>
                            <UButton
                                class="!bg-blue-500 !text-white"
                                :loading="isSaving"
                                @click="submit"
                                :disabled="!canSubmit"
                            >
                                {{ editingLabelId ? 'Save Changes' : 'Create Label' }}
                            </UButton>
                        </div>
                    </div>
                </template>
            </UCard>
        </div>
    </div>
</template>

<script setup lang="ts">
import { useCan, usePermissionsLoaded } from '~/composables/usePermissions'

interface InstructionLabel {
    id: string
    name: string
    color?: string | null
    description?: string | null
}

interface LabelForm {
    name: string
    description: string
    color: string
}

const props = defineProps<{
    modelValue: boolean
    label?: InstructionLabel | null
}>()

const emit = defineEmits<{
    'update:modelValue': [value: boolean]
    'saved': [payload: { label: InstructionLabel | null; isNew: boolean }]
    'deleted': [labelId: string]
}>()

const toast = useToast()
const permissionsLoaded = usePermissionsLoaded()
const nameInput = ref<HTMLInputElement>()

const isOpen = computed({
    get: () => props.modelValue,
    set: (value) => emit('update:modelValue', value)
})

const editingLabelId = computed(() => props.label?.id || null)
const title = computed(() => editingLabelId.value ? 'Edit Label' : 'New Label')

const canCreate = computed(() => permissionsLoaded.value && useCan('create_instructions'))
const canEdit = computed(() => permissionsLoaded.value && useCan('update_instructions'))
const canDelete = computed(() => permissionsLoaded.value && useCan('delete_instructions'))

const canSubmit = computed(() => {
    const hasPermission = editingLabelId.value ? canEdit.value : canCreate.value
    return hasPermission && Boolean(form.name.trim()) && !isSaving.value
})

const isSaving = ref(false)
const isDeleting = ref(false)

const defaultColors = [
    '#f87171', '#fb923c', '#facc15', '#bef264',
    '#34d399', '#2dd4bf', '#38bdf8', '#818cf8',
    '#c084fc', '#f472b6', '#a5b4fc', '#60a5fa'
]

const form = reactive<LabelForm>({
    name: '',
    description: '',
    color: defaultColors[4]
})

const resetForm = () => {
    form.name = ''
    form.description = ''
    form.color = defaultColors[Math.floor(Math.random() * defaultColors.length)]
}

const loadLabel = (label: InstructionLabel | null | undefined) => {
    if (label) {
        form.name = label.name || ''
        form.description = label.description || ''
        form.color = label.color || defaultColors[4]
    } else {
        resetForm()
    }
}

const close = () => {
    isOpen.value = false
}

const submit = async () => {
    const trimmedName = (form.name || '').trim()
    if (!trimmedName) {
        toast.add({
            title: 'Validation error',
            description: 'Label name is required',
            color: 'red'
        })
        return
    }

    if (editingLabelId.value && !canEdit.value) return
    if (!editingLabelId.value && !canCreate.value) return

    isSaving.value = true
    const payload = {
        name: trimmedName,
        color: form.color || null,
        description: form.description.trim() ? form.description.trim() : null
    }

    try {
        const endpoint = editingLabelId.value
            ? `/instructions/labels/${editingLabelId.value}`
            : '/instructions/labels'
        const method = editingLabelId.value ? 'PATCH' : 'POST'
        const { data, error } = await useMyFetch<InstructionLabel>(endpoint, {
            method,
            body: payload
        })

        if (error.value) {
            throw error.value
        }

        toast.add({
            title: `Label ${editingLabelId.value ? 'updated' : 'created'}`,
            color: 'green'
        })

        const isNew = !editingLabelId.value
        emit('saved', { label: (data.value as InstructionLabel) || null, isNew })
        close()
    } catch (error) {
        console.error('Failed to save instruction label:', error)
        toast.add({
            title: 'Failed to save label',
            color: 'red'
        })
    } finally {
        isSaving.value = false
    }
}

const handleDelete = async () => {
    if (!editingLabelId.value || !canDelete.value) return
    
    const labelName = props.label?.name || ''
    const confirmed = confirm(labelName ? `Delete label "${labelName}"?` : 'Delete this label?')
    if (!confirmed) return

    isDeleting.value = true
    try {
        const { error } = await useMyFetch(`/instructions/labels/${editingLabelId.value}`, {
            method: 'DELETE'
        })
        if (error.value) {
            throw error.value
        }

        toast.add({
            title: 'Label deleted',
            color: 'green'
        })

        emit('deleted', editingLabelId.value)
        close()
    } catch (error) {
        console.error('Failed to delete instruction label:', error)
        toast.add({
            title: 'Failed to delete label',
            color: 'red'
        })
    } finally {
        isDeleting.value = false
    }
}

// Watch for label prop changes
watch(() => props.label, (newLabel) => {
    loadLabel(newLabel)
}, { immediate: true })

// Watch for modal open/close
watch(isOpen, (isOpen) => {
    if (isOpen) {
        loadLabel(props.label)
        // Focus the name input after the modal opens
        nextTick(() => {
            nameInput.value?.focus()
        })
    } else {
        // Reset form when closing
        resetForm()
    }
})

// Global ESC handler in capture phase:
// - When this overlay is open, ESC closes it and stops propagation,
//   so the parent UModal won't see the event.
// - Once this overlay is closed, ESC will bubble normally and can
//   close the parent modal instead.
let escHandler: ((e: KeyboardEvent) => void) | null = null

onMounted(() => {
    escHandler = (e: KeyboardEvent) => {
        if (e.key === 'Escape' && isOpen.value) {
            e.stopPropagation()
            e.preventDefault()
            close()
        }
    }
    window.addEventListener('keydown', escHandler, { capture: true })
})

onUnmounted(() => {
    if (escHandler) {
        window.removeEventListener('keydown', escHandler, { capture: true })
    }
})
</script>

