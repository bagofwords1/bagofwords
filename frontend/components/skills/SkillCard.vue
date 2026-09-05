<template>
  <div
    :data-testid="`skill-card-${skill.key || skill.id}`"
    class="border rounded-lg p-3.5 transition-colors"
    :class="skill.installed
      ? 'border-blue-200 dark:border-blue-500/30 bg-blue-50/40 dark:bg-blue-500/5'
      : 'border-gray-200 dark:border-gray-800 hover:bg-gray-50 dark:hover:bg-gray-800/40'"
  >
    <div class="flex items-start gap-3">
      <span
        class="shrink-0 inline-flex items-center justify-center w-7 h-7 rounded-md border"
        :class="skill.installed
          ? 'border-blue-200 dark:border-blue-500/30 text-blue-500 dark:text-blue-400'
          : 'border-gray-200 dark:border-gray-800 text-gray-400 dark:text-gray-500'"
      >
        <UIcon name="i-heroicons-sparkles" class="w-3.5 h-3.5" />
      </span>

      <div class="min-w-0 flex-1">
        <div class="flex items-center gap-1.5 flex-wrap">
          <span class="text-[13px] font-medium text-gray-900 dark:text-white">{{ skill.title }}</span>
          <span
            v-if="showEnabledBadge && skill.installed"
            data-testid="skill-enabled-badge"
            class="inline-flex items-center px-1.5 h-4 rounded bg-blue-100 dark:bg-blue-500/20 text-blue-700 dark:text-blue-300 text-[10px] font-medium"
          >{{ $t('skillCatalog.enabled') }}</span>
          <span
            v-if="showOrigin"
            class="inline-flex items-center px-1.5 h-4 rounded bg-gray-100 dark:bg-gray-800 text-gray-500 dark:text-gray-400 text-[10px] font-medium"
          >{{ skill.key ? $t('skillCatalog.prebuilt') : $t('skillCatalog.custom') }}</span>
          <span
            v-if="skill.update_available"
            class="inline-flex items-center px-1.5 h-4 rounded bg-amber-100 dark:bg-amber-500/20 text-amber-700 dark:text-amber-300 text-[10px] font-medium"
          >{{ $t('skillCatalog.updateAvailable') }}</span>
          <span
            v-if="skill.is_customized"
            class="inline-flex items-center px-1.5 h-4 rounded bg-gray-100 dark:bg-gray-800 text-gray-600 dark:text-gray-300 text-[10px] font-medium"
            :title="$t('skillCatalog.customizedHint')"
          >{{ $t('skillCatalog.customized') }}</span>
        </div>

        <p v-if="skill.description" class="text-[11px] text-gray-500 dark:text-gray-400 mt-1 leading-relaxed">
          {{ skill.description }}
        </p>

        <div class="flex items-center gap-1.5 mt-1.5 flex-wrap">
          <span v-if="skill.version" class="text-[10px] text-gray-400 dark:text-gray-500">v{{ skill.version }}</span>
          <span
            v-for="tag in (skill.tags || [])"
            :key="tag"
            class="inline-flex items-center px-1.5 h-4 rounded bg-gray-100 dark:bg-gray-800 text-gray-500 dark:text-gray-400 text-[10px]"
          >{{ tag }}</span>
          <span
            v-if="skill.modes && skill.modes.length"
            class="inline-flex items-center px-1.5 h-4 rounded bg-gray-100 dark:bg-gray-800 text-gray-500 dark:text-gray-400 text-[10px]"
            :title="$t('skillCatalog.modeScopedHint')"
          >{{ $t('skillCatalog.modeScoped', { modes: skill.modes.join(', ') }) }}</span>
        </div>
      </div>

      <div class="shrink-0 flex items-center gap-1.5">
        <slot name="actions" />
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
// Presentation only — both tabs render the same card, and the page owns every
// action so the two tabs can offer different ones over identical markup.
defineProps<{
  skill: Record<string, any>
  // The Enabled tab mixes pre-built and hand-authored skills, so it labels the
  // origin; on the Catalog tab every entry is pre-built and the badge is noise.
  showOrigin?: boolean
  // Likewise the "Enabled" badge: it distinguishes rows on the Catalog tab and
  // is redundant on a list that is nothing but enabled skills.
  showEnabledBadge?: boolean
}>()
</script>
