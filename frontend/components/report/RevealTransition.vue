<template>
  <!-- Late-arriving blocks (follow-ups, instruction suggestions) land after the
       answer has rendered. Popping them in at full height shoves the transcript
       up in one frame; instead grow the row from 0fr→1fr (no height measuring)
       and fade the content in slightly after. Emits `growing` once per frame
       while animating so the page can follow-scroll smoothly. -->
  <Transition name="reveal" @enter="onEnter">
    <div v-if="show" class="reveal-grid">
      <div class="reveal-inner">
        <slot />
      </div>
    </div>
  </Transition>
</template>

<script setup lang="ts">
defineProps<{ show: boolean }>()
const emit = defineEmits<{ growing: [] }>()

const DURATION_MS = 260

function onEnter(_el: Element, done: () => void) {
  if (typeof window === 'undefined') return done()
  const start = performance.now()
  const tick = (now: number) => {
    emit('growing')
    if (now - start < DURATION_MS) window.requestAnimationFrame(tick)
    else done()
  }
  window.requestAnimationFrame(tick)
}
</script>

<style scoped>
.reveal-grid {
  display: grid;
  grid-template-rows: 1fr;
}
.reveal-inner {
  min-height: 0;
  overflow: hidden;
}
.reveal-enter-active {
  transition: grid-template-rows 260ms ease-out;
}
.reveal-enter-active .reveal-inner {
  transition: opacity 200ms ease-out 80ms;
}
.reveal-enter-from {
  grid-template-rows: 0fr;
}
.reveal-enter-from .reveal-inner {
  opacity: 0;
}
@media (prefers-reduced-motion: reduce) {
  .reveal-enter-active,
  .reveal-enter-active .reveal-inner {
    transition: none;
  }
}
</style>
