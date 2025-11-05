<template>
    <div v-if="modelValue" class="fixed inset-0 z-50 flex items-center justify-center">
        <div class="absolute inset-0 bg-black/30" @click="emit('update:modelValue', false)"></div>
        <div class="relative bg-white w-full max-w-2xl rounded-xl shadow-lg border border-gray-200">
            <form @submit.prevent="submit" class="p-4">
                <div class="flex items-center justify-between mb-2">
                    <label class="text-md font-medium text-gray-800">Create Test Suite</label>
                    <button type="button" class="text-gray-400 hover:text-gray-600" @click="emit('update:modelValue', false)">✕</button>
                </div>

                <div class="flex flex-col mx-auto max-w-2xl py-2">
                    <label class="text-xs font-medium text-gray-600 mb-1">Name</label>
                    <input 
                        v-model="form.name"
                        type="text"
                        class="w-full text-sm p-2 border border-gray-200 rounded-md focus:ring-0 focus:outline-none focus:border-gray-300"
                        placeholder="e.g., Core DB Smoke"
                        required
                    />
                </div>

                <div class="flex flex-col mx-auto max-w-2xl py-2">
                    <label class="text-xs font-medium text-gray-600 mb-1">Description</label>
                    <textarea 
                        v-model="form.description"
                        rows="4"
                        class="w-full text-sm p-2 min-h-[120px] border border-gray-200 rounded-md focus:ring-0 focus:outline-none focus:border-gray-300"
                        placeholder="Short description"
                    />
                </div>

                <p v-if="error" class="text-sm text-red-600 mx-auto max-w-2xl">{{ error }}</p>

                <div class="flex justify-end items-center pt-3 mx-auto max-w-2xl space-x-2">
                    <UButton color="gray" variant="soft" size="xs" @click="emit('update:modelValue', false)" :loading="isSubmitting">Cancel</UButton>
                    <UButton type="submit" size="xs" class="!bg-blue-500 !text-white" :loading="isSubmitting">Create Suite</UButton>
                </div>
            </form>
        </div>
    </div>
    
</template>

<script setup lang="ts">
const props = defineProps<{ modelValue: boolean }>()
const emit = defineEmits<{ (e: 'update:modelValue', v: boolean): void; (e: 'created', payload: any): void }>()

const isSubmitting = ref(false)
const error = ref('')
const form = reactive({ name: '', description: '' })

watch(() => props.modelValue, (v) => {
    if (v) {
        isSubmitting.value = false
        error.value = ''
        form.name = ''
        form.description = ''
        
    }
})

const submit = async () => {
    if (!form.name) return
    isSubmitting.value = true
    error.value = ''
    try {
        const res = await useMyFetch('/test/suites', {
            method: 'POST',
            body: { name: form.name, description: form.description || undefined }
        })
        if (res.error.value) {
            throw new Error(res.error.value?.message || 'Failed to create')
        }
        emit('created', res.data.value)
        emit('update:modelValue', false)
    } catch (e: any) {
        error.value = e?.message || 'Failed to create'
    } finally {
        isSubmitting.value = false
    }
}
</script>


