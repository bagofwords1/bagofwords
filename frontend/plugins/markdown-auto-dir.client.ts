/*
 * Starts the markdown direction observer once for the whole app, so every
 * rendered markdown surface — chat answers, note cards, eval run details, the
 * trace modal, modals teleported to <body> — takes the direction of its text
 * rather than of the UI locale. It used to start only on the report and
 * shared-link pages, so markdown anywhere else fell back to the browser's
 * first-letter `dir="auto"`.
 *
 * See composables/useMarkdownAutoDir.ts for what it stamps and
 * utils/textDirection.ts (resolveDirTree) for the rules.
 */
export default defineNuxtPlugin(() => {
  useMarkdownAutoDir()
})
