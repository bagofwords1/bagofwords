<template>
    <span class="inline-flex items-center">
        <!-- Custom provider: just show chip icon -->
        <Icon 
            v-if="isCustomProvider"
            name="heroicons-cpu-chip" 
            :class="[computedClass, 'text-gray-500']"
        />
        <!-- Regular providers: show image -->
        <img 
            v-else-if="!imageError"
            :src="iconPath" 
            :class="computedClass" 
            :alt="`${provider} logo`"
            @error="handleImageError"
            :style="{ objectFit: 'contain', maxWidth: '100%', maxHeight: '100%' }"
        />
        <!-- Fallback for missing images (non-custom) -->
        <span 
            v-else 
            :class="computedClass"
            class="flex items-center justify-center text-gray-500"
        >
            <Icon name="heroicons-cpu-chip" class="w-6 h-6" />
        </span>
        <button v-if="showAddProvider" 
                @click="$emit('add-provider')" 
                class="ml-2 text-gray-500 hover:text-gray-700">
            <Icon name="heroicons:plus-circle" class="w-5" />
        </button>
    </span>
</template>

<script setup lang="ts">
import { computed, ref, watch } from 'vue';

const props = defineProps<{
    provider: string;
    class?: string;
    showAddProvider?: boolean;
    icon?: boolean;
    showFallbackLabel?: boolean;
}>();

defineEmits<{
    'add-provider': []
}>();

const imageError = ref(false);

// Check if this is a custom provider (skip image loading entirely)
const isCustomProvider = computed(() => props.provider?.toLowerCase() === 'custom');

// Reset error state when provider changes
watch(() => props.provider, () => {
    imageError.value = false;
});

// Computed property to generate the icon path
const iconPath = computed(() => {
    if (props.icon) {
        return `/llm_providers_icons/${props.provider.toLowerCase()}-icon.png`;
    }
    return `/llm_providers_icons/${props.provider.toLowerCase()}.png`;
});

// Combine the passed class with any other classes you want
const computedClass = computed(() => {
    return props.class ? props.class : '';
});

// Handle image loading errors
const handleImageError = (event: Event) => {
    imageError.value = true;
};
       
</script>