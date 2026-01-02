<template>
    <img :src="imgSrc" :class="computedClass" alt="" @error="handleError" />
</template>

<script setup lang="ts">
import { computed, ref, watch } from 'vue';

// Props to accept the type of data source and class
const props = defineProps<{
    type: string | null | undefined;
    class?: string;
}>();

const FALLBACK_ICON = '/data_sources_icons/document.png'

const normalizeType = (raw: string) => {
    // normalize to icon-friendly token: lowercase, underscores, strip numeric suffixes
    let t = String(raw || '').toLowerCase().trim()
    t = t.replace(/\s+/g, '_').replace(/[^a-z0-9]+/g, '_').replace(/^_+|_+$/g, '')
    // common case: "postgresql-1" / "snowflake-2" etc
    t = t.replace(/_\d+$/g, '')

    // aliases
    if (t === 'postgres') t = 'postgresql'
    if (t === 'sqlserver' || t === 'sql_server') t = 'mssql'
    if (t === 'awsathena') t = 'aws_athena'
    if (t === 'athena') t = 'aws_athena'
    if (t === 'redshift') t = 'aws_redshift'

    return t
}

// Computed property to generate the icon path
const iconPath = computed(() => {
    if (!props.type) {
        return FALLBACK_ICON;
    }
    const t = normalizeType(props.type);

    // Prefer tool/resource icons when available (stored under /icons)
    const toolIconTypes = new Set(['dbt', 'lookml', 'markdown', 'resource', 'tableau', 'dataform']);
    if (toolIconTypes.has(t)) {
        return `/icons/${t}.png`;
    }

    // Fallback to data source icons set
    return `/data_sources_icons/${t}.png`;
});

const imgSrc = ref(iconPath.value)
watch(iconPath, (next) => {
    imgSrc.value = next
})

const handleError = () => {
    // Avoid infinite loop if fallback is also missing
    if (imgSrc.value !== FALLBACK_ICON) {
        imgSrc.value = FALLBACK_ICON
    }
}

// Combine the passed class with any other classes you might want
const computedClass = computed(() => {
    return props.class ? props.class : '';
});
</script>