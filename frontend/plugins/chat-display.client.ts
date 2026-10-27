/*
 * Applies the reader's stored conversation preferences (measure + text size)
 * to <html> before the first paint of a report, so the transcript does not
 * render at the default size and then jump.
 *
 * Client-only by filename: both preferences live in localStorage, which does
 * not exist on the server. `ssr: false` in nuxt.config.ts means that is
 * every render anyway, but the suffix keeps it honest.
 *
 * See `composables/useChatDisplay.ts` for the storage keys and the reasoning
 * about why these are personal rather than organizational settings.
 */
export default defineNuxtPlugin(() => {
  useChatDisplay().hydrate()
})
