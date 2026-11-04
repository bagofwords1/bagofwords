<template>
	<div>
		<div
			ref="bodyRef"
			class="relative"
			:class="isClamped ? 'overflow-hidden prompt-fade' : ''"
			:style="isClamped ? { maxHeight: `${limit}px` } : undefined"
			data-testid="collapsible-prompt-body"
		>
			<div ref="innerRef">
				<slot />
			</div>
		</div>
		<button
			v-if="isClamped"
			type="button"
			class="mt-1 text-chat-3 font-medium text-ink-muted hover:text-ink underline-offset-2 hover:underline"
			data-testid="prompt-read-more"
			@click="readMore"
		>
			{{ $t('reportView.readMore') }}
		</button>
		<button
			v-else-if="steps > 1"
			type="button"
			class="mt-1 text-chat-3 font-medium text-ink-muted hover:text-ink underline-offset-2 hover:underline"
			data-testid="prompt-show-less"
			@click="showLess"
		>
			{{ $t('reportView.showLess') }}
		</button>
	</div>
</template>

<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'

// Very long prompts (pasted specs, SQL, logs) push the whole conversation
// off-screen. Clamp them; each "Read more" doubles the visible height, so even
// a huge paste opens fully in a few clicks without dumping everything at once.
const props = withDefaults(defineProps<{
	// Initial visible height, in px.
	step?: number
}>(), { step: 320 })

const bodyRef = ref<HTMLElement | null>(null)
const innerRef = ref<HTMLElement | null>(null)
const contentHeight = ref(0)
const steps = ref(1)

const limit = computed(() => props.step * 2 ** (steps.value - 1))
// Don't clamp to hide only a few lines: allow ~25% slack past the limit.
const isClamped = computed(() => contentHeight.value > limit.value + props.step * 0.25)

function readMore() {
	steps.value += 1
}

function showLess() {
	steps.value = 1
	// Keep the prompt in view after it shrinks back.
	bodyRef.value?.scrollIntoView({ block: 'nearest' })
}

let observer: ResizeObserver | null = null
onMounted(() => {
	const el = innerRef.value
	if (!el) return
	contentHeight.value = el.scrollHeight
	if (typeof ResizeObserver !== 'undefined') {
		observer = new ResizeObserver(() => { contentHeight.value = el.scrollHeight })
		observer.observe(el)
	}
})
onBeforeUnmount(() => observer?.disconnect())
</script>

<style scoped>
/* Mask instead of a colored overlay so the fade works on any bubble background. */
.prompt-fade {
	-webkit-mask-image: linear-gradient(to bottom, #000 calc(100% - 3rem), transparent);
	mask-image: linear-gradient(to bottom, #000 calc(100% - 3rem), transparent);
}
</style>
