<template>
    <UModal v-model="isOpen" :ui="{ width: 'sm:max-w-md' }" :prevent-close="false">
        <UCard>
            <template #header>
                <div class="flex items-center justify-between">
                    <h3 class="text-base font-semibold text-gray-900">Create Suite</h3>
                    <UButton color="gray" variant="ghost" icon="i-heroicons-x-mark-20-solid" @click="close" />
                </div>
            </template>
            <div class="space-y-2">
                <label class="text-xs text-gray-600">Name</label>
                <input
                    v-model="suiteName"
                    type="text"
                    placeholder="Suite name"
                    class="border border-gray-300 rounded px-2 py-1 text-xs w-full"
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
</template>

<script setup lang="ts">
const props = defineProps<{ modelValue: boolean }>()
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
            body: { name }
        })
        if (res?.error?.value) throw res.error.value
        const suite = res?.data?.value as any
        if (suite?.id) {
            emit('created', { id: suite.id, name: suite.name })
            toast.add({ title: 'Suite created', icon: 'i-heroicons-check-circle', color: 'green' })
            close()
        }
    } catch (e) {
        console.error('Failed to create suite', e)
        toast.add({ title: 'Failed to create suite', icon: 'i-heroicons-x-circle', color: 'red' })
    } finally {
        isLoading.value = false
    }
}
</script>


