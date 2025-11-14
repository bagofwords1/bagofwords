<template>
    <img :src="iconPath" :class="computedClass" alt="" />
</template>

<script setup lang="ts">
import { computed } from 'vue';

// Props to accept the type of data source and class
const props = defineProps<{
    type: string | null | undefined;
    class?: string;
}>();

// Computed property to generate the icon path
const iconPath = computed(() => {
    if (!props.type) {
        return '/icons/document.png';
    }
    const t = String(props.type || '').toLowerCase();

    // Prefer tool/resource icons when available (stored under /icons)
    const toolIconTypes = new Set(['dbt', 'lookml', 'markdown', 'resource', 'tableau', 'dataform']);
    if (toolIconTypes.has(t)) {
        return `/icons/${t}.png`;
    }

    // Fallback to data source icons set
    return `/data_sources_icons/${t}.png`;
});

// Combine the passed class with any other classes you might want
const computedClass = computed(() => {
    return props.class ? props.class : '';
});
</script>