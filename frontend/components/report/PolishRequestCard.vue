<template>
  <!-- Polish request sent from the dashboard element picker. The agent still
       gets the full prompt (artifact id + element HTML); chat shows this card. -->
  <div class="flex flex-col gap-1.5 pt-1 min-w-0" data-testid="polish-request-card">
    <div class="flex items-center gap-1.5 text-2xs text-indigo-600 dark:text-indigo-400 min-w-0">
      <Icon name="heroicons:paint-brush" class="w-3.5 h-3.5 flex-shrink-0" />
      <span class="font-medium flex-shrink-0">{{ t('toolbar.polishRequestLabel') }}</span>
      <span class="text-gray-300 dark:text-gray-600 flex-shrink-0">·</span>
      <span class="truncate text-gray-500 dark:text-gray-400" :title="request.title">{{ request.title }}</span>
    </div>
    <UTooltip :popper="{ placement: 'bottom-end' }" :ui="{ width: 'max-w-md' }" class="self-start max-w-full">
      <template #text>
        <pre class="whitespace-pre-wrap break-all text-2xs leading-snug font-mono" dir="ltr">{{ request.htmlSnippet }}</pre>
      </template>
      <span
        class="inline-flex items-center gap-1.5 max-w-full rounded-md border border-indigo-100 dark:border-indigo-900/60 bg-indigo-50/70 dark:bg-indigo-950/40 px-1.5 py-0.5 text-xs text-gray-700 dark:text-gray-300"
        dir="auto"
      >
        <code class="flex-shrink-0 font-mono text-2xs text-indigo-600 dark:text-indigo-400" dir="ltr">&lt;{{ target.tag }}&gt;</code>
        <span v-if="target.text" class="truncate">{{ target.text }}</span>
      </span>
    </UTooltip>
    <div class="text-sm whitespace-pre-wrap break-words" dir="auto">{{ request.instruction }}</div>
  </div>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import { useI18n } from 'vue-i18n'
import { describePolishTarget, type PolishRequest } from '~/utils/polishPrompt'

const props = defineProps<{ request: PolishRequest }>()
const { t } = useI18n()
const target = computed(() => describePolishTarget(props.request.htmlSnippet))
</script>
