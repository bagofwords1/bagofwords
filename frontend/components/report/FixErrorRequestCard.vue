<template>
  <!-- Fix Error request sent from the dashboard pane's render-error state. The
       agent still gets the full prompt (artifact id + raw error); chat shows
       this card. -->
  <div class="flex flex-col gap-1.5 pt-1 min-w-0" data-testid="fix-error-request-card">
    <div class="flex items-center gap-1.5 text-2xs text-red-600 dark:text-red-400 min-w-0">
      <Icon name="heroicons:wrench-screwdriver" class="w-3.5 h-3.5 flex-shrink-0" />
      <span class="font-medium flex-shrink-0">{{ t('toolbar.fixErrorRequestLabel') }}</span>
      <span class="text-gray-300 dark:text-gray-600 flex-shrink-0">·</span>
      <span class="truncate text-gray-500 dark:text-gray-400" :title="request.title">{{ request.title }}</span>
    </div>
    <UTooltip :popper="{ placement: 'bottom-end' }" :ui="{ width: 'max-w-md' }" class="self-start max-w-full">
      <template #text>
        <pre class="whitespace-pre-wrap break-all text-2xs leading-snug font-mono" dir="ltr">{{ request.error }}</pre>
      </template>
      <code
        class="block max-w-full truncate rounded-md border border-red-100 dark:border-red-900/60 bg-red-50/70 dark:bg-red-950/40 px-1.5 py-0.5 font-mono text-2xs text-gray-700 dark:text-gray-300"
        dir="ltr"
      >{{ summary }}</code>
    </UTooltip>
  </div>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import { useI18n } from 'vue-i18n'
import { summarizeError, type FixErrorRequest } from '~/utils/fixErrorPrompt'

const props = defineProps<{ request: FixErrorRequest }>()
const { t } = useI18n()
const summary = computed(() => summarizeError(props.request.error))
</script>
