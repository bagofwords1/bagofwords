<template>
  <!-- Loading placeholder for a settings page or section. Replaces <ULoader>,
       which is not a Nuxt UI v2 component and rendered nothing at all. -->
  <div class="flex items-center justify-center gap-2 py-12 text-sm text-ink-muted" role="status" aria-live="polite">
    <Spinner class="w-4 h-4" />
    <span>{{ $t('common.loading') }}</span>
  </div>
</template>

<script setup lang="ts">
import Spinner from '~/components/Spinner.vue'
</script>
