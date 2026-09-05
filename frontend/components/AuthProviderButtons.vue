<template>
  <div class="space-y-2.5" v-if="allProviders.length">
    <button
      v-for="p in allProviders"
      :key="p.name"
      @click="start(p.name)"
      type="button"
      :disabled="loadingProvider !== null"
      :class="buttonClass"
    >
      <template v-if="loadingProvider === p.name">
        <Spinner class="h-4 w-4" />
      </template>
      <template v-else>
        <img v-if="iconSrc(p)" :src="iconSrc(p)!" alt="" aria-hidden="true" :class="iconClass" />
        <UIcon v-else name="i-heroicons-lock-closed" aria-hidden="true"
               :class="[iconClass, 'text-gray-400 dark:text-gray-500']" />
        <span>{{ mode === 'sign-up'
          ? $t('auth.continueWithProvider', { provider: label(p) })
          : $t('auth.signInWithProvider', { provider: label(p) }) }}</span>
      </template>
    </button>
  </div>
</template>

<script setup lang="ts">
import Spinner from '~/components/Spinner.vue'

interface AuthProvider {
  name: string
  enabled?: boolean
  // Derived server-side in /api/settings so the raw issuer (which can carry a
  // tenant id) never reaches an unauthenticated page.
  label?: string
  brand?: 'microsoft' | 'google' | 'custom'
  icon?: string | null
}

const props = withDefaults(defineProps<{
  providers: AuthProvider[]
  googleEnabled: boolean
  mode?: 'sign-in' | 'sign-up'
}>(), {
  mode: 'sign-in',
})

const emit = defineEmits<{ (e: 'error', message: string): void }>()

const { t } = useI18n()
const route = useRoute()
const loadingProvider = ref<string | null>(null)

// Google's built-in flow is just another button: folding it into one list keeps
// the verb consistent across the stack ("Continue with Google" next to
// "Continue with Microsoft", not "Sign up with Google").
const allProviders = computed<AuthProvider[]>(() => [
  ...(props.googleEnabled ? [{ name: 'google', label: 'Google', brand: 'google' as const }] : []),
  ...props.providers,
])

// The mark is pinned to the leading edge so every label lands on the same
// centered axis no matter how long the provider name is.
const buttonClass =
  'relative w-full h-11 inline-flex items-center justify-center rounded-xl border ' +
  'border-gray-200 dark:border-gray-800 bg-white dark:bg-gray-900 text-[15px] font-medium ' +
  'text-gray-900 dark:text-gray-100 hover:bg-gray-50 dark:hover:bg-gray-800/60 ' +
  'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-gray-900 ' +
  'dark:focus-visible:ring-white focus-visible:ring-offset-2 dark:focus-visible:ring-offset-gray-950 ' +
  'disabled:opacity-50 disabled:cursor-not-allowed transition-colors'

const iconClass = 'absolute start-4 h-[18px] w-[18px]'

// Only three marks are shipped: the two providers most deployments use, and a
// neutral key for every other OIDC issuer (Okta, Keycloak, an in-house IdP).
const BRAND_ICONS: Record<string, string> = {
  google: '/icons/google.svg',
  microsoft: '/icons/microsoft.svg',
}

function iconSrc(p: AuthProvider): string | null {
  return p.icon || BRAND_ICONS[p.brand ?? ''] || null
}

// `name` is a routing slug, so it is the last resort — not the first choice.
function label(p: AuthProvider): string {
  return p.label || p.name
}

const OAUTH_REDIRECT_STORAGE_NAME = 'bow:postSignInRedirect'

// Only honor redirects to same-origin paths to avoid open-redirect bugs
function safeRedirectTarget(value: unknown): string | null {
  if (typeof value !== 'string' || !value) return null
  if (!value.startsWith('/') || value.startsWith('//')) return null
  return value
}

// The IdP round-trip loses the query string, so stash the post-login target.
function persistRedirectForOAuth() {
  const target = safeRedirectTarget(route.query.redirect)
  try {
    if (target) {
      sessionStorage.setItem(OAUTH_REDIRECT_STORAGE_NAME, target)
    } else {
      sessionStorage.removeItem(OAUTH_REDIRECT_STORAGE_NAME)
    }
  } catch (_) {}
}

async function start(name: string) {
  try {
    loadingProvider.value = name
    persistRedirectForOAuth()
    const response = await $fetch(`/api/auth/${name}/authorize`, { method: 'GET' })
    if ((response as any)?.authorization_url) {
      window.location.href = (response as any).authorization_url
      return
    }
    throw new Error('missing authorization_url')
  } catch (error) {
    loadingProvider.value = null
    const provider = allProviders.value.find((p) => p.name === name)
    emit('error', name === 'google'
      ? t('auth.googleInitError')
      : t('auth.providerInitError', { provider: provider ? label(provider) : name }))
  }
}
</script>
