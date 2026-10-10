<template>
  <!-- Late-arriving blocks (follow-ups, instruction suggestions) land after the
       answer has rendered. Popping them in at full height shoves the transcript
       up in one frame; instead grow the row from 0fr→1fr (no height measuring)
       and fade the content in slightly after. The page's transcript
       ResizeObserver follow-scrolls each frame of the growth. -->
  <Transition name="reveal">
    <div v-if="show" class="reveal-grid">
      <div class="reveal-inner">
        <slot />
      </div>
    </div>
  </Transition>
</template>

<script setup lang="ts">
defineProps<{ show: boolean }>()
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
