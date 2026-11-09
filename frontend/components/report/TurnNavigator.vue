<!--
  Turn navigator: a rail of tick marks at the inline-start edge of the
  transcript, one per user prompt. Hovering (or focusing) a tick previews the
  prompt and the start of its answer; clicking jumps to that prompt. The tick
  for the turn currently in view is drawn longer and darker.

  It renders as an absolutely positioned sibling of the scroll container
  (same offset parent), aligned to the scroller's top, so it never scrolls
  and adds nothing to the transcript's layout.
  Everything is positioned with logical properties (inset-inline-start), so in
  RTL the rail sits on the right and the preview opens to its left.
-->
<template>
	<div v-if="turns.length >= 2" class="turn-nav-anchor" :style="{ top: anchorTop + 'px' }" data-testid="turn-navigator">
		<nav
			ref="railRef"
			class="turn-nav-rail"
			:class="{ compact }"
			:style="{ maxHeight: railMaxHeight + 'px' }"
			:aria-label="$t('reportView.turnNavigator.label')"
			@mouseleave="scheduleHide"
			@mouseenter="cancelHide"
		>
			<button
				v-for="(turn, i) in turns"
				:key="turn.id"
				:ref="el => setTickRef(i, el)"
				type="button"
				class="turn-nav-tick no-press"
				:class="{ active: i === activeIndex, hovered: i === hoverIndex }"
				:data-turn-id="turn.id"
				:aria-label="$t('reportView.turnNavigator.jumpTo', { n: i + 1, total: turns.length, text: oneLine(turn.prompt, 80) })"
				:aria-current="i === activeIndex ? 'location' : undefined"
				@mouseenter="show(i)"
				@focus="show(i)"
				@blur="scheduleHide"
				@keydown="onKeydown($event, i)"
				@click="jump(i)"
			>
				<span class="turn-nav-line" aria-hidden="true" />
			</button>
		</nav>

		<!-- Preview card: decorative for pointer users, the tick's aria-label
		     carries the same text for assistive tech. -->
		<div
			v-if="hoverIndex !== null && turns[hoverIndex]"
			class="turn-nav-preview bg-surface-elevated border border-line rounded-lg shadow-lg"
			:class="{ compact }"
			:style="{ top: previewTop + 'px' }"
			data-testid="turn-navigator-preview"
			aria-hidden="true"
			@mouseenter="cancelHide"
			@mouseleave="scheduleHide"
			@click="jump(hoverIndex)"
		>
			<p class="text-sm font-medium text-ink truncate" dir="auto">{{ oneLine(turns[hoverIndex].prompt, 200) }}</p>
			<p v-if="turns[hoverIndex].answer" class="mt-1 text-xs leading-5 text-ink-muted turn-nav-answer" dir="auto">{{ plainText(turns[hoverIndex].answer) }}</p>
			<p v-else class="mt-1 text-xs text-ink-subtle italic">{{ $t('reportView.turnNavigator.noResponse') }}</p>
		</div>
	</div>
</template>

<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, ref, watch } from 'vue'

export interface NavTurn {
	id: string
	prompt: string
	answer: string
}

const props = defineProps<{
	turns: NavTurn[]
	container: HTMLElement | null
	content: HTMLElement | null
}>()
const emit = defineEmits<{ (e: 'jump', id: string): void }>()

const railRef = ref<HTMLElement | null>(null)
const tickRefs: (HTMLElement | null)[] = []
function setTickRef(i: number, el: any) { tickRefs[i] = (el as HTMLElement) || null }

const activeIndex = ref(0)
const hoverIndex = ref<number | null>(null)
const previewTop = ref(0)
const railMaxHeight = ref(400)
const anchorTop = ref(16)
// Narrow gutter (split screen, small windows): shorter ticks so the rail
// never reaches the text column.
const compact = ref(false)

// ---- Active turn tracking ------------------------------------------------
let raf = 0
function scheduleUpdate() {
	if (raf) return
	raf = requestAnimationFrame(() => { raf = 0; updateActive() })
}
function updateActive() {
	const c = props.container
	if (!c || !props.turns.length) return
	anchorTop.value = c.offsetTop + 16
	const cTop = c.getBoundingClientRect().top
	// A turn is "current" once its prompt has passed the top third of the view.
	const line = cTop + c.clientHeight / 3
	let idx = 0
	for (let i = 0; i < props.turns.length; i++) {
		const el = c.querySelector(`[data-message-id="${CSS.escape(props.turns[i].id)}"]`) as HTMLElement | null
		if (!el) continue
		if (el.getBoundingClientRect().top <= line) idx = i
		else break
	}
	// At the very bottom the last turn is current even if it is short.
	if (c.scrollHeight - c.scrollTop - c.clientHeight < 4) idx = props.turns.length - 1
	if (idx !== activeIndex.value) {
		activeIndex.value = idx
		// Keep the active tick visible when the rail itself scrolls (long chats).
		if (hoverIndex.value === null) tickRefs[idx]?.scrollIntoView({ block: 'nearest' })
	}
}

function measure() {
	const c = props.container
	if (!c) return
	railMaxHeight.value = Math.max(80, c.clientHeight - 48)
	anchorTop.value = c.offsetTop + 16
	const content = props.content
	if (content) {
		const cr = c.getBoundingClientRect()
		const tr = content.getBoundingClientRect()
		const rtl = getComputedStyle(c).direction === 'rtl'
		// Space between the scroller edge and the content box, plus its padding.
		const pad = parseFloat(getComputedStyle(content)[rtl ? 'paddingRight' : 'paddingLeft']) || 0
		const gutter = (rtl ? cr.right - tr.right : tr.left - cr.left) + pad
		compact.value = gutter < 44
	}
	scheduleUpdate()
}

let ro: ResizeObserver | null = null
watch(() => [props.container, props.content] as const, ([c, content], _old, onCleanup) => {
	if (!c) return
	c.addEventListener('scroll', scheduleUpdate, { passive: true })
	ro = new ResizeObserver(measure)
	ro.observe(c)
	if (content) ro.observe(content)
	measure()
	onCleanup(() => {
		c.removeEventListener('scroll', scheduleUpdate)
		ro?.disconnect()
		ro = null
	})
}, { immediate: true })

watch(() => props.turns.length, () => nextTick(() => { measure() }))

// ---- Hover preview -------------------------------------------------------
let hideTimer: ReturnType<typeof setTimeout> | null = null
function cancelHide() { if (hideTimer) { clearTimeout(hideTimer); hideTimer = null } }
function scheduleHide() {
	cancelHide()
	hideTimer = setTimeout(() => { hoverIndex.value = null }, 140)
}
function show(i: number) {
	cancelHide()
	hoverIndex.value = i
	const tick = tickRefs[i]
	const rail = railRef.value
	if (tick && rail) {
		// Centre the card on the tick, relative to the anchor (rail top = 0).
		previewTop.value = tick.offsetTop - rail.scrollTop + tick.offsetHeight / 2
	}
}

function jump(i: number | null) {
	if (i === null) return
	const turn = props.turns[i]
	if (!turn) return
	activeIndex.value = i
	emit('jump', turn.id)
}

function onKeydown(e: KeyboardEvent, i: number) {
	let next = -1
	if (e.key === 'ArrowDown') next = Math.min(props.turns.length - 1, i + 1)
	else if (e.key === 'ArrowUp') next = Math.max(0, i - 1)
	else if (e.key === 'Home') next = 0
	else if (e.key === 'End') next = props.turns.length - 1
	else if (e.key === 'Escape') { hoverIndex.value = null; return }
	if (next < 0) return
	e.preventDefault()
	tickRefs[next]?.focus()
}

// ---- Text helpers --------------------------------------------------------
function oneLine(s: string, max: number) {
	const t = (s || '').replace(/\s+/g, ' ').trim()
	return t.length > max ? t.slice(0, max - 1) + '…' : t
}
// Markdown → short plain text for the preview (code, links, emphasis, tables).
function plainText(md: string) {
	const t = (md || '')
		.replace(/```[\s\S]*?(```|$)/g, ' ')
		.replace(/`([^`]*)`/g, '$1')
		.replace(/!\[[^\]]*\]\([^)]*\)/g, ' ')
		.replace(/\[([^\]]*)\]\([^)]*\)/g, '$1')
		.replace(/\{\{[^}]*\}\}/g, ' ')
		.replace(/^\s{0,3}(#{1,6}|>|[-*+]|\d+[.)])\s+/gm, '')
		.replace(/^\s*\|?[\s:|-]+\|[\s:|-]*$/gm, ' ')
		.replace(/\|/g, ' ')
		.replace(/(\*\*|__|\*|_|~~)(?=\S)([^*_~]+?)\1/g, '$2')
	return oneLine(t, 320)
}

onBeforeUnmount(() => {
	cancelHide()
	if (raf) cancelAnimationFrame(raf)
	ro?.disconnect()
})

defineExpose({ activeIndex: computed(() => activeIndex.value) })
</script>

<style scoped>
/* Zero-height anchor over the scroller's inline-start edge. */
.turn-nav-anchor {
	position: absolute;
	inset-inline-start: 0;
	height: 0;
	z-index: 20;
}
.turn-nav-rail {
	position: absolute;
	top: 0;
	inset-inline-start: 6px;
	width: 28px;
	display: flex;
	flex-direction: column;
	overflow-y: auto;
	overflow-x: hidden;
	scrollbar-width: none;
}
.turn-nav-rail::-webkit-scrollbar { display: none; }
.turn-nav-rail.compact { inset-inline-start: 2px; width: 14px; }

.turn-nav-tick {
	flex: 0 0 auto;
	height: 10px;
	width: 100%;
	display: flex;
	align-items: center;
	justify-content: flex-start; /* = inline-start, follows dir */
	background: none;
	border: 0;
	padding: 0;
	cursor: pointer;
	border-radius: 2px;
}
.turn-nav-tick:focus-visible { outline: 2px solid rgb(59 130 246 / 0.6); outline-offset: 0; }
.turn-nav-line {
	display: block;
	height: 2px;
	width: 10px;
	border-radius: 1px;
	background: rgb(209 213 219); /* gray-300 */
	transition: width 120ms ease, background-color 120ms ease;
}
.turn-nav-tick.hovered .turn-nav-line { width: 18px; background: rgb(107 114 128); }
.turn-nav-tick.active .turn-nav-line { width: 24px; background: rgb(17 24 39); }
.compact .turn-nav-line { width: 6px; }
.compact .turn-nav-tick.hovered .turn-nav-line { width: 9px; }
.compact .turn-nav-tick.active .turn-nav-line { width: 12px; }
/* `.dark` (on <html>) is a plain ancestor; never `:global(.dark) .x`, which Vue
   compiles to a bare `.dark {…}` that paints <html> (tests/unit/darkSelectorsScoped.mjs). */
.dark .turn-nav-line { background: rgb(75 85 99); }
.dark .turn-nav-tick.hovered .turn-nav-line { background: rgb(156 163 175); }
.dark .turn-nav-tick.active .turn-nav-line { background: rgb(243 244 246); }

.turn-nav-preview {
	position: absolute;
	inset-inline-start: 40px;
	transform: translateY(-50%);
	width: 320px;
	max-width: calc(100vw - 80px);
	padding: 10px 12px;
	cursor: pointer;
}
.turn-nav-preview.compact { inset-inline-start: 22px; }
.turn-nav-answer {
	display: -webkit-box;
	-webkit-line-clamp: 3;
	-webkit-box-orient: vertical;
	overflow: hidden;
}

@media (max-width: 639px), (pointer: coarse) {
	.turn-nav-anchor { display: none; }
}
</style>
