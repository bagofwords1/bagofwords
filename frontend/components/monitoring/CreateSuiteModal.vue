<template>
    <div>
    <UModal v-model="isOpen" :ui="{ width: 'sm:max-w-md' }" :prevent-close="false">
        <UCard>
            <template #header>
                <div class="flex items-center justify-between">
                    <h3 class="text-base font-semibold text-gray-900 dark:text-white">Create Suite</h3>
                    <UButton color="gray" variant="ghost" icon="i-heroicons-x-mark-20-solid" @click="close" />
                </div>
            </template>
            <div class="space-y-2">
                <label class="text-xs text-gray-600 dark:text-gray-400">Name</label>
                <input
                    v-model="suiteName"
                    type="text"
                    placeholder="Suite name"
                    class="border border-gray-300 dark:border-gray-600 rounded px-2 py-1 text-xs w-full bg-white dark:bg-gray-800 text-gray-900 dark:text-gray-100 placeholder:text-gray-400 dark:placeholder:text-gray-500"
                    @keyup.enter="create"
                />
            </div>
            <template #footer>
                <div class="flex items-center justify-end gap-2">
                    <UButton color="gray" variant="soft" @click="close">Cancel</UButton>
                    <UButton :loading="isLoading" class="!bg-blue-500 !text-white" @click="create">Create</UButton>
                </div>
            </template>
        </UCard>
    </UModal>
    </div>
</template>

<script setup lang="ts">
const props = defineProps<{
    modelValue: boolean
    /**
     * Agent this suite belongs under. Omitted creates an ORG-WIDE shelf, which
     * takes org-level manage_evals — so an agent manager authoring from their
     * own agent must pass it or the create 403s.
     */
    dataSourceId?: string | null
}>()
const emit = defineEmits<{
    (e: 'update:modelValue', v: boolean): void
    (e: 'created', suite: { id: string; name: string }): void
}>()

const isOpen = computed({
    get: () => props.modelValue,
    set: (v) => emit('update:modelValue', v)
})

const suiteName = ref('')
const isLoading = ref(false)
const toast = useToast()

const close = () => {
    suiteName.value = ''
    emit('update:modelValue', false)
}

const create = async () => {
    if (isLoading.value) return
    const name = (suiteName.value || '').trim()
    if (name.length === 0) {
        toast.add({ title: 'Suite name is required', icon: 'i-heroicons-x-circle', color: 'red' })
        return
    }
    isLoading.value = true
    try {
        const res: any = await useMyFetch('/api/tests/suites', {
            method: 'POST',
            body: { name, data_source_id: props.dataSourceId || null }
        })
        if (res?.error?.value) throw res.error.value
        const suite = res?.data?.value as any
        if (suite?.id) {
            emit('created', { id: suite.id, name: suite.name })
            toast.add({ title: 'Suite created', icon: 'i-heroicons-check-circle', color: 'green' })
            close()
        }
    } catch (e: any) {
        console.error('Failed to create suite', e)
        const detail = e?.data?.detail || e?.response?._data?.detail || e?.message
        toast.add({
            title: 'Failed to create suite',
            description: typeof detail === 'string' ? detail : undefined,
            icon: 'i-heroicons-x-circle', color: 'red',
        })
    } finally {
        isLoading.value = false
    }
}
</script>

