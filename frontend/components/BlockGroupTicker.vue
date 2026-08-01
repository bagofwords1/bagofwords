<template>
	<div
		class="flex items-center gap-1 py-1 text-xs text-gray-500 cursor-pointer select-none hover:text-gray-700 dark:hover:text-gray-300"
		data-testid="block-group-header"
		@click="$emit('toggle')"
	>
		<Icon :name="expanded ? 'heroicons-chevron-down' : 'heroicons-chevron-right'" class="w-4 h-4 text-gray-400 rtl-flip" />
		<Spinner v-if="group.active" class="w-2.5 h-2.5 me-0.5 text-gray-400 dark:text-gray-500" />
		<!-- Grid-stacked crossfade: old and new labels occupy the same cell, so
		     the new fades in WHILE the old fades out — there is never a frame
		     without text (out-in mode blanked the line between labels). -->
		<span class="ticker-stack">
			<Transition name="ticker">
				<span :key="shownLabel" :class="group.active ? 'ticker-shimmer' : ''">{{ shownLabel }}</span>
			</Transition>
		</span>
		<span v-if="group.issueCount" class="text-amber-500">· {{ group.issueCount }} {{ group.issueCount === 1 ? 'issue' : 'issues' }}</span>
	</div>
</template>

<script setup lang="ts">
/**
 * Live ticker line for a run of consecutive low-signal tool blocks.
 *
 * While the run is active it shows ONLY the current step ("Step 3 ·
 * Inspecting data…"), crossfading in place as steps change — nothing stacks.
 * When the run settles it becomes a compact summary ("3 steps · Searched
 * invoice tables · 15s") that expands to the full chain on click.
 *
 * Label changes are queued behind a minimum display time so parallel
 * batches that settle in sub-300ms bursts coalesce into one swap instead
 * of strobing.
 */
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import type { BlockGroup } from '~/composables/useBlockGrouping'

const props = defineProps<{ group: BlockGroup; expanded: boolean }>()
defineEmits<{ (e: 'toggle'): void }>()

// Minimum time a label stays on screen before the next one may swap in.
const MIN_HOLD_MS = 500

function labelFor(g: BlockGroup): string {
	if (g.active) {
		return g.runningLabel ? `Step ${g.count} · ${g.runningLabel}…` : `${g.count} steps…`
	}
	const secs = Math.max(1, Math.round((g.durationMs || 0) / 1000))
	const dur = g.durationComplete ? ` · ${secs}s` : ''
	// Settled: last meaningful step title when one exists; otherwise stay
	// minimal — the per-verb breakdown reads as telemetry, and the chain is
	// one click away.
	return g.lastTitle
		? `${g.count} steps · ${g.lastTitle}${dur}`
		: `${g.count} steps${dur}`
}

const targetLabel = computed(() => labelFor(props.group))
const _shown = ref(targetLabel.value)
// Never render an empty line: if the queued state is momentarily blank
// (e.g. a group identity handoff mid-stream), fall through to the target.
const shownLabel = computed(() => _shown.value || targetLabel.value)

let lastSwapAt = 0
let pendingTimer: ReturnType<typeof setTimeout> | null = null

watch(targetLabel, (next) => {
	if (next === _shown.value) {
		// Target reverted to what's already shown — cancel any queued swap
		// so a stale intermediate label can't land after the fact.
		if (pendingTimer) { clearTimeout(pendingTimer); pendingTimer = null }
		return
	}
	if (pendingTimer) clearTimeout(pendingTimer)
	const wait = Math.max(0, MIN_HOLD_MS - (Date.now() - lastSwapAt))
	// Only the LATEST target ever lands — intermediate labels from a fast
	// batch are dropped, which is exactly the coalescing we want.
	pendingTimer = setTimeout(() => {
		_shown.value = next
		lastSwapAt = Date.now()
		pendingTimer = null
	}, wait)
})

onBeforeUnmount(() => {
	if (pendingTimer) clearTimeout(pendingTimer)
})
</script>

<style scoped>
/* Both labels share one grid cell during the swap: the leaving label fades
   under the entering one, so the line never collapses or reflows. */
.ticker-stack {
	display: grid;
	min-width: 0;
}
.ticker-stack > * {
	grid-area: 1 / 1;
	white-space: nowrap;
}
.ticker-enter-active,
.ticker-leave-active {
	transition: opacity 0.18s ease;
}
.ticker-enter-from,
.ticker-leave-to {
	opacity: 0;
}

@keyframes ticker-shimmer-sweep {
	0% { background-position: -100% 0; }
	100% { background-position: 100% 0; }
}

/* Shimmering label while the run is active (matches the running state of
   the per-tool components, e.g. InspectDataTool). */
.ticker-shimmer {
	animation: ticker-shimmer-sweep 1.6s linear infinite;
	background: linear-gradient(90deg, rgba(0,0,0,0) 0%, rgba(160,160,160,0.15) 50%, rgba(0,0,0,0) 100%);
	background-size: 300% 100%;
	background-clip: text;
}
</style>
