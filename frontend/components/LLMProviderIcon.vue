<template>
    <span class="inline-flex items-center">
        <img 
            :src="iconPath" 
            :class="computedClass" 
            :alt="`${provider} logo`"
            @error="handleImageError"
            :style="{ objectFit: 'contain', maxWidth: '100%', maxHeight: '100%' }"
        />
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
    icon?: boolean;
}>();

defineEmits<{
    'add-provider': []
}>();

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
    const target = event.target as HTMLImageElement;
    // Fallback to a generic icon if the image fails to load
    target.style.display = 'none';
    // You could also set a fallback image here if needed
};
       
</script>