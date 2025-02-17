<template>
    <img :src="iconPath" :class="computedClass" alt="" />
</template>

<script setup lang="ts">
import { computed } from 'vue';

// Props to accept the type of data source and class
const props = defineProps<{
    type: string;
    class?: string;
}>();

// Computed property to generate the icon path
const iconPath = computed(() => {
    return `/data_sources_icons/${props.type.toLowerCase()}.png`;
});

// Combine the passed class with any other classes you might want
const computedClass = computed(() => {
    return props.class ? props.class : '';
});
</script>