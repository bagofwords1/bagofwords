<template>
	<!-- The whole row is the control: a native button, so Enter/Space toggle it
	     and aria-expanded announces the state. -->
	<button
		type="button"
		:aria-expanded="expanded"
		class="run-fold-toggle -mx-1.5 flex w-[calc(100%+0.75rem)] min-w-0 items-center gap-1 rounded-md px-1.5 py-1 text-start text-chat-2 text-ink-muted hover:text-ink focus-visible:bg-surface-muted focus-visible:text-ink focus-visible:outline focus-visible:outline-1 focus-visible:outline-blue-500"
		data-testid="run-fold-toggle"
		:data-live="live ? '' : undefined"
		@click="$emit('toggle')"
	>
		<template v-if="live">
			<span class="run-fold-pulse me-0.5 h-1.5 w-1.5 shrink-0 rounded-full bg-current" aria-hidden="true" />
			<span v-if="duration" class="nums shrink-0">{{ translate('reportView.runFold.workingFor', { duration }) }}</span>
			<span v-else class="shrink-0">{{ translate('reportView.runFold.working') }}</span>
			<template v-if="status">
				<span aria-hidden="true" class="shrink-0">·</span>
				<span class="min-w-0 truncate" data-testid="run-fold-status">{{ status }}</span>
			</template>
		</template>
		<template v-else>
			<span v-if="duration" class="nums">{{ translate('reportView.runFold.workedFor', { duration }) }}</span>
			<span v-else>{{ translate('reportView.runFold.worked') }}</span>
		</template>
		<span aria-hidden="true" class="shrink-0">·</span>
		<span class="shrink-0">{{ translate(expanded ? 'reportView.runFold.hideSteps' : 'reportView.runFold.viewSteps') }}</span>
		<Icon :name="expanded ? 'heroicons-chevron-down' : 'heroicons-chevron-right'" class="h-3.5 w-3.5 shrink-0 rtl-flip" aria-hidden="true" />
	</button>
</template>

<script setup lang="ts">
/**
 * The single row a run's activity folds into. While the run works:
 * "● Working for 12s · Checking the customers table · View steps ›"; once its
 * final message has arrived: "Worked for 10s · View steps ›" / "· Hide steps ⌄".
 * `translate` is the response-scoped translator, so the row reads in the
 * language of the answer it sits above.
 */
defineProps<{
	expanded: boolean
	/** Formatted elapsed time of the run ("10s", "1m 02s"); empty when unknown. */
	duration: string
	translate: (key: string, values?: any) => string
	/** The run is still working. */
	live?: boolean
	/** What it is doing now (live only), one line. */
	status?: string
}>()
defineEmits<{ (e: 'toggle'): void }>()
</script>

<style scoped>
.run-fold-pulse {
	animation: run-fold-pulse 1.4s ease-in-out infinite;
}
@keyframes run-fold-pulse {
	0%, 100% { opacity: 0.35; }
	50% { opacity: 1; }
}
@media (prefers-reduced-motion: reduce) {
	.run-fold-pulse { animation: none; }
}
</style>
