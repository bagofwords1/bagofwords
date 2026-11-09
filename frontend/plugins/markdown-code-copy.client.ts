/*
 * Starts the code-block copy-button enhancer once for the whole app, so every
 * rendered code block (`<pre>`) in a markdown surface — chat answers, note
 * cards, eval/trace markdown — gets its own copy button. The chat renders code
 * as a plain `<pre>` (markstream's `render-code-blocks-as-pre`), which has no
 * copy affordance of its own; the whole-answer copy button copies the full
 * reply, not a single snippet.
 *
 * See composables/useCodeBlockCopy.ts. Labels follow the active locale.
 */
export default defineNuxtPlugin((nuxtApp) => {
  const i18n = nuxtApp.vueApp.config.globalProperties.$i18n as { t: (k: string) => string } | undefined
  const t = (k: string, fallback: string) => {
    const v = i18n?.t?.(k)
    return v && v !== k ? v : fallback
  }
  useCodeBlockCopy(() => ({
    copy: t('common.copy', 'Copy'),
    copied: t('common.copied', 'Copied'),
  }))
})
