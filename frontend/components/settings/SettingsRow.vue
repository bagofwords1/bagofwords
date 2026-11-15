<template>
  <!-- One setting: label + help on the start side, its control on the end.
       `stacked` puts the control under the label for wide inputs (a text
       field, a select) so every form in the console lines up the same way. -->
  <div :class="['settings-row', stacked ? 'flex flex-col gap-2' : 'flex items-start justify-between gap-6']">
    <div class="min-w-0" :class="stacked ? '' : 'pt-0.5'">
      <label v-if="label" :for="forId" class="flex items-center gap-1.5 text-sm font-medium text-ink">
        {{ label }}
        <span v-if="optional" class="font-normal text-ink-muted">{{ $t('common.optional') }}</span>
        <slot name="label-extra" />
      </label>
      <p v-if="description && !stacked" class="mt-0.5 text-sm text-ink-muted leading-relaxed">{{ description }}</p>
    </div>
    <div :class="stacked ? 'w-full' : 'shrink-0 flex items-center'">
      <slot />
    </div>
    <p v-if="description && stacked" class="-mt-0.5 text-xs text-ink-muted leading-relaxed">{{ description }}</p>
  </div>
</template>

<script setup lang="ts">
defineProps<{
  label?: string
  description?: string
  stacked?: boolean
  optional?: boolean
  forId?: string
}>()
</script>

<style scoped>
/* Rows stacked in a column are spaced by this; rows placed in a grid are
   spaced by the grid's own gap, so the rule steps aside there. */
:not(.grid) > .settings-row + .settings-row {
  margin-top: 1.25rem;
}
</style>
