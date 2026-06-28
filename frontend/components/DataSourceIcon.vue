<template>
    <UIcon v-if="props.type === 'custom_api'" name="heroicons-cog-6-tooth" :class="[computedClass, 'text-gray-500 dark:text-gray-400']" />
    <img v-else :src="imgSrc" :class="computedClass" class="w-auto" alt="" @error="handleError" />
</template>

<script setup lang="ts">
import { computed, ref, watch } from 'vue';

// Props to accept the type of data source and class
const props = defineProps<{
    type: string | null | undefined;
    // Optional catalog key for a known connector (e.g. "notion", "monday"). When
    // set, the provider's brand icon is preferred over the generic type icon.
    connectorKey?: string | null;
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
    if (t === 'fabric' || t === 'microsoft_fabric') t = 'ms_fabric'
    if (t === 'qlik_sense') t = 'qlik'

    return t
}

// Known connector catalog keys with a brand icon under /connector_icons.
const CONNECTOR_ICON_KEYS = new Set([
    'monday', 'notion', 'atlassian', 'linear', 'sentry', 'github', 'gmail', 'supabase',
]);

// Computed property to generate the icon path
const iconPath = computed(() => {
    // Prefer the provider brand icon for known catalog connectors.
    const ck = props.connectorKey ? normalizeType(props.connectorKey) : '';
    if (ck && CONNECTOR_ICON_KEYS.has(ck)) {
        return `/connector_icons/${ck}.svg`;
    }
    if (!props.type) {
        return FALLBACK_ICON;
    }
    const t = normalizeType(props.type);

    // Prefer tool/resource icons when available (stored under /icons)
    const toolIconTypes = new Set(['dbt', 'lookml', 'markdown', 'resource', 'tableau', 'dataform', 'mcp', 'custom_api']);
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