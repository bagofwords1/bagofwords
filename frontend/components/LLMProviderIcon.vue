<template>
    <span class="inline-flex items-center">
        <img :src="iconPath" :class="computedClass" alt="" />
        <button v-if="showAddProvider" 
                @click="$emit('add-provider')" 
                class="ml-2 text-gray-500 hover:text-gray-700">
            <Icon name="heroicons:plus-circle" class="w-5" />
        </button>
    </span>
</template>

<script setup lang="ts">
import { computed } from 'vue';

const props = defineProps<{
    provider: string;
    class?: string;
    showAddProvider?: boolean;
}>();

defineEmits<{
    'add-provider': []
}>();

// Computed property to generate the icon path
const iconPath = computed(() => {
    return `/llm_providers_icons/${props.provider.toLowerCase()}.png`;
});

// Combine the passed class with any other classes you might want
const computedClass = computed(() => {
    return props.class ? props.class : '';
});
       
</script>