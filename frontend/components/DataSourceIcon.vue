<template>
    <!-- Custom per-agent emoji override takes precedence over the type icon. -->
    <span
        v-if="parsedIcon.kind === 'emoji'"
        :class="[computedClass, 'inline-flex items-center justify-center leading-none select-none']"
        :style="emojiStyle"
        role="img"
        aria-hidden="true"
    >{{ parsedIcon.value }}</span>
    <UIcon v-else-if="effectiveType === 'custom_api'" name="heroicons-cog-6-tooth" :class="[computedClass, 'text-gray-500 dark:text-gray-400']" />
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
    // Optional per-agent custom icon override token ("emoji:<grapheme>" |
    // "preset:<key>"). When it resolves to an emoji it wins over everything
    // else; otherwise the default type/connector logic below applies.
    icon?: string | null;
    class?: string;
}>();

const FALLBACK_ICON = '/data_sources_icons/document.png'

// Parse the custom icon override. Unrecognised/future tokens resolve to 'none'
// and fall through to the default type icon, so nothing ever renders broken.
const parsedIcon = computed(() => parseAgentIcon(props.icon))

// A "type:<key>" override pins one of the agent's connection type/connector
// icons. We feed the key into BOTH the connector-brand and the type-asset
// resolution below (the connector map wins if the key is a known brand, e.g.
// "notion"; otherwise it resolves as a plain type asset, e.g. "snowflake").
const typeToken = computed(() => (parsedIcon.value.kind === 'type' ? parsedIcon.value.value : null))
const effectiveType = computed(() => typeToken.value ?? props.type)
const effectiveConnectorKey = computed(() => typeToken.value ?? props.connectorKey)

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

// Brand icons for known connector catalog keys. Explicit filename per key — the
// assets live under /data_sources_icons with mixed extensions, and the catalog
// key isn't always the filename (e.g. atlassian → jira).
const CONNECTOR_ICON_FILE: Record<string, string> = {
    monday: 'monday.svg',
    notion: 'notion.png',
    atlassian: 'jira.png',
    linear: 'linear.png',
    sentry: 'sentry.png',
    github: 'github.svg',
    gmail: 'gmail.png',
    google_drive: 'google_drive.png',
    x: 'x.svg',
};

// Computed property to generate the icon path
const iconPath = computed(() => {
    // Prefer the provider brand icon for known catalog connectors (even though
    // the underlying connection type is just "mcp").
    const ck = effectiveConnectorKey.value ? normalizeType(effectiveConnectorKey.value) : '';
    if (ck && CONNECTOR_ICON_FILE[ck]) {
        return `/data_sources_icons/${CONNECTOR_ICON_FILE[ck]}`;
    }
    if (!effectiveType.value) {
        return FALLBACK_ICON;
    }
    const t = normalizeType(effectiveType.value);

    // Explicit brand icons for data-source types whose asset is an SVG (the
    // default resolver below only tries `<type>.png`).
    const TYPE_ICON_FILE: Record<string, string> = {
        csv: 'csv.png',
        outlook_mail: 'outlook_mail.svg',
        elasticsearch: 'elasticsearch.svg',
    };
    if (TYPE_ICON_FILE[t]) {
        return `/data_sources_icons/${TYPE_ICON_FILE[t]}`;
    }

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

// Emoji is text, so the width/height utility classes (e.g. `h-4 w-4`) that size
// the <img> don't size the glyph. Derive a font-size from the class so the emoji
// visually fills the same box. We read the largest h-*/w-* utility present.
const emojiStyle = computed(() => {
    const cls = props.class || ''
    // Match h-4 / w-3.5 / h-[18px] etc.
    let px = 16 // default ~ h-4
    const bracket = cls.match(/[hw]-\[(\d+)px\]/)
    if (bracket) {
        px = parseInt(bracket[1], 10)
    } else {
        const rem = cls.match(/[hw]-(\d+(?:\.\d+)?)/)
        if (rem) {
            // Tailwind spacing scale: unit * 0.25rem = unit * 4px
            px = parseFloat(rem[1]) * 4
        }
    }
    // Slightly shrink so the glyph sits inside the box rather than overflowing.
    const size = Math.max(8, Math.round(px * 0.95))
    return { fontSize: `${size}px`, lineHeight: '1' }
})
</script>
