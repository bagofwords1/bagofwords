// Mark the auth token cookie `Secure` whenever the app is served over HTTPS.
// Runs before nuxt-auth reads its config (enforce: 'pre'); plain-HTTP
// self-hosted installs keep a non-Secure cookie so sign-in still works there.
export default defineNuxtPlugin({
  name: 'auth-cookie-secure',
  enforce: 'pre',
  setup() {
    if (window.location.protocol !== 'https:') return
    const token = (useRuntimeConfig().public as any)?.auth?.provider?.token
    if (token) token.secureCookieAttribute = true
  },
})
