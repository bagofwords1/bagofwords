<template>
  <!-- One "still working" line for a live run: a breathing dot and the elapsed
       time. The timer is aria-hidden so the status region isn't re-announced
       every second. -->
  <span role="status" :aria-label="label" class="run-live inline-flex h-6 items-center gap-2 text-ink" data-testid="run-live">
    <span class="run-live-dot h-3 w-3 shrink-0 rounded-full bg-current" :class="{ invisible: dot === false }" aria-hidden="true" />
    <span v-if="elapsed" class="nums text-chat-3 text-ink-muted" aria-hidden="true" data-testid="run-live-elapsed">{{ elapsed }}</span>
  </span>
</template>

<script setup lang="ts">
// `dot: false` keeps the timer but hides the dot (in place, so the timer
// doesn't shift) while a visible running tool shows its own spinner.
// (Defaults to true: Vue casts an absent boolean prop to false.)
withDefaults(defineProps<{ label: string; elapsed?: string; dot?: boolean }>(), { elapsed: '', dot: true })
</script>

<style scoped>
.run-live-dot {
  animation: run-live-breathe 1.4s ease-in-out infinite;
}
@keyframes run-live-breathe {
  0%, 100% { opacity: 1; transform: scale(1); }
  50% { opacity: 0.3; transform: scale(0.8); }
}
@media (prefers-reduced-motion: reduce) {
  .run-live-dot { animation: none; }
}
</style>
