<template>
	<!-- The results an answer rests on, as one quiet line: the ones it cites,
	     numbered as in the text, then any table the run produced and did not
	     cite — so a result is never reachable only by opening the steps. -->
	<div v-if="items.length" class="run-sources mt-3 flex flex-wrap items-center gap-x-3 gap-y-1 text-start text-chat-3 text-ink-muted" data-testid="run-sources">
		<button
			v-for="item in items"
			:key="item.source.key"
			type="button"
			class="run-source inline-flex max-w-full items-center gap-1 rounded hover:text-ink focus-visible:text-ink focus-visible:outline focus-visible:outline-1 focus-visible:outline-offset-2 focus-visible:outline-blue-500"
			:aria-label="translate('reportView.citations.openSource', { title: titleOf(item) })"
			data-testid="run-source"
			:data-citation-key="item.source.key"
			@click="$emit('open', item, $event.currentTarget)"
			@mouseenter="$emit('preview', item, $event.currentTarget)"
			@mouseleave="$emit('leave')"
			@focus="$emit('preview', item, $event.currentTarget)"
			@blur="$emit('leave')"
		>
			<span v-if="item.number" class="citation-number" aria-hidden="true">{{ item.number }}</span>
			<Icon v-else name="heroicons:table-cells" class="h-3 w-3 shrink-0" />
			<span class="min-w-0 truncate"><bdi>{{ titleOf(item) }}</bdi></span>
		</button>
	</div>
</template>

<script setup lang="ts">
import type { CitationSource } from '~/composables/useCitations'

export interface SourceItem {
	/** The number the answer cites it by; null for a result it did not cite. */
	number: number | null
	source: CitationSource
}

const props = defineProps<{
	items: SourceItem[]
	translate: (key: string, values?: any) => string
}>()
defineEmits<{
	(e: 'open', item: SourceItem, el: EventTarget | null): void
	(e: 'preview', item: SourceItem, el: EventTarget | null): void
	(e: 'leave'): void
}>()

const titleOf = (item: SourceItem) => item.source.output.title || props.translate('reportView.citations.untitled')
</script>

<style scoped>
.citation-number {
	display: inline-flex;
	align-items: center;
	justify-content: center;
	min-width: 1rem;
	height: 1rem;
	padding: 0 0.25rem;
	border-radius: 9999px;
	font-size: 0.625rem;
	font-weight: 600;
	line-height: 1;
	color: rgb(var(--bow-accent-rgb));
	background: rgb(var(--bow-accent-subtle-rgb));
}
</style>
