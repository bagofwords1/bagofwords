<template>
	<!-- A citation chip (composables/useCitations.ts): a link only so it is
	     focusable and announced. The transcript handles hover, focus and click. -->
	<a
		v-if="isCitation"
		:href="node.href"
		class="citation-chip"
		:aria-label="label"
		data-testid="citation-chip"
		@click.prevent
	>{{ node.text }}</a>
	<LinkNode v-else v-bind="$attrs" :node="node" />
</template>

<script setup lang="ts">
/**
 * Link renderer for every markstream-vue surface (registered in
 * plugins/markdown-mermaid-png.ts). Ordinary links go to the library's own
 * LinkNode untouched; `#cite-…` links are drawn as numbered source chips.
 */
import { computed } from 'vue'
import { LinkNode } from 'markstream-vue'
import { CITATION_HREF_PREFIX } from '~/composables/useCitations'

defineOptions({ inheritAttrs: false })
const props = defineProps<{ node: { href?: string; title?: string | null; text?: string; [key: string]: any } }>()
const { t } = useI18n({ useScope: 'global' })

const isCitation = computed(() => String(props.node?.href || '').startsWith(CITATION_HREF_PREFIX))
const label = computed(() => props.node?.title
	? t('reportView.citations.chipLabel', { number: props.node.text, title: props.node.title })
	: t('reportView.citations.chipLabelUntitled', { number: props.node?.text }))
</script>

<style>
/* Not scoped, and insistent where it has to be: the transcripts style every
   link inside an answer (blue, underlined, an "opens in a new tab" icon on
   hover), and a chip is none of those things. */
a.citation-chip {
	display: inline-flex;
	align-items: center;
	justify-content: center;
	min-width: 1.35em;
	height: 1.35em;
	padding: 0 0.4em;
	margin-inline-start: 0.1em;
	border-radius: 9999px;
	font-size: 0.75em;
	font-weight: 600;
	line-height: 1;
	vertical-align: 0.15em;
	text-decoration: none !important;
	color: rgb(var(--bow-accent-rgb)) !important;
	background: rgb(var(--bow-accent-subtle-rgb));
	cursor: pointer;
	transition: color 120ms, background-color 120ms;
}
a.citation-chip::before,
a.citation-chip::after {
	content: none !important;
}
a.citation-chip:hover,
a.citation-chip:focus-visible {
	color: rgb(var(--bow-bg-rgb)) !important;
	background: rgb(var(--bow-accent-rgb));
	outline: none;
}
a.citation-chip:focus-visible {
	box-shadow: 0 0 0 2px rgb(var(--bow-bg-rgb)), 0 0 0 4px rgb(var(--bow-accent-rgb));
}
</style>
