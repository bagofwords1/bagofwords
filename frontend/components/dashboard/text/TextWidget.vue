<template>
  <div class="h-full w-full p-2" :style="wrapperStyle">
    <div class="rendered-html" :style="{ color: tokens.value?.textColor, fontFamily: tokens.value?.fontFamily }" v-html="contentHtml"></div>
  </div>
</template>

<script setup lang="ts">
import { computed, toRefs } from 'vue'
import { useDashboardTheme } from '@/components/dashboard/composables/useDashboardTheme'

const props = defineProps<{
  widget?: any
  step?: any
  view?: Record<string, any> | null
  reportThemeName?: string | null
  reportOverrides?: Record<string, any> | null
}>()

const { widget, step, reportThemeName, reportOverrides } = toRefs(props)
const { tokens } = useDashboardTheme(reportThemeName?.value, reportOverrides?.value, props.view || null)

const wrapperStyle = computed(() => ({
  backgroundColor: tokens.value?.cardBackground || tokens.value?.background || 'transparent',
  color: tokens.value?.textColor || 'inherit',
  borderColor: tokens.value?.cardBorder || undefined,
  '--headingFont': tokens.value?.headingFontFamily || tokens.value?.fontFamily || 'inherit',
  '--bodyFont': tokens.value?.fontFamily || 'inherit'
}))

const contentHtml = computed(() => (widget?.value?.content || step?.value?.content || ''))
</script>

<style scoped>
/* Apply theme typography/colors to v-html (scoped-safe via :deep) */
:deep(.rendered-html) {
  font-size: 15px;
  line-height: 1.7;
  letter-spacing: 0.1px;
  color: inherit;
  font-family: var(--bodyFont);
}
:deep(.rendered-html *) { color: inherit; font-family: var(--bodyFont); }
:deep(.rendered-html h1) {
  font-family: var(--headingFont);
  font-size: 1.3rem;
  font-weight: 700;
  margin: 0.8rem 0 0.5rem 0;
  padding-bottom: 0.2rem;
  border-bottom: 1px solid rgba(148, 163, 184, 0.2);
}
:deep(.rendered-html h2) { font-family: var(--headingFont); font-size: 1.1rem; font-weight: 600; margin: 0.7rem 0 0.4rem 0; }
:deep(.rendered-html p) { margin: 0.75rem 0; }
:deep(.rendered-html a) { color: #60a5fa; text-decoration: underline; }
:deep(.rendered-html strong) { font-weight: 600; }
:deep(.rendered-html em) { font-style: italic; }
:deep(.rendered-html ul), :deep(.rendered-html ol) { margin: 0.5rem 0 0.5rem 1.5rem; padding-left: revert; }
:deep(.rendered-html li) { margin-bottom: 0.2rem; display: list-item; }
:deep(.rendered-html blockquote) { border-left: 3px solid rgba(148, 163, 184, 0.3); padding-left: 1rem; margin: 0.5rem 0; }
:deep(.rendered-html code) { background-color: rgba(148, 163, 184, 0.15); padding: 2px 4px; border-radius: 3px; font-size: 0.9em; font-family: monospace; }
:deep(.rendered-html pre) { background-color: rgba(148, 163, 184, 0.12); padding: 0.5rem; border-radius: 4px; overflow-x: auto; font-family: monospace; white-space: pre-wrap; }
</style>


