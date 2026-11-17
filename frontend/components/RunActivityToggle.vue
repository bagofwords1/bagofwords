<template>
	<!-- The whole row is the control: a native button, so Enter/Space toggle it
	     and aria-expanded announces the state. -->
	<button
		type="button"
		:aria-expanded="expanded"
		class="run-fold-toggle flex w-full items-center gap-1 rounded py-1 text-start text-chat-2 text-ink-muted hover:text-ink focus-visible:outline focus-visible:outline-2 focus-visible:outline-blue-500"
		data-testid="run-fold-toggle"
		@click="$emit('toggle')"
	>
		<span v-if="duration" class="nums">{{ translate('reportView.runFold.workedFor', { duration }) }}</span>
		<span v-else>{{ translate('reportView.runFold.worked') }}</span>
		<span aria-hidden="true">·</span>
		<span>{{ translate(expanded ? 'reportView.runFold.hideSteps' : 'reportView.runFold.viewSteps') }}</span>
		<Icon :name="expanded ? 'heroicons-chevron-down' : 'heroicons-chevron-right'" class="h-3.5 w-3.5 shrink-0 rtl-flip" aria-hidden="true" />
	</button>
</template>

<script setup lang="ts">
/**
 * The single row a finished run's activity folds into, above its final
 * message: "Worked for 10s · View steps ›" / "Worked for 10s · Hide steps ⌄".
 * `translate` is the response-scoped translator, so the row reads in the
 * language of the answer it sits above.
 */
defineProps<{
	expanded: boolean
	/** Formatted elapsed time of the run ("10s", "1m 02s"); empty when unknown. */
	duration: string
	translate: (key: string, values?: any) => string
}>()
defineEmits<{ (e: 'toggle'): void }>()
</script>
