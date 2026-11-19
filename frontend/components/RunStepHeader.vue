<template>
	<!-- One plain-language step of a folded run. The toggle and the Open action
	     are siblings, never nested: a button inside a button is not operable. -->
	<div class="run-step flex items-center gap-2 text-chat-2 text-start" data-testid="run-step">
		<button
			type="button"
			class="run-step-toggle flex min-w-0 items-center gap-1 rounded text-ink-2 hover:text-ink focus-visible:outline focus-visible:outline-2 focus-visible:outline-blue-500"
			:aria-expanded="expanded"
			data-testid="run-step-toggle"
			@click="$emit('toggle')"
		>
			<Icon :name="expanded ? 'heroicons-chevron-down' : 'heroicons-chevron-right'" class="w-3.5 h-3.5 shrink-0 text-ink-muted rtl-flip" />
			<Spinner v-if="step.running" class="h-3 w-3 shrink-0 text-ink-muted" data-testid="run-step-running" />
			<span class="min-w-0 text-start" :class="{ 'tool-shimmer': step.running }" :title="step.subjects.join(', ') || undefined"><bdi>{{ label }}</bdi></span>
			<span v-if="step.output?.kind === 'number' && step.output.value" class="shrink-0 font-medium text-ink nums" data-testid="run-step-value"><bdi>{{ step.output.value }}</bdi></span>
			<span v-if="step.count > 1" class="shrink-0 text-ink-muted">· {{ translate(step.kind === 'inspect' ? 'reportView.runFold.checks' : 'reportView.runFold.actions', { count: step.count }) }}</span>
			<span v-if="isOpenable && step.output?.rowCount != null" class="shrink-0 text-ink-muted nums">· {{ translate('tools.common.rows', { n: step.output.rowCount.toLocaleString() }) }}</span>
			<span v-if="step.issueCount" class="shrink-0 text-amber-600 dark:text-amber-400">· {{ translate('tools.browser.checkIssues', { count: step.issueCount }) }}</span>
		</button>
		<UButton
			v-if="canOpen && isOpenable"
			color="gray"
			variant="link"
			size="2xs"
			:padded="false"
			trailing-icon="i-heroicons-arrow-top-right-on-square"
			data-testid="run-step-open"
			@click="$emit('open')"
		>{{ translate(step.output?.kind === 'chart' ? 'reportView.runFold.openChart' : 'reportView.runFold.openTable') }}</UButton>
	</div>
</template>

<script setup lang="ts">
/**
 * Header line of a run step (see composables/useRunActivity.ts): what the
 * step did, in plain language, plus what a create_data call produced — a
 * table or chart is named and can be opened, a single value is shown inline.
 * `translate` is the response-scoped translator where the host has one.
 */
import { computed } from 'vue'
import type { RunStep } from '~/composables/useRunActivity'

const props = defineProps<{
	step: RunStep
	expanded: boolean
	translate: (key: string, values?: any) => string
	/** Host can open a table/chart result somewhere (e.g. a side panel). */
	canOpen?: boolean
}>()
defineEmits<{ (e: 'toggle'): void; (e: 'open'): void }>()

const label = computed(() => props.step.labelText || props.translate(`reportView.runFold.steps.${props.step.labelKey}`, props.step.labelParams))
const isOpenable = computed(() => props.step.output?.kind === 'table' || props.step.output?.kind === 'chart')
</script>
