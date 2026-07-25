<template>
    <span class="inline-flex items-center">
        <!-- Custom provider: just show chip icon -->
        <Icon
            v-if="isCustomProvider"
            name="heroicons-cpu-chip"
            :class="[computedClass, 'text-gray-500 dark:text-gray-400']"
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
            class="flex items-center justify-center text-gray-500 dark:text-gray-400"
        >
            <Icon name="heroicons-cpu-chip" class="w-6 h-6" />
        </span>
        <button v-if="showAddProvider"
                @click="$emit('add-provider')"
                class="ms-2 text-gray-500 dark:text-gray-400 hover:text-gray-700 dark:hover:text-gray-300">
            <Icon name="heroicons:plus-circle" class="w-5" />
        </button>
    </span>
</template>

<script setup lang="ts">
import { computed, ref, watch } from 'vue';

import { resolveModelBrand } from '~/utils/llmBrand';

const props = defineProps<{
    provider: string;
    // Optional model name/id — when given, the brand is resolved name-first
    // (e.g. a "Qwen3 (NVIDIA DGX)" model on a `custom` provider gets the
    // NVIDIA mark instead of the generic chip), same rule as the chat avatar.
    model?: string | null;
    class?: string;
    showAddProvider?: boolean;
    icon?: boolean;
    showFallbackLabel?: boolean;
}>();

defineEmits<{
    'add-provider': []
}>();

const imageError = ref(false);

// Effective brand: name-first via the model string when provided, otherwise
// the provider string as-is (which may itself already be a resolved brand).
const brand = computed(() =>
    props.model
        ? resolveModelBrand(props.model, props.provider)
        : (props.provider || 'custom').toLowerCase()
);

// Custom/unknown brand: skip image loading entirely, show the generic chip
const isCustomProvider = computed(() => brand.value === 'custom');

// Reset error state when the resolved brand changes
watch(brand, () => {
    imageError.value = false;
});

// Computed property to generate the icon path
const iconPath = computed(() => {
    if (props.icon) {
        return `/llm_providers_icons/${brand.value}-icon.png`;
    }
    return `/llm_providers_icons/${brand.value}.png`;
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
