<template>
  <!-- Where a form page's save action lives: under the last section, after a
       hairline, aligned to the end. An optional note sits on the start side. -->
  <div class="mt-8 pt-6 border-t border-line flex items-center justify-between gap-4">
    <p class="text-xs text-ink-muted leading-relaxed min-w-0">
      <slot name="note" />
    </p>
    <div class="flex items-center gap-2 shrink-0">
      <slot />
    </div>
  </div>
</template>
