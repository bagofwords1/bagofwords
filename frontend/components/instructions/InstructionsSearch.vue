<template>
    <div class="relative" :class="fullWidth ? 'w-full' : 'w-full sm:max-w-xs'">
        <input
            :value="modelValue"
            @input="$emit('update:modelValue', ($event.target as HTMLInputElement).value)"
            type="text"
            :placeholder="placeholder"
            class="w-full pl-9 pr-3 py-2 text-sm border border-gray-200 rounded-lg focus:outline-none focus:ring-2 focus:ring-blue-100 focus:border-blue-300 bg-white"
        />
        <UIcon name="i-heroicons-magnifying-glass" class="absolute left-3 top-2.5 h-4 w-4 text-gray-400" />
        <button 
            v-if="modelValue" 
            @click="$emit('update:modelValue', '')"
            class="absolute right-3 top-2.5 text-gray-400 hover:text-gray-600"
        >
            <UIcon name="i-heroicons-x-mark" class="h-4 w-4" />
        </button>
    </div>
</template>

<script setup lang="ts">
withDefaults(defineProps<{
    modelValue: string
    placeholder?: string
    fullWidth?: boolean
}>(), {
    placeholder: 'Search instructions...',
    fullWidth: false
})

defineEmits<{
    'update:modelValue': [value: string]
}>()
</script>
