<template>
    <UModal v-model="instructionModalOpen">
        <div class="relative">
            <button @click="instructionModalOpen = false" class="absolute top-2 right-2 text-gray-500 hover:text-gray-700">
                <Icon name="heroicons:x-mark" class="w-5 h-5" />
            </button>
            <h1 class="text-lg font-semibold">{{ isEditing ? 'Edit Instruction' : 'Add New Instruction' }}</h1>
            <p class="text-sm text-gray-500">Create or modify instructions for AI agents</p>
            <hr class="my-4" />

            <!-- Conditional rendering based on the computed selectedInstructionType -->
            <InstructionGlobalCreateComponent 
                v-if="selectedInstructionType === 'global' && useCan('create_instructions')"
                :instruction="instruction"
                :shared-form="sharedForm"
                :selected-data-sources="selectedDataSources"
                @instruction-saved="handleInstructionSaved"
                @cancel="closeModal"
                @update-form="updateSharedForm"
                @update-data-sources="updateSelectedDataSources"
            />
            <InstructionPrivateCreateComponent 
                v-else
                :instruction="instruction"
                :shared-form="sharedForm"
                :selected-data-sources="selectedDataSources"
                :is-suggestion="effectiveIsSuggestion"
                @instruction-saved="handleInstructionSaved"
                @cancel="closeModal"
                @update-form="updateSharedForm"
                @update-data-sources="updateSelectedDataSources"
            />
        </div>
    </UModal>
</template>

<script setup lang="ts">
import InstructionGlobalCreateComponent from '~/components/InstructionGlobalCreateComponent.vue'
import InstructionPrivateCreateComponent from '~/components/InstructionPrivateCreateComponent.vue'
import { usePermissionsLoaded, useCan } from '~/composables/usePermissions'

// Define interfaces
interface DataSource {
    id: string
    name: string
    type: string
}

interface SharedForm {
    text: string
    status: 'draft' | 'published' | 'archived'
    category: 'code_gen' | 'data_modeling' | 'general'
    is_seen: boolean
    can_user_toggle: boolean
}

// Props and Emits
const props = defineProps<{
    modelValue: boolean
    instruction?: any
    initialType?: 'global' | 'private'
    isSuggestion?: boolean
}>()

const emit = defineEmits(['update:modelValue', 'instructionSaved'])

// Reactive state
const selectedDataSources = ref<string[]>([])
const sharedForm = ref<SharedForm>({
    text: '',
    status: 'draft',
    category: 'general',
    is_seen: true,
    can_user_toggle: true
})

// Computed properties
const isEditing = computed(() => !!props.instruction)

const instructionModalOpen = computed({
    get: () => props.modelValue,
    set: (value) => emit('update:modelValue', value)
})

const selectedInstructionType = computed(() => {
    // 1. If we are editing an existing instruction, its status is the source of truth.
    if (isEditing.value && props.instruction) {
        const inst = props.instruction
        // The "global" component handles approved global instructions and suggestions pending review.
        const isHandledByGlobalComponent = inst.global_status === 'approved' || inst.global_status === 'suggested'
        return isHandledByGlobalComponent ? 'global' : 'private'
    }
    
    // 2. If creating a new instruction, the `initialType` prop takes precedence.
    if (props.initialType) {
        return props.initialType
    }
    
    // 3. Otherwise, fall back to the user's permission level, waiting for them to load.
    const permissionsLoaded = usePermissionsLoaded()
    if (!permissionsLoaded.value) {
        // Default to private to avoid flashing the admin UI. It will correct itself once permissions load.
        return 'private' 
    }
    return useCan('create_instructions') ? 'global' : 'private'
})

// Non-admins default to suggestions when creating
const effectiveIsSuggestion = computed(() => {
    if (props.isSuggestion !== undefined) return props.isSuggestion
    return !useCan('create_instructions')
})

// Event handlers
const closeModal = () => {
    instructionModalOpen.value = false
    // resetForm is now called by the watcher below
}

const resetForm = () => {
    sharedForm.value = {
        text: '',
        status: 'draft',
        category: 'general',
        is_seen: true,
        can_user_toggle: true
    }
    selectedDataSources.value = []
}

const updateSharedForm = (formData: Partial<SharedForm>) => {
    Object.assign(sharedForm.value, formData)
}

const updateSelectedDataSources = (dataSources: string[]) => {
    selectedDataSources.value = dataSources
}

const handleInstructionSaved = (data: any) => {
    emit('instructionSaved', data)
    closeModal()
}

// Watchers
watch(() => props.instruction, (newInstruction) => {
    if (newInstruction) {
        // Populate the form when an instruction to edit is passed in.
        sharedForm.value = {
            text: newInstruction.text || '',
            status: newInstruction.status || 'draft',
            category: newInstruction.category || 'general',
            is_seen: newInstruction.is_seen !== undefined ? newInstruction.is_seen : true,
            can_user_toggle: newInstruction.can_user_toggle !== undefined ? newInstruction.can_user_toggle : true
        }
        selectedDataSources.value = newInstruction.data_sources?.map((ds: DataSource) => ds.id) || []
    } else {
        // If the instruction prop is cleared, reset the form for a clean 'create' state.
        resetForm()
    }
}, { immediate: true })

// Reset the form state only when the modal is closed.
watch(instructionModalOpen, (isOpen) => {
    if (!isOpen) {
        resetForm()
    }
})
</script> 