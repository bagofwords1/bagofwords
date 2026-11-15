<template>
  <!-- A block of related settings: heading + one-line description, then its
       rows. Sections stack with a hairline between them — no bordered cards,
       so a settings page reads as one document rather than a pile of boxes. -->
  <section class="settings-section">
    <header v-if="title || $slots.actions" class="flex items-start justify-between gap-4 mb-1">
      <div class="min-w-0">
        <h2 v-if="title" class="text-base font-semibold text-ink">{{ title }}</h2>
        <p v-if="description" class="mt-1 text-sm text-ink-muted leading-relaxed">{{ description }}</p>
      </div>
      <div v-if="$slots.actions" class="shrink-0 flex items-center gap-2">
        <slot name="actions" />
      </div>
    </header>
    <div :class="flush ? '' : 'mt-5'">
      <slot />
    </div>
  </section>
</template>

<script setup lang="ts">
defineProps<{
  title?: string
  description?: string
  // Drop the gap under the header (content that brings its own spacing).
  flush?: boolean
}>()
</script>

<style scoped>
.settings-section + .settings-section {
  margin-top: 2rem;
  padding-top: 2rem;
  border-top: 1px solid rgb(var(--bow-line-rgb));
}
</style>
